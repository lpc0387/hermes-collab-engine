package translate

import (
	"encoding/json"
	"testing"
)

func TestExtractSystemText(t *testing.T) {
	// String system
	got := extractSystemText(json.RawMessage(`"You are helpful"`))
	if got != "You are helpful" {
		t.Errorf("string system: got %q, want %q", got, "You are helpful")
	}

	// Array system
	got = extractSystemText(json.RawMessage(`[{"type":"text","text":"Hello "},{"type":"text","text":"world"}]`))
	if got != "Hello world" {
		t.Errorf("array system: got %q, want %q", got, "Hello world")
	}

	// Empty
	got = extractSystemText(json.RawMessage(``))
	if got != "" {
		t.Errorf("empty: got %q, want empty", got)
	}

	// Null
	got = extractSystemText(json.RawMessage(`null`))
	if got != "" {
		t.Errorf("null: got %q, want empty", got)
	}
}

func TestExtractToolResultContent(t *testing.T) {
	tests := []struct {
		name  string
		block ContentBlock
		want  string
	}{
		{"empty", ContentBlock{}, ""},
		{"string content", ContentBlock{Content: json.RawMessage(`"result"`)}, "result"},
		{"array text blocks", ContentBlock{Content: json.RawMessage(`[{"type":"text","text":"a"},{"type":"text","text":"b"}]`)}, "a\nb"},
		{"non-text block skipped", ContentBlock{Content: json.RawMessage(`[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"abc"}}]`)}, ""},
		{"fallback to input", ContentBlock{Input: json.RawMessage(`"fallback"`)}, "fallback"},
		{"fallback JSON", ContentBlock{Content: json.RawMessage(`{"custom":1}`)}, `{"custom":1}`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := extractToolResultContent(tc.block); got != tc.want {
				t.Errorf("extractToolResultContent = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestConvertFinishReason(t *testing.T) {
	tests := []struct {
		input *string
		want  string
	}{
		{nil, "end_turn"},
		{strPtr("stop"), "end_turn"},
		{strPtr("length"), "max_tokens"},
		{strPtr("tool_calls"), "tool_use"},
		{strPtr("unknown"), "end_turn"},
	}
	for _, tc := range tests {
		t.Run(ptrStr(tc.input), func(t *testing.T) {
			if got := convertFinishReason(tc.input); got != tc.want {
				t.Errorf("convertFinishReason(%v) = %q, want %q", tc.input, got, tc.want)
			}
		})
	}
}

func strPtr(s string) *string { return &s }
func ptrStr(p *string) string {
	if p == nil {
		return "nil"
	}
	return *p
}

func TestConvertClaudeMessage(t *testing.T) {
	// String content
	msg := ClaudeMessage{Role: "user", Content: json.RawMessage(`"hello"`)}
	got := convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Role != "user" || got[0].Content != "hello" {
		t.Errorf("string content: got %+v", got)
	}

	// Tool role remapped to user
	msg = ClaudeMessage{Role: "tool", Content: json.RawMessage(`"result"`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Role != "user" {
		t.Errorf("tool role should be remapped to user")
	}

	// Array with text content
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"hello"}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Content != "hello" {
		t.Errorf("single text block: got %+v", got)
	}

	// Array with image (base64)
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"abc"}}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 {
		t.Fatalf("image block: expected 1 message, got %d", len(got))
	}
	parts, ok := got[0].Content.([]ContentPart)
	if !ok || len(parts) != 1 || parts[0].Type != "image_url" {
		t.Errorf("image block: expected image_url content part, got %+v", got[0].Content)
	}

	// Tool result
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","tool_use_id":"call_1","content":"done"}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Role != "tool" || got[0].ToolCallID != "call_1" {
		t.Errorf("tool_result: got %+v", got)
	}

	// Tool result with OpenAI-style tool_call_id alias
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","tool_call_id":"call_1","content":"done"}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Role != "tool" || got[0].ToolCallID != "call_1" {
		t.Errorf("tool_result alias: got %+v", got)
	}

	// Tool result without an ID should not produce an invalid OpenAI tool message
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","content":"done"}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Role == "tool" || got[0].Content != "done" {
		t.Errorf("tool_result missing id: got %+v", got)
	}

	// Tool use → tool_calls
	msg = ClaudeMessage{Role: "assistant", Content: json.RawMessage(`[{"type":"tool_use","id":"call_1","name":"get_weather","input":{"city":"NYC"}}]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || len(got[0].ToolCalls) != 1 {
		t.Fatalf("tool_use: expected 1 msg with 1 tool_call, got %+v", got)
	}
	if got[0].ToolCalls[0].Function.Name != "get_weather" {
		t.Errorf("tool_call name: got %q", got[0].ToolCalls[0].Function.Name)
	}

	// Empty content array
	msg = ClaudeMessage{Role: "user", Content: json.RawMessage(`[]`)}
	got = convertClaudeMessage(msg)
	if len(got) != 1 || got[0].Content != "" {
		t.Errorf("empty array: got %+v", got)
	}
}

func TestFixMissingToolResponses(t *testing.T) {
	// All responses present — no injection
	msgs := []OpenAIMessage{
		{Role: "assistant", ToolCalls: []OpenAIToolCall{{ID: "call_1"}}},
		{Role: "tool", ToolCallID: "call_1", Content: "done"},
	}
	got := fixMissingToolResponses(msgs)
	if len(got) != 2 {
		t.Errorf("all present: expected 2 msgs, got %d", len(got))
	}

	// Missing response — injected
	msgs = []OpenAIMessage{
		{Role: "assistant", ToolCalls: []OpenAIToolCall{{ID: "call_1"}, {ID: "call_2"}}},
		{Role: "tool", ToolCallID: "call_1", Content: "done"},
	}
	got = fixMissingToolResponses(msgs)
	if len(got) != 3 {
		t.Fatalf("missing 1: expected 3 msgs, got %d", len(got))
	}
	if got[2].Role != "tool" || got[2].ToolCallID != "call_2" || got[2].Content != "[No response received]" {
		t.Errorf("injected message: got %+v", got[2])
	}

	// No tool calls — unchanged
	msgs = []OpenAIMessage{
		{Role: "user", Content: "hi"},
		{Role: "assistant", Content: "hello"},
	}
	got = fixMissingToolResponses(msgs)
	if len(got) != 2 {
		t.Errorf("no tool calls: expected 2 msgs, got %d", len(got))
	}
}

func TestClaudeToOpenAI(t *testing.T) {
	// Basic text message
	input := `{"model":"deepseek-v4","messages":[{"role":"user","content":"hello"}]}`
	output := ClaudeToOpenAI([]byte(input))
	var result OpenAIChatRequest
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}
	if result.Model != "deepseek-v4" {
		t.Errorf("model = %q", result.Model)
	}
	if len(result.Messages) != 1 || result.Messages[0].Role != "user" || result.Messages[0].Content != "hello" {
		t.Errorf("messages: got %+v", result.Messages)
	}

	// Streaming requests should ask upstream to include usage chunks
	input = `{"model":"deepseek-v4","stream":true,"messages":[{"role":"user","content":"hello"}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if result.StreamOptions == nil || !result.StreamOptions.IncludeUsage {
		t.Errorf("stream_options.include_usage should be true: got %+v", result.StreamOptions)
	}

	// System message as string
	input = `{"model":"deepseek-v4","system":"You are helpful","messages":[{"role":"user","content":"hi"}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Messages) != 2 || result.Messages[0].Role != "system" {
		t.Errorf("system message not first: got %+v", result.Messages)
	}

	// System message as array
	input = `{"model":"deepseek-v4","system":[{"type":"text","text":"Be helpful"}],"messages":[{"role":"user","content":"hi"}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if len(result.Messages) != 2 || result.Messages[0].Role != "system" || result.Messages[0].Content != "Be helpful" {
		t.Errorf("system array: got %+v", result.Messages)
	}

	// Tool choice — auto
	input = `{"model":"deepseek-v4","tool_choice":{"type":"auto"},"messages":[{"role":"user","content":"hi"}],"tools":[{"name":"test","input_schema":{"type":"object"}}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if result.ToolChoice != nil {
		raw, _ := json.Marshal(result.ToolChoice)
		if string(raw) != `"auto"` {
			t.Errorf("tool_choice auto: got %s", string(raw))
		}
	}

	// Tool choice — tool
	input = `{"model":"deepseek-v4","tool_choice":{"type":"tool","name":"get_weather"},"messages":[{"role":"user","content":"hi"}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	var tc struct {
		Type     string          `json:"type"`
		Function json.RawMessage `json:"function"`
	}
	raw, _ := json.Marshal(result.ToolChoice)
	if err := json.Unmarshal(raw, &tc); err != nil {
		t.Fatal(err)
	}
	if tc.Type != "function" {
		t.Errorf("tool_choice tool type: got %q", tc.Type)
	}

	// Stop sequences
	input = `{"model":"deepseek-v4","stop_sequences":["\n\n","."],"messages":[{"role":"user","content":"hi"}]}`
	output = ClaudeToOpenAI([]byte(input))
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if result.Stop == nil {
		t.Fatal("stop should be set")
	}
	stops := result.Stop.([]interface{})
	if len(stops) != 2 {
		t.Errorf("stop sequences: got %d", len(stops))
	}

	// Invalid JSON — pass through
	input = `not json`
	output = ClaudeToOpenAI([]byte(input))
	if string(output) != input {
		t.Errorf("invalid json should pass through unchanged")
	}
}
