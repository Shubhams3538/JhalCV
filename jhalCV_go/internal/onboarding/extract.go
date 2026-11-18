package onboarding

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

// MaxUpload is the largest resume file accepted.
const MaxUpload = 1_000_000

// Extracted is an imported resume before the user confirms anything.
type Extracted struct {
	Text    string           `json:"text"`
	Profile *storage.Profile `json:"profile"`
	Review  Review           `json:"review"`
	Links   []string         `json:"links"`
	Notice  string           `json:"notice"`
}

func lookPath(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

// Extract reads an uploaded PDF or DOCX (base64) into text and a suggested
// profile. Uploads stay in a temporary folder and are deleted afterwards.
func Extract(ctx context.Context, body map[string]any) (*Extracted, error) {
	name, err := storage.Text(body["name"], "Filename", 200, true)
	if err != nil {
		return nil, err
	}
	encoded, ok := body["data"].(string)
	if !ok || len(encoded) > MaxUpload*4/3+8 {
		return nil, usererr.New("Choose a PDF or DOCX up to 1 MB.")
	}
	raw, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, usererr.New("The upload is not valid base64.")
	}
	if len(raw) == 0 || len(raw) > MaxUpload {
		return nil, usererr.New("Choose a PDF or DOCX up to 1 MB.")
	}
	var content string
	var hyperlinks []string
	switch strings.ToLower(filepath.Ext(name)) {
	case ".pdf":
		content, hyperlinks, err = pdfText(ctx, raw)
	case ".docx":
		content, hyperlinks, err = docxContent(raw)
	default:
		err = usererr.New("Choose a PDF or DOCX file.")
	}
	if err != nil {
		return nil, err
	}
	content = strings.TrimSpace(strings.Map(func(r rune) rune {
		if r < 32 && r != '\n' && r != '\r' && r != '\t' {
			return '\n'
		}
		return r
	}, content))
	if n := utf8.RuneCountInString(content); n > 30_000 {
		return nil, usererr.New("Import a resume containing at most 30,000 characters.")
	} else if n < 30 {
		return nil, usererr.New("No readable resume text found. For scanned PDFs, use a text-based PDF, DOCX, or manual entry.")
	}
	profile := SuggestedProfile(content, hyperlinks)
	if len(hyperlinks) > 0 {
		content = firstRunes(content+"\n\nResume hyperlink targets:\n"+strings.Join(hyperlinks, "\n"), 30000)
	}
	links := orEmpty(WebLinks(content, hyperlinks))
	return &Extracted{Text: content, Profile: profile, Review: ReviewSummary(profile), Links: links,
		Notice: fmt.Sprintf("Found %d web link(s). Review their targets below. Local section grouping is provisional; choose AI to structure the linked document text, then confirm each field. PDF import reads the first 10 pages.", len(links))}, nil
}

func pdfText(ctx context.Context, raw []byte) (string, []string, error) {
	if !bytes.HasPrefix(raw, []byte("%PDF-")) {
		return "", nil, usererr.New("This is not a PDF file.")
	}
	if !lookPath("pdftotext") {
		return "", nil, usererr.New("Install Poppler (pdftotext) to import PDFs, or enter details manually.")
	}
	dir, err := os.MkdirTemp("", "jhalcv-import-")
	if err != nil {
		return "", nil, err
	}
	defer os.RemoveAll(dir)
	source, out := filepath.Join(dir, "resume.pdf"), filepath.Join(dir, "resume.txt")
	if err := os.WriteFile(source, raw, 0o600); err != nil {
		return "", nil, err
	}
	tctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	err = exec.CommandContext(tctx, "pdftotext", "-f", "1", "-l", "10", "-layout", source, out).Run()
	cancel()
	if tctx.Err() != nil {
		return "", nil, usererr.New("PDF extraction timed out. Use a smaller file or enter details manually.")
	}
	info, statErr := os.Stat(out)
	if err != nil || statErr != nil {
		return "", nil, usererr.New("Could not read this PDF. Unlock it or use DOCX/manual entry.")
	}
	if info.Size() > 120_000 {
		return "", nil, usererr.New("Resume text is too large. Import a shorter document.")
	}
	data, err := os.ReadFile(out)
	if err != nil {
		return "", nil, err
	}
	content := strings.ToValidUTF8(string(data), "�")

	if lookPath("pdftohtml") {
		// Plain text loses the label-to-URL link; Poppler's XML keeps it.
		xctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		xmlOut, err := exec.CommandContext(xctx, "pdftohtml", "-q", "-xml", "-i", "-stdout", "-f", "1", "-l", "10", source).Output()
		cancel()
		if err == nil && len(xmlOut) <= 2_000_000 {
			if linked, err := pdfXMLText(xmlOut); err == nil && utf8.RuneCountInString(linked) >= 30 {
				content = linked
			}
		}
	}
	var hyperlinks []string
	if lookPath("pdfinfo") {
		// Printed labels often hide the real LinkedIn or GitHub URLs.
		ictx, cancel := context.WithTimeout(ctx, 10*time.Second)
		info, err := exec.CommandContext(ictx, "pdfinfo", "-f", "1", "-l", "10", "-url", source).Output()
		cancel()
		if err == nil {
			hyperlinks = WebLinks(firstRunes(string(info), 120000), nil)
		}
	}
	return content, hyperlinks, nil
}

var bulletOnly = regexp.MustCompile(`^[•●▪◦*-]$`)

// pdfXMLText uses page coordinates to keep each linked label on its row.
func pdfXMLText(data []byte) (string, error) {
	tree, err := parseXML(data)
	if err != nil {
		return "", err
	}
	num := func(n *node, key string) float64 {
		f, _ := strconv.ParseFloat(n.attr(key), 64)
		return f
	}
	var pages []string
	for _, page := range tree.direct("page") {
		nodes := page.direct("text")
		left := 0.0
		for i, n := range nodes {
			if i == 0 || num(n, "left") < left {
				left = num(n, "left")
			}
		}
		sorted := slices.Clone(nodes)
		sort.SliceStable(sorted, func(i, j int) bool {
			ti, tj := num(sorted[i], "top"), num(sorted[j], "top")
			if ti != tj {
				return ti < tj
			}
			return num(sorted[i], "left") < num(sorted[j], "left")
		})
		type part struct {
			left float64
			text string
		}
		type row struct {
			top   float64
			parts []part
		}
		var rows []row
		for _, n := range sorted {
			top := num(n, "top")
			if len(rows) == 0 || math.Abs(rows[len(rows)-1].top-top) > 3 {
				rows = append(rows, row{top: top})
			}
			rows[len(rows)-1].parts = append(rows[len(rows)-1].parts, part{num(n, "left"), linkedText(n, nil)})
		}
		var out []string
		for _, r := range rows {
			sort.SliceStable(r.parts, func(i, j int) bool {
				if r.parts[i].left != r.parts[j].left {
					return r.parts[i].left < r.parts[j].left
				}
				return r.parts[i].text < r.parts[j].text
			})
			// Keep the relative margin: the local parser uses it to tell
			// wrapped bullet text from a new employer or project.
			indent := strings.Repeat(" ", min(1000, max(0, int(math.RoundToEven((r.parts[0].left-left)/2)))))
			var values []string
			for _, p := range r.parts {
				values = append(values, p.text)
			}
			line := strings.Join(values, "    ")
			if len(values) > 1 && bulletOnly.MatchString(strings.TrimSpace(values[0])) {
				line = values[0] + " " + strings.Join(values[1:], "    ")
			}
			out = append(out, indent+strings.TrimRight(line, " \t\n"))
		}
		pages = append(pages, strings.Join(out, "\n"))
	}
	return strings.TrimSpace(strings.Join(pages, "\n\n")), nil
}

var hyperlinkField = regexp.MustCompile(`(?i)\bHYPERLINK\s+"([^"\n]+)"`)

func docxContent(raw []byte) (string, []string, error) {
	bad := usererr.New("Could not read this DOCX. Use a standard, unlocked DOCX file.")
	archive, err := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if err != nil {
		return "", nil, bad
	}
	var total uint64
	files := map[string]*zip.File{}
	for _, f := range archive.File {
		total += f.UncompressedSize64
		files[f.Name] = f
	}
	if len(archive.File) > 1000 || total > 10_000_000 {
		return "", nil, usererr.New("DOCX archive is too large when expanded.")
	}
	read := func(name string, limit uint64) ([]byte, error) {
		f := files[name]
		if f == nil {
			return nil, bad
		}
		if f.UncompressedSize64 > limit {
			return nil, usererr.New("DOCX text is too large.")
		}
		r, err := f.Open()
		if err != nil {
			return nil, bad
		}
		defer r.Close()
		return io.ReadAll(io.LimitReader(r, int64(limit)+1))
	}
	doc, err := read("word/document.xml", 1_000_000)
	if err != nil {
		return "", nil, err
	}
	if !utf8.Valid(bytes.TrimPrefix(doc, []byte("\xef\xbb\xbf"))) {
		return "", nil, usererr.New("Use a standard UTF-8 DOCX or enter details manually.")
	}
	if upper := strings.ToUpper(string(doc)); strings.Contains(upper, "<!DOCTYPE") || strings.Contains(upper, "<!ENTITY") {
		return "", nil, usererr.New("Unsupported DOCX XML declarations.")
	}
	tree, err := parseXML(doc)
	if err != nil {
		return "", nil, bad
	}
	relationships := map[string]string{}
	var hyperlinks []string
	if f := files["word/_rels/document.xml.rels"]; f != nil && f.UncompressedSize64 <= 200000 {
		rels, err := read("word/_rels/document.xml.rels", 200000)
		if err != nil {
			return "", nil, err
		}
		if upper := strings.ToUpper(string(rels)); strings.Contains(upper, "<!DOCTYPE") || strings.Contains(upper, "<!ENTITY") {
			return "", nil, usererr.New("Unsupported DOCX XML declarations.")
		}
		relTree, err := parseXML(rels)
		if err != nil {
			return "", nil, bad
		}
		var targets []string
		for _, n := range relTree.Children {
			target := n.attr("Target")
			if strings.HasSuffix(n.attr("Type"), "/hyperlink") && n.attr("TargetMode") == "External" &&
				(strings.HasPrefix(target, "https://") || strings.HasPrefix(target, "http://")) {
				relationships[n.attr("Id")] = target
				targets = append(targets, target)
			}
		}
		hyperlinks = WebLinks("", targets)
	}
	var paragraphs []string
	for _, p := range tree.all("p") {
		value := linkedText(p, relationships)
		var instructions []string
		for _, n := range p.all("instrText") {
			instructions = append(instructions, n.Text)
		}
		for _, n := range p.all("fldSimple") {
			instructions = append(instructions, n.attr("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}instr"))
		}
		for _, m := range hyperlinkField.FindAllStringSubmatch(strings.Join(instructions, " "), -1) {
			if strings.HasPrefix(m[1], "http://") || strings.HasPrefix(m[1], "https://") {
				for _, u := range WebLinks("", []string{m[1]}) {
					if !strings.Contains(value, u) {
						value += " [" + u + "]"
					}
				}
			}
		}
		paragraphs = append(paragraphs, value)
	}
	return strings.Join(paragraphs, "\n"), hyperlinks, nil
}
