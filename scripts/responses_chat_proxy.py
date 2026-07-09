#!/usr/bin/env python3
"""Responses API ↔ Chat API proxy with SSE streaming support."""
import json, os, sys, time, logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

UPSTREAM = "http://localhost:18080/v1"
logging.basicConfig(level=logging.INFO, format="[RPC] %(message)s")
log = logging.getLogger("rpc")

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        data = json.loads(body) if body else {}
        
        if self.path in ("/v1/responses", "/v1/responses/create"):
            self._handle_responses(data)
        else:
            self._forward(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json(200, {"data": [{"id": self.path.split("/")[-1], "object": "model"}]})
        else:
            self._forward(b"")

    def _handle_responses(self, data):
        stream = data.get("stream", False)
        model = data.get("model", "deepseek-v4-flash")
        input_data = data.get("input", "")
        instructions = data.get("instructions", "")
        
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        if isinstance(input_data, str):
            messages.append({"role": "user", "content": input_data})
        elif isinstance(input_data, list):
            for item in input_data:
                if isinstance(item, dict):
                    messages.append({"role": item.get("role","user"), "content": item.get("content","")})

        max_tokens = data.get("max_output_tokens", 4096)
        chat_body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens, "stream": stream}).encode()

        try:
            req = Request(f"{UPSTREAM}/chat/completions", data=chat_body,
                headers={"Content-Type": "application/json",
                         "Authorization": self.headers.get("Authorization","")},
                method="POST")
            
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()
                
                resp = urlopen(req, timeout=120)
                rid = None
                buf = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    # Process SSE lines
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.decode().strip()
                        if line.startswith("data: "):
                            d = line[6:]
                            if d == "[DONE]":
                                # Send proper Responses API completion events
                                final = json.dumps({"type": "response.complete", "response": {}})
                                self.wfile.write(f"event: response.done\ndata: {final}\n\n".encode())
                                self.wfile.write(b"data: [DONE]\n\n")
                                break
                            try:
                                chat_evt = json.loads(d)
                                choices = chat_evt.get("choices", [{}])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if not content:
                                        continue
                                    if not rid:
                                        rid = chat_evt.get("id", f"resp_{os.urandom(4).hex()}")
                                    resp_evt = json.dumps({
                                        "type": "response.output_text.delta",
                                        "delta": content,
                                    })
                                    self.wfile.write(f"event: response.output_text.delta\ndata: {resp_evt}\n\n".encode())
                                    self.wfile.flush()
                            except (json.JSONDecodeError, KeyError):
                                pass
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                resp = urlopen(req, timeout=120)
                chat_result = json.loads(resp.read())
                choices = chat_result.get("choices", [])
                content = choices[0].get("message", {}).get("content", "") if choices else ""
                result = {
                    "id": chat_result.get("id", f"resp_{os.urandom(4).hex()}"),
                    "object": "response",
                    "model": model,
                    "output": [{"type": "message", "role": "assistant", "content": content}],
                    "usage": chat_result.get("usage", {}),
                    "status": "completed",
                }
                self._json(200, result)
        except Exception as e:
            self._json(502, {"error": {"type": "upstream", "message": str(e)}})

    def _forward(self, body):
        try:
            req = Request(f"{UPSTREAM}{self.path}", data=body or None,
                headers={k:v for k,v in self.headers.items() if k.lower() not in ("host","content-length")},
                method=self.command)
            resp = urlopen(req, timeout=30)
            self.send_response(resp.status)
            for k,v in resp.headers.items():
                if k.lower() not in ("transfer-encoding","content-encoding","content-length"):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self._json(502, {"error": str(e)})

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        log.info("%s %s", self.command, self.path)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18083
    up = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:18080/v1"
    UPSTREAM = up
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
