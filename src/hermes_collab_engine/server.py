from __future__ import annotations

import json
import os
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
        env_model = os.environ.get("HERMES_COLLAB_MODEL") or os.environ.get("ANTHROPIC_MODEL")
        self.model = model or env_model
        self.leader_model = leader_model or model or os.environ.get("HERMES_COLLAB_LEADER_MODEL") or env_model
        self.worker_model = worker_model or model or os.environ.get("HERMES_COLLAB_WORKER_MODEL") or env_model
        self.agent = agent
        self.store = CollabStore(db_path)

    def config_payload(self) -> dict:
        return {
            "model": self.model,
            "leader_model": self.leader_model,
            "worker_model": self.worker_model,
            "effective_leader_model": self.leader_model,
            "effective_worker_model": self.worker_model,
            "model_overrides_readonly": False,
            "agent": self.agent,
        }

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
        # Initialize unified registry with store for persistence
        from .registry import get_unified_registry
        get_unified_registry(store=outer.store)

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
                    query = parse_qs(urlparse(self.path).query)
                    full = (query.get("full") or [""])[0].lower() in {"1", "true", "yes"}
                    log_limit_raw = (query.get("log_limit") or ["80"])[0]
                    try:
                        log_limit = max(0, min(200, int(log_limit_raw)))
                    except ValueError:
                        log_limit = 80
                    self._json(outer.store.run_detail(path.rsplit("/", 1)[-1], full=full, log_limit=log_limit, include_workers=full))
                elif path == "/api/logs":
                    self._json(outer.store.recent_logs())
                elif path == "/api/lessons":
                    self._json(outer.store.lessons())
                elif path == "/api/session-chains":
                    self._json(outer.store.session_chains())
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
                elif path == "/api/config":
                    self._json(outer.config_payload())
                elif path == "/api/resume-context":
                    query = parse_qs(urlparse(self.path).query)
                    run_id = (query.get("run_id") or [None])[0]
                    context = outer.store.session_resume_context(run_id)
                    self._json(context or {"error": "no previous run"}, 200 if context else 404)
                elif path == "/api/registry":
                    from .registry import get_unified_registry
                    registry = get_unified_registry()
                    query = parse_qs(urlparse(self.path).query)
                    capability = (query.get("capability") or [""])[0]
                    entries = registry.select_for_capability(capability) if capability else registry.list_all()
                    skills = [e.to_dict() for e in entries if e.__class__.__name__ == "SkillEntry"]
                    tools = [e.to_dict() for e in entries if e.__class__.__name__ == "ToolEntry"]
                    mcp = [e.to_dict() for e in entries if e.__class__.__name__ == "MCPEntry"]
                    self._json({"skills": skills, "tools": tools, "mcp": mcp, "total": len(entries)})
                elif path.startswith("/api/runs/"):
                    run_id = path.split("/")[3]
                    detail = outer.store.run_detail(run_id, full=True)
                    self._json(detail)
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
                    title = data.get("title")
                    resume_context = None
                    if data.get("resume"):
                        title = title or request[:80]
                        request, resume_context = outer.store.resume_prompt(request, data.get("resume_run_id"))
                    engine = CollabEngine(
                        outer.db_path,
                        outer.cwd,
                        outer.model,
                        leader_model=outer.leader_model,
                        worker_model=outer.worker_model,
                        agent=outer.agent,
                    )
                    def run_async():
                        result = engine.run(
                            request,
                            title=title,
                            concurrency=int(data.get("concurrency", 4)),
                            timeout=int(data.get("timeout", 900)),
                            max_retries=int(data.get("max_retries", 2)),
                            split_count=int(data.get("split_count", 4)),
                            aggregate=bool(data.get("aggregate", True)),
                        )
                        if resume_context and result.get("run_id"):
                            outer.store.log(result["run_id"], "info", "run resumed previous context", {"source_run_id": resume_context["run"]["id"], "estimated_tokens": resume_context["estimated_tokens"]})
                    t = threading.Thread(target=run_async, daemon=True)
                    t.start()
                    payload = {"accepted": True}
                    if resume_context:
                        payload["resume"] = {"source_run_id": resume_context["run"]["id"], "estimated_tokens": resume_context["estimated_tokens"]}
                    self._json(payload)
                elif path.startswith("/api/runs/") and path.endswith("/interrupt"):
                    run_id = path.split("/")[3]
                    run = outer.store._one("SELECT id,status FROM runs WHERE id=?", (run_id,))
                    if not run:
                        return self._json({"error": "run not found"}, 404)
                    if run["status"] not in ("running", "created"):
                        return self._json({"error": f"run is {run['status']}, cannot interrupt"}, 409)
                    outer.store.fail_stale_run(run_id, "interrupted via dashboard")
                    self._json({"ok": True, "run_id": run_id, "status": "failed"})
                elif path == "/api/agents":
                    import re
                    from .agents import AgentBackend, register_backend, list_backends
                    name = str(data.get("name", "")).strip()
                    # name: required, [a-z0-9_-], length 2-32
                    if not name or not re.fullmatch(r"[a-z0-9_-]{2,32}", name):
                        return self._json({"error": "name 必填，只允许 [a-z0-9_-]，长度 2-32"}, 400)
                    # command: required, non-empty list of non-empty strings
                    cmd = data.get("command")
                    if isinstance(cmd, str):
                        cmd = [c for c in cmd.split() if c]
                    elif isinstance(cmd, list):
                        cmd = [str(c) for c in cmd if str(c).strip()]
                    else:
                        cmd = []
                    if not cmd:
                        return self._json({"error": "command 必填，非空数组"}, 400)
                    # capabilities: at least 1, only [a-z_-]
                    caps = data.get("capabilities", [])
                    if not isinstance(caps, list) or not caps:
                        return self._json({"error": "capabilities 至少 1 个"}, 400)
                    for c in caps:
                        if not re.fullmatch(r"[a-z_-]+", str(c)):
                            return self._json({"error": f"capability 格式不合法: {c!r}，只允许 [a-z_-]"}, 400)
                    # display_name: optional, max 64
                    display_name = str(data.get("display_name", name))
                    if len(display_name) > 64:
                        return self._json({"error": "display_name 长度不超过 64"}, 400)
                    # output_parser: must be known type
                    known_parsers = {"raw_text", "claude_json", "codex_json"}
                    parser = data.get("output_parser", "raw_text")
                    if parser not in known_parsers:
                        return self._json({"error": f"output_parser 必须是 {sorted(known_parsers)} 之一"}, 400)
                    # name conflict: check built-in + already registered
                    existing_names = {b.name for b in list_backends()}
                    if name in existing_names:
                        return self._json({"error": f"agent {name!r} 已存在"}, 409)
                    backend = AgentBackend(
                        name=name,
                        display_name=display_name,
                        command=cmd,
                        prompt_flag=data.get("prompt_flag", "-p"),
                        output_format_flags=data.get("output_format_flags", []),
                        supports_model_flag=bool(data.get("supports_model_flag", True)),
                        model_flag=data.get("model_flag", "--model"),
                        permission_flags=data.get("permission_flags"),
                        allowed_tools_flag=data.get("allowed_tools_flag"),
                        output_parser=parser,
                        process_pattern=data.get("process_pattern", name),
                        prompt_prefix=data.get("prompt_prefix", ""),
                        prompt_suffix=data.get("prompt_suffix", ""),
                        default_allowed_tools=data.get("default_allowed_tools", []),
                        capabilities=caps,
                    )
                    register_backend(backend)
                    self._json({"ok": True, "name": name})
                elif path == "/api/registry":
                    entry_type = data.get("type", "skill")
                    name = str(data.get("name", "")).strip()
                    if not name:
                        return self._json({"error": "name is required"}, 400)
                    from .registry import get_unified_registry, SkillEntry, MCPEntry
                    registry = get_unified_registry()
                    if entry_type == "skill":
                        entry = SkillEntry(
                            name=name,
                            display_name=data.get("display_name", name),
                            category=data.get("category", "custom"),
                            description=data.get("description", ""),
                            capabilities=data.get("capabilities", ["*"]),
                            source="web-ui",
                            priority=int(data.get("priority", 2)),
                            content=data.get("content", ""),
                            file_path=data.get("file_path", ""),
                        )
                    elif entry_type == "mcp":
                        entry = MCPEntry(
                            name=name,
                            display_name=data.get("display_name", name),
                            category=data.get("category", "mcp"),
                            description=data.get("description", f"MCP tool {data.get('tool_name', name)}"),
                            capabilities=data.get("capabilities", ["*"]),
                            source="web-ui",
                            priority=int(data.get("priority", 2)),
                            server_name=data.get("server_name", ""),
                            tool_name=data.get("tool_name", ""),
                            endpoint=data.get("endpoint", ""),
                            allowed_tools=data.get("allowed_tools", []),
                            config_path=data.get("config_path", ""),
                        )
                    else:
                        return self._json({"error": f"unknown type: {entry_type}"}, 400)
                    registry.register(entry)
                    self._json({"ok": True, "name": name, "type": entry_type})
                else:
                    self._json({"error": "not found"}, 404)

            def do_PUT(self):
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode() if length else "{}"
                try:
                    data = json.loads(body)
                except Exception:
                    return self._json({"error": "invalid json"}, 400)
                if path == "/api/config":
                    allowed = {"base_url", "api_key", "leader_model", "worker_model"}
                    updates = {k: v for k, v in data.items() if k in allowed}
                    if not updates:
                        return self._json({"error": "no valid fields"}, 400)
                    saved = outer.store.get_setting("web_config") or {}
                    saved.update(updates)
                    outer.store.set_setting("web_config", saved)
                    return self._json(outer.config_payload())
                self._json({"error": "not found"}, 404)

            def do_DELETE(self):
                path = urlparse(self.path).path
                if path.startswith("/api/registry/"):
                    name = path.split("/")[-1]
                    from .registry import get_unified_registry
                    reg = get_unified_registry()
                    if reg.get(name):
                        reg.delete(name)
                        self._json({"ok": True, "name": name})
                    else:
                        self._json({"error": f"entry {name!r} not found"}, 404)
                else:
                    self._json({"error": "not found"}, 404)

            def log_message(self, fmt, *args):
                return

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        print(f"Hermes Collab Engine dashboard: http://{self.host}:{self.port}")
        httpd.serve_forever()
