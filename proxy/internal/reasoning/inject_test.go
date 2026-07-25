package reasoning

import (
	"testing"
)

func TestHasPrefixFold(t *testing.T) {
	tests := []struct {
		s, prefix string
		want      bool
	}{
		{"kimi-v2", "kimi-", true},
		{"Kimi-v2", "kimi-", true},
		{"KIMI-v2", "kimi-", true},
		{"gpt-4", "kimi-", false},
		{"ki", "kimi-", false},
		{"", "kimi-", false},
	}
	for _, tc := range tests {
		t.Run(tc.s+"/"+tc.prefix, func(t *testing.T) {
			if got := hasPrefixFold(tc.s, tc.prefix); got != tc.want {
				t.Errorf("hasPrefixFold(%q, %q) = %v, want %v", tc.s, tc.prefix, got, tc.want)
			}
		})
	}
}

func TestContainsFold(t *testing.T) {
	tests := []struct {
		s, substr string
		want      bool
	}{
		{"deepseek-v2", "deepseek", true},
		{"DeepSeek-v2", "deepseek", true},
		{"DEEPSEEK-v2", "deepseek", true},
		{"my-deepseek-model", "deepseek", true},
		{"gpt-4", "deepseek", false},
		{"", "deepseek", false},
	}
	for _, tc := range tests {
		t.Run(tc.s+"/"+tc.substr, func(t *testing.T) {
			if got := containsFold(tc.s, tc.substr); got != tc.want {
				t.Errorf("containsFold(%q, %q) = %v, want %v", tc.s, tc.substr, got, tc.want)
			}
		})
	}
}

func TestShouldInject(t *testing.T) {
	tests := []struct {
		name  string
		role  string
		rc    interface{}
		tc    interface{}
		scope string
		want  bool
	}{
		{"assistant no rc scope all", "assistant", "", nil, "all", true},
		{"assistant has rc scope all", "assistant", " ", nil, "all", false},
		{"user role", "user", "", nil, "all", false},
		{"tool role", "tool", "", nil, "all", false},
		{"assistant no rc scope toolCalls no tc", "assistant", "", []interface{}{}, "toolCalls", false},
		{"assistant no rc scope toolCalls has tc", "assistant", "", []interface{}{map[string]interface{}{}}, "toolCalls", true},
		{"non-empty rc string", "assistant", "some", nil, "all", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := shouldInject(tc.role, tc.rc, tc.tc, tc.scope); got != tc.want {
				t.Errorf("shouldInject(%q, %v, %v, %q) = %v, want %v", tc.role, tc.rc, tc.tc, tc.scope, got, tc.want)
			}
		})
	}
}

func TestInjectReasoningContent(t *testing.T) {
	// Nil body
	if got := InjectReasoningContent("deepseek-v2", nil); got != nil {
		t.Error("InjectReasoningContent(nil) should return nil")
	}

	// Empty messages
	body := &ChatBody{Model: "deepseek-v2"}
	got := InjectReasoningContent("deepseek-v2", body)
	if got != nil && len(got.Messages) != 0 {
		t.Error("empty messages should stay empty")
	}

	// Non-matching model — returned unchanged (same pointer)
	body = &ChatBody{
		Model: "gpt-4",
		Messages: []Message{
			{Role: "assistant", Content: "hello"},
		},
	}
	got = InjectReasoningContent("gpt-4", body)
	if got != body {
		t.Error("non-matching model should return the same pointer")
	}

	// Matching deepseek model — all assistant messages get injection
	body = &ChatBody{
		Model: "deepseek-v2",
		Messages: []Message{
			{Role: "user", Content: "hi"},
			{Role: "assistant", Content: "hello"},
			{Role: "user", Content: "again"},
			{Role: "assistant", Content: "world"},
		},
	}
	got = InjectReasoningContent("deepseek-v2", body)
	if got == nil {
		t.Fatal("deepseek model should inject")
	}
	if got.Messages[1].ReasoningContent != " " {
		t.Errorf("assistant message 1 should have reasoning_content, got %v", got.Messages[1].ReasoningContent)
	}
	if got.Messages[3].ReasoningContent != " " {
		t.Errorf("assistant message 3 should have reasoning_content, got %v", got.Messages[3].ReasoningContent)
	}
	if got.Messages[0].ReasoningContent != nil {
		t.Errorf("user message should not have reasoning_content")
	}

	// Matching kimi model — only tool-call assistant messages
	body = &ChatBody{
		Model: "kimi-k2",
		Messages: []Message{
			{Role: "assistant", Content: "plain"},
			{Role: "assistant", Content: "with tools", ToolCalls: []interface{}{map[string]interface{}{"id": "call_1"}}},
		},
	}
	got = InjectReasoningContent("kimi-k2", body)
	if got == nil {
		t.Fatal("kimi model should inject")
	}
	if got.Messages[0].ReasoningContent != nil {
		t.Errorf("kimi plain assistant should NOT have reasoning_content")
	}
	if got.Messages[1].ReasoningContent != " " {
		t.Errorf("kimi tool-call assistant should have reasoning_content")
	}

	// Original body should not be mutated
	if body.Messages[0].ReasoningContent != nil {
		t.Error("original body should not be mutated")
	}
}
