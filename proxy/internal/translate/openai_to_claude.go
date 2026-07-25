package translate

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"
)

// ClaudeSSEEvent represents a single Claude SSE event line.
type ClaudeSSEEvent map[string]interface{}

// OpenAIStreamChunk represents one line of OpenAI SSE.
type OpenAIStreamChunk struct {
	ID      string         `json:"id,omitempty"`
	Object  string         `json:"object,omitempty"`
	Created int64          `json:"created,omitempty"`
	Model   string         `json:"model,omitempty"`
	Choices []OpenAIChoice `json:"choices,omitempty"`
	Usage   *OpenAIUsage   `json:"usage,omitempty"`
}

type OpenAIChoice struct {
	Index        int              `json:"index"`
	Delta        OpenAIChunkDelta `json:"delta"`
	FinishReason *string          `json:"finish_reason,omitempty"`
}

type OpenAIChunkDelta struct {
	Role             string                `json:"role,omitempty"`
	Content          string                `json:"content,omitempty"`
	ReasoningContent string                `json:"reasoning_content,omitempty"`
	Reasoning        string                `json:"reasoning,omitempty"`
	ToolCalls        []OpenAIDeltaToolCall `json:"tool_calls,omitempty"`
}

type OpenAIDeltaToolCall struct {
	Index    int              `json:"index"`
	ID       string           `json:"id,omitempty"`
	Function *OpenAIDeltaFunc `json:"function,omitempty"`
}

type OpenAIDeltaFunc struct {
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

type OpenAIUsage struct {
	PromptTokens     int `json:"prompt_tokens,omitempty"`
	CompletionTokens int `json:"completion_tokens,omitempty"`
}

// claudeStreamState tracks state across multiple SSE events.
type claudeStreamState struct {
	messageStartSent bool
	messageID        string
	model            string
	nextBlockIndex   int

	thinkingStarted bool
	thinkingIndex   int

	textStarted bool
	textIndex   int

	toolCalls   map[int]toolCallInfo
	toolArgBufs map[int]string

	pendingFinishReason *string
	usage               *OpenAIUsage
	stopSent            bool
}

type toolCallInfo struct {
	id         string
	name       string
	blockIndex int
}

func newClaudeState() *claudeStreamState {
	return &claudeStreamState{
		toolCalls:   map[int]toolCallInfo{},
		toolArgBufs: map[int]string{},
	}
}

// OpenAIStreamToClaudeStream translates an OpenAI SSE byte stream
// into Claude Messages API SSE format. The ctx is used to cancel
// reading from openaiBody when the client disconnects.
func OpenAIStreamToClaudeStream(ctx context.Context, openaiBody io.ReadCloser) io.ReadCloser {
	pr, pw := io.Pipe()
	state := newClaudeState()

	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.With("component", "translate").Error("panic in stream goroutine", "panic", r)
			}
		}()
		defer pw.Close()
		defer openaiBody.Close()

		// Close upstream body when context is cancelled to unblock scanner
		go func() {
			<-ctx.Done()
			openaiBody.Close()
		}()

		scanner := bufio.NewScanner(openaiBody)
		scanner.Buffer(make([]byte, 256*1024), 256*1024)

		for scanner.Scan() {
			line := scanner.Text()
			trimmed := strings.TrimSpace(line)
			if trimmed == "" || !strings.HasPrefix(trimmed, "data:") {
				continue
			}

			data := strings.TrimSpace(strings.TrimPrefix(trimmed, "data:"))
			if data == "[DONE]" {
				if state.messageStartSent && !state.stopSent {
					events := buildStopEvents(state, state.pendingFinishReason, state.usage)
					for _, e := range events {
						writeClaudeEvent(pw, e)
					}
				}
				continue
			}

			var chunk OpenAIStreamChunk
			if err := json.Unmarshal([]byte(data), &chunk); err != nil {
				continue
			}

			events := translateChunk(state, &chunk)
			for _, e := range events {
				writeClaudeEvent(pw, e)
			}
		}

		if err := scanner.Err(); err != nil {
			slog.With("component", "translate").Error("SSE scanner error", "error", err)
		}

		if state.messageStartSent && !state.stopSent {
			events := buildStopEvents(state, state.pendingFinishReason, state.usage)
			for _, e := range events {
				writeClaudeEvent(pw, e)
			}
		}
	}()

	return pr
}

func translateChunk(state *claudeStreamState, chunk *OpenAIStreamChunk) []ClaudeSSEEvent {
	var events []ClaudeSSEEvent

	if chunk.Usage != nil {
		state.usage = chunk.Usage
		if state.messageStartSent && state.pendingFinishReason != nil && !state.stopSent {
			return buildStopEvents(state, state.pendingFinishReason, state.usage)
		}
	}

	if len(chunk.Choices) == 0 {
		return nil
	}
	choice := chunk.Choices[0]
	delta := choice.Delta

	// ── First chunk: message_start ──
	if !state.messageStartSent {
		state.messageStartSent = true
		state.messageID = chunk.ID
		if state.messageID == "" {
			state.messageID = fmt.Sprintf("msg_%d", time.Now().UnixMilli())
		}
		state.model = chunk.Model
		if state.model == "" {
			state.model = "unknown"
		}

		events = append(events, ClaudeSSEEvent{
			"type": "message_start",
			"message": map[string]interface{}{
				"id":            state.messageID,
				"type":          "message",
				"role":          "assistant",
				"model":         state.model,
				"content":       []interface{}{},
				"stop_reason":   nil,
				"stop_sequence": nil,
				"usage": map[string]interface{}{
					"input_tokens":  0,
					"output_tokens": 0,
				},
			},
		})
	}

	// ── reasoning_content → thinking_delta ──
	rc := delta.ReasoningContent
	if rc == "" {
		rc = delta.Reasoning
	}
	if rc != "" {
		events = append(events, closeTextBlock(state)...)

		if !state.thinkingStarted {
			state.thinkingIndex = state.nextBlockIndex
			state.nextBlockIndex++
			state.thinkingStarted = true
			events = append(events, ClaudeSSEEvent{
				"type":  "content_block_start",
				"index": state.thinkingIndex,
				"content_block": map[string]interface{}{
					"type":     "thinking",
					"thinking": "",
				},
			})
		}

		events = append(events, ClaudeSSEEvent{
			"type":  "content_block_delta",
			"index": state.thinkingIndex,
			"delta": map[string]interface{}{
				"type":     "thinking_delta",
				"thinking": rc,
			},
		})
	}

	// ── delta.content → text_delta ──
	if delta.Content != "" {
		events = append(events, closeThinkingBlock(state)...)

		if !state.textStarted {
			state.textIndex = state.nextBlockIndex
			state.nextBlockIndex++
			state.textStarted = true
			events = append(events, ClaudeSSEEvent{
				"type":  "content_block_start",
				"index": state.textIndex,
				"content_block": map[string]interface{}{
					"type": "text",
					"text": "",
				},
			})
		}

		events = append(events, ClaudeSSEEvent{
			"type":  "content_block_delta",
			"index": state.textIndex,
			"delta": map[string]interface{}{
				"type": "text_delta",
				"text": delta.Content,
			},
		})
	}

	// ── tool_calls ──
	for _, tc := range delta.ToolCalls {
		idx := tc.Index

		if tc.ID != "" {
			events = append(events, closeThinkingBlock(state)...)
			events = append(events, closeTextBlock(state)...)

			blockIdx := state.nextBlockIndex
			state.nextBlockIndex++
			name := ""
			if tc.Function != nil {
				name = tc.Function.Name
			}

			events = append(events, ClaudeSSEEvent{
				"type":  "content_block_start",
				"index": blockIdx,
				"content_block": map[string]interface{}{
					"type":  "tool_use",
					"id":    tc.ID,
					"name":  name,
					"input": map[string]interface{}{},
				},
			})

			state.toolCalls[idx] = toolCallInfo{
				id:         tc.ID,
				name:       name,
				blockIndex: blockIdx,
			}

			// Replay any buffered arguments that arrived before the tool ID
			if buf, hasBuf := state.toolArgBufs[idx]; hasBuf && buf != "" {
				events = append(events, ClaudeSSEEvent{
					"type":  "content_block_delta",
					"index": blockIdx,
					"delta": map[string]interface{}{
						"type":         "input_json_delta",
						"partial_json": buf,
					},
				})
			}
		}

		if tc.Function != nil && tc.Function.Arguments != "" {
			state.toolArgBufs[idx] += tc.Function.Arguments
			info, ok := state.toolCalls[idx]
			if ok {
				events = append(events, ClaudeSSEEvent{
					"type":  "content_block_delta",
					"index": info.blockIndex,
					"delta": map[string]interface{}{
						"type":         "input_json_delta",
						"partial_json": tc.Function.Arguments,
					},
				})
			}
		}
	}

	// ── finish_reason ──
	if choice.FinishReason != nil && *choice.FinishReason != "" && !state.stopSent {
		finishReason := *choice.FinishReason
		state.pendingFinishReason = &finishReason
		if chunk.Usage != nil {
			events = append(events, buildStopEvents(state, state.pendingFinishReason, chunk.Usage)...)
		}
	}

	return events
}

func closeThinkingBlock(state *claudeStreamState) []ClaudeSSEEvent {
	if !state.thinkingStarted {
		return nil
	}
	state.thinkingStarted = false
	return []ClaudeSSEEvent{{
		"type":  "content_block_stop",
		"index": state.thinkingIndex,
	}}
}

func closeTextBlock(state *claudeStreamState) []ClaudeSSEEvent {
	if !state.textStarted {
		return nil
	}
	state.textStarted = false
	return []ClaudeSSEEvent{{
		"type":  "content_block_stop",
		"index": state.textIndex,
	}}
}

func buildStopEvents(state *claudeStreamState, finishReason *string, usage *OpenAIUsage) []ClaudeSSEEvent {
	state.stopSent = true
	var events []ClaudeSSEEvent

	events = append(events, closeThinkingBlock(state)...)
	events = append(events, closeTextBlock(state)...)

	// Close tool blocks — do NOT emit final input_json_delta since incremental
	// chunks are already streamed in translateChunk.
	for _, info := range state.toolCalls {
		events = append(events, ClaudeSSEEvent{
			"type":  "content_block_stop",
			"index": info.blockIndex,
		})
	}

	stopReason := convertFinishReason(finishReason)
	usageObj := map[string]interface{}{
		"output_tokens": 0,
	}
	if usage != nil {
		usageObj["output_tokens"] = usage.CompletionTokens
	}

	events = append(events, ClaudeSSEEvent{
		"type": "message_delta",
		"delta": map[string]interface{}{
			"stop_reason": stopReason,
		},
		"usage": usageObj,
	})
	events = append(events, ClaudeSSEEvent{
		"type": "message_stop",
	})

	return events
}

func convertFinishReason(reason *string) string {
	if reason == nil {
		return "end_turn"
	}
	switch *reason {
	case "stop":
		return "end_turn"
	case "length":
		return "max_tokens"
	case "tool_calls":
		return "tool_use"
	default:
		return "end_turn"
	}
}

func writeClaudeEvent(w *io.PipeWriter, event ClaudeSSEEvent) {
	data, err := json.Marshal(event)
	if err != nil {
		return
	}
	eventType, _ := event["type"].(string)
	if eventType != "" {
		fmt.Fprintf(w, "event: %s\ndata: %s\n\n", eventType, data)
	} else {
		fmt.Fprintf(w, "data: %s\n\n", data)
	}
}

// ClaudeBodyToOpenAI translates a Claude-format request body to OpenAI format.
func ClaudeBodyToOpenAI(body []byte) []byte {
	return ClaudeToOpenAI(body)
}

// OpenAIResponseToClaudeResponse converts a non-streaming OpenAI chat completion
// response to Claude Messages API format.
func OpenAIResponseToClaudeResponse(openaiBody []byte) []byte {
	var resp struct {
		ID      string `json:"id"`
		Object  string `json:"object"`
		Created int64  `json:"created"`
		Model   string `json:"model"`
		Choices []struct {
			Index        int           `json:"index"`
			Message      OpenAIMessage `json:"message"`
			FinishReason string        `json:"finish_reason"`
		} `json:"choices"`
		Usage *OpenAIUsage `json:"usage,omitempty"`
	}
	if err := json.Unmarshal(openaiBody, &resp); err != nil {
		return openaiBody
	}
	if len(resp.Choices) == 0 {
		return openaiBody
	}

	choice := resp.Choices[0]
	msg := choice.Message

	var content []map[string]interface{}

	reasoning := extractOpenAIReasoning(msg)

	if reasoning != "" {
		content = append(content, map[string]interface{}{
			"type":     "thinking",
			"thinking": reasoning,
		})
	}

	text := extractOpenAIText(msg.Content)
	if text != "" {
		content = append(content, map[string]interface{}{
			"type": "text",
			"text": text,
		})
	}

	for _, tc := range msg.ToolCalls {
		if tc.Type == "function" {
			content = append(content, map[string]interface{}{
				"type":  "tool_use",
				"id":    tc.ID,
				"name":  tc.Function.Name,
				"input": parseJSON(tc.Function.Arguments),
			})
		}
	}

	stopReason := "end_turn"
	switch choice.FinishReason {
	case "stop":
		stopReason = "end_turn"
	case "length":
		stopReason = "max_tokens"
	case "tool_calls":
		stopReason = "tool_use"
	}

	usage := map[string]int{"input_tokens": 0, "output_tokens": 0}
	if resp.Usage != nil {
		usage["input_tokens"] = resp.Usage.PromptTokens
		usage["output_tokens"] = resp.Usage.CompletionTokens
	}

	msgID := resp.ID
	if msgID == "" {
		msgID = fmt.Sprintf("msg_%d", time.Now().UnixMilli())
	}

	claudeResp := map[string]interface{}{
		"id":            msgID,
		"type":          "message",
		"role":          "assistant",
		"model":         resp.Model,
		"content":       content,
		"stop_reason":   stopReason,
		"stop_sequence": nil,
		"usage":         usage,
	}

	if len(msg.ToolCalls) == 0 && text == "" && reasoning == "" {
		claudeResp["content"] = []map[string]interface{}{}
	}

	out, _ := json.Marshal(claudeResp)
	return out
}

func extractOpenAIReasoning(msg OpenAIMessage) string {
	if msg.ReasoningContent != "" {
		return msg.ReasoningContent
	}
	if m, ok := msg.Content.(map[string]interface{}); ok {
		if rc, ok := m["reasoning_content"].(string); ok {
			return rc
		}
		if rc, ok := m["reasoning"].(string); ok {
			return rc
		}
	}
	return ""
}

func extractOpenAIText(content interface{}) string {
	switch t := content.(type) {
	case string:
		return t
	case nil:
		return ""
	case map[string]interface{}:
		if text, ok := t["text"].(string); ok {
			return text
		}
		if text, ok := t["content"].(string); ok {
			return text
		}
		filtered := make(map[string]interface{}, len(t))
		for k, v := range t {
			if k == "reasoning_content" || k == "reasoning" {
				continue
			}
			filtered[k] = v
		}
		if len(filtered) == 0 {
			return ""
		}
		b, _ := json.Marshal(filtered)
		return string(b)
	default:
		b, _ := json.Marshal(t)
		return string(b)
	}
}

func parseJSON(raw string) interface{} {
	if raw == "" {
		raw = "{}"
	}
	var v interface{}
	if json.Unmarshal([]byte(raw), &v) == nil {
		return v
	}
	return map[string]interface{}{}
}

// OpenAIErrorToClaudeError translates an OpenAI-format error body
// to Claude Messages API error format.
func OpenAIErrorToClaudeError(openaiBody []byte) map[string]interface{} {
	var openAIErr struct {
		Error struct {
			Message string `json:"message"`
			Type    string `json:"type"`
			Code    string `json:"code,omitempty"`
		} `json:"error"`
	}
	msg := "upstream error"
	errType := "api_error"
	if json.Unmarshal(openaiBody, &openAIErr) == nil && openAIErr.Error.Message != "" {
		msg = openAIErr.Error.Message
		switch openAIErr.Error.Type {
		case "invalid_request_error":
			errType = "invalid_request_error"
		case "authentication_error":
			errType = "authentication_error"
		case "rate_limit_error":
			errType = "rate_limit_error"
		}
	}
	return map[string]interface{}{
		"error": map[string]interface{}{
			"message": msg,
			"type":    errType,
		},
	}
}
