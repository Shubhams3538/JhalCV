package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"

	"jhalcv/internal/app"
)

const (
	contentTypeJSON       = "application/json; charset=utf-8"
	contentSecurityPolicy = "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'self'"
)

// newRouter maps each URL the browser may request to the code that answers it.
func newRouter(webDir string, a *app.App) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /{$}", serveFile(webDir, "index.html", "text/html; charset=utf-8"))
	mux.HandleFunc("GET /app.js", serveFile(webDir, "app.js", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /theme.js", serveFile(webDir, "theme.js", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /onboarding.js", serveFile(webDir, "onboarding.js", "text/javascript; charset=utf-8"))
	mux.HandleFunc("GET /style.css", serveFile(webDir, "style.css", "text/css; charset=utf-8"))

	mux.HandleFunc("GET /api/state", func(w http.ResponseWriter, _ *http.Request) {
		state, err := a.State()
		if err != nil {
			log.Printf("GET /api/state: %v", err)
			replyError(w, http.StatusInternalServerError, "Could not load your workspace. Check the terminal for details.")
			return
		}
		replyJSON(w, http.StatusOK, state)
	})

	// Anything not matched above.
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		replyError(w, http.StatusNotFound, "Not found.")
	})

	return mux
}

// serveFile returns a handler that sends one file from web/.
func serveFile(webDir, name, contentType string) http.HandlerFunc {
	path := filepath.Join(webDir, name)

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
	// Encoding a map of strings cannot fail, so the error is ignored.
	body, _ := json.Marshal(map[string]string{"error": message})
	reply(w, status, contentTypeJSON, body)
}

// replyJSON sends any Go value encoded as JSON.
func replyJSON(w http.ResponseWriter, status int, value any) {
	body, err := json.Marshal(value)
	if err != nil {
		log.Printf("encode response: %v", err)
		replyError(w, http.StatusInternalServerError, "Could not prepare the response.")
		return
	}
	reply(w, status, contentTypeJSON, body)
}

// requireLocalHost rejects requests that were not addressed to our exact local
// URL. This blocks DNS-rebinding attacks, where a website tricks your browser
// into talking to a server on your own machine.
func requireLocalHost(port int, next http.Handler) http.Handler {
	trusted := "127.0.0.1:" + strconv.Itoa(port)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != trusted {
			replyError(w, http.StatusForbidden, "Use the local app URL printed in your terminal.")
			return
		}
		next.ServeHTTP(w, r)
	})
}
