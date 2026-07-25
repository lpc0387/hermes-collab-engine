package config

import (
	"os"
	"testing"
)

func TestParseModelList(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  int
	}{
		{"empty string", "", 0},
		{"single model", "deepseek-v4", 1},
		{"multiple models", "deepseek-v4,gpt-4,claude-3", 3},
		{"with whitespace", " deepseek-v4 , gpt-4 ", 2},
		{"empty entries", "deepseek-v4,,gpt-4,", 2},
		{"only commas", ",,,", 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := parseModelList(tc.input)
			if tc.want == 0 {
				if got == nil {
					return
				}
				if len(got) != 0 {
					t.Errorf("parseModelList(%q) = %v (len=%d), want nil or empty map", tc.input, got, len(got))
				}
				return
			}
			if len(got) != tc.want {
				t.Errorf("parseModelList(%q) = %v, want %d entries", tc.input, got, tc.want)
			}
		})
	}
}

func TestIsModelAllowed(t *testing.T) {
	tests := []struct {
		name    string
		models  map[string]struct{}
		model   string
		allowed bool
	}{
		{"nil map", nil, "anything", true},
		{"empty map", map[string]struct{}{}, "anything", true},
		{"model in list", map[string]struct{}{"deepseek-v4": {}}, "deepseek-v4", true},
		{"model not in list", map[string]struct{}{"gpt-4": {}}, "deepseek-v4", false},
		{"case sensitive", map[string]struct{}{"DeepSeek-v4": {}}, "deepseek-v4", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c := &Config{AllowedModels: tc.models}
			if got := c.IsModelAllowed(tc.model); got != tc.allowed {
				t.Errorf("IsModelAllowed(%q) = %v, want %v", tc.model, got, tc.allowed)
			}
		})
	}
}

func TestLoadDefaults(t *testing.T) {
	// Ensure env vars are clean
	os.Unsetenv("OPCODE_API_KEY")
	os.Unsetenv("OPCODE_LISTEN")
	os.Unsetenv("OPCODE_UPSTREAM_BASE")
	os.Unsetenv("OPCODE_UPSTREAM_TOKEN")
	os.Unsetenv("OPCODE_ALLOWED_MODELS")
	os.Unsetenv("OPCODE_UPSTREAM_TIMEOUT")

	c := Load()
	if c.ListenAddr != ":8080" {
		t.Errorf("ListenAddr = %q, want :8080", c.ListenAddr)
	}
	if c.UpstreamBase != "https://opencode.ai" {
		t.Errorf("UpstreamBase = %q, want https://opencode.ai", c.UpstreamBase)
	}
	if c.UpstreamToken != "public" {
		t.Errorf("UpstreamToken = %q, want public", c.UpstreamToken)
	}
	if c.APIKey != "" {
		t.Errorf("APIKey = %q, want empty", c.APIKey)
	}
	if c.AllowedModels != nil {
		t.Errorf("AllowedModels = %v, want nil", c.AllowedModels)
	}
	if c.UpstreamTimeout == 0 {
		t.Error("UpstreamTimeout should not be zero")
	}
}

func TestLoadEnvOverride(t *testing.T) {
	os.Setenv("OPCODE_LISTEN", ":9999")
	os.Setenv("OPCODE_API_KEY", "test-key")
	os.Setenv("OPCODE_ALLOWED_MODELS", "a,b")
	os.Setenv("OPCODE_UPSTREAM_TIMEOUT", "30s")
	defer func() {
		os.Unsetenv("OPCODE_LISTEN")
		os.Unsetenv("OPCODE_API_KEY")
		os.Unsetenv("OPCODE_ALLOWED_MODELS")
		os.Unsetenv("OPCODE_UPSTREAM_TIMEOUT")
	}()

	c := Load()
	if c.ListenAddr != ":9999" {
		t.Errorf("ListenAddr = %q, want :9999", c.ListenAddr)
	}
	if c.APIKey != "test-key" {
		t.Errorf("APIKey = %q, want test-key", c.APIKey)
	}
	if len(c.AllowedModels) != 2 {
		t.Errorf("AllowedModels = %v, want 2 entries", c.AllowedModels)
	}
}
