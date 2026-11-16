package web

import (
	"encoding/json"
	"net/http"
	"strings"

	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

func (s *Server) getRoutes() {
	a := s.app
	s.get("/api/state", http.StatusBadRequest, func(*http.Request) (any, error) {
		a.Mu.Lock()
		defer a.Mu.Unlock()
		return a.State()
	})
	s.get("/api/tailor/progress", http.StatusBadRequest, func(*http.Request) (any, error) {
		return a.Timing.Snapshot(), nil
	})
	s.get("/api/usage", http.StatusServiceUnavailable, func(*http.Request) (any, error) {
		summary, err := a.Usage.Summary()
		if err != nil {
			return nil, usererr.New("Usage accounting is currently unavailable.")
		}
		return summary, nil
	})
}

func (s *Server) postRoutes() {
	a := s.app
	s.post("/api/profile", true, func(_ *http.Request, _ map[string]any, raw []byte) (any, error) {
		return a.SaveProfile(raw)
	})
	s.post("/api/projects/remove", true, func(_ *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.RemoveProject(body)
	})
	s.post("/api/settings", true, func(_ *http.Request, _ map[string]any, raw []byte) (any, error) {
		var settings storage.Settings
		if err := json.Unmarshal(raw, &settings); err != nil {
			return nil, usererr.New("Settings: expected an object.")
		}
		return a.Store.SaveSettings(settings)
	})
	s.post("/api/usage/settings", true, func(_ *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.Usage.SetReference(body["model"])
	})
	for _, action := range []string{"add", "remove", "prefer", "model", "reminder"} {
		s.post("/api/keys/"+action, true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
			return a.KeyAction(strings.TrimPrefix(r.URL.Path, "/api/keys/"), body)
		})
	}
}
