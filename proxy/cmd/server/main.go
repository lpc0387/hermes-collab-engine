package main

import (
	"bufio"
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/lpc0387/hermes-collab-engine/proxy/config"
	"github.com/lpc0387/hermes-collab-engine/proxy/internal/proxy"
)

func loadEnvFile() {
	f, err := os.Open(".env")
	if err != nil {
		return
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		val := strings.TrimSpace(parts[1])
		// Strip surrounding quotes
		if len(val) > 1 && (val[0] == '"' || val[0] == '\'') && val[0] == val[len(val)-1] {
			val = val[1 : len(val)-1]
		}
		if os.Getenv(key) == "" {
			os.Setenv(key, val)
		}
	}
	if err := scanner.Err(); err != nil {
		slog.Warn("env parse error", "error", err)
	}
}

// loggingMiddleware wraps an http.Handler with request ID and structured logging.
func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID := fmt.Sprintf("req_%d", time.Now().UnixNano())
		ctx := proxy.WithRequestID(r.Context(), reqID)
		r = r.WithContext(ctx)
		w.Header().Set("X-Request-Id", reqID)

		start := time.Now()
		lrw := &loggedResponseWriter{ResponseWriter: w, statusCode: http.StatusOK}
		next.ServeHTTP(lrw, r)
		duration := time.Since(start)

		if r.URL.Path != "/health" {
			slog.Debug("request",
				"req_id", reqID,
				"method", r.Method,
				"path", r.URL.Path,
				"status", lrw.statusCode,
				"duration", duration.String(),
			)
		}
	})
}

type loggedResponseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (lrw *loggedResponseWriter) WriteHeader(code int) {
	lrw.statusCode = code
	lrw.ResponseWriter.WriteHeader(code)
}

func main() {
	loadEnvFile()

	// Configure structured logging
	lvl := slog.LevelInfo
	switch strings.ToLower(os.Getenv("OPCODE_LOG_LEVEL")) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	}
	var handler slog.Handler
	switch strings.ToLower(os.Getenv("OPCODE_LOG_FORMAT")) {
	case "json":
		handler = slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: lvl})
	default:
		handler = slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl})
	}
	slog.SetDefault(slog.New(handler))

	log := slog.With("component", "server")
	cfg := config.Load()

	h := proxy.New(cfg)

	mux := http.NewServeMux()

	mux.Handle("/v1/chat/completions", h)
	mux.Handle("/v1/messages", h)
	mux.Handle("/v1/responses", h)
	mux.Handle("/v1/models", h)

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if _, err := w.Write([]byte(`{"status":"ok"}`)); err != nil {
			slog.With("component", "server").Error("health write error", "error", err)
		}
	})

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           loggingMiddleware(mux),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      0,
		IdleTimeout:       2 * time.Minute,
	}

	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		log.Info("shutting down gracefully...")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := server.Shutdown(ctx); err != nil {
			log.Error("graceful shutdown error", "error", err)
		}
	}()

	authMode := "no authentication"
	if cfg.APIKey != "" {
		authMode = "API key required"
	}

	log.Info("server starting",
		"listen", cfg.ListenAddr,
		"auth", authMode,
		"upstream", cfg.UpstreamBase,
	)

	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Error("server error", "error", err)
		os.Exit(1)
	}
}
