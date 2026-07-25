package reasoning

import "strings"

// Models that need reasoning_content injected into assistant messages.
// These providers (DeepSeek, Kimi, etc.) require a non-empty placeholder
// on assistant messages or they reject the request.
var modelRules = []struct {
	match func(string) bool
	scope string // "all" or "toolCalls"
}{
	{match: func(m string) bool { return hasPrefixFold(m, "kimi-") }, scope: "toolCalls"},
	{match: func(m string) bool { return containsFold(m, "deepseek") }, scope: "all"},
}

func hasPrefixFold(s, prefix string) bool {
	return len(s) >= len(prefix) && strings.EqualFold(s[:len(prefix)], prefix)
}

func containsFold(s, substr string) bool {
	s, substr = strings.ToLower(s), strings.ToLower(substr)
	return strings.Contains(s, substr)
}

// shouldInject checks if an assistant message needs a reasoning_content placeholder.
func shouldInject(role string, rc interface{}, toolCalls interface{}, scope string) bool {
	if role != "assistant" {
		return false
	}
	// Already has reasoning_content
	if s, ok := rc.(string); ok && len(s) > 0 {
		return false
	}
	if scope == "toolCalls" {
		tc, ok := toolCalls.([]interface{})
		return ok && len(tc) > 0
	}
	return true
}

// Message represents a single chat message in OpenAI format.
type Message struct {
	Role             string      `json:"role"`
	Content          interface{} `json:"content"`
	ReasoningContent interface{} `json:"reasoning_content,omitempty"`
	ToolCalls        interface{} `json:"tool_calls,omitempty"`
}

// ChatBody is the OpenAI chat completion request body.
type ChatBody struct {
	Model    string    `json:"model"`
	Messages []Message `json:"messages"`
	Stream   *bool     `json:"stream,omitempty"`
	// passthrough fields
	MaxTokens           *int                   `json:"max_tokens,omitempty"`
	MaxCompletionTokens *int                   `json:"max_completion_tokens,omitempty"`
	Temperature         *float64               `json:"temperature,omitempty"`
	TopP                *float64               `json:"top_p,omitempty"`
	Tools               []interface{}          `json:"tools,omitempty"`
	ToolChoice          interface{}            `json:"tool_choice,omitempty"`
	Stop                interface{}            `json:"stop,omitempty"`
	ExtraBody           map[string]interface{} `json:"extra_body,omitempty"`
	ReasoningEffort     interface{}            `json:"reasoning_effort,omitempty"`
}

// InjectReasoningContent adds a reasoning_content placeholder to assistant
// messages when the model requires it (DeepSeek, Kimi, etc.).
func InjectReasoningContent(model string, body *ChatBody) *ChatBody {
	if body == nil || len(body.Messages) == 0 {
		return body
	}

	// Find matching rule
	var rule *struct {
		match func(string) bool
		scope string
	}
	for _, r := range modelRules {
		if r.match(model) {
			rule = &r
			break
		}
	}
	if rule == nil {
		return body
	}

	msgs := make([]Message, len(body.Messages))
	for i, m := range body.Messages {
		msgs[i] = m
		if shouldInject(m.Role, m.ReasoningContent, m.ToolCalls, rule.scope) {
			msgs[i].ReasoningContent = " "
		}
	}

	return &ChatBody{
		Model:               body.Model,
		Messages:            msgs,
		Stream:              body.Stream,
		MaxTokens:           body.MaxTokens,
		MaxCompletionTokens: body.MaxCompletionTokens,
		Temperature:         body.Temperature,
		TopP:                body.TopP,
		Tools:               body.Tools,
		ToolChoice:          body.ToolChoice,
		Stop:                body.Stop,
		ExtraBody:           body.ExtraBody,
		ReasoningEffort:     body.ReasoningEffort,
	}
}
