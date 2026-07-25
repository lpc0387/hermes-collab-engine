package proxy

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/lpc0387/hermes-collab-engine/proxy/config"
)

func newTestConfig(opts ...func(*config.Config)) *config.Config {
	cfg := &config.Config{
		ListenAddr:      ":8080",
		UpstreamBase:    "",
		UpstreamToken:   "public",
		UpstreamTimeout: 5 * time.Minute,
	}
	for _, opt := range opts {
		opt(cfg)
	}
	return cfg
}

func withAPIKey(key string) func(*config.Config) {
	return func(c *config.Config) { c.APIKey = key }
}

func withAllowedModels(models ...string) func(*config.Config) {
	return func(c *config.Config) {
		m := make(map[string]struct{})
		for _, mod := range models {
			m[mod] = struct{}{}
		}
		c.AllowedModels = m
	}
}
func TestCORSPreflight(t *testing.T) {
	h := New(newTestConfig())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodOptions, "/v1/chat/completions", nil)

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Errorf("status = %d, want 204", rec.Code)
	}
	if rec.Header().Get("Access-Control-Allow-Origin") != "*" {
		t.Error("missing CORS origin header")
	}
	if rec.Header().Get("Access-Control-Allow-Methods") == "" {
		t.Error("missing CORS methods header")
	}
}

func TestCORSOnAPIResponse(t *testing.T) {
	h := New(newTestConfig())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)

	h.ServeHTTP(rec, req)

	// CORS as a handler-level header is set by middleware in main.go;
	// the handler itself only sets Content-Type.
	if rec.Header().Get("Content-Type") != "application/json" {
		t.Error("expected JSON content type")
	}
}

// Test /v1/models

func TestListModels(t *testing.T) {
	h := New(newTestConfig(withAllowedModels("deepseek-v4", "gpt-4")))
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var resp struct {
		Data []map[string]interface{} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Data) != 2 {
		t.Errorf("expected 2 models, got %d", len(resp.Data))
	}
}

func TestListModelsWithAuth(t *testing.T) {
	h := New(newTestConfig(withAPIKey("secret")))
	rec := httptest.NewRecorder()

	// No API key → 401
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("no auth: status = %d, want 401", rec.Code)
	}

	// With valid API key via x-api-key → 200
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("x-api-key", "secret")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Errorf("x-api-key: status = %d, want 200", rec.Code)
	}

	// With valid API key via Authorization: Bearer → 200
	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	req.Header.Set("Authorization", "Bearer secret")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Errorf("Authorization: Bearer: status = %d, want 200", rec.Code)
	}
}

// Test authentication on proxy endpoints

func TestAuth(t *testing.T) {
	h := New(newTestConfig(withAPIKey("secret")))
	body := `{"model":"test","messages":[{"role":"user","content":"hi"}]}`

	tests := []struct {
		name   string
		req    *http.Request
		status int
	}{
		{"no key", httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)), 401},
		{"wrong key", func() *http.Request {
			r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
			r.Header.Set("x-api-key", "wrong")
			return r
		}(), 401},
		{"x-api-key", func() *http.Request {
			r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
			r.Header.Set("x-api-key", "secret")
			return r
		}(), 502}, // 502 because no upstream configured
		{"Bearer", func() *http.Request {
			r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
			r.Header.Set("Authorization", "Bearer secret")
			return r
		}(), 502},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, tc.req)
			if rec.Code != tc.status {
				t.Errorf("status = %d, want %d. Body: %s", rec.Code, tc.status, rec.Body.String())
			}
		})
	}
}

// Test model allowlist

func TestModelAllowlist(t *testing.T) {
	h := New(newTestConfig(withAllowedModels("deepseek-v4")))
	body := func(model string) string {
		return fmt.Sprintf(`{"model":"%s","messages":[{"role":"user","content":"hi"}]}`, model)
	}

	tests := []struct {
		name   string
		body   string
		status int
	}{
		{"allowed", body("deepseek-v4"), 502}, // 502 because no upstream
		{"disallowed", body("gpt-4"), 403},
	}

	// Need auth since auth happens before model check
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(tc.body))
			h.ServeHTTP(rec, req)
			if rec.Code != tc.status {
				t.Errorf("status = %d, want %d", rec.Code, tc.status)
			}
		})
	}
}

func TestEmptyModel(t *testing.T) {
	h := New(newTestConfig())
	body := `{"messages":[{"role":"user","content":"hi"}]}`

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	h.ServeHTTP(rec, req)

	if rec.Code != 400 {
		t.Errorf("empty model: status = %d, want 400. Body: %s", rec.Code, rec.Body.String())
	}
}

// Test routing

func TestUnknownEndpoint(t *testing.T) {
	h := New(newTestConfig())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/unknown", nil)

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", rec.Code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := New(newTestConfig())
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/chat/completions", nil)

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("status = %d, want 405", rec.Code)
	}
}

// Test streaming detection and upstream behavior

func TestStreamingPassthrough(t *testing.T) {
	// Mock upstream that returns a streaming response
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: {}\n\ndata: [DONE]\n\n"))
	}))
	defer upstream.Close()

	cfg := newTestConfig()
	cfg.UpstreamBase = upstream.URL

	h := New(cfg)
	body := `{"model":"deepseek-v4","stream":true,"messages":[{"role":"user","content":"hi"}]}`

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200. Body: %s", rec.Code, rec.Body.String())
	}

	// Check streaming content type
	ct := rec.Header().Get("Content-Type")
	if !strings.Contains(ct, "text/event-stream") {
		t.Errorf("Content-Type = %q, should contain text/event-stream", ct)
	}
}

func TestNonStreamingPassthrough(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"id":"123","choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}]}`))
	}))
	defer upstream.Close()

	cfg := newTestConfig()
	cfg.UpstreamBase = upstream.URL

	h := New(cfg)
	body := `{"model":"deepseek-v4","messages":[{"role":"user","content":"hi"}]}`

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "hello") {
		t.Errorf("body should contain response: %s", rec.Body.String())
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

type failingReadCloser struct{}

func (failingReadCloser) Read([]byte) (int, error) {
	return 0, fmt.Errorf("read failed")
}

func (failingReadCloser) Close() error {
	return nil
}

func TestNonStreamingReadErrorReturnsBadGateway(t *testing.T) {
	cfg := newTestConfig()
	cfg.UpstreamBase = "http://upstream.test"
	h := New(cfg)
	h.upstream = &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body:       failingReadCloser{},
			}, nil
		}),
	}

	body := `{"model":"deepseek-v4","messages":[{"role":"user","content":"hi"}]}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Errorf("status = %d, want 502. Body: %s", rec.Code, rec.Body.String())
	}
}

func TestClaudeStreaming(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var upstreamReq struct {
			StreamOptions *struct {
				IncludeUsage bool `json:"include_usage"`
			} `json:"stream_options"`
		}
		if err := json.NewDecoder(r.Body).Decode(&upstreamReq); err != nil {
			t.Errorf("decode upstream request: %v", err)
		}
		if upstreamReq.StreamOptions == nil || !upstreamReq.StreamOptions.IncludeUsage {
			t.Errorf("stream_options.include_usage should be true: %+v", upstreamReq.StreamOptions)
		}

		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: {\"id\":\"1\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hello\"},\"finish_reason\":null}]}\n\ndata: {\"id\":\"1\",\"object\":\"chat.completion.chunk\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: {\"id\":\"1\",\"object\":\"chat.completion.chunk\",\"choices\":[],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5}}\n\ndata: [DONE]\n\n"))
	}))
	defer upstream.Close()

	cfg := newTestConfig()
	cfg.UpstreamBase = upstream.URL

	h := New(cfg)
	body := `{"model":"deepseek-v4","stream":true,"messages":[{"role":"user","content":"hi"}],"max_tokens":100}`

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/messages", strings.NewReader(body))
	req.Header.Set("x-api-key", "test")
	req.Header.Set("anthropic-version", "2023-06-01")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200. Body: %s", rec.Code, rec.Body.String())
	}

	// Should get Claude-format SSE events
	bodyStr := rec.Body.String()
	if !strings.Contains(bodyStr, "event: message_start") {
		t.Error("expected Claude message_start event")
	}
	if !strings.Contains(bodyStr, "event: message_stop") {
		t.Error("expected Claude message_stop event")
	}
	if !strings.Contains(bodyStr, `"output_tokens":5`) {
		t.Errorf("expected output_tokens in Claude stream, got: %s", bodyStr)
	}
}

func TestParseRequestMeta(t *testing.T) {
	tests := []struct {
		name       string
		body       string
		wantModel  string
		wantStream bool
	}{
		{"basic", `{"model":"deepseek-v4","stream":true}`, "deepseek-v4", true},
		{"no stream", `{"model":"gpt-4"}`, "gpt-4", false},
		{"empty", `{}`, "", false},
		{"invalid json", `not json`, "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			model, stream := parseRequestMeta([]byte(tc.body))
			if model != tc.wantModel {
				t.Errorf("model = %q, want %q", model, tc.wantModel)
			}
			if stream != tc.wantStream {
				t.Errorf("stream = %v, want %v", stream, tc.wantStream)
			}
		})
	}
}

func TestWriteJSONError(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSONError(rec, http.StatusBadRequest, "bad stuff", "invalid_request_error")

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d", rec.Code)
	}
	if rec.Header().Get("Content-Type") != "application/json" {
		t.Error("missing Content-Type")
	}
	var resp struct {
		Error struct {
			Message string `json:"message"`
			Type    string `json:"type"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Error.Message != "bad stuff" || resp.Error.Type != "invalid_request_error" {
		t.Errorf("error body: %+v", resp.Error)
	}
}
