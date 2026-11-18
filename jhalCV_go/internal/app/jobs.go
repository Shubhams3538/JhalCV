package app

import (
	"context"
	"errors"
	"math"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"modernc.org/sqlite"

	"jhalcv/internal/jobs"
	"jhalcv/internal/llm"
	"jhalcv/internal/usererr"
)

// newAsker builds the job AI for one refresh: after setup, the provider the
// user chose; before it, the installed subscription CLIs.
func (a *App) newAsker() jobs.Asker {
	if a.SavedSetup()["completed"] == true {
		return a.configuredAgents()
	}
	return jobs.NewAgents(a.Usage, filepath.Join(a.Root, ".cache", "jobs-ai"))
}

func (a *App) configuredAgents() *jobs.Configured {
	provider := "rules"
	if p, ok := a.SavedSetup()["provider"].(string); ok {
		provider = p
	}
	var order []string
	routes, cli, err := a.Selection(&provider)
	if err == nil {
		for _, r := range routes {
			if !slices.Contains(order, r.Provider) {
				order = append(order, r.Provider)
			}
		}
		if cli != nil {
			order = append(order, cli...)
		} else {
			for _, r := range llm.CLIRoutes(context.Background()) {
				order = append(order, r.Provider)
			}
		}
	}
	return jobs.NewConfigured(a.Usage, order, func(ctx context.Context) (context.Context, *llm.Session, error) {
		return a.Scope(ctx, nil)
	})
}

// jobID reads a job or source ID the way Python's int() does.
func jobID(v any) (int64, error) {
	switch x := v.(type) {
	case float64:
		return int64(math.Trunc(x)), nil
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case string:
		if n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64); err == nil {
			return n, nil
		}
	}
	return 0, usererr.New("Choose a job or source.")
}

// dbBusy turns a SQLite failure into the message Python shows.
func dbBusy(err error, message string) error {
	var sqliteErr *sqlite.Error
	if errors.As(err, &sqliteErr) {
		return usererr.New(message)
	}
	return err
}

const jobsBusy = "The jobs database is busy or unreadable. Try again in a moment."

// JobsState handles GET /api/jobs and /api/jobs/test.
func (a *App) JobsState(ctx context.Context, test bool) (*jobs.State, error) {
	profile, _, err := a.Store.Profile()
	if err != nil {
		return nil, err
	}
	service := a.Jobs
	message := jobsBusy
	if test {
		service, message = a.JobsTest, "The test database is busy or unreadable. Try again in a moment."
	}
	state, err := service.State(ctx, profile)
	return state, dbBusy(err, message)
}

// JobsProgress handles GET /api/jobs/progress.
func (a *App) JobsProgress() map[string]any {
	return map[string]any{"progress": a.Jobs.Progress(), "test_progress": a.JobsTest.Progress()}
}

// JobsAction handles POST /api/jobs/<action>.
func (a *App) JobsAction(ctx context.Context, action string, body map[string]any) (any, error) {
	result, err := a.jobsAction(ctx, action, body)
	return result, dbBusy(err, jobsBusy)
}

func (a *App) jobsAction(ctx context.Context, action string, body map[string]any) (any, error) {
	profile, _, err := a.Store.Profile()
	if err != nil {
		return nil, err
	}
	store := a.Jobs.Store
	id := func() (int64, error) { return jobID(body["id"]) }
	switch action {
	case "detail":
		n, err := id()
		if err != nil {
			return nil, err
		}
		return store.Job(ctx, n)
	case "sources/add":
		include, exclude, locations := orBlank(body, "include"), orBlank(body, "exclude"), orBlank(body, "locations")
		_, err = a.Jobs.AddSource(ctx, body["name"], body["url"], include, exclude, locations)
	case "sources/preview":
		return a.Jobs.PreviewSources(ctx, body["csv"])
	case "sources/import":
		locations, ok := body["locations"]
		if !ok {
			locations = "India"
		}
		result, err := a.Jobs.ImportSources(ctx, body["rows"], locations)
		if err != nil {
			return nil, err
		}
		state, err := a.Jobs.State(ctx, profile)
		if err != nil {
			return nil, err
		}
		return map[string]any{"imported": result["imported"], "skipped": result["skipped"], "state": state}, nil
	case "sources/url":
		n, err := id()
		if err != nil {
			return nil, err
		}
		err = a.Jobs.EditSourceURL(ctx, n, body["url"])
		if err != nil {
			return nil, err
		}
	case "sources/locations":
		n, err := id()
		if err != nil {
			return nil, err
		}
		if err := store.SetSourceLocations(ctx, n, jobs.Locations(orBlank(body, "locations"))); err != nil {
			return nil, err
		}
	case "sources/remove":
		n, err := id()
		if err != nil {
			return nil, err
		}
		if err := store.RemoveSource(ctx, n); err != nil {
			return nil, err
		}
	case "refresh":
		sources, err := store.Sources(ctx)
		if err != nil {
			return nil, err
		}
		if len(sources) == 0 {
			return nil, usererr.New("Add a careers page first.")
		}
		test := truthy(body["test"])
		var chosen []int64
		if raw, ok := body["sources"]; ok && raw != nil {
			list, isList := raw.([]any)
			valid := isList && len(list) > 0
			for _, item := range list {
				n, isNum := item.(float64)
				if !isNum || n != math.Trunc(n) || !slices.ContainsFunc(sources, func(s jobs.Source) bool { return s.ID == int64(n) }) {
					valid = false
					break
				}
				chosen = append(chosen, int64(n))
			}
			if !valid {
				return nil, usererr.New("Choose at least one of your saved companies to refresh.")
			}
		}
		service := a.Jobs
		if test {
			chosen, service = nil, a.JobsTest // A test run always uses its own company slice.
		}
		if !service.Start(profile, false, chosen, test) {
			return nil, usererr.New("A refresh is already running.")
		}
	case "recheck", "review":
		if !a.Jobs.Start(profile, true, nil, false) {
			return nil, usererr.New("A refresh is already running.")
		}
	case "delete-all":
		err = store.DeleteCategory(ctx, body["category"])
	case "delete":
		n, err := id()
		if err != nil {
			return nil, err
		}
		if err := store.DeleteJob(ctx, n); err != nil {
			return nil, err
		}
	case "status":
		n, err := id()
		if err != nil {
			return nil, err
		}
		if err := store.SetStatus(ctx, n, body["status"]); err != nil {
			return nil, err
		}
	case "eligibility":
		n, err := id()
		if err != nil {
			return nil, err
		}
		verdict, ok := body["eligible"]
		if !ok {
			return nil, usererr.New("Choose a manual eligibility decision.")
		}
		if err := store.SetManualEligibility(ctx, n, verdict); err != nil {
			return nil, err
		}
	case "experience":
		err = a.Jobs.SetExperienceYears(ctx, body["years"])
	default:
		return nil, usererr.New("Unknown jobs action.")
	}
	if err != nil {
		return nil, err
	}
	return a.Jobs.State(ctx, profile)
}

func orBlank(body map[string]any, key string) any {
	if v, ok := body[key]; ok {
		return v
	}
	return ""
}

// truthy matches Python's bool() for decoded JSON.
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}
