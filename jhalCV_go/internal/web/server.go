// Package web is the local HTTP server: it serves the frontend in web/ and
// turns /api/... requests into calls on the App. Like resume/web.py it
// listens only on 127.0.0.1 and accepts actions only from its own page.
package web

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"jhalcv/internal/app"
	"jhalcv/internal/usererr"
)

const (
	contentTypeJSON       = "application/json; charset=utf-8"
	contentSecurityPolicy = "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'self'"
	maxBody               = 2_000_000
	genericFailure        = "The operation failed. Your saved profile is unchanged. Check your tools and settings and retry."
)

// Server answers the page's requests for one App.
type Server struct {
	app    *app.App
	webDir string
	origin string // e.g. http://127.0.0.1:8765
	host   string // e.g. 127.0.0.1:8765
	mux    *http.ServeMux
	posts  map[string]postHandler
}

// postHandler answers one POST action. body is the decoded JSON object and
// raw its exact bytes, for actions that decode into typed structs.
type postHandler struct {
	run func(r *http.Request, body map[string]any, raw []byte) (any, error)
	// locked actions hold the App lock, like the Python app's lock. Long
	// operations (tailoring, AI calls) manage their own locking.
	locked bool
}

// New builds the handler for app, serving frontend files from webDir.
func New(a *app.App, webDir string, port int) http.Handler {
	s := &Server{
		app:    a,
		webDir: webDir,
		host:   "127.0.0.1:" + strconv.Itoa(port),
		origin: "http://127.0.0.1:" + strconv.Itoa(port),
		mux:    http.NewServeMux(),
		posts:  map[string]postHandler{},
	}
	s.routes()
	return s
}

// ServeHTTP checks the Host header on every request. This blocks DNS
// rebinding, where a website tricks the browser into calling a local server.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Host != s.host {
		replyError(w, http.StatusForbidden, "Use the local app URL printed in your terminal.")
		return
	}
	if r.Method == http.MethodPost {
		s.servePost(w, r)
		return
	}
	s.mux.ServeHTTP(w, r)
}

func (s *Server) routes() {
	for path, file := range map[string][2]string{
		"/{$}":           {"index.html", "text/html; charset=utf-8"},
		"/app.js":        {"app.js", "text/javascript; charset=utf-8"},
		"/theme.js":      {"theme.js", "text/javascript; charset=utf-8"},
		"/onboarding.js": {"onboarding.js", "text/javascript; charset=utf-8"},
		"/style.css":     {"style.css", "text/css; charset=utf-8"},
	} {
		s.mux.HandleFunc("GET "+path, s.serveFile(file[0], file[1]))
	}
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		replyError(w, http.StatusNotFound, "Not found.")
	})
	s.getRoutes()
	s.postRoutes()
}

// get registers a GET route whose handler returns a value to send as JSON.
// A failure is reported with status failStatus (400 for most reads).
func (s *Server) get(path string, failStatus int, fn func(r *http.Request) (any, error)) {
	s.mux.HandleFunc("GET "+path, func(w http.ResponseWriter, r *http.Request) {
		value, err := fn(r)
		if err != nil {
			replyFailure(w, r, err, failStatus)
			return
		}
		replyJSON(w, http.StatusOK, value)
	})
}

// post registers a POST action.
func (s *Server) post(path string, locked bool, fn func(r *http.Request, body map[string]any, raw []byte) (any, error)) {
	s.posts[path] = postHandler{fn, locked}
}

func (s *Server) servePost(w http.ResponseWriter, r *http.Request) {
	// Only our own page knows the token, which it got from /api/state; other
	// websites cannot read it, so they cannot trigger actions.
	token := r.Header.Get("X-App-Token")
	origin := r.Header.Get("Origin")
	if (origin != "" && origin != s.origin) || subtle.ConstantTimeCompare([]byte(token), []byte(s.app.Token())) != 1 {
		replyError(w, http.StatusForbidden, "Reload the local app before trying again.")
		return
	}
	if r.ContentLength <= 0 || r.ContentLength > maxBody {
		replyError(w, http.StatusRequestEntityTooLarge, "Request must be under 2 MB.")
		return
	}
	if mediaType, _, _ := strings.Cut(r.Header.Get("Content-Type"), ";"); mediaType != "application/json" {
		replyError(w, http.StatusBadRequest, "Expected JSON.")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, maxBody))
	if err != nil {
		replyError(w, http.StatusBadRequest, "Expected JSON.")
		return
	}
	var body map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := dec.Decode(&body); err != nil || body == nil {
		replyError(w, http.StatusBadRequest, "Expected a JSON object.")
		return
	}

	h, ok := s.posts[r.URL.Path]
	if !ok {
		replyError(w, http.StatusBadRequest, "Unknown action.")
		return
	}
	if h.locked {
		s.app.Mu.Lock()
		defer s.app.Mu.Unlock()
	}
	value, err := h.run(r, body, raw)
	if err != nil {
		replyFailure(w, r, err, http.StatusBadRequest)
		return
	}
	replyJSON(w, http.StatusOK, value)
}

func (s *Server) serveFile(name, contentType string) http.HandlerFunc {
	path := filepath.Join(s.webDir, name)
	return func(w http.ResponseWriter, _ *http.Request) {
		body, err := os.ReadFile(path)
		if err != nil {
			replyError(w, http.StatusInternalServerError, "Could not read "+name+".")
			return
		}
		reply(w, http.StatusOK, contentType, body)
	}
}

// reply sends a response with the same security headers as the Python app.
func reply(w http.ResponseWriter, status int, contentType string, body []byte) {
	h := w.Header()
	h.Set("Content-Type", contentType)
	h.Set("Content-Length", strconv.Itoa(len(body)))
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("Content-Security-Policy", contentSecurityPolicy)
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// replyError sends {"error": message} as JSON.
func replyError(w http.ResponseWriter, status int, message string) {
	body, _ := json.Marshal(map[string]string{"error": message})
	reply(w, status, contentTypeJSON, body)
}

// replyJSON sends any Go value encoded as JSON.
func replyJSON(w http.ResponseWriter, status int, value any) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		log.Printf("encode response: %v", err)
		replyError(w, http.StatusInternalServerError, "Could not prepare the response.")
		return
	}
	reply(w, status, contentTypeJSON, bytes.TrimRight(buf.Bytes(), "\n"))
}

// replyFailure tells the page an operation failed. A user-facing error, or
// a file-system error (as Python's OSError), is shown as is with status. Any
// other error is logged here and replaced with a generic message (500), so
// internals never reach the page.
func replyFailure(w http.ResponseWriter, r *http.Request, err error, status int) {
	if msg, ok := usererr.Message(err); ok {
		replyError(w, status, msg)
		return
	}
	var pathErr *fs.PathError
	if errors.As(err, &pathErr) {
		replyError(w, status, pathErr.Error())
		return
	}
	log.Printf("%s %s: %v", r.Method, r.URL.Path, err)
	replyError(w, http.StatusInternalServerError, genericFailure)
}
