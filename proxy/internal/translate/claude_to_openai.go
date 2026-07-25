package translate

import "encoding/json"

type ClaudeRequest struct {
	Model         string          `json:"model"`
	Messages      []ClaudeMessage `json:"messages"`
	System        json.RawMessage `json:"system,omitempty"`
	MaxTokens     int             `json:"max_tokens"`
	Stream        bool            `json:"stream,omitempty"`
	Temperature   *float64        `json:"temperature,omitempty"`
	Tools         []ClaudeTool    `json:"tools,omitempty"`
	ToolChoice    json.RawMessage `json:"tool_choice,omitempty"`
	StopSequences []string        `json:"stop_sequences,omitempty"`
	Metadata      json.RawMessage `json:"metadata,omitempty"`
	Thinking      json.RawMessage `json:"thinking,omitempty"`
}

type ClaudeMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

type ContentBlock struct {
	Type       string          `json:"type"`
	Text       string          `json:"text,omitempty"`
	Content    json.RawMessage `json:"content,omitempty"`
	ID         string          `json:"id,omitempty"`
	Name       string          `json:"name,omitempty"`
	Input      json.RawMessage `json:"input,omitempty"`
	ToolUseID  string          `json:"tool_use_id,omitempty"`
	ToolCallID string          `json:"tool_call_id,omitempty"`
	Source     *ImageSource    `json:"source,omitempty"`
	Thinking   string          `json:"thinking,omitempty"`
	Signature  string          `json:"signature,omitempty"`
}

type ImageSource struct {
	Type      string `json:"type"`
	MediaType string `json:"media_type"`
	Data      string `json:"data"`
	URL       string `json:"url"`
}

type ClaudeTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	InputSchema json.RawMessage `json:"input_schema"`
}

type OpenAIChatRequest struct {
	Model         string               `json:"model"`
	Messages      []OpenAIMessage      `json:"messages"`
	Stream        bool                 `json:"stream,omitempty"`
	StreamOptions *OpenAIStreamOptions `json:"stream_options,omitempty"`
	MaxTokens     int                  `json:"max_tokens,omitempty"`
	Temperature   *float64             `json:"temperature,omitempty"`
	Tools         []OpenAITool         `json:"tools,omitempty"`
	ToolChoice    json.RawMessage      `json:"tool_choice,omitempty"`
	Stop          interface{}          `json:"stop,omitempty"`
}

type OpenAIStreamOptions struct {
	IncludeUsage bool `json:"include_usage"`
}

type OpenAIMessage struct {
	Role             string           `json:"role"`
	Content          interface{}      `json:"content"`
	ReasoningContent string           `json:"reasoning_content,omitempty"`
	ToolCalls        []OpenAIToolCall `json:"tool_calls,omitempty"`
	ToolCallID       string           `json:"tool_call_id,omitempty"`
}

type ContentPart struct {
	Type     string    `json:"type"`
	Text     string    `json:"text,omitempty"`
	ImageURL *ImageURL `json:"image_url,omitempty"`
}

type ImageURL struct {
	URL string `json:"url"`
}

type OpenAIToolCall struct {
	ID       string         `json:"id"`
	Type     string         `json:"type"`
	Function OpenAIFunction `json:"function"`
}

type OpenAIFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type OpenAITool struct {
	Type     string         `json:"type"`
	Function OpenAIToolFunc `json:"function"`
}

type OpenAIToolFunc struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	Parameters  json.RawMessage `json:"parameters"`
}

func ClaudeToOpenAI(body []byte) []byte {
	var cr ClaudeRequest
	if err := json.Unmarshal(body, &cr); err != nil {
		return body
	}

	result := OpenAIChatRequest{
		Model:       cr.Model,
		Stream:      cr.Stream,
		MaxTokens:   cr.MaxTokens,
		Temperature: cr.Temperature,
	}
	if cr.Stream {
		result.StreamOptions = &OpenAIStreamOptions{IncludeUsage: true}
	}

	// System message
	if cr.System != nil {
		sysContent := extractSystemText(cr.System)
		if sysContent != "" {
			result.Messages = append(result.Messages, OpenAIMessage{
				Role:    "system",
				Content: sysContent,
			})
		}
	}

	// Convert messages
	for _, msg := range cr.Messages {
		converted := convertClaudeMessage(msg)
		if converted != nil {
			result.Messages = append(result.Messages, converted...)
		}
	}

	// Fix missing tool responses — OpenAI requires every tool_call to have a response
	result.Messages = fixMissingToolResponses(result.Messages)

	// Tools
	for _, t := range cr.Tools {
		result.Tools = append(result.Tools, OpenAITool{
			Type: "function",
			Function: OpenAIToolFunc{
				Name:        t.Name,
				Description: t.Description,
				Parameters:  t.InputSchema,
			},
		})
	}

	// Tool choice — translate Claude format to OpenAI format
	if cr.ToolChoice != nil {
		var tc struct {
			Type string `json:"type"`
			Name string `json:"name,omitempty"`
		}
		if json.Unmarshal(cr.ToolChoice, &tc) == nil {
			switch tc.Type {
			case "auto":
				result.ToolChoice = json.RawMessage(`"auto"`)
			case "any":
				result.ToolChoice = json.RawMessage(`"required"`)
			case "tool":
				mapped, _ := json.Marshal(map[string]interface{}{
					"type": "function",
					"function": map[string]string{
						"name": tc.Name,
					},
				})
				result.ToolChoice = mapped
			default:
				result.ToolChoice = cr.ToolChoice
			}
		} else {
			result.ToolChoice = cr.ToolChoice
		}
	}

	// Stop sequences
	if len(cr.StopSequences) > 0 {
		result.Stop = cr.StopSequences
	}

	out, _ := json.Marshal(result)
	return out
}

func extractSystemText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var blocks []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		var out string
		for _, b := range blocks {
			out += b.Text
		}
		return out
	}
	return ""
}

// convertClaudeMessage mirrors 9router's convertClaudeMessage exactly.
// Returns nil when the message should be skipped.
func convertClaudeMessage(msg ClaudeMessage) []OpenAIMessage {
	role := msg.Role
	if role == "tool" {
		role = "user"
	}

	// Simple string content
	var contentStr string
	if json.Unmarshal(msg.Content, &contentStr) == nil {
		return []OpenAIMessage{{Role: role, Content: contentStr}}
	}

	// Array content
	var blocks []ContentBlock
	if json.Unmarshal(msg.Content, &blocks) != nil {
		return nil
	}

	var parts []ContentPart
	var toolCalls []OpenAIToolCall
	var toolResults []OpenAIMessage

	for _, block := range blocks {
		switch block.Type {
		case "text":
			parts = append(parts, ContentPart{Type: "text", Text: block.Text})

		case "image":
			if block.Source != nil && block.Source.Type == "base64" {
				parts = append(parts, ContentPart{
					Type: "image_url",
					ImageURL: &ImageURL{
						URL: "data:" + block.Source.MediaType + ";base64," + block.Source.Data,
					},
				})
			} else if block.Source != nil && block.Source.Type == "url" && block.Source.URL != "" {
				parts = append(parts, ContentPart{
					Type: "image_url",
					ImageURL: &ImageURL{
						URL: block.Source.URL,
					},
				})
			}

		case "tool_use":
			args := "{}"
			if block.Input != nil {
				if b, err := json.Marshal(block.Input); err == nil {
					args = string(b)
				}
			}
			toolCalls = append(toolCalls, OpenAIToolCall{
				ID:   block.ID,
				Type: "function",
				Function: OpenAIFunction{
					Name:      block.Name,
					Arguments: args,
				},
			})

		case "tool_result":
			content := extractToolResultContent(block)
			toolCallID := extractToolResultID(block)
			if toolCallID != "" {
				toolResults = append(toolResults, OpenAIMessage{
					Role:       "tool",
					ToolCallID: toolCallID,
					Content:    content,
				})
			} else {
				parts = append(parts, ContentPart{Type: "text", Text: content})
			}
		}
	}

	// If has tool results, return array of tool messages (+ user text if any)
	if len(toolResults) > 0 {
		if len(parts) > 0 {
			textContent := joinParts(parts)
			return append(toolResults, OpenAIMessage{Role: "user", Content: textContent})
		}
		return toolResults
	}

	// If has tool calls, return assistant message with tool_calls
	if len(toolCalls) > 0 {
		msg := OpenAIMessage{Role: "assistant"}
		if len(parts) > 0 {
			msg.Content = joinParts(parts)
		}
		msg.ToolCalls = toolCalls
		return []OpenAIMessage{msg}
	}

	// Return content
	if len(parts) > 0 {
		return []OpenAIMessage{{Role: role, Content: joinParts(parts)}}
	}

	// Empty content array
	if len(blocks) == 0 {
		return []OpenAIMessage{{Role: role, Content: ""}}
	}

	return nil
}

func extractToolResultID(b ContentBlock) string {
	if b.ToolUseID != "" {
		return b.ToolUseID
	}
	if b.ToolCallID != "" {
		return b.ToolCallID
	}
	return b.ID
}

func joinParts(parts []ContentPart) interface{} {
	if len(parts) == 1 && parts[0].Type == "text" {
		return parts[0].Text
	}
	return parts
}

func extractToolResultContent(b ContentBlock) string {
	// tool_result uses the "content" field (not "input"), which can be
	// a string or an array of content blocks.
	src := b.Content
	if len(src) == 0 {
		// fallback for non-standard clients that put it in "input"
		src = b.Input
	}
	if len(src) == 0 {
		return ""
	}
	// Plain string
	var s string
	if json.Unmarshal(src, &s) == nil {
		return s
	}
	// Array of content blocks
	var inner []ContentBlock
	if json.Unmarshal(src, &inner) == nil {
		var out []string
		for _, ib := range inner {
			if ib.Type == "text" {
				out = append(out, ib.Text)
			}
		}
		if len(out) > 0 {
			result := ""
			for i, s := range out {
				if i > 0 {
					result += "\n"
				}
				result += s
			}
			return result
		}
		return ""
	}
	// Fallback JSON
	jsonBytes, _ := json.Marshal(src)
	return string(jsonBytes)
}

// fixMissingToolResponses mirrors 9router's fixMissingToolResponses exactly.
// For each assistant message with tool_calls, check that immediately following
// tool messages cover all tool_call_ids, and inject empty responses for any missing.
func fixMissingToolResponses(messages []OpenAIMessage) []OpenAIMessage {
	for i := 0; i < len(messages); i++ {
		msg := messages[i]
		if msg.Role != "assistant" || len(msg.ToolCalls) == 0 {
			continue
		}

		// Collect all tool_call_ids from this assistant message
		toolCallIDs := make([]string, len(msg.ToolCalls))
		for j, tc := range msg.ToolCalls {
			toolCallIDs[j] = tc.ID
		}

		// Collect tool response IDs that immediately follow
		respondedIDs := make(map[string]bool)
		insertPos := i + 1
		for j := i + 1; j < len(messages); j++ {
			next := messages[j]
			if next.Role == "tool" && next.ToolCallID != "" {
				respondedIDs[next.ToolCallID] = true
				insertPos = j + 1
			} else {
				break
			}
		}

		// Find missing IDs
		var missingIDs []string
		for _, id := range toolCallIDs {
			if !respondedIDs[id] {
				missingIDs = append(missingIDs, id)
			}
		}

		if len(missingIDs) > 0 {
			missing := make([]OpenAIMessage, len(missingIDs))
			for j, id := range missingIDs {
				missing[j] = OpenAIMessage{
					Role:       "tool",
					ToolCallID: id,
					Content:    "[No response received]",
				}
			}
			// Insert at insertPos
			messages = append(messages[:insertPos], append(missing, messages[insertPos:]...)...)
			i = insertPos + len(missingIDs) - 1
		}
	}
	return messages
}
