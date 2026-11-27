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
	s.get("/api/insights", http.StatusBadRequest, func(*http.Request) (any, error) {
		a.Mu.Lock()
		defer a.Mu.Unlock()
		return a.InsightsSummary()
	})
	s.mux.HandleFunc("GET /api/pdf/{id}/{revision}", func(w http.ResponseWriter, r *http.Request) {
		a.Mu.Lock()
		pdf, err := a.PreviewPDF(r.PathValue("id"), r.PathValue("revision"))
		a.Mu.Unlock()
		if err != nil {
			replyFailure(w, r, err, http.StatusNotFound)
			return
		}
		reply(w, http.StatusOK, "application/pdf", pdf)
	})
	s.get("/api/jobs", http.StatusBadRequest, func(r *http.Request) (any, error) {
		return a.JobsState(r.Context(), false)
	})
	s.get("/api/jobs/test", http.StatusBadRequest, func(r *http.Request) (any, error) {
		return a.JobsState(r.Context(), true)
	})
	s.get("/api/jobs/progress", http.StatusBadRequest, func(*http.Request) (any, error) {
		return a.JobsProgress(), nil
	})
	s.get("/api/benchmarks", http.StatusBadRequest, func(r *http.Request) (any, error) {
		report, err := a.Jobs.Benchmarks.Report(r.URL.Query().Get("run"))
		if err != nil {
			if _, ok := usererr.Message(err); ok {
				return nil, err
			}
			return nil, usererr.New("Timing reports are currently unavailable.")
		}
		return report, nil
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
	s.post("/api/tailor", false, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.Tailor(r.Context(), body)
	})
	s.post("/api/preview", true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.Preview(r.Context(), body)
	})
	s.post("/api/export", true, func(_ *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.Export(body)
	})
	s.post("/api/drafts/swap", true, func(_ *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.SwapProject(body)
	})
	s.post("/api/import", true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.ImportProject(r.Context(), body)
	})
	s.post("/api/folder", true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.ChooseFolder(r.Context(), body)
	})
	// Structuring calls an AI and can take a while, so it does not hold the
	// App lock, like the Python app.
	s.post("/api/setup/structure", false, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
		return a.StructureResume(r.Context(), body)
	})
	for _, action := range []string{"extract", "test-ai", "check", "complete"} {
		s.post("/api/setup/"+action, true, func(r *http.Request, body map[string]any, raw []byte) (any, error) {
			return a.SetupAction(r.Context(), strings.TrimPrefix(r.URL.Path, "/api/setup/"), body, raw)
		})
	}
	for _, action := range []string{"detail", "sources/add", "sources/preview", "sources/import", "sources/url",
		"sources/locations", "sources/remove", "refresh", "recheck", "review", "delete-all", "delete", "status",
		"eligibility", "experience"} {
		s.post("/api/jobs/"+action, true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
			return a.JobsAction(r.Context(), strings.TrimPrefix(r.URL.Path, "/api/jobs/"), body)
		})
	}
	for _, action := range []string{"add", "hide", "unhide", "reset"} {
		s.post("/api/insights/"+action, true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
			return a.InsightAction(strings.TrimPrefix(r.URL.Path, "/api/insights/"), body)
		})
	}
	for _, action := range []string{"add", "remove", "prefer", "model", "reminder"} {
		s.post("/api/keys/"+action, true, func(r *http.Request, body map[string]any, _ []byte) (any, error) {
			return a.KeyAction(strings.TrimPrefix(r.URL.Path, "/api/keys/"), body)
		})
	}
}
