package proxy

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"runtime/debug"
	"strings"
	"time"

	"github.com/lpc0387/hermes-collab-engine/proxy/config"
	"github.com/lpc0387/hermes-collab-engine/proxy/internal/reasoning"
	"github.com/lpc0387/hermes-collab-engine/proxy/internal/translate"
)

type endpointConfig struct {
	upstreamPath string
	needInject   bool
	isClaude     bool
}

var endpoints = map[string]endpointConfig{
	"/v1/chat/completions": {
		upstreamPath: "/zen/go/v1/chat/completions",
		needInject:   true,
	},
	"/v1/messages": {
		upstreamPath: "/zen/go/v1/chat/completions",
		isClaude:     true,
	},
	"/v1/responses": {
		upstreamPath: "/zen/go/v1/responses",
	},
}

type Handler struct {
	cfg      *config.Config
	upstream *http.Client
}

func New(cfg *config.Config) *Handler {
	return &Handler{
		cfg: cfg,
		upstream: &http.Client{
			Timeout: cfg.UpstreamTimeout,
		},
	}
}

// reqLog builds a component+request-id logger from context.
func reqLog(ctx context.Context) *slog.Logger {
	l := slog.With("component", "proxy")
	if id := GetRequestID(ctx); id != "" {
		l = l.With("req_id", id)
	}
	return l
}

// Package-level context key types for request ID propagation.
type ctxKey string

const reqIDKey ctxKey = "request_id"

// WithRequestID returns a context with the request ID attached.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, reqIDKey, id)
}

// GetRequestID extracts the request ID from context, if any.
func GetRequestID(ctx context.Context) string {
	if id, ok := ctx.Value(reqIDKey).(string); ok {
		return id
	}
	return ""
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	log := reqLog(r.Context())

	defer func() {
		if rec := recover(); rec != nil {
			log.Error("panic recovered", "panic", rec, "stack", string(debug.Stack()))
			writeJSONError(w, http.StatusInternalServerError, "internal server error", "server_error")
		}
	}()

	// Set CORS headers on all responses
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key, anthropic-version")

	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}

	if r.URL.Path == "/v1/models" {
		h.handleListModels(w, r)
		return
	}

	ep, ok := endpoints[r.URL.Path]
	if !ok {
		writeJSONError(w, http.StatusNotFound, "unknown endpoint: "+r.URL.Path, "not_found")
		return
	}

	if r.Method != http.MethodPost {
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed", "invalid_request_error")
		return
	}

	if !h.checkAuth(r) {
		log.Warn("authentication failed", "remote", r.RemoteAddr, "path", r.URL.Path)
		writeJSONError(w, http.StatusUnauthorized, "invalid API key", "authentication_error")
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, 10<<20)
	bodyBytes, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, "request too large or cannot read body", "invalid_request_error")
		return
	}
	r.Body.Close()

	model, isStream := parseRequestMeta(bodyBytes)

	if model == "" {
		writeJSONError(w, http.StatusBadRequest, "model field is required", "invalid_request_error")
		return
	}

	aliased := aliasModel(model)
	if aliased != model {
		log.Info("aliased model name", "original", model, "aliased", aliased)
		bodyBytes = replaceModelInBody(bodyBytes, model, aliased)
		model = aliased
	}

	// Claude Code replaces dots with dashes in model names (e.g. glm-5.1 → glm-5-1).
	// Restore them so the upstream recognizes the correct model ID.
	restored := restoreModelDots(model)
	if restored != model {
		log.Info("restored model name", "original", model, "restored", restored)
		bodyBytes = replaceModelInBody(bodyBytes, model, restored)
		model = restored
	}

	if !h.cfg.IsModelAllowed(model) {
		log.Warn("model not allowed", "model", model)
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "model not allowed: " + model})
		return
	}

	log = log.With("model", model, "stream", isStream, "endpoint", r.URL.Path)
	log.Debug("processing request")

	start := time.Now()

	forwardBody := bodyBytes
	if ep.isClaude {
		forwardBody = translate.ClaudeBodyToOpenAI(bodyBytes)
	} else if ep.needInject {
		forwardBody = tryInjectReasoning(model, bodyBytes)
	}

	baseURL := strings.TrimRight(h.cfg.UpstreamBase, "/")
	upstreamURL := baseURL + ep.upstreamPath

	upstreamReq, err := http.NewRequestWithContext(r.Context(), http.MethodPost, upstreamURL, bytes.NewReader(forwardBody))
	if err != nil {
		log.Error("failed to create upstream request", "error", err)
		writeJSONError(w, http.StatusInternalServerError, "failed to create upstream request", "server_error")
		return
	}

	upstreamReq.Header.Set("Content-Type", "application/json")
	upstreamReq.Header.Set("Authorization", "Bearer "+h.cfg.UpstreamToken)
	upstreamReq.Header.Set("x-opencode-client", "desktop")

	resp, err := h.upstream.Do(upstreamReq)
	if err != nil {
		log.Error("upstream request failed", "error", err, "upstream", upstreamURL)
		writeJSONError(w, http.StatusBadGateway, "upstream request failed", "server_error")
		return
	}
	defer resp.Body.Close()

	elapsed := time.Since(start)

	if ep.isClaude {
		if resp.StatusCode != http.StatusOK {
			out, _ := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamBodySize))
			log.Warn("upstream non-200 for claude endpoint", "status", resp.StatusCode)
			writeJSON(w, resp.StatusCode, translate.OpenAIErrorToClaudeError(out))
			return
		}
		if isStream {
			log.Info("upstream", "status", resp.StatusCode, "duration", elapsed.String())
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Cache-Control", "no-cache")
			w.Header().Set("Connection", "keep-alive")
			w.WriteHeader(resp.StatusCode)
			claudeStream := translate.OpenAIStreamToClaudeStream(r.Context(), resp.Body)
			defer claudeStream.Close()
			if _, err := io.Copy(w, claudeStream); err != nil {
				log.Debug("response write error", "error", err)
			}
		} else {
			out, err := readUpstreamBody(resp.Body)
			if err != nil {
				log.Error("failed to read upstream response", "error", err)
				writeJSONError(w, http.StatusBadGateway, "upstream response error", "server_error")
				return
			}
			prompt, comp := extractUsage(out)
			log.With("prompt_tokens", prompt, "completion_tokens", comp).Info("upstream", "status", resp.StatusCode, "duration", elapsed.String())
			claudeResp := translate.OpenAIResponseToClaudeResponse(out)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(resp.StatusCode)
			if _, err := w.Write(claudeResp); err != nil {
				log.Debug("response write error", "error", err)
			}
		}
		return
	}

	if isStream {
		log.Info("upstream", "status", resp.StatusCode, "duration", elapsed.String())
		forwardHeaders(w, resp)
		if w.Header().Get("Content-Type") == "" {
			w.Header().Set("Content-Type", "text/event-stream")
		}
		w.WriteHeader(resp.StatusCode)
		if _, err := io.Copy(w, resp.Body); err != nil {
			log.Debug("response write error", "error", err)
		}
	} else {
		out, err := readUpstreamBody(resp.Body)
		if err != nil {
			log.Error("failed to read upstream response", "error", err)
			writeJSONError(w, http.StatusBadGateway, "upstream response error", "server_error")
			return
		}
		prompt, comp := extractUsage(out)
		log.With("prompt_tokens", prompt, "completion_tokens", comp).Info("upstream", "status", resp.StatusCode, "duration", elapsed.String())
		forwardHeaders(w, resp)
		w.WriteHeader(resp.StatusCode)
		if _, err := w.Write(out); err != nil {
			log.Debug("response write error", "error", err)
		}
	}
}

func parseRequestMeta(body []byte) (model string, stream bool) {
	var raw struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	if json.Unmarshal(body, &raw) == nil {
		return raw.Model, raw.Stream
	}
	return "", false
}

func (h *Handler) checkAuth(r *http.Request) bool {
	if h.cfg.APIKey == "" {
		return true
	}
	key := r.Header.Get("x-api-key")
	if key == "" {
		if auth := r.Header.Get("Authorization"); strings.HasPrefix(auth, "Bearer ") {
			key = strings.TrimPrefix(auth, "Bearer ")
		}
	}
	if key == "" {
		return false
	}
	expected := []byte(h.cfg.APIKey)
	got := []byte(key)
	return subtle.ConstantTimeCompare(expected, got) == 1
}

var allowedHeaders = map[string]bool{
	"Content-Type":      true,
	"Cache-Control":     true,
	"Connection":        true,
	"Transfer-Encoding": true,
	"X-Request-Id":      true,
}

func forwardHeaders(w http.ResponseWriter, resp *http.Response) {
	for k, vv := range resp.Header {
		if !allowedHeaders[k] {
			continue
		}
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
}

func tryInjectReasoning(model string, body []byte) []byte {
	var chatBody reasoning.ChatBody
	if json.Unmarshal(body, &chatBody) != nil {
		return body
	}
	modified := reasoning.InjectReasoningContent(model, &chatBody)
	if modified == nil || modified == &chatBody {
		return body
	}
	modifiedBytes, err := json.Marshal(modified)
	if err != nil {
		return body
	}
	return modifiedBytes
}

const maxUpstreamBodySize = 50 << 20 // 50 MB

// readUpstreamBody reads the upstream response body, limiting to maxUpstreamBodySize.
func readUpstreamBody(body io.ReadCloser) ([]byte, error) {
	limited := io.LimitReader(body, maxUpstreamBodySize+1)
	out, err := io.ReadAll(limited)
	if err != nil {
		return nil, err
	}
	if int64(len(out)) > maxUpstreamBodySize {
		return nil, fmt.Errorf("upstream response exceeds %d bytes", maxUpstreamBodySize)
	}
	return out, nil
}

// extractUsage attempts to parse token usage from an OpenAI response body.
func extractUsage(body []byte) (prompt, completion int) {
	var v struct {
		Usage *struct {
			Prompt     int `json:"prompt_tokens"`
			Completion int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if json.Unmarshal(body, &v) == nil && v.Usage != nil {
		return v.Usage.Prompt, v.Usage.Completion
	}
	return 0, 0
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		slog.With("component", "proxy").Error("write json error", "error", err)
	}
}

func writeJSONError(w http.ResponseWriter, status int, message, errType string) {
	writeJSON(w, status, map[string]interface{}{
		"error": map[string]string{
			"message": message,
			"type":    errType,
		},
	})
}

func (h *Handler) handleListModels(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed", "invalid_request_error")
		return
	}
	if !h.checkAuth(r) {
		writeJSONError(w, http.StatusUnauthorized, "invalid API key", "authentication_error")
		return
	}
	models := h.cfg.AllowedModels
	if len(models) == 0 {
		writeJSON(w, http.StatusOK, map[string]interface{}{
			"object": "list",
			"data":   []interface{}{},
		})
		return
	}
	var data []map[string]interface{}
	for id := range models {
		data = append(data, map[string]interface{}{
			"id":       id,
			"object":   "model",
			"owned_by": "opencode",
		})
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"object": "list",
		"data":   data,
	})
}

// modelAliases maps user-facing model names to backend model IDs.
// Agents (Claude Code, Codex, etc.) send their native model names;
// the proxy translates them to the actual backend model the upstream understands.
//
// Configure via OPCODE_MODEL_ALIASES env var: "src1=dst1,src2=dst2"
// to override or extend the defaults below.
var modelAliases = map[string]string{
	"claude-sonnet-4-20250514":    "deepseek-v4-flash",
	"claude-sonnet-4-20250515":    "deepseek-v4-flash",
	"claude-3-5-sonnet-20241022": "deepseek-v4-flash",
	"claude-3-opus-20240229":     "deepseek-v4-flash",
	"gpt-4o":                     "deepseek-v4-flash",
	"gpt-4o-mini":                "deepseek-v4-flash",
	"gemini-2-5-pro":             "deepseek-v4-flash",
}

// aliasModel applies model aliasing: user-facing name → backend model ID.
// If no alias matches, returns the model unchanged.
func aliasModel(model string) string {
	if aliased, ok := modelAliases[model]; ok {
		return aliased
	}
	return model
}

// knownDottedModels lists model IDs that contain dots in their canonical form.
// Claude Code replaces dots with dashes (e.g. "glm-5.1" → "glm-5-1"), so we
// restore them when the dashed version doesn't exist upstream.
var knownDottedModels = map[string]string{
	"glm-5-1":                "glm-5.1",
	"minimax-m2-5":           "minimax-m2.5",
	"minimax-m2-7":           "minimax-m2.7",
	"mimo-v2-5":              "mimo-v2.5",
	"mimo-v2-5-pro":          "mimo-v2.5-pro",
	"qwen3-5-plus":           "qwen3.5-plus",
	"qwen3-6-plus":           "qwen3.6-plus",
	"qwen3-7-plus":           "qwen3.7-plus",
	"qwen3-7-max":            "qwen3.7-max",
	"kimi-k2-5":              "kimi-k2.5",
	"kimi-k2-6":              "kimi-k2.6",
	"kimi-k2-7-code":         "kimi-k2.7-code",
	"deepseek-v4-flash-free": "deepseek-v4-flash.free",
}

// restoreModelDots tries to find a known dotted model name for a dashed input.
// Falls back to a heuristic: replace the first digit-dash-digit with a dot.
func restoreModelDots(model string) string {
	if mapped, ok := knownDottedModels[model]; ok {
		return mapped
	}
	// Heuristic: digit-dash-digit → digit.dot.digit
	for i := 0; i < len(model)-2; i++ {
		if model[i] >= '0' && model[i] <= '9' && model[i+1] == '-' && model[i+2] >= '0' && model[i+2] <= '9' {
			return model[:i+1] + "." + model[i+2:]
		}
	}
	return model
}

// replaceModelInBody replaces the model field value in the JSON body.
func replaceModelInBody(body []byte, oldModel, newModel string) []byte {
	// Try no-space format first: "model":"oldModel"
	oldStr := `"model":"` + oldModel + `"`
	newStr := `"model":"` + newModel + `"`
	if bytes.Contains(body, []byte(oldStr)) {
		return bytes.Replace(body, []byte(oldStr), []byte(newStr), 1)
	}
	// Try space-after-colon format: "model": "oldModel"
	oldStr = `"model": "` + oldModel + `"`
	newStr = `"model": "` + newModel + `"`
	if bytes.Contains(body, []byte(oldStr)) {
		return bytes.Replace(body, []byte(oldStr), []byte(newStr), 1)
	}
	return body
}
