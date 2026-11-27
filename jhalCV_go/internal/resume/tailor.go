package resume

import (
	"encoding/json"
	"regexp"
	"slices"
	"strings"

	"jhalcv/internal/storage"
)

// angleKeywords score a job description against each angle, in this order.
var angleKeywords = []struct {
	angle string
	words []string
}{
	{"backend", strings.Fields(`api apis rest restful backend server-side microservice microservices
        endpoint endpoints node nodejs express spring django fastapi flask grpc graphql
        sql database databases postgres postgresql mysql mongodb redis orm auth
        authentication authorization webhook queue caching`)},
	{"systems", strings.Fields(`performance latency throughput concurrency concurrent thread threading
        threads multithreaded memory low-level systems compiler runtime interpreter
        optimize optimization profiling kernel socket sockets sandbox sandboxing
        parallel lock-free allocator embedded c c++ cpp rust zig go golang`)},
	{"infra", strings.Fields(`docker kubernetes k8s aws gcp azure cloud ci cd ci/cd pipeline deploy
        deployment terraform ansible infrastructure devops sre cdn scaling scalability
        observability monitoring prometheus grafana serverless lambda s3 ec2`)},
	{"frontend", strings.Fields(`react reactjs frontend front-end ui ux javascript typescript css html
        tailwind next nextjs vue angular svelte responsive android ios mobile electron
        chromium browser dom component components`)},
	{"agentic", strings.Fields(`agent agents agentic llm llms rag prompt prompting openai anthropic
        claude gpt langchain langgraph llamaindex embedding embeddings vector
        vectordb pinecone chroma genai generative tool-calling function-calling mcp
        fine-tuning inference chatbot copilot autonomous multi-agent`)},
	{"data", strings.Fields(`data etl elt pipeline pipelines analytics warehouse lakehouse pandas numpy
        spark airflow dbt kafka streaming ml machine-learning model training dataset`)},
	{"eval", strings.Fields(`eval evals evaluation testing test tests qa benchmark benchmarking
        regression pytest unit integration coverage golden`)},
	{"dsa", strings.Fields(`algorithm algorithms data-structures problem-solving competitive
        complexity optimization leetcode codeforces`)},
	{"teaching", strings.Fields(`mentor mentoring mentorship teach teaching training instructor
        onboarding curriculum workshop lead leadership`)},
}

// aliases are spellings a substring match would miss.
var aliases = map[string]string{
	"k8s": "kubernetes", "kubernetes": "k8s",
	"js": "javascript", "ts": "typescript",
	"postgres": "postgresql", "postgresql": "postgres",
	"golang": "go", "cpp": "c++", "c++": "cpp",
	"nodejs": "node.js", "node": "node.js",
	"ml": "machine learning", "ai": "artificial intelligence",
	"rest": "rest apis", "restful": "rest apis",
}

// techTerms are named technologies only, so the gap report says "you are
// missing Kubernetes", never "you are missing performance".
var techTerms = setOf(strings.Fields(`
    python java javascript typescript go golang rust c c++ cpp ruby php scala kotlin swift
    node nodejs express django flask fastapi spring rails laravel dotnet
    react vue angular svelte next nextjs tailwind redux
    postgres postgresql mysql mongodb redis sqlite cassandra dynamodb elasticsearch neo4j
    docker kubernetes k8s terraform ansible jenkins github gitlab circleci argocd
    aws gcp azure lambda s3 ec2 eks cloudflare vercel
    kafka rabbitmq celery airflow spark hadoop dbt snowflake databricks
    graphql grpc rest websocket protobuf openapi
    prometheus grafana datadog sentry opentelemetry
    openai anthropic claude gpt langchain langgraph llamaindex huggingface
    pinecone weaviate chroma qdrant faiss pgvector
    pytorch tensorflow sklearn pandas numpy
    pytest jest cypress playwright selenium junit
    linux kubernetes nginx envoy istio grpc
    electron chromium android ios flutter firebase supabase`))

func setOf(items []string) map[string]bool {
	m := make(map[string]bool, len(items))
	for _, item := range items {
		m[item] = true
	}
	return m
}

const (
	minShippedBullets  = 2
	maxSkillsPerGroup  = 9
	defaultProjectCap  = 2
	defaultJobBullets  = 4
	defaultProjBullets = 3
)

var token = regexp.MustCompile(`[a-z0-9][a-z0-9+#./-]*`)

// AngleScore is one angle's share of a job description.
type AngleScore struct {
	Angle string
	Score float64
}

// AngleScores are sorted highest first and encode as a JSON object.
type AngleScores []AngleScore

// Get returns the score for angle, or 0.
func (s AngleScores) Get(angle string) float64 {
	for _, a := range s {
		if a.Angle == angle {
			return a.Score
		}
	}
	return 0
}

// MarshalJSON writes the scores as an object, highest first.
func (s AngleScores) MarshalJSON() ([]byte, error) {
	var b strings.Builder
	b.WriteByte('{')
	for i, a := range s {
		if i > 0 {
			b.WriteByte(',')
		}
		k, _ := json.Marshal(a.Angle)
		v, _ := json.Marshal(a.Score)
		b.Write(k)
		b.WriteByte(':')
		b.Write(v)
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}

func sortScores(s AngleScores) {
	slices.SortStableFunc(s, func(a, b AngleScore) int {
		switch {
		case a.Score > b.Score:
			return -1
		case a.Score < b.Score:
			return 1
		}
		return 0
	})
}

// Analysis is what a job description asks for.
type Analysis struct {
	AngleScores   AngleScores `json:"angle_scores"`
	MatchedSkills []string    `json:"matched_skills"`
	GapSkills     []string    `json:"gap_skills"`
	Blob          string      `json:"-"`
	Summary       string      `json:"summary,omitempty"`
	Seniority     string      `json:"seniority,omitempty"`
}

// vocabulary is every technology the profile claims.
func vocabulary(p *storage.Profile) []string {
	seen := map[string]bool{}
	add := func(items []string) {
		for _, item := range items {
			seen[item] = true
		}
	}
	for _, g := range p.Skills {
		if !g.IsNote() {
			add(g.Items)
		}
	}
	for _, e := range p.Experience {
		add(e.Stack)
	}
	for _, pr := range p.Projects {
		add(pr.Stack)
	}
	vocab := make([]string, 0, len(seen))
	for item := range seen {
		vocab = append(vocab, item)
	}
	slices.Sort(vocab)
	return vocab
}

// ParseJD scores a job description against the angles and lists the
// profile technologies it names and the technologies it asks for that the
// profile lacks (the gap report). It is deterministic and needs no AI.
func ParseJD(jd string, p *storage.Profile) *Analysis {
	blob := " " + strings.ToLower(jd) + " "
	var scores AngleScores
	total := 0
	counts := make([]int, len(angleKeywords))
	for i, ak := range angleKeywords {
		for _, w := range ak.words {
			if termPresent(w, blob) {
				counts[i]++
			}
		}
		total += counts[i]
	}
	if total == 0 {
		total = 1
	}
	for i, ak := range angleKeywords {
		if counts[i] > 0 {
			scores = append(scores, AngleScore{ak.angle, round(float64(counts[i])/float64(total), 4)})
		}
	}
	sortScores(scores)

	vocab := vocabulary(p)
	matched := []string{}
	for _, term := range vocab {
		alias, ok := aliases[strings.ToLower(term)]
		if termPresent(term, blob) || (ok && termPresent(alias, blob)) {
			matched = append(matched, term)
		}
	}

	known := map[string]bool{}
	for _, v := range vocab {
		known[strings.ToLower(v)] = true
	}
	for v := range known {
		if a, ok := aliases[v]; ok {
			known[strings.ToLower(a)] = true
		}
	}
	gaps := map[string]bool{}
	for _, tok := range token.FindAllString(strings.ToLower(jd), -1) {
		if !techTerms[tok] || known[tok] {
			continue
		}
		if a, ok := aliases[tok]; ok && known[strings.ToLower(a)] {
			continue
		}
		gaps[tok] = true
	}
	gapList := make([]string, 0, len(gaps))
	for g := range gaps {
		gapList = append(gapList, g)
	}
	slices.Sort(gapList)
	return &Analysis{AngleScores: scores, MatchedSkills: matched, GapSkills: gapList, Blob: blob}
}

// Renderable removes everything not yet true before any tailorer sees it:
// planned bullets, and projects with fewer than two shipped bullets.
func Renderable(p *storage.Profile) *storage.Profile {
	out := clone(p)
	projects := out.Projects[:0]
	for _, pr := range out.Projects {
		var shipped []storage.Bullet
		for _, b := range pr.Bullets {
			if b.Status == "" || b.Status == "shipped" {
				shipped = append(shipped, b)
			}
		}
		if len(shipped) >= minShippedBullets {
			pr.Bullets = shipped
			projects = append(projects, pr)
		}
	}
	out.Projects = projects
	return out
}

// ProjectCap is the number of projects on the page: the profile's own limit
// wins over the requested one.
func ProjectCap(p *storage.Profile, requested int) int {
	if hard := p.Locked.MaxProjectsOnPage; hard != nil && *hard > 0 {
		return min(requested, *hard)
	}
	return requested
}

func scoreBullet(b storage.Bullet, a *Analysis) float64 {
	score := 0.0
	for _, angle := range b.Angles {
		score += a.AngleScores.Get(angle)
	}
	text := strings.ToLower(b.Text)
	for _, term := range a.MatchedSkills {
		if termPresent(term, text) {
			score += 0.05
		}
	}
	if strings.ContainsAny(b.Text, "0123456789") {
		score += 0.02
	}
	return round(score, 5)
}

func scoreProject(p storage.Project, a *Analysis) float64 {
	score := 0.0
	for _, angle := range p.Angles {
		score += a.AngleScores.Get(angle)
	}
	for _, tech := range p.Stack {
		if slices.Contains(a.MatchedSkills, tech) {
			score += 0.15
		}
	}
	best := make([]float64, 0, len(p.Bullets))
	for _, b := range p.Bullets {
		best = append(best, scoreBullet(b, a))
	}
	slices.SortFunc(best, func(x, y float64) int { return compareDesc(x, y) })
	for i := 0; i < len(best) && i < 3; i++ {
		score += best[i] * 0.5
	}
	return round(score, 5)
}

func compareDesc(x, y float64) int {
	switch {
	case x > y:
		return -1
	case x < y:
		return 1
	}
	return 0
}

func pickAngle(p storage.Project, a *Analysis) any {
	if len(p.Angles) == 0 {
		return nil
	}
	best := p.Angles[0]
	for _, angle := range p.Angles[1:] {
		if a.AngleScores.Get(angle) > a.AngleScores.Get(best) {
			best = angle
		}
	}
	return best
}

// printedSkills is the printed part of a skill group, at most nine items.
// A pinned skill is never cut by the cap.
func printedSkills(ordered, pinnedNames []string) []string {
	pinned := map[string]bool{}
	for _, name := range pinnedNames {
		pinned[Canonical(name)] = true
	}
	printed := slices.Clone(ordered[:min(len(ordered), maxSkillsPerGroup)])
	if len(ordered) > maxSkillsPerGroup {
		for _, skill := range ordered[maxSkillsPerGroup:] {
			if !pinned[Canonical(skill)] {
				continue
			}
			for i := len(printed) - 1; i >= 0; i-- {
				if !pinned[Canonical(printed[i])] {
					printed[i] = skill
					break
				}
			}
		}
	}
	if printed == nil {
		printed = []string{}
	}
	return printed
}

// termWeight is a job term's spelling and weight (required terms count 2).
type termWeight struct {
	term   string
	weight int
}

// Keyword is a technology term the job names, from the AI analysis.
type Keyword struct {
	Term       string `json:"term"`
	Importance string `json:"importance"`
}

func termWeights(matched []string, keywords []Keyword) map[string]termWeight {
	weights := map[string]termWeight{}
	for _, term := range matched {
		key := Canonical(term)
		if _, ok := weights[key]; !ok {
			weights[key] = termWeight{term, 1}
		}
	}
	for _, item := range keywords {
		weight := 1
		if item.Importance == "required" {
			weight = 2
		}
		key := Canonical(item.Term)
		weights[key] = termWeight{item.Term, max(weight, weights[key].weight)}
	}
	return weights
}

// coveredTerms are the job terms a project proves through its stack or text.
func coveredTerms(p storage.Project, terms map[string]termWeight) map[string]bool {
	stack := map[string]bool{}
	for _, tech := range p.Stack {
		stack[Canonical(tech)] = true
	}
	var texts []string
	for _, b := range p.Bullets {
		texts = append(texts, strings.ReplaceAll(b.Text, "**", ""))
	}
	text := " " + strings.ToLower(strings.Join(texts, " ")) + " "
	covered := map[string]bool{}
	for key, tw := range terms {
		if stack[key] || termPresent(tw.term, text) {
			covered[key] = true
		}
	}
	return covered
}

// swap records a lower-ranked project chosen because it proves more of the job.
type swap struct {
	insteadOf, chosen storage.Project
	adds              []string
}

// diversify takes projects in rank order, except where a later slot can prove
// more of the job: a lower-ranked project replaces the ranked one only when
// it proves strictly more job terms not already shown.
func diversify(ranked []storage.Project, terms map[string]termWeight, count int) ([]storage.Project, []swap) {
	coverage := make([]map[string]bool, len(ranked))
	for i, p := range ranked {
		coverage[i] = coveredTerms(p, terms)
	}
	pool := make([]int, len(ranked))
	for i := range pool {
		pool[i] = i
	}
	covered := map[string]bool{}
	gain := func(i int) int {
		g := 0
		for key := range coverage[i] {
			if !covered[key] {
				g += terms[key].weight
			}
		}
		return g
	}
	var chosen []storage.Project
	var swaps []swap
	for len(pool) > 0 && len(chosen) < count {
		pick := pool[0]
		if len(chosen) > 0 {
			best := pool[0] // Ties keep the higher-ranked project.
			for _, i := range pool[1:] {
				if gain(i) > gain(best) {
					best = i
				}
			}
			if gain(best) > gain(pick) {
				var adds []string
				for key := range coverage[best] {
					if !covered[key] {
						adds = append(adds, terms[key].term)
					}
				}
				slices.Sort(adds)
				swaps = append(swaps, swap{ranked[pick], ranked[best], adds})
				pick = best
			}
		}
		chosen = append(chosen, ranked[pick])
		pool = slices.DeleteFunc(pool, func(i int) bool { return i == pick })
		for key := range coverage[pick] {
			covered[key] = true
		}
	}
	return chosen, swaps
}

// Tailor is the deterministic tailorer: it ranks the profile's bullets,
// projects and skills for the job and returns the resume data plus a report
// explaining every decision.
func Tailor(profile *storage.Profile, jd string, maxProjects, bulletsPerJob, bulletsPerProject int) (*Data, Report) {
	p := Renderable(profile)
	maxProjects = ProjectCap(p, maxProjects)
	a := ParseJD(jd, p)
	decisions := []any{}
	report := Report{"angle_scores": a.AngleScores, "gap_skills": a.GapSkills,
		"matched_skills": a.MatchedSkills}

	frozen := setOf(p.Locked.FrozenSections)
	experience := []ExperienceEntry{}
	for _, e := range p.Experience {
		entry := ExperienceEntry{Org: e.Org, Role: e.Role, Location: e.Location, Start: e.Start, End: e.End}
		if frozen["experience"] {
			// Frozen: every bullet, verbatim, in profile order.
			var kept []string
			for _, b := range e.Bullets {
				entry.Bullets = append(entry.Bullets, b.Text)
				entry.BulletMeta = append(entry.BulletMeta, BulletMeta{ID: b.ID})
				kept = append(kept, b.ID)
			}
			decisions = append(decisions, map[string]any{"section": "experience", "org": e.Org, "frozen": true, "kept": kept})
		} else {
			quota := bulletsPerJob
			if e.MaxBullets != nil {
				quota = *e.MaxBullets
			}
			chosen, dropped := topBullets(e.Bullets, a, quota)
			for _, b := range chosen {
				score := scoreBullet(b, a)
				entry.Bullets = append(entry.Bullets, b.Text)
				entry.BulletMeta = append(entry.BulletMeta, BulletMeta{ID: b.ID, Score: &score})
			}
			decisions = append(decisions, map[string]any{"section": "experience", "org": e.Org,
				"kept": bulletIDs(chosen), "dropped": bulletIDs(dropped)})
		}
		experience = append(experience, entry.normalized())
	}

	// Projects: rank, take the top N (diversified), angle each one.
	ranked := slices.Clone(p.Projects)
	scores := map[string]float64{}
	for _, pr := range ranked {
		scores[pr.ID] = scoreProject(pr, a)
	}
	slices.SortStableFunc(ranked, func(x, y storage.Project) int { return compareDesc(scores[x.ID], scores[y.ID]) })
	selected, swaps := diversify(ranked, termWeights(a.MatchedSkills, nil), maxProjects)
	for _, s := range swaps {
		decisions = append(decisions, map[string]any{"section": "projects", "chosen": s.chosen.Name,
			"instead_of": s.insteadOf.Name, "adds": s.adds})
	}
	projects := []ProjectEntry{}
	for _, pr := range selected {
		chosen, _ := topBullets(pr.Bullets, a, bulletsPerProject)
		entry := newProjectEntry(pr, chosen, a, true)
		projects = append(projects, entry)
		decisions = append(decisions, map[string]any{"section": "projects", "name": pr.Name,
			"score": scores[pr.ID], "angle": pickAngle(pr, a), "kept": bulletIDs(chosen)})
	}
	notSelected := []any{}
	for _, pr := range ranked {
		if !slices.ContainsFunc(selected, func(s storage.Project) bool { return s.ID == pr.ID }) {
			notSelected = append(notSelected, map[string]any{"name": pr.Name, "score": scores[pr.ID]})
		}
	}
	decisions = append(decisions, map[string]any{"section": "projects", "not_selected": notSelected})
	report["decisions"] = decisions

	// Skills: inside each group, the job's matched terms come first.
	skills := []SkillRow{}
	for _, g := range p.Skills {
		if g.IsNote() {
			continue
		}
		hits, rest := []string{}, []string{}
		for _, s := range g.Items {
			if slices.Contains(a.MatchedSkills, s) {
				hits = append(hits, s)
			} else {
				rest = append(rest, s)
			}
		}
		skills = append(skills, SkillRow{Label: g.Name, Items: printedSkills(append(slices.Clone(hits), rest...), p.Locked.PinnedSkills), Matched: hits})
	}

	data := &Data{
		Basics:         p.Basics,
		Experience:     experience,
		Projects:       projects,
		CodingProfiles: p.CodingProfiles,
		Skills:         skills,
		Education:      p.Education,
		SectionOrder:   []string{"experience", "coding_profiles", "projects", "skills", "education"},
		Config:         map[string]any{},
	}
	return data.normalized(), report
}

// topBullets picks the quota highest-scoring bullets, returned in profile
// order (chronology reads better), plus the ones left out (highest first).
func topBullets(bullets []storage.Bullet, a *Analysis, quota int) (chosen, dropped []storage.Bullet) {
	scored := slices.Clone(bullets)
	slices.SortStableFunc(scored, func(x, y storage.Bullet) int { return compareDesc(scoreBullet(x, a), scoreBullet(y, a)) })
	quota = max(0, min(quota, len(scored)))
	keep := map[string]bool{}
	for _, b := range scored[:quota] {
		keep[b.ID] = true
	}
	for _, b := range bullets {
		if keep[b.ID] {
			chosen = append(chosen, b)
		}
	}
	return chosen, scored[quota:]
}

func bulletIDs(bullets []storage.Bullet) []string {
	ids := []string{}
	for _, b := range bullets {
		ids = append(ids, b.ID)
	}
	return ids
}

// newProjectEntry renders chosen bullets of a project. With scored, each
// bullet carries its job score (so the fitter can trim the weakest).
func newProjectEntry(p storage.Project, chosen []storage.Bullet, a *Analysis, scored bool) ProjectEntry {
	entry := ProjectEntry{ProjectID: p.ID, Name: p.Name, Stack: slices.Clone(p.Stack), Link: p.Link}
	for _, b := range chosen {
		meta := BulletMeta{ID: b.ID}
		if scored {
			score := scoreBullet(b, a)
			meta.Score = &score
		}
		entry.Bullets = append(entry.Bullets, b.Text)
		entry.BulletMeta = append(entry.BulletMeta, meta)
	}
	return entry.normalized()
}
