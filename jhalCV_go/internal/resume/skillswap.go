package resume

import (
	"cmp"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"jhalcv/internal/storage"
)

// Skill swap fits the job's own skill terms into the existing skill groups.
// The model names terms; this code decides what is allowed: a term must
// appear in the job description, groups are never created, and pinned skills
// and skills the job asks for are never removed. Which removals are needed is
// decided against the real page (FitSkills), after the layout is final.

const (
	maxAddedSkills = 15
	maxTermChars   = 40
)

var skillTerm = regexp.MustCompile(`^[A-Za-z0-9.+#][A-Za-z0-9+#./ -]*$`)

// Terms made only of these describe a person or a job, not a skill.
var genericTokens = setOf(strings.Fields(`
    problem solving skill skills ability abilities experience knowledge understanding
    scalability resilience reliability availability maintainability performance quality
    excellence fundamentals principles practices methodologies patterns architecture
    design development engineering programming coding software systems backend frontend
    fullstack cloud security testing agile strong excellent good solid proven deep broad`))

// One of these anywhere in a term rejects it.
var softTokens = setOf(strings.Fields(`
    communication teamwork collaboration leadership ownership mentorship mentoring
    thinking mindset attitude passion motivation degree bachelors bachelor masters
    master phd years experience`))

func isSkillTerm(term string) bool {
	if !skillTerm.MatchString(term) || len([]rune(term)) > maxTermChars {
		return false
	}
	tokens := strings.Fields(strings.ToLower(term))
	if len(tokens) < 1 || len(tokens) > 3 {
		return false
	}
	allGeneric := true
	for _, t := range tokens {
		if softTokens[t] {
			return false
		}
		if !genericTokens[t] {
			allGeneric = false
		}
	}
	return !allGeneric
}

// SkillAddition is a job term to add to one of the user's groups.
type SkillAddition struct {
	Term       string `json:"term"`
	Importance string `json:"importance"`
	Group      string `json:"group"`
}

// SkillRename shows a saved skill with the job's spelling too.
type SkillRename struct {
	Group string `json:"group"`
	From  string `json:"from"`
	To    string `json:"to"`
}

// SkillPlan describes every skill change for one job; nothing is applied yet.
type SkillPlan struct {
	Pinned    []string        `json:"pinned"`
	Keep      []string        `json:"keep"`
	Keywords  []Keyword       `json:"keywords"`
	Additions []SkillAddition `json:"additions"`
	Renames   []SkillRename   `json:"renames"`
}

type jobTerm struct {
	term, importance string
	group            *string
}

// jobTerms keeps valid terms from the model's list, required first, one per
// skill.
func jobTerms(raw any, jd string, groups []string) []jobTerm {
	blob := " " + strings.ToLower(jd) + " "
	found := map[string]*jobTerm{}
	var order []string
	list, _ := raw.([]any)
	for _, item := range list {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		raw, ok := entry["term"].(string)
		if !ok {
			continue
		}
		term := strings.Join(strings.Fields(raw), " ")
		importance := "preferred"
		if entry["importance"] == "required" {
			importance = "required"
		}
		if !isSkillTerm(term) || !termPresent(term, blob) {
			continue
		}
		key := Canonical(term)
		// Matching ignores case, so a lowercase spelling can read properly.
		if term == strings.ToLower(term) && term != strings.ToUpper(term) {
			if d, ok := displayNames[key]; ok {
				term = d
			} else {
				term = strings.ToUpper(term[:1]) + term[1:]
			}
		}
		known := found[key]
		if known == nil || (importance == "required" && known.importance != "required") {
			var group *string
			if g, ok := entry["group"].(string); ok && slices.Contains(groups, g) {
				group = &g
			}
			if known == nil {
				order = append(order, key)
			}
			found[key] = &jobTerm{term, importance, group}
		}
	}
	terms := make([]jobTerm, 0, len(order))
	for _, key := range order {
		terms = append(terms, *found[key])
	}
	slices.SortStableFunc(terms, func(a, b jobTerm) int {
		return cmp.Compare(boolInt(a.importance != "required"), boolInt(b.importance != "required"))
	})
	return terms
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func pinnedSkills(p *storage.Profile) []string {
	seen := map[string]bool{}
	for _, skill := range p.Locked.PinnedSkills {
		seen[Canonical(skill)] = true
	}
	return sortedKeys(seen)
}

func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

// PlanSwaps describes every skill change for this job.
func PlanSwaps(p *storage.Profile, skills []SkillRow, rawTerms any, jd string, matched []string) *SkillPlan {
	groups := make([]string, 0, len(skills))
	for _, g := range skills {
		groups = append(groups, g.Label)
	}
	terms := jobTerms(rawTerms, jd, groups)
	keys := map[string]bool{}
	for _, t := range terms {
		keys[Canonical(t.term)] = true
	}
	location := map[string]string{}
	spelling := map[string]string{}
	for _, g := range skills {
		for _, skill := range g.Items {
			key := Canonical(skill)
			location[key] = g.Label
			if _, ok := spelling[key]; !ok {
				spelling[key] = skill
			}
		}
	}
	blob := " " + strings.ToLower(jd) + " "
	plan := &SkillPlan{Pinned: pinnedSkills(p), Keywords: []Keyword{}, Additions: []SkillAddition{}, Renames: []SkillRename{}}
	for _, t := range terms {
		key := Canonical(t.term)
		if group, ok := location[key]; ok {
			current := spelling[key]
			// Both spellings keep an exact-match search working for either one.
			if strings.ToLower(current) != strings.ToLower(t.term) && !termPresent(current, blob) {
				plan.Renames = append(plan.Renames, SkillRename{group, current, fmt.Sprintf("%s (%s)", current, t.term)})
			}
		} else if t.group != nil && len(plan.Additions) < maxAddedSkills {
			plan.Additions = append(plan.Additions, SkillAddition{t.term, t.importance, *t.group})
		}
		plan.Keywords = append(plan.Keywords, Keyword{t.term, t.importance})
	}
	keep := maps(keys)
	for _, skill := range matched {
		keep[Canonical(skill)] = true
	}
	plan.Keep = sortedKeys(keep)
	return plan
}

func maps(m map[string]bool) map[string]bool {
	out := make(map[string]bool, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// SkillRemoval is one skill to take off the page to make room.
type SkillRemoval struct {
	Group string `json:"group"`
	Skill string `json:"skill"`
}

// expand returns every skill at once, plus removals in the order space
// should be freed: unmentioned skills first (from the longest line), then
// preferred additions, then required ones. Protected skills are never listed.
func expand(skills []SkillRow, plan *SkillPlan) ([]SkillRow, []SkillRemoval) {
	protected := setOf(append(slices.Clone(plan.Pinned), plan.Keep...))
	renames := map[[2]string]string{}
	for _, r := range plan.Renames {
		renames[[2]string{r.Group, r.From}] = r.To
	}
	var full []SkillRow
	fillers := map[string][]string{}
	var labels []string
	for _, g := range skills {
		var kept, spare []string
		for _, skill := range g.Items {
			if protected[Canonical(skill)] {
				kept = append(kept, skill)
			} else {
				spare = append(spare, skill)
			}
		}
		items := []string{}
		for _, skill := range kept {
			if to, ok := renames[[2]string{g.Label, skill}]; ok {
				skill = to
			}
			items = append(items, skill)
		}
		for _, a := range plan.Additions {
			if a.Group == g.Label {
				items = append(items, a.Term)
			}
		}
		items = append(items, spare...)
		row := g
		row.Items = items
		full = append(full, row)
		fillers[g.Label] = spare
		labels = append(labels, g.Label)
	}

	lengths, remaining := map[string]int{}, map[string]int{}
	for _, g := range full {
		for _, item := range g.Items {
			lengths[g.Label] += len([]rune(item)) + 2
		}
	}
	for _, g := range skills {
		remaining[g.Label] = len(g.Items)
	}
	var removals []SkillRemoval
	// The longest line most likely sheds a wrapped row. A group keeps one
	// saved item, so losing an addition can never leave it empty.
	for {
		best := ""
		for _, label := range labels {
			if len(fillers[label]) > 0 && remaining[label] > 1 && (best == "" || lengths[label] > lengths[best]) {
				best = label
			}
		}
		if best == "" {
			break
		}
		spare := fillers[best]
		skill := spare[len(spare)-1]
		fillers[best] = spare[:len(spare)-1]
		lengths[best] -= len([]rune(skill)) + 2
		remaining[best]--
		removals = append(removals, SkillRemoval{best, skill})
	}
	for i := len(plan.Additions) - 1; i >= 0; i-- {
		removals = append(removals, SkillRemoval{plan.Additions[i].Group, plan.Additions[i].Term})
	}
	return full, removals
}

// without removes skills, dropping any group that ends up empty.
func without(skills []SkillRow, removals []SkillRemoval) []SkillRow {
	gone := map[SkillRemoval]bool{}
	for _, r := range removals {
		gone[r] = true
	}
	result := []SkillRow{}
	for _, g := range skills {
		var items []string
		for _, skill := range g.Items {
			if !gone[SkillRemoval{g.Label, skill}] {
				items = append(items, skill)
			}
		}
		if len(items) > 0 {
			row := g
			row.Items = items
			result = append(result, row)
		}
	}
	return result
}

func resumeText(data *Data, skills []SkillRow) string {
	var parts []string
	for _, g := range skills {
		parts = append(parts, g.Items...)
	}
	for _, e := range data.Experience {
		for _, b := range e.Bullets {
			parts = append(parts, strings.ReplaceAll(b, "**", ""))
		}
	}
	for _, p := range data.Projects {
		parts = append(parts, p.Stack...)
		for _, b := range p.Bullets {
			parts = append(parts, strings.ReplaceAll(b, "**", ""))
		}
	}
	return " " + strings.ToLower(strings.Join(parts, " \n ")) + " "
}

// Coverage counts how many of the job's terms of one importance the resume shows.
type Coverage struct {
	Covered int      `json:"covered"`
	Total   int      `json:"total"`
	Missing []string `json:"missing"`
}

func coverage(plan *SkillPlan, data *Data, skills []SkillRow) map[string]Coverage {
	blob := resumeText(data, skills)
	listed := map[string]bool{}
	for _, g := range skills {
		for _, s := range g.Items {
			listed[Canonical(s)] = true
		}
	}
	out := map[string]Coverage{}
	for _, importance := range []string{"required", "preferred"} {
		c := Coverage{Missing: []string{}}
		for _, k := range plan.Keywords {
			if k.Importance != importance {
				continue
			}
			c.Total++
			if !listed[Canonical(k.Term)] && !termPresent(k.Term, blob) {
				c.Missing = append(c.Missing, k.Term)
			}
		}
		c.Covered = c.Total - len(c.Missing)
		out[importance] = c
	}
	return out
}

// SwapSummary is what changed on the page and how much of the job it covers.
type SwapSummary struct {
	Added          []string            `json:"added"`
	Removed        []string            `json:"removed"`
	Renamed        []map[string]string `json:"renamed"`
	CoverageBefore map[string]Coverage `json:"coverage_before"`
	CoverageAfter  map[string]Coverage `json:"coverage_after"`
}

func summarizeSwap(plan *SkillPlan, data *Data, before, after []SkillRow) *SwapSummary {
	pairs := func(rows []SkillRow) map[[2]string]bool {
		m := map[[2]string]bool{}
		for _, g := range rows {
			for _, s := range g.Items {
				m[[2]string{g.Label, s}] = true
			}
		}
		return m
	}
	was, now := pairs(before), pairs(after)
	renamed := map[[2]string]string{} // (group, new spelling) -> old spelling
	for _, r := range plan.Renames {
		renamed[[2]string{r.Group, r.To}] = r.From
	}
	out := &SwapSummary{Added: []string{}, Removed: []string{}, Renamed: []map[string]string{},
		CoverageBefore: coverage(plan, data, before), CoverageAfter: coverage(plan, data, after)}
	kept := map[[2]string]bool{}
	for _, r := range plan.Renames { // In plan order, like Python's dict.
		key := [2]string{r.Group, r.To}
		if now[key] {
			out.Renamed = append(out.Renamed, map[string]string{"from": r.From, "to": r.To})
			kept[[2]string{r.Group, r.From}] = true
		}
	}
	var added, removed [][2]string
	for k := range now {
		if _, isRename := renamed[k]; !was[k] && !isRename {
			added = append(added, k)
		}
	}
	for k := range was {
		if !now[k] && !kept[k] {
			removed = append(removed, k)
		}
	}
	sortPairs := func(p [][2]string) {
		slices.SortFunc(p, func(a, b [2]string) int {
			return cmp.Or(strings.Compare(a[0], b[0]), strings.Compare(a[1], b[1]))
		})
	}
	sortPairs(added)
	sortPairs(removed)
	for _, k := range added {
		out.Added = append(out.Added, k[1])
	}
	for _, k := range removed {
		out.Removed = append(out.Removed, k[1])
	}
	return out
}

func swapNotice(s *SwapSummary) []string {
	var lines []string
	if len(s.Added) > 0 {
		lines = append(lines, "Skills added from the job: "+strings.Join(s.Added, ", ")+".")
	}
	if len(s.Removed) > 0 {
		lines = append(lines, "Skills removed to make room: "+strings.Join(s.Removed, ", ")+".")
	}
	if len(s.Renamed) > 0 {
		var to []string
		for _, r := range s.Renamed {
			to = append(to, r["to"])
		}
		lines = append(lines, "Skills respelled to match the job: "+strings.Join(to, ", ")+".")
	}
	for _, imp := range [][2]string{{"required", "Required"}, {"preferred", "Preferred"}} {
		after := s.CoverageAfter[imp[0]]
		if after.Total == 0 {
			continue
		}
		line := fmt.Sprintf("%s job terms on the resume: %d to %d of %d.", imp[1],
			s.CoverageBefore[imp[0]].Covered, after.Covered, after.Total)
		if len(after.Missing) > 0 {
			line += " Still missing: " + strings.Join(after.Missing, ", ") + "."
		}
		lines = append(lines, line)
	}
	return lines
}
