"""Reuse owned headless Chrome processes; isolate each render in a new context.

Only the local DevTools socket is used. No user Chrome profile, external
automation service, WebSocket package, or additional Python dependency.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time

from .fetch import HEADERS, MAX_BYTES, FetchError, check_url, remaining
from .metrics import measure

MAX_MESSAGE = 24_000_000
MAX_PROCESSES = 3
TABS_PER_PROCESS = 2
RECYCLE_AFTER = 50


class BrowserError(RuntimeError):
    pass


class DevTools:
    """Small RFC6455 client for sequential CDP calls and request interception."""
    def __init__(self, port, path, deadline):
        self.deadline, self.buffer, self.serial, self.events = deadline, bytearray(), 0, []
        self.children, self.setup_commands = {}, {}
        self.loads = set()
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=self.left())
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n'
                   f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        try:
            self.sock.sendall(request.encode('ascii'))
            headers = bytearray()
            while b'\r\n\r\n' not in headers:
                self.sock.settimeout(self.left())
                data = self.sock.recv(4096)
                if not data or len(headers) + len(data) > 32768:
                    raise BrowserError('Invalid local browser handshake.')
                headers.extend(data)
            head, tail = bytes(headers).split(b'\r\n\r\n', 1)
            lines = head.decode('latin1').split('\r\n')
            values = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if ' 101 ' not in lines[0] or values.get('sec-websocket-accept') != expected:
                raise BrowserError('Local browser rejected the connection.')
            self.buffer.extend(tail)
        except BaseException:
            self.sock.close()
            raise

    def left(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('Browser render deadline reached.')
        return value

    def _read(self, size):
        while len(self.buffer) < size:
            self.sock.settimeout(self.left())
            data = self.sock.recv(min(65536, size - len(self.buffer)))
            if not data:
                raise BrowserError('Local browser connection closed.')
            self.buffer.extend(data)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def _send(self, data, opcode=1):
        mask = os.urandom(4)
        length = len(data)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length <= 65535:
            header += bytes([0x80 | 126]) + struct.pack('!H', length)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', length)
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.settimeout(self.left())
        self.sock.sendall(header + mask + payload)

    def _message(self):
        payload, started = bytearray(), False
        while True:
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 0x80)
            if first & 0x70 or second & 0x80:
                raise BrowserError('Unexpected local WebSocket frame.')
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
            if length > MAX_MESSAGE or len(payload) + length > MAX_MESSAGE:
                raise BrowserError('Browser response exceeded its size limit.')
            data = self._read(length)
            if opcode == 8:
                raise BrowserError('Local browser disconnected.')
            if opcode == 9:
                if not final or length > 125:
                    raise BrowserError('Invalid local WebSocket ping.')
                self._send(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0 and not started) or (opcode == 1 and started):
                raise BrowserError('Unexpected local browser message.')
            started = True
            payload.extend(data)
            if final:
                return json.loads(payload.decode('utf-8'))

    def send(self, method, params=None, session=None):
        self.serial += 1
        message = {'id': self.serial, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        self._send(json.dumps(message).encode())
        return self.serial

    def receive(self):
        while True:
            message = self._message()
            if message.get('method') == 'Page.lifecycleEvent' and message['params'].get('name') == 'load':
                self.loads.add((message.get('sessionId'), message['params']['loaderId']))
            if message.get('method') == 'Target.attachedToTarget' and message['params'].get('waitingForDebugger'):
                data = message['params']
                child, target = data['sessionId'], data['targetInfo']
                if target['type'] not in ('iframe', 'worker', 'shared_worker', 'service_worker'):
                    # Unneeded popup targets stay paused until closed. Every
                    # frame/worker that runs must have filtering installed first.
                    self.send('Target.closeTarget', {'targetId': target['targetId']})
                    continue
                commands = [self.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]}, child),
                            self.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}, child)]
                self.children[child] = {'commands': set(commands), 'target': target['targetId'], 'failed': False}
                self.setup_commands.update({ident: child for ident in commands})
                continue
            ident = message.get('id')
            if ident in self.setup_commands:
                child = self.setup_commands.pop(ident)
                setup = self.children[child]
                setup['commands'].discard(ident)
                if 'error' in message:
                    setup['failed'] = True
                    self.send('Target.closeTarget', {'targetId': setup['target']})
                if not setup['commands'] and not setup['failed']:
                    self.send('Runtime.runIfWaitingForDebugger', session=child)
                continue
            if message.get('method') == 'Fetch.requestPaused':
                data = message['params']
                try:
                    check_url(data['request']['url'])
                    if data.get('resourceType') in ('Image', 'Media', 'Font'):
                        raise FetchError('Unneeded media resource.')
                except ValueError:
                    self.send('Fetch.failRequest', {'requestId': data['requestId'], 'errorReason': 'Aborted'}, message.get('sessionId'))
                else:
                    self.send('Fetch.continueRequest', {'requestId': data['requestId']}, message.get('sessionId'))
                continue
            if message.get('method') in ('Inspector.targetCrashed', 'Target.targetCrashed'):
                raise BrowserError('Browser page crashed.')
            return message

    def call(self, method, params=None, session=None):
        ident = self.send(method, params, session)
        while True:
            response = self.receive()
            if response.get('id') == ident:
                if 'error' in response:
                    raise BrowserError('Local browser command failed: ' + method)
                return response.get('result', {})
            if response.get('method') == 'Emulation.virtualTimeBudgetExpired':
                self.events.append(response)

    def wait_budget(self, session, loader):
        expired = any(event.get('sessionId') == session for event in self.events)
        self.events.clear()
        # The parent's virtual budget can expire while an out-of-process frame
        # is still paused for filter setup. Wait for this navigation's load too.
        while not expired or (session, loader) not in self.loads or self.setup_commands:
            message = self.receive()
            if message.get('method') == 'Emulation.virtualTimeBudgetExpired' and message.get('sessionId') == session:
                expired = True

    def close(self):
        self.sock.close()


class ChromeWorker:
    def __init__(self, executable):
        self.executable, self.process, self.profile = executable, None, None
        self.port, self.path = None, None
        self.start_lock = threading.Lock()
        self.active = self.uses = 0
        self.broken = False

    def start(self, deadline):
        with self.start_lock:
            if self.broken:
                raise BrowserError('Browser worker has stopped.')
            if self.process and self.process.poll() is None:
                return
            self.close()
            with measure('browser_startup'):
                self.profile = tempfile.TemporaryDirectory(prefix='jhalcv-browser-pool-')
                self.process = subprocess.Popen([
                    self.executable, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                    '--disable-extensions', '--mute-audio', '--disable-background-networking', '--disable-sync',
                    '--disable-component-update', '--remote-debugging-port=0', f'--user-data-dir={self.profile.name}',
                    f'--user-agent={HEADERS["User-Agent"]}', '--disable-blink-features=AutomationControlled', 'about:blank'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                ready = os.path.join(self.profile.name, 'DevToolsActivePort')
                end = min(deadline, time.monotonic() + 20)
                while time.monotonic() < end:
                    if self.process.poll() is not None:
                        raise BrowserError('Could not start headless Chrome.')
                    try:
                        with open(ready) as stream:
                            lines = stream.read().splitlines()
                        port = int(lines[0])
                        if 0 < port < 65536 and lines[1].startswith('/devtools/browser/'):
                            self.port, self.path = port, lines[1]
                            return
                    except (OSError, ValueError, IndexError):
                        pass
                    threading.Event().wait(.03)
                raise BrowserError('Chrome startup timed out.')

    def render(self, url, deadline):
        self.start(deadline)
        client = DevTools(self.port, self.path, deadline)
        context = None
        try:
            context = client.call('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
            client.call('Browser.setDownloadBehavior', {'behavior': 'deny', 'browserContextId': context})
            target = client.call('Target.createTarget', {'url': 'about:blank', 'browserContextId': context})['targetId']
            session = client.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
            with measure('browser_navigation'):
                client.call('Page.enable', session=session)
                client.call('Page.setLifecycleEventsEnabled', {'enabled': True}, session)
                client.call('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True, 'flatten': True}, session)
                client.call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]}, session)
                navigation = client.call('Page.navigate', {'url': url}, session)
                if navigation.get('errorText') or not navigation.get('loaderId'):
                    raise BrowserError('Browser navigation did not complete.')
                client.call('Emulation.setVirtualTimePolicy', {'policy': 'pauseIfNetworkFetchesPending', 'budget': 9000,
                            'maxVirtualTimeTaskStarvationCount': 10000}, session)
                client.wait_budget(session, navigation['loaderId'])
            with measure('browser_dom'):
                root = client.call('DOM.getDocument', {'depth': 0}, session)['root']['nodeId']
                return client.call('DOM.getOuterHTML', {'nodeId': root}, session)['outerHTML']
        finally:
            try:
                if context:
                    client.deadline = time.monotonic() + 2
                    client.call('Target.disposeBrowserContext', {'browserContextId': context})
            except (OSError, BrowserError, ValueError):
                self.broken = True
            finally:
                client.close()

    def close(self):
        if self.process:
            if self.process.poll() is None:
                try:
                    if os.name == 'posix':
                        os.killpg(self.process.pid, signal.SIGTERM)
                    else:
                        self.process.terminate()
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    if self.process.poll() is None:
                        if os.name == 'posix':
                            try:
                                os.killpg(self.process.pid, signal.SIGKILL)
                            except OSError:
                                pass
                        else:
                            self.process.kill()
                        self.process.wait(timeout=2)
            self.process = None
        if self.profile:
            self.profile.cleanup()
            self.profile = None


class BrowserPool:
    def __init__(self, executable, *, processes=MAX_PROCESSES, tabs=TABS_PER_PROCESS):
        self.executable, self.maximum, self.tabs = executable, processes, tabs
        self.condition = threading.Condition()
        self.workers = []
        self.closed = False

    def render(self, url):
        check_url(url)
        deadline = time.monotonic() + remaining(80)
        with self.condition:
            while True:
                if self.closed:
                    raise FetchError('The browser pool has stopped.')
                available = [worker for worker in self.workers if not worker.broken and worker.active < self.tabs]
                if len(self.workers) < self.maximum and not any(worker.active == 0 for worker in available):
                    worker = ChromeWorker(self.executable)
                    self.workers.append(worker)
                    break
                if available:
                    worker = min(available, key=lambda item: item.active)
                    break
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise FetchError('Browser pool is busy. Try again later.')
                self.condition.wait(wait)
            worker.active += 1
        try:
            return worker.render(url, deadline)
        except (OSError, BrowserError, ValueError, KeyError) as error:
            worker.broken = True
            raise FetchError('Chrome could not finish rendering this page; its worker will restart.') from error
        finally:
            with self.condition:
                worker.active -= 1
                worker.uses += 1
                if worker.uses >= RECYCLE_AFTER:
                    worker.broken = True
                if worker.broken and worker.active == 0:
                    if worker in self.workers:
                        self.workers.remove(worker)
                    worker.close()
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            workers, self.workers = self.workers, []
            self.condition.notify_all()
        for worker in workers:
            worker.broken = True
            with worker.start_lock:
                worker.close()


_POOL = None
_LOCK = threading.Lock()


def render(executable, url):
    global _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = BrowserPool(executable)
        pool = _POOL
    return pool.render(url)


def close():
    with _LOCK:
        pool = _POOL
    if pool:
        pool.close()


atexit.register(close)
