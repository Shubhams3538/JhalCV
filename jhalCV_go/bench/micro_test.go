// Package bench holds the Go side of the CPU micro benchmarks. They read
// the same fixtures as bench/micro.py:
//
//	python3 ../bench/fixtures.py /tmp/fixtures
//	JHALCV_FIXTURES=/tmp/fixtures go test -run '^$' -bench . -benchmem ./bench/
package bench

import (
	"os"
	"path/filepath"
	"testing"

	"jhalcv/internal/jobs"
	"jhalcv/internal/resume"
	"jhalcv/internal/storage"
)

func fixture(b *testing.B, name string) string {
	b.Helper()
	dir := os.Getenv("JHALCV_FIXTURES")
	if dir == "" {
		b.Skip("set JHALCV_FIXTURES (see bench/fixtures.py)")
	}
	data, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		b.Fatal(err)
	}
	return string(data)
}

func profile(b *testing.B) (string, *storage.Profile) {
	raw := fixture(b, "profile.json")
	p, err := storage.ParseProfile([]byte(raw))
	if err != nil {
		b.Fatal(err)
	}
	return raw, p
}

func BenchmarkHTMLToText(b *testing.B) {
	page := fixture(b, "page.html")
	for b.Loop() {
		jobs.HTMLToText(page)
	}
}

func BenchmarkPageWithLinks(b *testing.B) {
	page := fixture(b, "page.html")
	for b.Loop() {
		jobs.PageWithLinks(page, "https://example.com/careers")
	}
}

func BenchmarkParseProfile(b *testing.B) {
	raw, _ := profile(b)
	for b.Loop() {
		if _, err := storage.ParseProfile([]byte(raw)); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkParseJD(b *testing.B) {
	_, p := profile(b)
	jd := fixture(b, "jd.txt")
	for b.Loop() {
		resume.ParseJD(jd, p)
	}
}

func BenchmarkTailor(b *testing.B) {
	_, p := profile(b)
	jd := fixture(b, "jd.txt")
	for b.Loop() {
		resume.Tailor(p, jd, 2, 4, 3)
	}
}

func BenchmarkRequirementYears(b *testing.B) {
	posting := fixture(b, "posting.txt")
	for b.Loop() {
		jobs.RequirementYears(posting)
	}
}
