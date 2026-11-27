package llm

import (
	"encoding/json"
	"maps"
	"math"
	"slices"
)

// Schema is a JSON Schema, written as plain Go maps so it can be sent to any
// provider unchanged.
type Schema = map[string]any

// Small builders keep schemas readable where they are declared.
var (
	String  = Schema{"type": "string"}
	Strings = Array(String)
	Boolean = Schema{"type": "boolean"}
	Integer = Schema{"type": "integer"}
	Number  = Schema{"type": "number"}
)

// Object is an object schema with the given properties.
func Object(properties map[string]any) Schema {
	return Schema{"type": "object", "properties": properties}
}

// Array is a list schema whose items follow item.
func Array(item Schema) Schema {
	return Schema{"type": "array", "items": item}
}

// Enum is a string schema limited to the given values.
func Enum(values ...string) Schema {
	list := make([]any, len(values))
	for i, v := range values {
		list[i] = v
	}
	return Schema{"type": "string", "enum": list}
}

// Strictify returns a copy of schema that satisfies OpenAI's strict mode:
// every object lists all its properties as required and allows no others.
// Forgetting this is the most common reason a structured call is rejected.
func Strictify(schema Schema) Schema {
	out := maps.Clone(schema)
	switch out["type"] {
	case "object":
		props, _ := out["properties"].(map[string]any)
		strict := make(map[string]any, len(props))
		for key, child := range props {
			strict[key] = strictifyAny(child)
		}
		out["properties"] = strict
		out["additionalProperties"] = false
		out["required"] = slices.Sorted(maps.Keys(props))
	case "array":
		if items, ok := out["items"]; ok {
			out["items"] = strictifyAny(items)
		}
	}
	for _, key := range []string{"anyOf", "oneOf", "allOf"} {
		if list, ok := out[key].([]any); ok {
			strict := make([]any, len(list))
			for i, child := range list {
				strict[i] = strictifyAny(child)
			}
			out[key] = strict
		}
	}
	return out
}

func strictifyAny(v any) any {
	if s, ok := v.(map[string]any); ok {
		return Strictify(s)
	}
	return v
}

// ValidateOutput checks an answer against the subset of JSON Schema this app
// uses, before the answer is used or cached.
func ValidateOutput(value any, schema Schema) error {
	switch kind := schema["type"].(type) {
	case string:
		if !validType(value, kind) {
			return newError("Provider response has an invalid value type.")
		}
	case []any:
		if !slices.ContainsFunc(kind, func(k any) bool { s, _ := k.(string); return validType(value, s) }) {
			return newError("Provider response has an invalid value type.")
		}
	case []string:
		if !slices.ContainsFunc(kind, func(k string) bool { return validType(value, k) }) {
			return newError("Provider response has an invalid value type.")
		}
	}
	if enum, ok := schema["enum"].([]any); ok && !slices.ContainsFunc(enum, func(e any) bool { return sameJSON(e, value) }) {
		return newError("Provider response contains an unsupported selection.")
	}
	if obj, ok := value.(map[string]any); ok {
		props, _ := schema["properties"].(map[string]any)
		for _, key := range stringList(schema["required"]) {
			if _, ok := obj[key]; !ok {
				return newError("Provider response is missing required fields.")
			}
		}
		if schema["additionalProperties"] == false {
			for key := range obj {
				if _, ok := props[key]; !ok {
					return newError("Provider response contains unexpected fields.")
				}
			}
		}
		for key, child := range props {
			if v, ok := obj[key]; ok {
				if childSchema, ok := child.(map[string]any); ok {
					if err := ValidateOutput(v, childSchema); err != nil {
						return err
					}
				}
			}
		}
	}
	if list, ok := value.([]any); ok {
		if items, ok := schema["items"].(map[string]any); ok {
			for _, child := range list {
				if err := ValidateOutput(child, items); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// validType matches Python's type checks. Decoded JSON numbers are float64,
// so an "integer" is a whole, finite number.
func validType(value any, kind string) bool {
	switch kind {
	case "object":
		_, ok := value.(map[string]any)
		return ok
	case "array":
		_, ok := value.([]any)
		return ok
	case "string":
		_, ok := value.(string)
		return ok
	case "integer":
		n, ok := value.(float64)
		return ok && !math.IsInf(n, 0) && n == math.Trunc(n)
	case "number":
		n, ok := value.(float64)
		return ok && !math.IsInf(n, 0) && !math.IsNaN(n)
	case "boolean":
		_, ok := value.(bool)
		return ok
	case "null":
		return value == nil
	}
	return false
}

func stringList(v any) []string {
	switch list := v.(type) {
	case []string:
		return list
	case []any:
		out := make([]string, 0, len(list))
		for _, item := range list {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

func sameJSON(a, b any) bool {
	x, err1 := json.Marshal(a)
	y, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(x) == string(y)
}

// Normalize turns any Go value into plain decoded JSON (maps, slices,
// float64...), the same shape an answer has after json.Unmarshal.
func Normalize(v any) (any, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var out any
	err = json.Unmarshal(data, &out)
	return out, err
}
