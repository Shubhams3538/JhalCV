package jobs

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// The pool reuses a few owned headless Chrome processes and isolates every
// render in a fresh browser context. Only the local DevTools socket is used:
// no user profile and no automation service.

const (
	maxMessage     = 24_000_000
	maxProcesses   = 3
	tabsPerProcess = 2
	recycleAfter   = 50
)

type browserError struct{ msg string }

func (e *browserError) Error() string { return e.msg }

// devTools is a small RFC 6455 client for sequential DevTools calls, with
// request interception so a page can never reach a private address.
type devTools struct {
	conn     net.Conn
	r        *bufio.Reader
	deadline time.Time
	serial   int
	events   []map[string]any
	children map[string]*childSetup
	setup    map[int]string // Setup command id -> child session.
	loads    map[[2]string]bool
	ctx      context.Context
}

type childSetup struct {
	commands map[int]bool
	target   string
	failed   bool
}

func dialDevTools(ctx context.Context, port int, path string, deadline time.Time) (*devTools, error) {
	d := net.Dialer{Deadline: deadline}
	conn, err := d.DialContext(ctx, "tcp", "127.0.0.1:"+strconv.Itoa(port))
	if err != nil {
		return nil, err
	}
	keyBytes := make([]byte, 16)
	_, _ = rand.Read(keyBytes)
	key := base64.StdEncoding.EncodeToString(keyBytes)
	_ = conn.SetDeadline(deadline)
	fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n", path, port, key)
	r := bufio.NewReader(conn)
	resp, err := http.ReadResponse(r, nil)
	if err != nil {
		conn.Close()
		return nil, &browserError{"Invalid local browser handshake."}
	}
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	if resp.StatusCode != 101 || resp.Header.Get("Sec-WebSocket-Accept") != base64.StdEncoding.EncodeToString(sum[:]) {
		conn.Close()
		return nil, &browserError{"Local browser rejected the connection."}
	}
	return &devTools{conn: conn, r: r, deadline: deadline, children: map[string]*childSetup{},
		setup: map[int]string{}, loads: map[[2]string]bool{}, ctx: ctx}, nil
}

func (d *devTools) checkTime() error {
	if !time.Now().Before(d.deadline) {
		return &browserError{"Browser render deadline reached."}
	}
	if d.ctx.Err() != nil {
		return d.ctx.Err()
	}
	return d.conn.SetDeadline(d.deadline)
}

func (d *devTools) writeFrame(data []byte, opcode byte) error {
	if err := d.checkTime(); err != nil {
		return err
	}
	header := []byte{0x80 | opcode}
	switch n := len(data); {
	case n < 126:
		header = append(header, 0x80|byte(n))
	case n <= 65535:
		header = append(header, 0x80|126, byte(n>>8), byte(n))
	default:
		header = append(header, 0x80|127)
		header = binary.BigEndian.AppendUint64(header, uint64(n))
	}
	mask := make([]byte, 4)
	_, _ = rand.Read(mask)
	payload := make([]byte, len(data))
	for i, b := range data {
		payload[i] = b ^ mask[i%4]
	}
	_, err := d.conn.Write(append(append(header, mask...), payload...))
	return err
}

func (d *devTools) message() (map[string]any, error) {
	var payload []byte
	started := false
	for {
		if err := d.checkTime(); err != nil {
			return nil, err
		}
		head := make([]byte, 2)
		if _, err := io.ReadFull(d.r, head); err != nil {
			return nil, &browserError{"Local browser connection closed."}
		}
		opcode, final := head[0]&15, head[0]&0x80 != 0
		if head[0]&0x70 != 0 || head[1]&0x80 != 0 {
			return nil, &browserError{"Unexpected local WebSocket frame."}
		}
		length := uint64(head[1] & 127)
		switch length {
		case 126:
			b := make([]byte, 2)
			if _, err := io.ReadFull(d.r, b); err != nil {
				return nil, err
			}
			length = uint64(binary.BigEndian.Uint16(b))
		case 127:
			b := make([]byte, 8)
			if _, err := io.ReadFull(d.r, b); err != nil {
				return nil, err
			}
			length = binary.BigEndian.Uint64(b)
		}
		if length > maxMessage || uint64(len(payload))+length > maxMessage {
			return nil, &browserError{"Browser response exceeded its size limit."}
		}
		data := make([]byte, length)
		if _, err := io.ReadFull(d.r, data); err != nil {
			return nil, &browserError{"Local browser connection closed."}
		}
		switch {
		case opcode == 8:
			return nil, &browserError{"Local browser disconnected."}
		case opcode == 9:
			if !final || length > 125 {
				return nil, &browserError{"Invalid local WebSocket ping."}
			}
			if err := d.writeFrame(data, 10); err != nil {
				return nil, err
			}
			continue
		case opcode == 10:
			continue
		case opcode > 1 || (opcode == 0 && !started) || (opcode == 1 && started):
			return nil, &browserError{"Unexpected local browser message."}
		}
		started = true
		payload = append(payload, data...)
		if final {
			var msg map[string]any
			if err := json.Unmarshal(payload, &msg); err != nil {
				return nil, &browserError{"Unexpected local browser message."}
			}
			return msg, nil
		}
	}
}

func (d *devTools) send(method string, params map[string]any, session string) (int, error) {
	d.serial++
	if params == nil {
		params = map[string]any{}
	}
	msg := map[string]any{"id": d.serial, "method": method, "params": params}
	if session != "" {
		msg["sessionId"] = session
	}
	data, _ := json.Marshal(msg)
	return d.serial, d.writeFrame(data, 1)
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func num(v any) int {
	f, _ := v.(float64)
	return int(f)
}

// receive returns the next message that is not handled internally: frame
// filter setup, request interception, and load events.
func (d *devTools) receive() (map[string]any, error) {
	for {
		msg, err := d.message()
		if err != nil {
			return nil, err
		}
		method := str(msg["method"])
		params, _ := msg["params"].(map[string]any)
		session := str(msg["sessionId"])
		if method == "Page.lifecycleEvent" && params["name"] == "load" {
			d.loads[[2]string{session, str(params["loaderId"])}] = true
		}
		if method == "Target.attachedToTarget" && params["waitingForDebugger"] == true {
			child := str(params["sessionId"])
			target, _ := params["targetInfo"].(map[string]any)
			switch str(target["type"]) {
			case "iframe", "worker", "shared_worker", "service_worker":
			default:
				// Popups stay paused until closed. Every frame or worker
				// that runs gets filtering installed first.
				if _, err := d.send("Target.closeTarget", map[string]any{"targetId": target["targetId"]}, ""); err != nil {
					return nil, err
				}
				continue
			}
			a, err := d.send("Fetch.enable", map[string]any{"patterns": []any{map[string]any{"urlPattern": "*", "requestStage": "Request"}}}, child)
			if err != nil {
				return nil, err
			}
			b, err := d.send("Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": true, "flatten": true}, child)
			if err != nil {
				return nil, err
			}
			d.children[child] = &childSetup{commands: map[int]bool{a: true, b: true}, target: str(target["targetId"])}
			d.setup[a], d.setup[b] = child, child
			continue
		}
		if id, ok := msg["id"].(float64); ok {
			if child, isSetup := d.setup[int(id)]; isSetup {
				delete(d.setup, int(id))
				s := d.children[child]
				delete(s.commands, int(id))
				if msg["error"] != nil {
					s.failed = true
					if _, err := d.send("Target.closeTarget", map[string]any{"targetId": s.target}, ""); err != nil {
						return nil, err
					}
				}
				if len(s.commands) == 0 && !s.failed {
					if _, err := d.send("Runtime.runIfWaitingForDebugger", nil, child); err != nil {
						return nil, err
					}
				}
				continue
			}
		}
		if method == "Fetch.requestPaused" {
			req, _ := params["request"].(map[string]any)
			_, urlErr := CheckURL(d.ctx, str(req["url"]))
			kind := str(params["resourceType"])
			if urlErr != nil || kind == "Image" || kind == "Media" || kind == "Font" {
				_, err = d.send("Fetch.failRequest", map[string]any{"requestId": params["requestId"], "errorReason": "Aborted"}, session)
			} else {
				_, err = d.send("Fetch.continueRequest", map[string]any{"requestId": params["requestId"]}, session)
			}
			if err != nil {
				return nil, err
			}
			continue
		}
		if method == "Inspector.targetCrashed" || method == "Target.targetCrashed" {
			return nil, &browserError{"Browser page crashed."}
		}
		return msg, nil
	}
}

func (d *devTools) call(method string, params map[string]any, session string) (map[string]any, error) {
	id, err := d.send(method, params, session)
	if err != nil {
		return nil, err
	}
	for {
		msg, err := d.receive()
		if err != nil {
			return nil, err
		}
		if num(msg["id"]) == id && msg["id"] != nil {
			if msg["error"] != nil {
				return nil, &browserError{"Local browser command failed: " + method}
			}
			result, _ := msg["result"].(map[string]any)
			return result, nil
		}
		if msg["method"] == "Emulation.virtualTimeBudgetExpired" {
			d.events = append(d.events, msg)
		}
	}
}

// waitBudget waits until the page's virtual-time budget expired, its load
// event fired, and every frame's filter setup finished.
func (d *devTools) waitBudget(session, loader string) error {
	expired := false
	for _, e := range d.events {
		if str(e["sessionId"]) == session {
			expired = true
		}
	}
	d.events = nil
	for !expired || !d.loads[[2]string{session, loader}] || len(d.setup) > 0 {
		msg, err := d.receive()
		if err != nil {
			return err
		}
		if msg["method"] == "Emulation.virtualTimeBudgetExpired" && str(msg["sessionId"]) == session {
			expired = true
		}
	}
	return nil
}

type chromeWorker struct {
	executable string
	startMu    sync.Mutex
	cmd        *exec.Cmd
	exited     chan struct{}
	profile    string
	port       int
	path       string
	active     int
	uses       int
	broken     bool
}

func (w *chromeWorker) running() bool {
	if w.cmd == nil {
		return false
	}
	select {
	case <-w.exited:
		return false
	default:
		return true
	}
}

func (w *chromeWorker) start(ctx context.Context, deadline time.Time) error {
	w.startMu.Lock()
	defer w.startMu.Unlock()
	if w.broken {
		return &browserError{"Browser worker has stopped."}
	}
	if w.running() {
		return nil
	}
	w.close()
	end := Measure(ctx, "browser_startup")
	err := w.launch(deadline)
	end(err != nil)
	return err
}

func (w *chromeWorker) launch(deadline time.Time) error {
	profile, err := os.MkdirTemp("", "jhalcv-browser-pool-")
	if err != nil {
		return err
	}
	w.profile = profile
	w.cmd = exec.Command(w.executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
		"--disable-extensions", "--mute-audio", "--disable-background-networking", "--disable-sync",
		"--disable-component-update", "--remote-debugging-port=0", "--user-data-dir="+profile,
		"--user-agent="+userAgent, "--disable-blink-features=AutomationControlled", "about:blank")
	w.cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} // Its own group, so it can be stopped whole.
	if err := w.cmd.Start(); err != nil {
		return &browserError{"Could not start headless Chrome."}
	}
	w.exited = make(chan struct{})
	go func(cmd *exec.Cmd, done chan struct{}) { _ = cmd.Wait(); close(done) }(w.cmd, w.exited)

	ready := filepath.Join(profile, "DevToolsActivePort")
	stop := time.Now().Add(20 * time.Second)
	if deadline.Before(stop) {
		stop = deadline
	}
	for time.Now().Before(stop) {
		if !w.running() {
			return &browserError{"Could not start headless Chrome."}
		}
		if data, err := os.ReadFile(ready); err == nil {
			lines := strings.Split(strings.TrimSpace(string(data)), "\n")
			if len(lines) >= 2 {
				port, err := strconv.Atoi(strings.TrimSpace(lines[0]))
				if err == nil && port > 0 && port < 65536 && strings.HasPrefix(lines[1], "/devtools/browser/") {
					w.port, w.path = port, strings.TrimSpace(lines[1])
					return nil
				}
			}
		}
		time.Sleep(30 * time.Millisecond)
	}
	return &browserError{"Chrome startup timed out."}
}

func (w *chromeWorker) render(ctx context.Context, link string, deadline time.Time) (page string, err error) {
	if err := w.start(ctx, deadline); err != nil {
		return "", err
	}
	d, err := dialDevTools(ctx, w.port, w.path, deadline)
	if err != nil {
		return "", err
	}
	browserContext := ""
	defer func() {
		if browserContext != "" {
			d.deadline = time.Now().Add(2 * time.Second)
			if _, err := d.call("Target.disposeBrowserContext", map[string]any{"browserContextId": browserContext}, ""); err != nil {
				w.broken = true
			}
		}
		d.conn.Close()
	}()

	created, err := d.call("Target.createBrowserContext", map[string]any{"disposeOnDetach": true}, "")
	if err != nil {
		return "", err
	}
	browserContext = str(created["browserContextId"])
	if _, err := d.call("Browser.setDownloadBehavior", map[string]any{"behavior": "deny", "browserContextId": browserContext}, ""); err != nil {
		return "", err
	}
	target, err := d.call("Target.createTarget", map[string]any{"url": "about:blank", "browserContextId": browserContext}, "")
	if err != nil {
		return "", err
	}
	attached, err := d.call("Target.attachToTarget", map[string]any{"targetId": target["targetId"], "flatten": true}, "")
	if err != nil {
		return "", err
	}
	session := str(attached["sessionId"])

	end := Measure(ctx, "browser_navigation")
	loader, err := navigate(d, session, link)
	end(err != nil)
	if err != nil {
		return "", err
	}
	_ = loader
	end = Measure(ctx, "browser_dom")
	defer func() { end(err != nil) }()
	doc, err := d.call("DOM.getDocument", map[string]any{"depth": 0}, session)
	if err != nil {
		return "", err
	}
	root, _ := doc["root"].(map[string]any)
	outer, err := d.call("DOM.getOuterHTML", map[string]any{"nodeId": root["nodeId"]}, session)
	if err != nil {
		return "", err
	}
	return str(outer["outerHTML"]), nil
}

func navigate(d *devTools, session, link string) (string, error) {
	for _, step := range []struct {
		method string
		params map[string]any
	}{
		{"Page.enable", nil},
		{"Page.setLifecycleEventsEnabled", map[string]any{"enabled": true}},
		{"Target.setAutoAttach", map[string]any{"autoAttach": true, "waitForDebuggerOnStart": true, "flatten": true}},
		{"Fetch.enable", map[string]any{"patterns": []any{map[string]any{"urlPattern": "*", "requestStage": "Request"}}}},
	} {
		if _, err := d.call(step.method, step.params, session); err != nil {
			return "", err
		}
	}
	nav, err := d.call("Page.navigate", map[string]any{"url": link}, session)
	if err != nil {
		return "", err
	}
	loader := str(nav["loaderId"])
	if str(nav["errorText"]) != "" || loader == "" {
		return "", &browserError{"Browser navigation did not complete."}
	}
	if _, err := d.call("Emulation.setVirtualTimePolicy", map[string]any{"policy": "pauseIfNetworkFetchesPending",
		"budget": 9000, "maxVirtualTimeTaskStarvationCount": 10000}, session); err != nil {
		return "", err
	}
	return loader, d.waitBudget(session, loader)
}

func (w *chromeWorker) close() {
	if w.running() {
		_ = syscall.Kill(-w.cmd.Process.Pid, syscall.SIGTERM)
		select {
		case <-w.exited:
		case <-time.After(2 * time.Second):
			_ = syscall.Kill(-w.cmd.Process.Pid, syscall.SIGKILL)
			<-w.exited
		}
	}
	w.cmd = nil
	if w.profile != "" {
		_ = os.RemoveAll(w.profile)
		w.profile = ""
	}
}

type browserPool struct {
	executable string
	mu         sync.Mutex
	changed    *sync.Cond
	workers    []*chromeWorker
	closed     bool
}

var (
	poolMu     sync.Mutex
	sharedPool *browserPool
)

func pool(executable string) *browserPool {
	poolMu.Lock()
	defer poolMu.Unlock()
	if sharedPool == nil {
		sharedPool = &browserPool{executable: executable}
		sharedPool.changed = sync.NewCond(&sharedPool.mu)
	}
	return sharedPool
}

// CloseBrowsers stops every pooled Chrome process. Call it before exiting.
func CloseBrowsers() {
	poolMu.Lock()
	p := sharedPool
	poolMu.Unlock()
	if p == nil {
		return
	}
	p.mu.Lock()
	p.closed = true
	workers := p.workers
	p.workers = nil
	p.changed.Broadcast()
	p.mu.Unlock()
	for _, w := range workers {
		w.startMu.Lock()
		w.broken = true
		w.close()
		w.startMu.Unlock()
	}
}

func (p *browserPool) render(ctx context.Context, link string) (string, error) {
	if _, err := CheckURL(ctx, link); err != nil {
		return "", err
	}
	limit, err := Remaining(ctx, renderTimeout)
	if err != nil {
		return "", err
	}
	deadline := time.Now().Add(limit)

	p.mu.Lock()
	var worker *chromeWorker
	for worker == nil {
		if p.closed {
			p.mu.Unlock()
			return "", fetchErr("The browser pool has stopped.")
		}
		var available []*chromeWorker
		idle := false
		for _, w := range p.workers {
			if !w.broken && w.active < tabsPerProcess {
				available = append(available, w)
				idle = idle || w.active == 0
			}
		}
		switch {
		case len(p.workers) < maxProcesses && !idle:
			worker = &chromeWorker{executable: p.executable}
			p.workers = append(p.workers, worker)
		case len(available) > 0:
			worker = available[0]
			for _, w := range available[1:] {
				if w.active < worker.active {
					worker = w
				}
			}
		default:
			if !time.Now().Before(deadline) {
				p.mu.Unlock()
				return "", fetchErr("Browser pool is busy. Try again later.")
			}
			// sync.Cond has no timeout; wake the waiters at the deadline.
			timer := time.AfterFunc(time.Until(deadline), func() {
				p.mu.Lock()
				p.changed.Broadcast()
				p.mu.Unlock()
			})
			p.changed.Wait()
			timer.Stop()
		}
	}
	worker.active++
	p.mu.Unlock()

	page, err := worker.render(ctx, link, deadline)
	p.mu.Lock()
	if err != nil {
		worker.broken = true
	}
	worker.active--
	worker.uses++
	if worker.uses >= recycleAfter {
		worker.broken = true
	}
	retire := worker.broken && worker.active == 0
	if retire {
		for i, w := range p.workers {
			if w == worker {
				p.workers = append(p.workers[:i], p.workers[i+1:]...)
				break
			}
		}
	}
	p.changed.Broadcast()
	p.mu.Unlock()
	if retire {
		worker.startMu.Lock()
		worker.close()
		worker.startMu.Unlock()
	}
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return "", err
		}
		return "", fetchErr("Chrome could not finish rendering this page; its worker will restart.")
	}
	return page, nil
}
