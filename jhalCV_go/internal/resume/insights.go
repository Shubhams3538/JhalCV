package resume

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

const (
	maxInsightJobs = 500
	insightTop     = 10
)

// canonicalNames are spellings that mean the same skill.
var canonicalNames = map[string]string{
	"k8s": "kubernetes", "k8": "kubernetes",
	"postgres": "postgresql", "postgre": "postgresql", "psql": "postgresql",
	"golang": "go", "cpp": "c++", "c plus plus": "c++",
	"node": "node.js", "nodejs": "node.js", "node js": "node.js",
	"js": "javascript", "ts": "typescript", "py": "python",
	"reactjs": "react", "react.js": "react", "react js": "react",
	"nextjs": "next.js", "next": "next.js", "vuejs": "vue", "vue.js": "vue",
	"expressjs": "express", "express.js": "express",
	"mongo": "mongodb", "sklearn": "scikit-learn", "tf": "tensorflow",
	"dotnet": ".net", "amazon web services": "aws", "google cloud": "gcp",
	"restful": "rest", "rest api": "rest", "rest apis": "rest", "restful apis": "rest",
	"ci cd": "ci/cd", "cicd": "ci/cd", "ml": "machine learning",
	"llm": "llms", "gen ai": "genai", "generative ai": "genai",
}

// displayNames show a canonical skill when the profile has no spelling of it.
var displayNames = map[string]string{
	"c++": "C++", "c#": "C#", "go": "Go", "python": "Python", "java": "Java", "rust": "Rust",
	"javascript": "JavaScript", "typescript": "TypeScript", "node.js": "Node.js",
	"react": "React", "next.js": "Next.js", "vue": "Vue", "kubernetes": "Kubernetes",
	"postgresql": "PostgreSQL", "mongodb": "MongoDB", "mysql": "MySQL", "sqlite": "SQLite",
	"graphql": "GraphQL", "grpc": "gRPC", "rest": "REST", "fastapi": "FastAPI",
	"github": "GitHub", "gitlab": "GitLab", "openai": "OpenAI", "llms": "LLMs",
	"genai": "GenAI", "pytorch": "PyTorch", "tensorflow": "TensorFlow", "numpy": "NumPy",
	"ci/cd": "CI/CD", ".net": ".NET", "dynamodb": "DynamoDB", "langchain": "LangChain",
	"langgraph": "LangGraph", "llamaindex": "LlamaIndex", "huggingface": "Hugging Face",
	"rabbitmq": "RabbitMQ", "elasticsearch": "Elasticsearch", "opentelemetry": "OpenTelemetry",
	"machine learning": "Machine learning", "scikit-learn": "scikit-learn", "pgvector": "pgvector",
}

var spaces = regexp.MustCompile(`\s+`)

// Canonical turns any spelling of a skill into one key, so "C++", "c++" and
// "cpp" count as one skill.
func Canonical(skill string) string {
	key := strings.Trim(spaces.ReplaceAllString(strings.ToLower(strings.TrimSpace(skill)), " "), " ,;:")
	_, known := displayNames[key]
	_, alias := canonicalNames[key]
	if !known && !alias {
		// A hyphen or underscore between two word characters is a space.
		runes := []rune(key)
		for i := 1; i < len(runes)-1; i++ {
			if (runes[i] == '-' || runes[i] == '_') && isWordRune(runes[i-1]) && isWordRune(runes[i+1]) {
				runes[i] = ' '
			}
		}
		key = string(runes)
	}
	if c, ok := canonicalNames[key]; ok {
		return c
	}
	return key
}

func displayName(key, seen string) string {
	if d, ok := displayNames[key]; ok {
		return d
	}
	if seen != "" && seen != strings.ToLower(seen) {
		return seen
	}
	if utf8Len(key) <= 3 {
		return strings.ToUpper(key)
	}
	r := []rune(key)
	return strings.ToUpper(string(r[:1])) + string(r[1:])
}

func utf8Len(s string) int { return len([]rune(s)) }

// profileSkills maps every skill the profile claims anywhere to its spelling.
func profileSkills(p *storage.Profile) map[string]string {
	var names []string
	for _, g := range p.Skills {
		if !g.IsNote() {
			names = append(names, g.Items...)
		}
	}
	for _, e := range p.Experience {
		names = append(names, e.Stack...)
	}
	for _, pr := range p.Projects {
		names = append(names, pr.Stack...)
	}
	owned := map[string]string{}
	for _, name := range names {
		key := Canonical(name)
		if _, ok := owned[key]; !ok {
			owned[key] = name
		}
	}
	return owned
}

func jobID(jd string) string {
	sum := sha256.Sum256([]byte(spaces.ReplaceAllString(strings.ToLower(strings.TrimSpace(jd)), " ")))
	return hex.EncodeToString(sum[:])
}

// Insights tracks which skills the user's job descriptions ask for, in
// .local/insights.json. Only skill names are kept, never job text.
type Insights struct {
	path string
	mu   sync.Mutex
}

// NewInsights opens the insights for root.
func NewInsights(root string) *Insights {
	return &Insights{path: filepath.Join(root, ".local", "insights.json")}
}

type insightJob struct {
	ID    string            `json:"id"`
	At    int64             `json:"at"`
	Asked orderedStringsMap `json:"asked"`
}

type insightFile struct {
	Jobs   []insightJob `json:"jobs"`
	Hidden []string     `json:"hidden"`
}

func (in *Insights) load() (*insightFile, error) {
	data, err := os.ReadFile(in.path)
	if errors.Is(err, fs.ErrNotExist) {
		return &insightFile{Jobs: []insightJob{}, Hidden: []string{}}, nil
	}
	if err != nil {
		return nil, err
	}
	var f insightFile
	if err := json.Unmarshal(data, &f); err != nil {
		return nil, usererr.Errorf("Could not read %s: %v", in.path, err)
	}
	if f.Jobs == nil {
		f.Jobs = []insightJob{}
	}
	if f.Hidden == nil {
		f.Hidden = []string{}
	}
	return &f, nil
}

// Record stores what one job asked for. The same job counts once; the latest
// analysis replaces the earlier one.
func (in *Insights) Record(jd string, skills []string) error {
	in.mu.Lock()
	defer in.mu.Unlock()
	asked := orderedStringsMap{}
	for _, skill := range skills {
		key := Canonical(skill)
		if key != "" && utf8Len(key) <= 60 && !asked.has(key) {
			asked.set(key, displayName(key, strings.TrimSpace(skill)))
		}
	}
	data, err := in.load()
	if err != nil {
		return err
	}
	id := jobID(jd)
	data.Jobs = slices.DeleteFunc(data.Jobs, func(j insightJob) bool { return j.ID == id })
	data.Jobs = append(data.Jobs, insightJob{ID: id, At: time.Now().Unix(), Asked: asked})
	if len(data.Jobs) > maxInsightJobs {
		data.Jobs = data.Jobs[len(data.Jobs)-maxInsightJobs:]
	}
	return storage.WriteJSON(in.path, data)
}

// Hide stops one skill from being suggested.
func (in *Insights) Hide(skill string) error {
	in.mu.Lock()
	defer in.mu.Unlock()
	data, err := in.load()
	if err != nil {
		return err
	}
	key := Canonical(skill)
	if key != "" && !slices.Contains(data.Hidden, key) {
		data.Hidden = append(data.Hidden, key)
		return storage.WriteJSON(in.path, data)
	}
	return nil
}

// UnhideAll shows every hidden skill again.
func (in *Insights) UnhideAll() error {
	in.mu.Lock()
	defer in.mu.Unlock()
	data, err := in.load()
	if err != nil {
		return err
	}
	data.Hidden = []string{}
	return storage.WriteJSON(in.path, data)
}

// Reset forgets every recorded job.
func (in *Insights) Reset() error {
	in.mu.Lock()
	defer in.mu.Unlock()
	return storage.WriteJSON(in.path, &insightFile{Jobs: []insightJob{}, Hidden: []string{}})
}

// InsightRow is one skill in the Insights view.
type InsightRow struct {
	Key     string `json:"key"`
	Skill   string `json:"skill"`
	Count   int    `json:"count"`
	Percent int    `json:"percent"`
	Owned   bool   `json:"owned"`
}

// InsightSummary is the Insights view.
type InsightSummary struct {
	Jobs    int          `json:"jobs"`
	Hidden  int          `json:"hidden"`
	Missing []InsightRow `json:"missing"`
	Asked   []InsightRow `json:"asked"`
}

// Summary ranks the most requested skills, and those the profile lacks.
// Whether a skill is missing is decided now, against the current profile.
func (in *Insights) Summary(p *storage.Profile) (*InsightSummary, error) {
	in.mu.Lock()
	data, err := in.load()
	in.mu.Unlock()
	if err != nil {
		return nil, err
	}
	owned := profileSkills(p)
	counts, names := map[string]int{}, map[string]string{}
	var order []string
	for _, job := range data.Jobs {
		for _, kv := range job.Asked {
			if _, ok := counts[kv.key]; !ok {
				order = append(order, kv.key)
				names[kv.key] = kv.value
			}
			counts[kv.key]++
		}
	}
	total := len(data.Jobs)
	row := func(key string) InsightRow {
		skill := owned[key]
		if skill == "" {
			skill = names[key]
		}
		percent := 0
		if total > 0 {
			percent = int(math.RoundToEven(100 * float64(counts[key]) / float64(total)))
		}
		_, isOwned := owned[key]
		return InsightRow{key, skill, counts[key], percent, isOwned}
	}
	slices.SortStableFunc(order, func(a, b string) int {
		if counts[a] != counts[b] {
			return counts[b] - counts[a]
		}
		return strings.Compare(strings.ToLower(names[a]), strings.ToLower(names[b]))
	})
	out := &InsightSummary{Jobs: total, Hidden: len(data.Hidden), Missing: []InsightRow{}, Asked: []InsightRow{}}
	for _, key := range order {
		if _, isOwned := owned[key]; !isOwned && !slices.Contains(data.Hidden, key) && len(out.Missing) < insightTop {
			out.Missing = append(out.Missing, row(key))
		}
		if len(out.Asked) < insightTop {
			out.Asked = append(out.Asked, row(key))
		}
	}
	return out, nil
}

// AddSkill adds a suggested skill to one of the profile's skill groups.
func AddSkill(p *storage.Profile, skillValue, groupValue any) error {
	skill, _ := skillValue.(string)
	name := strings.TrimSpace(skill)
	if name == "" || utf8Len(name) > 100 {
		return usererr.New("Choose a skill to add.")
	}
	group, _ := groupValue.(string)
	index := -1
	for i, g := range p.Skills {
		if !g.IsNote() && g.Name == group {
			index = i
		}
	}
	if index < 0 {
		return usererr.New("Choose one of your skill groups.")
	}
	key := Canonical(name)
	for _, g := range p.Skills {
		if g.IsNote() {
			continue
		}
		for _, item := range g.Items {
			if Canonical(item) == key {
				return usererr.Errorf("%s is already in your %s skills.", name, g.Name)
			}
		}
	}
	p.Skills[index].Items = append(p.Skills[index].Items, displayName(key, name))
	return nil
}

// orderedStringsMap is a JSON object of strings that keeps its key order.
type orderedStringsMap []struct{ key, value string }

func (m orderedStringsMap) has(key string) bool {
	return slices.ContainsFunc(m, func(kv struct{ key, value string }) bool { return kv.key == key })
}

func (m *orderedStringsMap) set(key, value string) {
	*m = append(*m, struct{ key, value string }{key, value})
}

func (m orderedStringsMap) MarshalJSON() ([]byte, error) {
	var b strings.Builder
	b.WriteByte('{')
	for i, kv := range m {
		if i > 0 {
			b.WriteByte(',')
		}
		k, _ := json.Marshal(kv.key)
		v, _ := json.Marshal(kv.value)
		b.Write(k)
		b.WriteByte(':')
		b.Write(v)
	}
	b.WriteByte('}')
	return []byte(b.String()), nil
}

func (m *orderedStringsMap) UnmarshalJSON(data []byte) error {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return errors.New("expected an object")
	}
	out := orderedStringsMap{}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		var value string
		if err := dec.Decode(&value); err != nil {
			return err
		}
		out.set(tok.(string), value)
	}
	*m = out
	return nil
}
