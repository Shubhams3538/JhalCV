import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from resume.jobs.browser_pool import BrowserPool, BrowserError, DevTools, MAX_MESSAGE
from resume.jobs.fetch import FetchError, browser, fetch_budget
from resume.jobs.metrics import Benchmarks


def frame(payload, opcode=1, final=True):
    first = (0x80 if final else 0) | opcode
    size = len(payload)
    return bytes([first, size]) + payload if size < 126 else (
        bytes([first, 126]) + struct.pack('!H', size) + payload if size <= 65535 else
        bytes([first, 127]) + struct.pack('!Q', size) + payload)


class BrowserProtocolTests(unittest.TestCase):
    def client(self, data):
        client = object.__new__(DevTools)
        client.buffer, client.deadline, client.sock = bytearray(data), time.monotonic() + 2, Mock()
        client.children, client.setup_commands = {}, {}
        return client

    def test_large_fragmented_response_and_ping_are_supported(self):
        payload = json.dumps({'html': 'x' * 70000}).encode()
        client = self.client(frame(payload[:200], final=False) + frame(b'ping', opcode=9) + frame(payload[200:], opcode=0))
        self.assertEqual(client._message(), {'html': 'x' * 70000})
        self.assertTrue(client.sock.sendall.called)

    def test_oversized_and_invalid_continuations_are_rejected(self):
        for data in (bytes([0x81, 127]) + struct.pack('!Q', MAX_MESSAGE + 1), frame(b'{}', opcode=0)):
            with self.assertRaises(BrowserError):
                self.client(data)._message()

    def test_failing_worker_is_replaced_and_pool_close_is_terminal(self):
        class Worker:
            def __init__(self, executable, fail):
                self.active = self.uses = 0
                self.broken, self.fail = False, fail
                self.start_lock = threading.Lock()
            def render(self, *_):
                if self.fail:
                    raise BrowserError('crash')
                return '<html>ok</html>'
            def close(self):
                pass
        workers = []
        def make(executable):
            worker = Worker(executable, not workers)
            workers.append(worker)
            return worker
        with patch('resume.jobs.browser_pool.check_url'), patch('resume.jobs.browser_pool.ChromeWorker', side_effect=make):
            pool = BrowserPool('fake', processes=1, tabs=1)
            with self.assertRaises(FetchError):
                pool.render('https://careers.example')
            self.assertEqual(pool.render('https://careers.example'), '<html>ok</html>')
            self.assertEqual(len(workers), 2)
            pool.close()
            with self.assertRaises(FetchError):
                pool.render('https://careers.example')

    def test_private_intercepted_requests_are_aborted(self):
        client = self.client(b'')
        client._message = Mock(side_effect=[{'method': 'Fetch.requestPaused', 'sessionId': 'page',
            'params': {'requestId': 'private', 'resourceType': 'Document', 'request': {'url': 'http://127.0.0.1/secret'}}}, {'id': 1}])
        client.send = Mock()
        self.assertEqual(client.receive(), {'id': 1})
        self.assertEqual(client.send.call_args.args[0], 'Fetch.failRequest')


@unittest.skipUnless(browser(), 'Google Chrome is not installed')
class ChromePoolTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                owner.requests.append(self.path)
                if self.path == '/iframe':
                    content = f'<html><body><iframe src="http://localhost:{self.server.server_port}/child"></iframe></body></html>'.encode()
                elif self.path == '/child':
                    content = b'<html><body><script>fetch("/allowed-child");fetch("/blocked");</script></body></html>'
                else:
                    content = ('''<!doctype html><html><body><span id="cookie"></span><script>
                  %s
                  document.getElementById('cookie').textContent=document.cookie || 'none';
                  setTimeout(()=>{const e=document.createElement('div');e.id='late';e.textContent='late jobs';document.body.append(e)},350);
                  </script></body></html>''' % ("document.cookie='render_secret=one';" if self.path == '/first' else '')).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pool = BrowserPool(browser(), processes=1, tabs=2)
        self.addCleanup(self.pool.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def render(self, path):
        # Loopback is allowed only for this controlled, local test server.
        with fetch_budget(time.monotonic() + 10):
            return self.pool.render(self.base + path)

    def test_reuses_process_but_isolates_cookies_and_runs_delayed_javascript(self):
        with tempfile.TemporaryDirectory() as root, patch('resume.jobs.browser_pool.check_url'):
            benchmarks = Benchmarks(root)
            with benchmarks.run('test'):
                first = self.render('/first')
                process = self.pool.workers[0].process
                second = self.render('/second')
                self.assertEqual(self.pool.workers[0].process.pid, process.pid)
            self.assertIn('id="late">late jobs', first)
            self.assertIn('id="cookie">none', second)
            stages = {row['stage']: row for row in benchmarks.report()['report']['stages']}
            self.assertEqual(stages['browser_startup']['calls'], 1)
            self.assertEqual(stages['browser_navigation']['calls'], 2)
            self.assertEqual(stages['browser_dom']['calls'], 2)
            self.pool.close()
            self.assertIsNotNone(process.poll())

    def test_two_tabs_render_concurrently_inside_one_process(self):
        with patch('resume.jobs.browser_pool.check_url'), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.render, path) for path in ('/first', '/second')]
            pages = [future.result() for future in futures]
        self.assertEqual(len(self.pool.workers), 1)
        self.assertEqual(self.pool.workers[0].uses, 2)
        self.assertTrue(all('id="late">late jobs' in page for page in pages))
        self.assertIn('id="cookie">none', pages[1])

    def test_cross_site_iframe_cannot_bypass_request_filter(self):
        def guard(url):
            if url.endswith('/blocked'):
                raise FetchError('Blocked test URL')
            return url
        with patch('resume.jobs.browser_pool.check_url', side_effect=guard):
            self.render('/iframe')
        self.assertIn('/child', self.requests)
        self.assertIn('/allowed-child', self.requests)
        self.assertNotIn('/blocked', self.requests)
