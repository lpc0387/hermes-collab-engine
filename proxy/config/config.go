package config

import (
	"log/slog"
	"os"
	"strings"
	"time"
)

type Config struct {
	ListenAddr string

	APIKey string

	AllowedModels map[string]struct{}

	UpstreamBase  string
	UpstreamToken string

	// UpstreamTimeout is the timeout for upstream HTTP requests.
	// Default: 5 minutes.
	UpstreamTimeout time.Duration
}

func parseModelList(raw string) map[string]struct{} {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	set := make(map[string]struct{})
	for _, part := range strings.Split(raw, ",") {
		m := strings.TrimSpace(part)
		if m != "" {
			set[m] = struct{}{}
		}
	}
	return set
}

func Load() *Config {
	apiKey := os.Getenv("OPCODE_API_KEY")

	listen := os.Getenv("OPCODE_LISTEN")
	if listen == "" {
		listen = ":8080"
	}

	base := os.Getenv("OPCODE_UPSTREAM_BASE")
	if base == "" {
		base = "https://opencode.ai"
	}

	token := os.Getenv("OPCODE_UPSTREAM_TOKEN")
	if token == "" {
		token = "public"
	}

	timeoutStr := os.Getenv("OPCODE_UPSTREAM_TIMEOUT")
	timeout := 5 * time.Minute
	if timeoutStr != "" {
		if d, err := time.ParseDuration(timeoutStr); err == nil {
			timeout = d
		} else {
			slog.With("component", "config").Warn("invalid OPCODE_UPSTREAM_TIMEOUT, using default", "value", timeoutStr, "default", timeout)
		}
	}

	return &Config{
		ListenAddr:      listen,
		APIKey:          apiKey,
		AllowedModels:   parseModelList(os.Getenv("OPCODE_ALLOWED_MODELS")),
		UpstreamBase:    base,
		UpstreamToken:   token,
		UpstreamTimeout: timeout,
	}
}

func (c *Config) IsModelAllowed(model string) bool {
	if len(c.AllowedModels) == 0 {
		return true
	}
	_, ok := c.AllowedModels[model]
	return ok
}
