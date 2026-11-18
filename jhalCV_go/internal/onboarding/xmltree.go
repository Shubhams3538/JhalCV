// Package onboarding runs first-run setup: importing a resume (PDF or
// DOCX), structuring it with AI on request, and validating the final setup.
// It is the Go version of resume/onboarding.py, resume_import.py and
// document_text.py.
package onboarding

import (
	"bytes"
	"encoding/xml"
	"errors"
	"io"
	"strings"

	"jhalcv/internal/usererr"
)

// node is an XML element with ElementTree-style text: Text is the text
// before the first child, Tail the text after the element's end tag.
type node struct {
	Space, Tag string
	Attrs      map[string]string
	Text, Tail string
	Children   []*node
}

func (n *node) attr(name string) string { return n.Attrs[name] }

// parseXML builds a tree, refusing entity declarations and internal DTDs.
// Poppler's harmless external DOCTYPE is allowed (never fetched).
func parseXML(data []byte) (*node, error) {
	data = bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
	upper := strings.ToUpper(string(data))
	if strings.Contains(upper, "<!ENTITY") {
		return nil, usererr.New("Unsupported document XML declarations.")
	}
	if i := strings.Index(upper, "<!DOCTYPE"); i >= 0 {
		rest := upper[i:]
		if end := strings.Index(rest, ">"); end >= 0 && strings.Contains(rest[:end], "[") {
			return nil, usererr.New("Unsupported document XML declarations.")
		}
	}
	dec := xml.NewDecoder(bytes.NewReader(data))
	dec.Strict = true
	var root *node
	var stack []*node
	for {
		tok, err := dec.Token()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		switch t := tok.(type) {
		case xml.StartElement:
			n := &node{Space: t.Name.Space, Tag: t.Name.Local, Attrs: map[string]string{}}
			for _, a := range t.Attr {
				key := a.Name.Local
				if a.Name.Space != "" && a.Name.Space != "xmlns" {
					key = "{" + a.Name.Space + "}" + a.Name.Local
				}
				n.Attrs[key] = a.Value
			}
			if len(stack) > 0 {
				parent := stack[len(stack)-1]
				parent.Children = append(parent.Children, n)
			} else if root == nil {
				root = n
			}
			stack = append(stack, n)
		case xml.EndElement:
			stack = stack[:len(stack)-1]
		case xml.CharData:
			if len(stack) == 0 {
				continue
			}
			parent := stack[len(stack)-1]
			if len(parent.Children) == 0 {
				parent.Text += string(t)
			} else {
				last := parent.Children[len(parent.Children)-1]
				last.Tail += string(t)
			}
		}
	}
	if root == nil {
		return nil, errors.New("empty XML document")
	}
	return root, nil
}

// all lists every descendant with the given local tag, in document order.
func (n *node) all(tag string) []*node {
	var out []*node
	for _, c := range n.Children {
		if c.Tag == tag {
			out = append(out, c)
		}
		out = append(out, c.all(tag)...)
	}
	return out
}

// direct lists the children with the given local tag.
func (n *node) direct(tag string) []*node {
	var out []*node
	for _, c := range n.Children {
		if c.Tag == tag {
			out = append(out, c)
		}
	}
	return out
}

const relNamespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

// linkedText is a node's text with each hyperlink target written beside its
// visible label. relationships maps DOCX hyperlink IDs to URLs (nil for PDF).
func linkedText(n *node, relationships map[string]string) string {
	if n.Tag == "instrText" || n.Tag == "del" {
		return ""
	}
	out := n.Text
	switch n.Tag {
	case "t", "a", "text", "b", "i":
	default:
		if strings.TrimSpace(out) == "" {
			out = ""
		}
	}
	for _, child := range n.Children {
		value := linkedText(child, relationships)
		target := ""
		if child.Tag == "a" {
			target = child.attr("href")
		}
		if child.Tag == "hyperlink" && relationships != nil {
			target = relationships[child.attr("{"+relNamespace+"}id")]
		}
		if strings.HasPrefix(target, "https://") || strings.HasPrefix(target, "http://") {
			if urls := WebLinks("", []string{target}); len(urls) > 0 && !strings.Contains(value, urls[0]) {
				value += " [" + urls[0] + "]"
			}
		}
		switch child.Tag {
		case "tab":
			value += "\t"
		case "br":
			value += "\n"
		}
		if relationships == nil || strings.TrimSpace(child.Tail) != "" {
			value += child.Tail
		}
		out += value
	}
	return out
}
