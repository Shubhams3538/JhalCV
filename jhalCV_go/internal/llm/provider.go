package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"jhalcv/internal/usage"
)

// noRedirects never follows a redirect: a redirect could carry the request,
// and its key header, to another host.
var noRedirects = &http.Client{
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// requestShape builds each provider's request. Secrets travel only in headers.
func requestShape(r Route, req Request) (string, http.Header, map[string]any, error) {
	schema := Strictify(req.Schema)
	headers := http.Header{"Content-Type": {"application/json"}}
	var endpoint string
	var body map[string]any

	switch r.Provider {
	case "openai", "openrouter":
		endpoint = "https://api.openai.com/v1/chat/completions"
		if r.Provider == "openrouter" {
			endpoint = "https://openrouter.ai/api/v1/chat/completions"
		}
		headers.Set("Authorization", "Bearer "+r.Key)
		messages := []any{}
		if req.System != "" {
			messages = append(messages, map[string]any{"role": "system", "content": req.System})
		}
		messages = append(messages, map[string]any{"role": "user", "content": req.Prompt})
		body = map[string]any{"model": r.Model, "messages": messages, "temperature": req.Temperature,
			"response_format": map[string]any{"type": "json_schema", "json_schema": map[string]any{
				"name": req.SchemaName, "strict": true, "schema": schema}}}
		if r.Provider == "openrouter" {
			body["provider"] = map[string]any{"require_parameters": true}
		}
	case "gemini":
		endpoint = "https://generativelanguage.googleapis.com/v1beta/models/" + url.PathEscape(r.Model) + ":generateContent"
		headers.Set("x-goog-api-key", r.Key)
		body = map[string]any{
			"contents": []any{map[string]any{"role": "user", "parts": []any{map[string]any{"text": req.Prompt}}}},
			"generationConfig": map[string]any{"temperature": req.Temperature,
				"responseMimeType": "application/json", "responseJsonSchema": schema}}
		if req.System != "" {
			body["systemInstruction"] = map[string]any{"parts": []any{map[string]any{"text": req.System}}}
		}
	case "anthropic":
		endpoint = "https://api.anthropic.com/v1/messages"
		headers.Set("x-api-key", r.Key)
		headers.Set("anthropic-version", "2023-06-01")
		body = map[string]any{"model": r.Model, "max_tokens": 8192,
			"messages":      []any{map[string]any{"role": "user", "content": req.Prompt}},
			"output_config": map[string]any{"format": map[string]any{"type": "json_schema", "schema": schema}}}
		if req.System != "" {
			body["system"] = req.System
		}
	case "zai":
		// Anthropic-compatible Coding Plan endpoint. Structured output is not
		// documented there, so the schema goes in the system prompt and the
		// answer is validated like every other provider's.
		endpoint = "https://api.z.ai/api/anthropic/v1/messages"
		headers.Set("x-api-key", r.Key)
		headers.Set("Authorization", "Bearer "+r.Key)
		headers.Set("anthropic-version", "2023-06-01")
		compact, _ := json.Marshal(schema)
		rule := "Respond with exactly one JSON object and nothing else: no prose, no Markdown fences. " +
			"It must match this JSON Schema: " + string(compact)
		system := rule
		if req.System != "" {
			system = req.System + "\n\n" + rule
		}
		body = map[string]any{"model": r.Model, "max_tokens": 8192, "temperature": req.Temperature,
			"system": system, "messages": []any{map[string]any{"role": "user", "content": req.Prompt}}}
	default:
		return "", nil, nil, newError("Unsupported AI provider.")
	}
	return endpoint, headers, body, nil
}

var fenced = regexp.MustCompile("(?s)^```(?:json)?\\s*(.*?)\\s*```$")

// jsonFromText finds the JSON object in a model's text answer, tolerating
// Markdown fences or a sentence before it.
func jsonFromText(content string) (any, error) {
	text := strings.TrimSpace(content)
	if m := fenced.FindStringSubmatch(text); m != nil {
		text = m[1]
	}
	var value any
	if err := json.Unmarshal([]byte(text), &value); err == nil {
		return value, nil
	}
	start, end := strings.Index(text, "{"), strings.LastIndex(text, "}")
	if start < 0 || end <= start {
		return nil, newError("Provider returned an invalid structured response.")
	}
	if err := json.Unmarshal([]byte(text[start:end+1]), &value); err != nil {
		return nil, newError("Provider returned an invalid structured response.")
	}
	return value, nil
}

func send(ctx context.Context, endpoint string, headers http.Header, body map[string]any, timeout time.Duration) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	data, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(data))
	if err != nil {
		return nil, newError("Provider connection failed or returned invalid JSON.")
	}
	httpReq.Header = headers
	resp, err := noRedirects.Do(httpReq)
	if err != nil {
		return nil, newError("Provider connection failed or returned invalid JSON.")
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		// Never show response bodies: providers can echo request headers,
		// and neither reports nor the browser may ever contain a key.
		var unsupportedTemperature, noBalance bool
		if resp.StatusCode == 400 || resp.StatusCode == 429 {
			raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4000))
			detail := strings.ToLower(string(raw))
			unsupportedTemperature = strings.Contains(detail, "temperature") &&
				(strings.Contains(detail, "unsupported") || strings.Contains(detail, "not support"))
			// Z.AI reports an empty balance as HTTP 429; retrying cannot help.
			noBalance = strings.Contains(detail, "insufficient balance") || strings.Contains(detail, "no resource package")
		}
		switch {
		case resp.StatusCode == 401 || resp.StatusCode == 403:
			return nil, &Error{Message: "Provider rejected this API key.", Status: resp.StatusCode, Auth: true}
		case noBalance:
			return nil, &Error{Message: "Provider account has insufficient balance or no resource package.", Status: resp.StatusCode}
		}
		e := &Error{Message: fmt.Sprintf("Provider request failed (HTTP %d).", resp.StatusCode), Status: resp.StatusCode}
		if unsupportedTemperature {
			e.Body = "unsupported temperature"
		}
		return nil, e
	}

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 2_000_001))
	if err != nil {
		return nil, newError("Provider connection failed or returned invalid JSON.")
	}
	if len(raw) > 2_000_000 {
		return nil, newError("Provider response exceeded the size limit.")
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, newError("Provider connection failed or returned invalid JSON.")
	}
	return out, nil
}

// call sends one request to a provider API and returns its parsed answer.
func call(ctx context.Context, r Route, req Request, timeout time.Duration) (any, error) {
	endpoint, headers, body, err := requestShape(r, req)
	if err != nil {
		return nil, err
	}
	measured := func() (map[string]any, error) {
		resp, err := send(ctx, endpoint, headers, body, timeout)
		usage.RecordAPI(ctx, r.Provider, r.Model, resp) // nil resp records an unmeasured call.
		return resp, err
	}
	resp, err := measured()
	if e, ok := err.(*Error); ok && e.Status == 400 && e.Body == "unsupported temperature" {
		if _, has := body["temperature"]; has {
			delete(body, "temperature")
			resp, err = measured()
		}
	}
	if err != nil {
		return nil, err
	}

	invalid := newError("Provider returned an invalid structured response.")
	refused := newError("Provider refused or truncated this response.")
	var content string
	switch r.Provider {
	case "openai", "openrouter":
		choices, _ := resp["choices"].([]any)
		if len(choices) == 0 {
			return nil, invalid
		}
		choice, _ := choices[0].(map[string]any)
		message, _ := choice["message"].(map[string]any)
		if message == nil {
			return nil, invalid
		}
		if refusal, _ := message["refusal"].(string); refusal != "" {
			return nil, refused
		}
		if reason := choice["finish_reason"]; reason != nil && reason != "stop" {
			return nil, refused
		}
		if content, _ = message["content"].(string); content == "" {
			return nil, invalid
		}
	case "gemini":
		candidates, _ := resp["candidates"].([]any)
		if len(candidates) == 0 {
			return nil, invalid
		}
		candidate, _ := candidates[0].(map[string]any)
		if reason := candidate["finishReason"]; reason != nil && reason != "STOP" {
			return nil, refused
		}
		inner, _ := candidate["content"].(map[string]any)
		parts, _ := inner["parts"].([]any)
		for _, part := range parts {
			p, _ := part.(map[string]any)
			if text, ok := p["text"].(string); ok && p["thought"] != true {
				content += text
			}
		}
	default: // anthropic, zai
		if reason := resp["stop_reason"]; reason != nil && reason != "end_turn" && reason != "stop" {
			return nil, refused
		}
		parts, _ := resp["content"].([]any)
		for _, part := range parts {
			p, _ := part.(map[string]any)
			if p["type"] == "text" {
				text, _ := p["text"].(string)
				content += text
			}
		}
	}

	var result any
	if r.Provider == "zai" {
		if result, err = jsonFromText(content); err != nil {
			return nil, err
		}
	} else if json.Unmarshal([]byte(content), &result) != nil {
		return nil, invalid
	}
	if err := ValidateOutput(result, Strictify(req.Schema)); err != nil {
		return nil, invalid
	}
	return result, nil
}
