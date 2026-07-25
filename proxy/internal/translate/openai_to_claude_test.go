package translate

import (
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"
)

func TestOpenAIResponseToClaudeResponse(t *testing.T) {
	// Text-only response
	input := `{"id":"123","model":"deepseek-v4","choices":[{"index":0,"message":{"role":"assistant","content":"Hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}`
	output := OpenAIResponseToClaudeResponse([]byte(input))
	var result map[string]interface{}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}

	if result["type"] != "message" {
		t.Errorf("type = %v", result["type"])
	}
	if result["stop_reason"] != "end_turn" {
		t.Errorf("stop_reason = %v", result["stop_reason"])
	}

	// Invalid JSON — pass through
	input = `not json`
	output = OpenAIResponseToClaudeResponse([]byte(input))
	if string(output) != input {
		t.Errorf("invalid json should pass through unchanged")
	}

	// Zero choices — pass through
	input = `{"id":"123","model":"deepseek-v4","choices":[]}`
	output = OpenAIResponseToClaudeResponse([]byte(input))
	if string(output) != input {
		t.Errorf("zero choices should pass through unchanged")
	}
}

func TestOpenAIResponseThinking(t *testing.T) {
	// Response with reasoning_content in nested map content
	input := `{"id":"msg_1","model":"deepseek-v4","choices":[{"index":0,"message":{"role":"assistant","content":{"reasoning_content":"thinking...","text":"answer"}},"finish_reason":"stop"}]}`
	output := OpenAIResponseToClaudeResponse([]byte(input))
	var result map[string]interface{}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}

	content := result["content"].([]interface{})
	if len(content) != 2 {
		t.Fatalf("expected 2 content blocks (thinking + text), got %d", len(content))
	}

	thinking := content[0].(map[string]interface{})
	if thinking["type"] != "thinking" || thinking["thinking"] != "thinking..." {
		t.Errorf("thinking block: %+v", thinking)
	}
	text := content[1].(map[string]interface{})
	if text["type"] != "text" || text["text"] != "answer" {
		t.Errorf("text block: %+v", text)
	}
}

func TestOpenAIResponseTopLevelReasoning(t *testing.T) {
	input := `{"id":"msg_1","model":"deepseek-v4","choices":[{"index":0,"message":{"role":"assistant","content":"answer","reasoning_content":"thinking..."},"finish_reason":"stop"}]}`
	output := OpenAIResponseToClaudeResponse([]byte(input))
	var result map[string]interface{}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}

	content := result["content"].([]interface{})
	if len(content) != 2 {
		t.Fatalf("expected 2 content blocks (thinking + text), got %d", len(content))
	}
	thinking := content[0].(map[string]interface{})
	if thinking["type"] != "thinking" || thinking["thinking"] != "thinking..." {
		t.Errorf("thinking block: %+v", thinking)
	}
	text := content[1].(map[string]interface{})
	if text["type"] != "text" || text["text"] != "answer" {
		t.Errorf("text block: %+v", text)
	}
}

func TestOpenAIResponseToolCalls(t *testing.T) {
	input := `{"id":"msg_1","model":"deepseek-v4","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"NYC\"}"}}]},"finish_reason":"tool_calls"}]}`
	output := OpenAIResponseToClaudeResponse([]byte(input))
	var result map[string]interface{}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}

	if result["stop_reason"] != "tool_use" {
		t.Errorf("stop_reason = %v, want tool_use", result["stop_reason"])
	}
	content := result["content"].([]interface{})
	if len(content) != 1 {
		t.Fatalf("expected 1 content block, got %d", len(content))
	}
	toolUse := content[0].(map[string]interface{})
	if toolUse["type"] != "tool_use" || toolUse["name"] != "get_weather" {
		t.Errorf("tool_use block: %+v", toolUse)
	}
}

func TestOpenAIEmptyContent(t *testing.T) {
	// Empty content response
	input := `{"id":"msg_1","model":"deepseek-v4","choices":[{"index":0,"message":{"role":"assistant","content":null},"finish_reason":"stop"}]}`
	output := OpenAIResponseToClaudeResponse([]byte(input))
	var result map[string]interface{}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	content := result["content"].([]interface{})
	if len(content) != 0 {
		t.Errorf("expected empty content, got %d blocks", len(content))
	}
}

func TestTranslateChunk(t *testing.T) {
	t.Run("message_start", func(t *testing.T) {
		state := newClaudeState()
		chunk := &OpenAIStreamChunk{
			ID: "chunk_1", Model: "deepseek-v4",
			Choices: []OpenAIChoice{{Delta: OpenAIChunkDelta{Role: "assistant", Content: ""}}},
		}
		events := translateChunk(state, chunk)
		if !state.messageStartSent {
			t.Error("message_start should be sent")
		}
		// First event should be message_start
		if len(events) == 0 || events[0]["type"] != "message_start" {
			t.Errorf("first event should be message_start, got %v", events)
		}
	})

	t.Run("text_content", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true

		chunk := &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{Delta: OpenAIChunkDelta{Content: "Hello"}}},
		}
		events := translateChunk(state, chunk)
		if len(events) == 0 {
			t.Fatal("expected events")
		}

		// Check for content_block_start followed by text_delta
		hasStart, hasDelta := false, false
		for _, e := range events {
			if e["type"] == "content_block_start" {
				hasStart = true
			}
			if e["type"] == "content_block_delta" {
				if delta, ok := e["delta"].(map[string]interface{}); ok {
					if delta["type"] == "text_delta" {
						hasDelta = true
					}
				}
			}
		}
		if !hasStart || !hasDelta {
			t.Errorf("expected text content_block_start and text_delta, got events: %v", events)
		}
	})

	t.Run("reasoning_content", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true

		chunk := &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{Delta: OpenAIChunkDelta{ReasoningContent: "thinking..."}}},
		}
		events := translateChunk(state, chunk)
		hasThinking := false
		for _, e := range events {
			if e["type"] == "content_block_delta" {
				if delta, ok := e["delta"].(map[string]interface{}); ok {
					if delta["type"] == "thinking_delta" {
						hasThinking = true
					}
				}
			}
		}
		if !hasThinking {
			t.Errorf("expected thinking_delta, got events: %v", events)
		}
	})

	t.Run("tool_calls", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true

		chunk := &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{
				Delta: OpenAIChunkDelta{
					ToolCalls: []OpenAIDeltaToolCall{
						{Index: 0, ID: "call_1", Function: &OpenAIDeltaFunc{Name: "get_weather", Arguments: `{"city":"`}},
					},
				},
			}},
		}
		events := translateChunk(state, chunk)
		hasToolStart, hasArgs := false, false
		for _, e := range events {
			if e["type"] == "content_block_start" {
				hasToolStart = true
			}
			if e["type"] == "content_block_delta" {
				if d, ok := e["delta"].(map[string]interface{}); ok {
					if d["type"] == "input_json_delta" {
						hasArgs = true
					}
				}
			}
		}
		if !hasToolStart {
			t.Error("expected tool content_block_start")
		}
		if !hasArgs {
			t.Error("expected input_json_delta for tool args")
		}
	})

	t.Run("tool_call_without_function", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true

		chunk := &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{
				Delta: OpenAIChunkDelta{
					ToolCalls: []OpenAIDeltaToolCall{
						{Index: 0, ID: "call_1"},
					},
				},
			}},
		}
		events := translateChunk(state, chunk)
		if len(events) == 0 {
			t.Fatal("expected events")
		}
		if _, ok := state.toolCalls[0]; !ok {
			t.Fatalf("expected tool call state to be recorded")
		}
	})

	t.Run("finish_reason", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true
		stop := "stop"
		chunk := &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{
				Delta:        OpenAIChunkDelta{},
				FinishReason: &stop,
			}},
		}
		events := translateChunk(state, chunk)
		if state.stopSent {
			t.Error("stop should wait for usage chunk or DONE")
		}
		if state.pendingFinishReason == nil || *state.pendingFinishReason != "stop" {
			t.Errorf("pending finish reason = %v", state.pendingFinishReason)
		}
		for _, e := range events {
			if e["type"] == "message_stop" {
				t.Errorf("message_stop should be deferred, got events: %v", events)
			}
		}
	})

	t.Run("usage_chunk_after_finish", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true
		stop := "stop"
		translateChunk(state, &OpenAIStreamChunk{
			Choices: []OpenAIChoice{{
				Delta:        OpenAIChunkDelta{},
				FinishReason: &stop,
			}},
		})

		events := translateChunk(state, &OpenAIStreamChunk{
			Usage: &OpenAIUsage{PromptTokens: 10, CompletionTokens: 5},
		})
		if !state.stopSent {
			t.Error("stop should be sent after usage chunk")
		}

		hasUsage := false
		for _, e := range events {
			if e["type"] == "message_delta" {
				u := e["usage"].(map[string]interface{})
				if u["output_tokens"] != 5 {
					t.Errorf("output_tokens = %v", u["output_tokens"])
				}
				hasUsage = true
			}
		}
		if !hasUsage {
			t.Errorf("expected usage in message_delta, got events: %v", events)
		}
	})

	t.Run("no_choices", func(t *testing.T) {
		state := newClaudeState()
		chunk := &OpenAIStreamChunk{Choices: []OpenAIChoice{}}
		events := translateChunk(state, chunk)
		if events != nil {
			t.Error("no choices should return nil")
		}
	})
}

func TestBuildStopEvents(t *testing.T) {
	t.Run("basic stop", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true
		state.nextBlockIndex = 2
		state.textStarted = true
		state.textIndex = 0

		events := buildStopEvents(state, nil, &OpenAIUsage{PromptTokens: 10, CompletionTokens: 5})
		if !state.stopSent {
			t.Error("stopSent should be true")
		}
		if len(events) < 3 {
			t.Fatalf("expected at least 3 events (block_stop, message_delta, message_stop), got %d", len(events))
		}

		last := events[len(events)-1]
		if last["type"] != "message_stop" {
			t.Errorf("last event should be message_stop, got %v", last["type"])
		}

		// Check usage only has output_tokens
		for _, e := range events {
			if e["type"] == "message_delta" {
				u := e["usage"].(map[string]interface{})
				if _, ok := u["input_tokens"]; ok {
					t.Error("message_delta should not contain input_tokens")
				}
				if u["output_tokens"] != 5 {
					t.Errorf("output_tokens = %v", u["output_tokens"])
				}
			}
		}
	})

	t.Run("tool blocks also closed", func(t *testing.T) {
		state := newClaudeState()
		state.messageStartSent = true
		state.toolCalls[0] = toolCallInfo{id: "call_1", name: "test", blockIndex: 1}

		events := buildStopEvents(state, nil, nil)
		blockStops := 0
		for _, e := range events {
			if e["type"] == "content_block_stop" {
				blockStops++
			}
		}
		if blockStops < 1 {
			t.Errorf("expected at least 1 content_block_stop for tool, got %d", blockStops)
		}
	})
}

func TestOpenAIStreamEOFUsesPendingStopReason(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	stream := strings.Join([]string{
		`data: {"id":"1","model":"deepseek-v4","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}`,
		`data: {"id":"1","model":"deepseek-v4","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}`,
		``,
	}, "\n\n")

	rc := OpenAIStreamToClaudeStream(ctx, io.NopCloser(strings.NewReader(stream)))
	defer rc.Close()
	out, err := io.ReadAll(rc)
	if err != nil {
		t.Fatal(err)
	}
	body := string(out)
	if !strings.Contains(body, `event: message_stop`) {
		t.Fatalf("expected message_stop event, got: %s", body)
	}
	if !strings.Contains(body, `"stop_reason":"max_tokens"`) {
		t.Fatalf("expected preserved stop reason, got: %s", body)
	}
	messageDeltaIndex := strings.Index(body, "event: message_delta")
	if messageDeltaIndex == -1 {
		t.Fatalf("expected message_delta event, got: %s", body)
	}
	messageDelta := body[messageDeltaIndex:]
	if strings.Contains(messageDelta, `"input_tokens"`) {
		t.Fatalf("message_delta usage should not include input_tokens: %s", body)
	}
}

func TestOpenAIErrorToClaudeError(t *testing.T) {
	// Standard error
	input := `{"error":{"message":"rate limit exceeded","type":"rate_limit_error","code":"rate_limit"}}`
	result := OpenAIErrorToClaudeError([]byte(input))
	err, ok := result["error"].(map[string]interface{})
	if !ok {
		t.Fatal("expected error field")
	}
	if err["message"] != "rate limit exceeded" {
		t.Errorf("message = %q", err["message"])
	}
	if err["type"] != "rate_limit_error" {
		t.Errorf("type = %q, want rate_limit_error", err["type"])
	}

	// Auth error
	input = `{"error":{"message":"invalid key","type":"authentication_error"}}`
	result = OpenAIErrorToClaudeError([]byte(input))
	err = result["error"].(map[string]interface{})
	if err["type"] != "authentication_error" {
		t.Errorf("type = %q", err["type"])
	}

	// Invalid request
	input = `{"error":{"message":"bad request","type":"invalid_request_error"}}`
	result = OpenAIErrorToClaudeError([]byte(input))
	err = result["error"].(map[string]interface{})
	if err["type"] != "invalid_request_error" {
		t.Errorf("type = %q", err["type"])
	}

	// Empty body — defaults
	result = OpenAIErrorToClaudeError([]byte(`{}`))
	err = result["error"].(map[string]interface{})
	if err["message"] != "upstream error" {
		t.Errorf("empty: message = %q", err["message"])
	}

	// Invalid JSON — defaults
	result = OpenAIErrorToClaudeError([]byte(`not json`))
	err = result["error"].(map[string]interface{})
	if err["message"] != "upstream error" {
		t.Errorf("invalid json: message = %q", err["message"])
	}
}

func TestCloseThinkingBlock(t *testing.T) {
	state := newClaudeState()
	// Not started → nil
	if events := closeThinkingBlock(state); events != nil {
		t.Error("not started should return nil")
	}

	// Started → stop event
	state.thinkingStarted = true
	state.thinkingIndex = 0
	events := closeThinkingBlock(state)
	if len(events) != 1 || events[0]["type"] != "content_block_stop" {
		t.Errorf("started: expected stop event, got %v", events)
	}
	if state.thinkingStarted {
		t.Error("thinkingStarted should be false after close")
	}
}

func TestCloseTextBlock(t *testing.T) {
	state := newClaudeState()
	if events := closeTextBlock(state); events != nil {
		t.Error("not started should return nil")
	}

	state.textStarted = true
	state.textIndex = 0
	events := closeTextBlock(state)
	if len(events) != 1 || events[0]["type"] != "content_block_stop" {
		t.Errorf("started: expected stop event, got %v", events)
	}
}

func TestParseJSON(t *testing.T) {
	tests := []struct {
		input string
		check func(interface{}) bool
	}{
		{`{"a":1}`, func(v interface{}) bool {
			m, ok := v.(map[string]interface{})
			return ok && m["a"] == float64(1)
		}},
		{`"hello"`, func(v interface{}) bool { return v == "hello" }},
		{``, func(v interface{}) bool {
			_, ok := v.(map[string]interface{})
			return ok
		}},
		{`invalid`, func(v interface{}) bool {
			_, ok := v.(map[string]interface{})
			return ok
		}},
	}
	for _, tc := range tests {
		t.Run(tc.input, func(t *testing.T) {
			if got := parseJSON(tc.input); !tc.check(got) {
				t.Errorf("parseJSON(%q) = %v", tc.input, got)
			}
		})
	}
}

func TestJoinParts(t *testing.T) {
	// Single text → plain string
	parts := []ContentPart{{Type: "text", Text: "hello"}}
	got := joinParts(parts)
	if got != "hello" {
		t.Errorf("single text: got %v", got)
	}

	// Multiple parts → array
	parts = []ContentPart{
		{Type: "text", Text: "a"},
		{Type: "image_url", ImageURL: &ImageURL{URL: "data:img"}},
	}
	got = joinParts(parts)
	arr, ok := got.([]ContentPart)
	if !ok || len(arr) != 2 {
		t.Errorf("multiple parts: expected array, got %T %v", got, got)
	}
}
