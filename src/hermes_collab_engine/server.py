from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import CollabEngine
from .store import CollabStore

INDEX_HTML = Path(__file__).resolve().parents[2] / "web" / "index.html"


class DashboardServer:
    def __init__(self, host: str, port: int, db_path: str, cwd: str, model: str | None = None, leader_model: str | None = None, worker_model: str | None = None, agent: str = "claude-code"):
        self.host = host
        self.port = port
        self.db_path = db_path
        self.cwd = cwd
        self.model = model
        self.leader_model = leader_model
        self.worker_model = worker_model
        self.agent = agent
        self.store = CollabStore(db_path)

    def skills_payload(self, node_type: str = "", task: str = "") -> list[dict]:
        from .skills import get_default_registry
        registry = get_default_registry()
        skills = registry.select_for_node(node_type, task) if node_type else registry.list_all()
        return [skill.to_dict() for skill in skills]

    def tools_payload(self, node_type: str = "", task: str = "") -> list[dict]:
        from .tools import get_default_tool_registry
        registry = get_default_tool_registry()
        profiles = registry.select_for_node(node_type, task) if node_type else registry.list_all()
        return [profile.to_dict() for profile in profiles]

    def serve(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, data, status=200):
                body = json.dumps(data, ensure_ascii=False, indent=2).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    body = INDEX_HTML.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/overview":
                    self._json(outer.store.overview())
                elif path == "/api/runs":
                    self._json(outer.store.list_runs())
                elif path.startswith("/api/runs/"):
                    self._json(outer.store.run_detail(path.rsplit("/", 1)[-1]))
                elif path == "/api/logs":
                    self._json(outer.store.recent_logs())
                elif path == "/api/lessons":
                    self._json(outer.store.lessons())
                elif path == "/api/agents":
                    from .agents import detect_available_backends
                    self._json([b.to_dict() for b in detect_available_backends()])
                elif path == "/api/skills":
                    query = parse_qs(urlparse(self.path).query)
                    node_type = (query.get("node_type") or [""])[0]
                    task = (query.get("task") or [""])[0]
                    self._json(outer.skills_payload(node_type, task))
                elif path == "/api/tools":
                    query = parse_qs(urlparse(self.path).query)
                    node_type = (query.get("node_type") or [""])[0]
                    task = (query.get("task") or [""])[0]
                    self._json(outer.tools_payload(node_type, task))
                elif path == "/api/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    try:
                        import time
                        while True:
                            payload = json.dumps({"overview": outer.store.overview(), "logs": outer.store.recent_logs(20)}, ensure_ascii=False)
                            self.wfile.write(f"data: {payload}\n\n".encode())
                            self.wfile.flush()
                            time.sleep(2)
                    except Exception:
                        pass
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode() if length else "{}"
                try:
                    data = json.loads(body)
                except Exception:
                    return self._json({"error": "invalid json"}, 400)
                if path == "/api/runs":
                    request = str(data.get("request") or "").strip()
                    if not request:
                        return self._json({"error": "request is required"}, 400)
                    engine = CollabEngine(outer.db_path, outer.cwd, outer.model, leader_model=outer.leader_model, worker_model=outer.worker_model, agent=outer.agent)
                    def run_async():
                        engine.run(
                            request,
                            title=data.get("title"),
                            concurrency=int(data.get("concurrency", 4)),
                            timeout=int(data.get("timeout", 900)),
                            max_retries=int(data.get("max_retries", 2)),
                            split_count=int(data.get("split_count", 4)),
                            aggregate=bool(data.get("aggregate", True)),
                        )
                    t = threading.Thread(target=run_async, daemon=True)
                    t.start()
                    self._json({"accepted": True})
                else:
                    self._json({"error": "not found"}, 404)

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        print(f"Hermes Collab Engine dashboard: http://{self.host}:{self.port}")
        httpd.serve_forever()
