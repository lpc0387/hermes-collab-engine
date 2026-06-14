"""Agent Backend Registry — ACP-compliant multi-agent support.

Each AgentBackend defines how to invoke and parse output from a specific
coding agent CLI (Claude Code, Codex, OpenCode, etc.).

The Engine's _run_worker uses the selected backend to build commands and
parse results, instead of hardcoding claude-specific logic.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any


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

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> list[str]:
        """Build the full command to invoke this agent."""
        cmd = list(self.command)
        cmd.append(self.prompt_flag)
        cmd.append(prompt)
        cmd.extend(self.output_format_flags)
        if self.permission_flags:
            cmd.extend(self.permission_flags)
        if self.allowed_tools_flag and (allowed_tools or self.default_allowed_tools):
            tools = allowed_tools or self.default_allowed_tools
            cmd.extend([self.allowed_tools_flag, ",".join(tools)])
        if model and self.supports_model_flag:
            cmd.extend([self.model_flag, model])
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
        """Parse Claude Code JSON output format."""
        text = stdout.strip()
        session_id = None
        ok = returncode == 0
        try:
            parsed = json.loads(text)
            text = str(parsed.get("result", text))
            session_id = parsed.get("session_id")
            ok = ok and not bool(parsed.get("is_error"))
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


# ---------------------------------------------------------------------------
# Built-in backend registry
# ---------------------------------------------------------------------------

_BUILTINS: dict[str, AgentBackend] = {}

def _register_builtin(b: AgentBackend) -> None:
    _BUILTINS[b.name] = b

_register_builtin(AgentBackend(
    name="claude-code",
    display_name="Claude Code",
    command=["claude"],
    prompt_flag="-p",
    output_format_flags=["--output-format", "json"],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=["--permission-mode", "acceptEdits"],
    allowed_tools_flag="--allowedTools",
    output_parser="claude_json",
    process_pattern="claude.*--output-format",
    prompt_prefix="You are a Claude Code worker in a Hermes collaboration engine.",
    prompt_suffix="",
    default_allowed_tools=[
        "Read", "Edit", "Write", "MultiEdit",
        "Bash(git diff*)", "Bash(git status*)", "Bash(git ls-files*)", "Bash(git clone*)",
        "Bash(git add*)", "Bash(git commit*)", "Bash(git push*)",
        "Bash(python3 -m unittest*)", "Bash(python3 -m py_compile*)", "Bash(bash -n*)",
    ],
    capabilities=["file-edit", "git-ops", "test-run", "mcp-host", "search"],
))

_register_builtin(AgentBackend(
    name="codex",
    display_name="Codex CLI",
    command=["codex"],
    prompt_flag="--prompt",
    output_format_flags=[],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="codex_json",
    process_pattern="codex",
    prompt_prefix="You are a Codex worker in a Hermes collaboration engine.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["file-edit", "git-ops"],
))

_register_builtin(AgentBackend(
    name="opencode",
    display_name="OpenCode",
    command=["opencode"],
    prompt_flag="-p",
    output_format_flags=[],
    supports_model_flag=False,
    model_flag="",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="raw_text",
    process_pattern="opencode",
    prompt_prefix="You are an OpenCode worker in a Hermes collaboration engine.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["file-edit", "git-ops"],
))

_register_builtin(AgentBackend(
    name="hermes",
    display_name="Hermes Agent",
    command=["hermes"],
    prompt_flag="",
    output_format_flags=[],
    supports_model_flag=True,
    model_flag="--model",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="raw_text",
    process_pattern="hermes",
    prompt_prefix="You are Hermes, the orchestration agent in a collaboration engine.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities=["planning", "analysis", "orchestration", "delegation", "file-edit", "git-ops", "search"],
))


def list_backends() -> list[AgentBackend]:
    """List all registered backends (built-in + custom)."""
    return list(_BUILTINS.values())


def get_backend(name: str) -> AgentBackend:
    """Get a backend by name. Raises KeyError if not found."""
    if name not in _BUILTINS:
        raise KeyError(f"Unknown agent backend: {name!r}. Available: {sorted(_BUILTINS.keys())}")
    return _BUILTINS[name]


def detect_available_backends() -> list[AgentBackend]:
    """Return only backends whose command is available on PATH."""
    return [b for b in _BUILTINS.values() if b.is_available()]


def backends_for_capability(capability: str) -> list[AgentBackend]:
    """Return backends that declare the given capability."""
    return [b for b in _BUILTINS.values() if capability in b.capabilities]


def register_backend(backend: AgentBackend) -> None:
    """Register a custom backend at runtime (or override a built-in)."""
    _BUILTINS[backend.name] = backend


def delete_backend(name: str) -> bool:
    """Remove a registered backend by name. Returns True if removed, False if not found."""
    if name in _BUILTINS:
        del _BUILTINS[name]
        return True
    return False
