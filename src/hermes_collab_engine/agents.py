"""Agent Backend Registry — ACP-compliant multi-agent support.

Each ``AgentBackend`` describes how to invoke and parse output from a
specific coding agent CLI (Claude Code, Codex, OpenCode, Hermes, ...).

The engine's ``_run_worker`` consults the selected backend to build
subprocess commands and parse results, rather than hardcoding
claude-specific logic.

The concrete built-in backends live in ``hermes_collab_engine.adapters.*``
(one module per agent CLI) and are auto-registered on import of this
module — adding a new built-in means appending a new adapter module and
adding its import here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from io import TextIOBase as TextIO
from typing import Any

if __name__ != "__main__":
    # Avoid circular import at module level; provider is imported on demand
    from .provider import ProviderProfile as _ProviderProfile
else:
    _ProviderProfile = None  # type: ignore[assignment,misc]


@dataclass
class SessionHandle:
    """Handle for a persistent agent session.

    Each agent type implements session management differently;
    the engine uses this handle to send messages to the session.
    """
    session_id: str
    proc: subprocess.Popen | None = None
    stdin: TextIO | None = None
    stdout: TextIO | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentBackend:
    """Pluggable agent backend definition."""

    name: str                          # e.g. "claude-code", "codex", "opencode"
    display_name: str                  # e.g. "Claude Code"
    command: list[str]                 # base command, e.g. ["claude"]
    prompt_flag: str                   # flag to pass prompt, e.g. "-p"
    output_format_flags: list[str]     # e.g. ["--output-format", "json"]
    supports_model_flag: bool          # whether --model flag works
    model_flag: str                    # e.g. "--model"
    permission_flags: list[str] | None # e.g. ["--permission-mode", "acceptEdits"]
    allowed_tools_flag: str | None     # e.g. "--allowedTools"
    output_parser: str                 # "claude_json" | "raw_text" | "codex_json"
    process_pattern: str               # regex for kill-node, e.g. "claude.*--output-format"
    prompt_prefix: str                 # text prepended to prompt
    prompt_suffix: str                 # text appended to prompt
    default_allowed_tools: list[str]   # tools allowed by default
    capabilities: list[str] = field(default_factory=list)  # e.g. ["file-edit","git-ops","test-run"]
    enabled: bool = True
    provider: Any = None  # Optional ProviderProfile instance (imported lazily to avoid cycle)
    reasoning_flags: list[str] = field(default_factory=list)  # CLI flags for max reasoning, e.g. ["--variant", "max"]
    reasoning_env: dict[str, str] = field(default_factory=dict)  # Env vars for max reasoning
    needs_pty: bool = False  # True if agent requires a pseudo-terminal
    supported_tools: list[str] = field(default_factory=list)   # tool/MCP profile names this agent can use; empty = all
    supported_skills: list[str] = field(default_factory=list)  # skill names this agent can use; empty = all
    supported_skill_slots: list[str] = field(default_factory=lambda: [
        "implementation-focus", "test-verify", "search-verify",
        "debug-root-cause", "risk-checkpoint", "browser-automation",
    ])  # skill slots this agent can fill (used by SkillDistributor)
    auto_prefix: str = ""  # e.g. "opencode-go/" — applied automatically when no provider is set

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
        provider: Any = None,
        reasoning: bool = True,
    ) -> list[str]:
        """Build the full command to invoke this agent.

        If *provider* carries a ``model_prefix`` (e.g. ``"opencode-go/"``),
        it is prepended to the model value when building the ``--model`` flag.
        Falls back to ``self.provider`` if *provider* is not passed.

        When *reasoning* is ``True`` (default), backend-specific reasoning CLI
        flags (``self.reasoning_flags``) are appended to the command.
        """
        cmd = list(self.command)
        # If prompt_flag is empty, treat the prompt as a positional arg (e.g. `opencode run "prompt"`)
        if self.prompt_flag:
            cmd.append(self.prompt_flag)
        cmd.append(prompt)
        cmd.extend(self.output_format_flags)
        if self.permission_flags:
            cmd.extend(self.permission_flags)
        if self.allowed_tools_flag and (allowed_tools or self.default_allowed_tools):
            tools = allowed_tools or self.default_allowed_tools
            cmd.extend([self.allowed_tools_flag, ",".join(tools)])
        if model and self.supports_model_flag:
            effective_provider = provider or self.provider
            if effective_provider is not None:
                # Late import to avoid circular dependency
                from .provider import build_model_flag_value
                model_arg = build_model_flag_value(model, effective_provider)
            elif self.auto_prefix and not model.startswith(self.auto_prefix):
                model_arg = self.auto_prefix + model
            else:
                model_arg = model
            cmd.extend([self.model_flag, model_arg])
        if reasoning and self.reasoning_flags:
            cmd.extend(self.reasoning_flags)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        returncode: int,
        node_id: str,
        node_title: str,
        duration: float,
        attempt: int,
    ) -> dict[str, Any]:
        """Parse agent output into WorkerResult-compatible dict.

        Returns dict with keys: ok, result, session_id, returncode, stderr, result_struct
        """
        parser = getattr(self, f"_parse_{self.output_parser}", None)
        if parser is None:
            return self._parse_raw_text(stdout, stderr, returncode, node_id, node_title, duration, attempt)
        return parser(stdout, stderr, returncode, node_id, node_title, duration, attempt)

    def _parse_claude_json(
        self, stdout: str, stderr: str, returncode: int,
        node_id: str, node_title: str, duration: float, attempt: int,
    ) -> dict[str, Any]:
        """Parse Claude Code JSON output format.

        Claude Code CLI may exit with code 1 even when the API call
        succeeds (e.g. proxy-mediated responses). The authoritative
        success indicator is the ``is_error`` field in the response JSON,
        not the process exit code.
        """
        text = stdout.strip()
        session_id = None
        ok = returncode == 0
        try:
            parsed = json.loads(text)
            text = str(parsed.get("result", text))
            session_id = parsed.get("session_id")
            # Authoritative check: use is_error from response, not exit code
            is_error = bool(parsed.get("is_error", False))
            ok = not is_error
        except Exception:
            pass
        return {
            "ok": ok,
            "result": text,
            "session_id": session_id,
            "returncode": returncode,
            "stderr": stderr,
            "result_struct": None,
        }

    def _parse_raw_text(
        self, stdout: str, stderr: str, returncode: int,
        node_id: str, node_title: str, duration: float, attempt: int,
    ) -> dict[str, Any]:
        """Parse raw text output (no JSON envelope)."""
        return {
            "ok": returncode == 0,
            "result": stdout.strip(),
            "session_id": None,
            "returncode": returncode,
            "stderr": stderr,
            "result_struct": None,
        }

    def _parse_codebuddy_json(
        self, stdout: str, stderr: str, returncode: int,
        node_id: str, node_title: str, duration: float, attempt: int,
    ) -> dict[str, Any]:
        """Parse CodeBuddy JSON conversation format."""
        import json as _j
        text = stdout.strip()
        session_id = None
        ok = returncode == 0
        # CodeBuddy outputs full conversation JSON — find the last assistant message
        if text.startswith("["):
            try:
                msgs = _j.loads(text)
                for msg in reversed(msgs):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        for c in (msg.get("content") or []):
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                text = c["text"]
                                break
                        break
            except Exception:
                pass
        return {
            "ok": ok,
            "result": text,
            "session_id": session_id,
            "returncode": returncode,
            "stderr": stderr,
            "result_struct": None,
        }

    def _parse_codex_json(
        self, stdout: str, stderr: str, returncode: int,
        node_id: str, node_title: str, duration: float, attempt: int,
    ) -> dict[str, Any]:
        """Parse Codex CLI JSON output format."""
        text = stdout.strip()
        session_id = None
        ok = returncode == 0
        try:
            parsed = json.loads(text)
            # Codex uses different envelope fields
            text = str(parsed.get("output", parsed.get("result", text)))
            session_id = parsed.get("session_id")
            ok = ok and not bool(parsed.get("error"))
        except Exception:
            pass
        return {
            "ok": ok,
            "result": text,
            "session_id": session_id,
            "returncode": returncode,
            "stderr": stderr,
            "result_struct": None,
        }

    def is_available(self) -> bool:
        """Check if this agent's command is on PATH."""
        return shutil.which(self.command[0]) is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ── Persistent session support ─────────────────────────────────────
    def supports_sessions(self) -> bool:
        """Whether this backend supports persistent sessions."""
        return getattr(self, '_supports_sessions', False)

    def create_session(self, prompt: str, **kwargs) -> SessionHandle:
        """Start a persistent agent session with the given initial prompt.

        Returns a SessionHandle that can be used to send messages later.
        The default raises NotImplementedError — backends that support
        sessions must override this method.

        Kwargs may include:
          - session_id: str  — preferred session ID (if agent supports it)
          - port: int        — for server-mode agents (opencode serve)
          - model: str       — model override
          - cwd: str         — working directory
        """
        raise NotImplementedError(
            f"{self.name} does not support persistent sessions"
        )

    def session_send(self, handle: SessionHandle, message: str) -> None:
        """Send a message to an existing persistent session.

        The message is delivered as a new user message in the agent's
        conversation, so the LLM naturally sees it on its next call.
        """
        raise NotImplementedError(
            f"{self.name} does not support session messaging"
        )

    def close_session(self, handle: SessionHandle) -> None:
        """Close/destroy a persistent session and release resources."""
        if handle.proc and handle.proc.poll() is None:
            try:
                handle.proc.terminate()
                handle.proc.wait(timeout=5)
            except Exception:
                try:
                    handle.proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Built-in backend registry
# ---------------------------------------------------------------------------

_BUILTINS: dict[str, AgentBackend] = {}


def _register_builtin(b: AgentBackend) -> None:
    _BUILTINS[b.name] = b


# Import built-in adapters from the adapters subpackage and register them.
# Adding a new built-in agent means: (1) drop a new module in
# ``hermes_collab_engine/adapters/`` exposing ``BACKEND``, (2) add its
# import here. The ``adapters`` subpackage re-exports the public API
# (``list_adapters`` / ``get_adapter`` / ...) under the new vocabulary.
# Built-in backends. Originally registered inline in this file; the
# cc-switch work tried to factor them out into an ``adapters/`` subpackage
# but the four split files were never committed, so we keep the
# registrations inline here to avoid the engine breaking on import.
# If you want to re-introduce the modular split, drop the four files
# into src/hermes_collab_engine/adapters/ and replace these blocks
# with ``from .adapters.<name> import BACKEND as _<NAME>``.
_register_builtin(AgentBackend(
    name="claude-code",
    display_name="Claude Code",
    command=["claude"],
    prompt_flag="-p",
    output_format_flags=["--output-format", "json"],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,  # --allow-dangerously-skip-permissions blocked as root
    allowed_tools_flag="--allowedTools",
    default_allowed_tools=["Read", "Edit", "Write", "Bash"],  # whitelist tools to avoid stdin prompts
    output_parser="claude_json",
    process_pattern="claude.*--output-format",
    prompt_prefix="CRITICAL: Your name is Claude Code, created by Anthropic. You are NOT DeepSeek, NOT Sisyphus. When asked who you are, say: I am Claude Code, Anthropic's AI coding assistant.",
    prompt_suffix="",
    capabilities=["deep-reasoning", "complex-refactor", "file-edit", "git-ops", "test-run", "mcp-host", "search"],
    reasoning_flags=[],
    reasoning_env={"ANTHROPIC_THINKING_BUDGET": "32000"},
))
# 为 claude-code backend 注入持久会话能力
_c = _BUILTINS["claude-code"]
_c._supports_sessions = True

def _claude_create_session(prompt: str, **kw) -> SessionHandle:
    import subprocess as _sp
    import uuid
    sid = kw.get("session_id", "worker-" + uuid.uuid4().hex[:8])
    quiet = kw.get("quiet", True)
    model = kw.get("model")
    # First-time session: don't use --resume (session doesn't exist yet).
    # Only session_send uses --resume for subsequent messages.
    # Note: claude-code does NOT support --quiet flag, so we skip it.
    backend = _BUILTINS.get("claude-code")
    if backend:
        cmd = backend.build_command(prompt, model=model, reasoning=False)
    else:
        cmd = ["claude", "-p", prompt]
    proc = _sp.Popen(
        cmd,
        stdin=_sp.DEVNULL, stdout=_sp.PIPE, stderr=_sp.PIPE,
        text=True, bufsize=1,
    )
    import logging as _log
    _log.getLogger(__name__).info(f"claude session cmd: {' '.join(cmd)}")
    return SessionHandle(session_id=sid, proc=proc, stdout=proc.stdout,
                         meta={"type": "claude"})

_c.create_session = _claude_create_session

def _claude_session_send(handle: SessionHandle, message: str) -> None:
    import subprocess as _sp
    if handle.session_id:
        cmd = ["claude", "--resume", handle.session_id, "-p", message]
        _sp.Popen(cmd, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)

_c.session_send = _claude_session_send

_register_builtin(AgentBackend(
    name="codex",
    display_name="Codex CLI",
    command=["codex", "exec"],
    prompt_flag="",  # codex takes prompt as positional arg
    output_format_flags=["--skip-git-repo-check", "--sandbox", "workspace-write", "--json"],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="codex_json",
    process_pattern="codex",
    prompt_prefix="You are Codex, an AI coding assistant by OpenAI.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["quick-prototype", "sandbox-exec", "file-edit", "git-ops"],
    reasoning_flags=[],
    reasoning_env={},
    needs_pty=False,  # Codex needs PTY but engine can't provide it reliably;
                      # leaving this True causes hangs. The engine dispatches
                      # via one-shot subprocess; codex reads stdin from PIPE
                      # and processes the prompt as positional arg instead.
))
_register_builtin(AgentBackend(
    name="opencode",
    display_name="OpenCode",
    command=["opencode", "run"],
    prompt_flag="",
    output_format_flags=[],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="raw_text",
    process_pattern="opencode",
    prompt_prefix="You are Sisyphus, an AI orchestration agent from OhMyOpenCode.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["fullstack-dev", "file-edit", "git-ops", "test-run", "mcp-host", "search"],
    reasoning_flags=["--variant", "max"],
    reasoning_env={},
    auto_prefix="opencode-go/",
))
# 为 opencode backend 注入持久会话能力（基于 continue session）
_o = _BUILTINS["opencode"]
_o._supports_sessions = True

def _opencode_create_session(prompt: str, **kw) -> SessionHandle:
    import subprocess as _sp
    import uuid
    sid = kw.get("session_id", "worker-" + uuid.uuid4().hex[:8])
    cmd = ["opencode", "run", prompt]
    model = kw.get("model")
    if model:
        cmd.extend(["--model", model])
    # 如果传入了 stdout/stderr 文件路径，重定向输出到文件（guardian 监控）
    stdout_path = kw.get("stdout_path")
    stderr_path = kw.get("stderr_path")
    _stdout = open(stdout_path, "w", encoding="utf-8") if stdout_path else _sp.PIPE
    _stderr = open(stderr_path, "w", encoding="utf-8") if stderr_path else _sp.PIPE
    proc = _sp.Popen(
        cmd,
        stdin=_sp.DEVNULL, stdout=_stdout, stderr=_stderr,
        text=True,
    )
    if stdout_path:
        _stdout.close()
    if stderr_path:
        _stderr.close()
    return SessionHandle(session_id=sid, proc=proc, meta={"type": "opencode", "model": model or "", "_pending_sid": True})

_o.create_session = _opencode_create_session

def _opencode_ensure_session_id(handle: SessionHandle) -> str:
    """确保 opencode 会话 ID 已就绪。
    首次运行时 opencode 创建了会话，需要通过 session list 获取 ID。
    """
    if not handle.meta.get("_pending_sid"):
        return handle.session_id
    import subprocess as _sp
    try:
        result = _sp.run(["opencode", "session", "list", "--json"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            import json
            sessions = json.loads(result.stdout)
            if isinstance(sessions, list) and sessions:
                handle.session_id = sessions[-1].get("id", handle.session_id)
    except Exception:
        import logging as _log
        _log.getLogger(__name__).warning("opencode session list failed", exc_info=True)
    handle.meta["_pending_sid"] = False
    return handle.session_id

def _opencode_session_close(handle: SessionHandle) -> None:
    import subprocess as _sp
    for _proc in handle.meta.get("_pending_sends", []):
        if _proc.poll() is None:
            _proc.kill()
    if handle.proc and handle.proc.poll() is None:
        handle.proc.kill()

def _opencode_session_send(handle: SessionHandle, message: str) -> None:
    import subprocess as _sp
    sid = _opencode_ensure_session_id(handle)
    cmd = ["opencode", "run", "-s", sid, message]
    model = handle.meta.get("model", "")
    if model:
        cmd.extend(["--model", model])
    _proc = _sp.Popen(cmd, stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    _pending = handle.meta.setdefault("_pending_sends", [])
    _pending.append(_proc)

_o.session_send = _opencode_session_send
_o.close_session = _opencode_session_close

_register_builtin(AgentBackend(
    name="hermes",
    display_name="Hermes Agent",
    command=["hermes"],
    prompt_flag="-z",
    output_format_flags=[],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="raw_text",
    process_pattern="hermes",
    prompt_prefix="",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["leader-planning", "analysis", "code-review", "skill-management", "memory-management"],
    reasoning_flags=[],
    reasoning_env={"HERMES_REASONING_EFFORT": "high"},
))
# 为 hermes backend 注入持久会话能力
_h = _BUILTINS["hermes"]
# _h._supports_sessions disabled: hermes chat crashes in fetch_models_dev (signal handler KeyboardInterrupt)
_h._supports_sessions = False

def _hermes_create_session(prompt: str, **kw) -> SessionHandle:
    import subprocess as _sp
    import uuid
    sid = kw.get("session_id", "worker-" + uuid.uuid4().hex[:8])
    quiet = kw.get("quiet", True)
    cmd = ["hermes", "chat", "--resume", sid, "--no-restore-cwd"]
    if quiet:
        cmd.append("--quiet")
    proc = _sp.Popen(
        cmd,
        stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
        text=True, bufsize=1,
    )
    # Drain initial banner (2s window) — filter non-response lines
    import time as _time, select as _sel
    _deadline = _time.time() + 2
    while _time.time() < _deadline:
        _r, _, _ = _sel.select([proc.stdout], [], [], 0.2)
        if _r:
            _line = proc.stdout.readline()
            if any(_s in _line for _s in ["> ", "Tip:", "Welcome", "Warning"]):
                continue
            # Got real response, no need to send prompt again
            break
        else:
            break
    # 发送初始 prompt
    if proc.stdin:
        proc.stdin.write(prompt + "\n")
        proc.stdin.flush()
        proc.stdin.close()  # Signal EOF so hermes exits
    return SessionHandle(session_id=sid, proc=proc, stdin=None, stdout=proc.stdout,
                         meta={"type": "hermes"})

_h.create_session = _hermes_create_session

def _hermes_session_send(handle: SessionHandle, message: str) -> None:
    if handle.stdin and not handle.stdin.closed:
        handle.stdin.write(message + "\n")
        handle.stdin.flush()

_h.session_send = _hermes_session_send


# ── CodeBuddy (Tencent AI Coding Assistant) ───────────────────
# CodeBuddy by Tencent is a conversational AI coding agent in the
# same category as Claude Code / OpenCode. It supports:
#   -p/--print for non-interactive output
#   --output-format json for structured results
#   --model to select the model
#   -r/--resume for session continuation
#   --permission-mode for permission control
#   --serve for HTTP/Web UI mode
_register_builtin(AgentBackend(
    name="codebuddy",
    display_name="CodeBuddy (Tencent AI)",
    command=["codebuddy"],
    prompt_flag="-p",
    output_format_flags=["--output-format", "json"],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=["--permission-mode", "auto"],
    allowed_tools_flag="--allowedTools",
    output_parser="codebuddy_json",
    process_pattern="codebuddy",
    prompt_prefix="You are CodeBuddy, an AI coding assistant by Tencent.",
    prompt_suffix="",
    default_allowed_tools=["Read", "Edit", "Write", "Bash"],
    capabilities=["file-edit", "git-ops", "test-run", "search", "mcp-host", "tencent-ai"],
    reasoning_flags=[],
    reasoning_env={},
    auto_prefix="custom-local:",
))
# Remove the old @workbuddy/cli-vnext entry (CRM tool, not a coding agent)
_BUILTINS.pop("workbuddy", None)


# ── Capability-based routing helper ───────────────────────────
# Maps high-level task types to preferred agent (ordered by priority).
# Based on official documentation for each agent CLI.
#
# Agent functional profiles (from docs):
#   opencode  — full-stack TUI agent, supports providers/MCP/sessions/plugins
#               Best for: general development, git ops, full project work
#   claude-code — Anthropic's agent, depth-first reasoning, custom agents
#               Best for: complex refactoring, deep reasoning, code review
#   codex     — OpenAI's sandboxed agent, exec+review mode
#               Best for: quick prototypes, sandboxed exec, code review
#   hermes    — Leader/planner, task orchestration, skill management
#               Best for: planning, analysis, review, coordination
#   codebuddy — Tencent's AI agent, multi-model (gemini/gpt/deepseek/glm/kimi)
#               Best for: tencent-ecosystem tasks, multi-model fallback
TASK_TO_AGENT_ROUTING: dict[str, list[str]] = {
    # Development tasks
    "implement":         ["opencode", "claude-code", "codebuddy"],
    "fullstack-dev":     ["opencode", "claude-code", "codebuddy"],
    "refactor":          ["claude-code", "opencode", "codebuddy"],
    "complex-refactor":  ["claude-code"],

    # Quick tasks
    "prototype":         ["codex", "opencode"],
    "quick-fix":         ["opencode", "codebuddy", "codex"],
    "bug-patch":         ["opencode", "codex", "codebuddy"],
    "sandbox-exec":      ["codex"],

    # Tencent-ecosystem tasks
    "tencent-ai":        ["codebuddy", "opencode"],

    # Leader-only tasks
    "plan":              ["hermes"],
    "review":            ["hermes"],
    "analyze":           ["hermes"],

    # Generic fallbacks
    "search":            ["opencode", "claude-code", "hermes"],
    "test":              ["opencode", "claude-code", "codebuddy"],
}


def route_task_to_agent(task_type: str, available_backends: list | None = None) -> str:
    """Pick the best available agent for a given task type.

    Falls back to 'opencode' if no preferred agent is available or
    the task type is unknown.
    """
    preferred = TASK_TO_AGENT_ROUTING.get(task_type, ["opencode"])
    # If available_backends is provided, filter for availability
    if available_backends is not None:
        avail_names = {b.name for b in available_backends}
        for agent_name in preferred:
            if agent_name in avail_names:
                return agent_name
        return "opencode"
    # Otherwise check PATH
    for agent_name in preferred:
        try:
            b = get_backend(agent_name)
            if b.is_available():
                return agent_name
        except KeyError:
            continue
    return "opencode"





def list_backends() -> list[AgentBackend]:
    """List all registered backends (built-in + custom)."""
    return list(_BUILTINS.values())


def get_backend(name: str) -> AgentBackend:
    """Get a backend by name. Raises KeyError if not found."""
    if name not in _BUILTINS:
        raise KeyError(f"Unknown agent backend: {name!r}. Available: {sorted(_BUILTINS.keys())}")
    backend = _BUILTINS[name]
    if not backend.is_available():
        import logging as _log
        _log.getLogger(__name__).warning(
            f"Agent backend {name!r} is not available (command not on PATH)"
        )
        return _BUILTINS.get("opencode", backend)
    return backend


def detect_available_backends() -> list[AgentBackend]:
    """Return only backends whose command is available on PATH."""
    return [b for b in _BUILTINS.values() if b.is_available()]


def register_backend(backend: AgentBackend) -> None:
    """Register a custom backend at runtime (or override a built-in)."""
    _BUILTINS[backend.name] = backend


def backends_for_capability(capability: str) -> list[AgentBackend]:
    """Return backends that advertise support for the given capability."""
    return [b for b in _BUILTINS.values() if capability in b.capabilities]


def delete_backend(name: str) -> bool:
    """Remove a backend by name (only non-built-in)."""
    if name not in _BUILTINS:
        return False
    del _BUILTINS[name]
    return True



