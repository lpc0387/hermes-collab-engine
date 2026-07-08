"""
8765 Hermes Collab Engine v7.0 — Agent Connectivity Detector

Detects installed agents, tests connectivity with simple requests,
and returns structured health reports for the CLI dialog mode.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .agents import get_backend


@dataclass
class AgentHealth:
    """Health status of a single agent backend."""
    name: str
    display_name: str = ""
    installed: bool = False
    reachable: bool = False
    latency_ms: float = 0.0
    error: str = ""
    model: str = ""
    binary: str = ""
    capabilities: list[str] = field(default_factory=list)


# Lightweight test prompts per agent — minimal, fast, language-agnostic
TEST_PROMPTS: dict[str, str] = {
    "opencode": "Output the number 42 and nothing else.",
    "claude-code": "Output the number 42 and nothing else.",
    "codex": "Output the number 42 and nothing else.",
    "hermes": "Output the number 42 and nothing else.",
    "mimocode": "Output the number 42 and nothing else.",
}


def detect_agent(name: str, model: str | None = None, timeout: int = 30) -> AgentHealth:
    """Detect a single agent: check installation and connectivity.

    Steps:
    1. Look up backend profile via agents.get_backend()
    2. Check if the binary exists on PATH via shutil.which()
    3. If installed, run a minimal test request to verify connectivity
    4. Return AgentHealth with all fields populated
    """
    health = AgentHealth(name=name)

    try:
        backend = get_backend(name)
    except KeyError:
        health.error = f"unknown agent: {name}"
        return health

    health.display_name = backend.display_name
    health.binary = backend.command[0] if isinstance(backend.command, (list, tuple)) else backend.command
    health.capabilities = list(getattr(backend, 'capabilities', []))

    # Step 1: Check binary on PATH
    binary_path = shutil.which(health.binary)
    if binary_path is None:
        health.installed = False
        health.error = f"binary '{health.binary}' not found on PATH"
        return health

    health.installed = True

    # Step 2: Build test command
    test_prompt = TEST_PROMPTS.get(name, "42")
    cmd = backend.build_command(
        prompt=test_prompt,
        model=model,
        allowed_tools=[],
        provider=None,
        reasoning=False,
    )

    # Step 3: Run connectivity test
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None,  # inherit parent env
        )
        elapsed = (time.time() - start) * 1000
        health.latency_ms = round(elapsed, 1)

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # Check if output contains our expected marker "42"
        if "42" in stdout:
            health.reachable = True
            # Extract model info if available from stderr
            for line in stderr.split("\n"):
                if "model:" in line.lower():
                    health.model = line.split(":", 1)[1].strip()
        else:
            # Process ran but output didn't contain expected result
            error_parts = []
            if stderr:
                error_parts.append(stderr[:200])
            if stdout:
                error_parts.append(f"unexpected: {stdout[:100]}")
            health.error = "; ".join(error_parts) if error_parts else "no expected output (42)"
            health.reachable = False
            # Still mark reachable if we got output at all (just unexpected)
            if stdout or proc.returncode == 0:
                health.reachable = True

    except subprocess.TimeoutExpired:
        health.error = f"timed out after {timeout}s"
        health.latency_ms = timeout * 1000
    except FileNotFoundError as e:
        health.error = f"failed to spawn: {e}"
    except Exception as e:
        health.error = f"{type(e).__name__}: {e}"

    if not health.model:
        health.model = model or ""

    return health


def detect_all_agents(
    agent_names: list[str] | None = None,
    model: str | None = None,
    timeout: int = 30,
) -> list[AgentHealth]:
    """Detect all registered agents or a specific subset."""
    if agent_names is None:
        from .agents import list_backends as _lb
        agent_names = [b.name for b in _lb()]

    results = []
    for name in agent_names:
        if name is None:
            continue
        health = detect_agent(name, model=model, timeout=timeout)
        results.append(health)

    return results


def format_health_report(results: list[AgentHealth]) -> str:
    """Format agent health report as a styled terminal table."""
    lines = []
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════╗")
    lines.append("║   Hermes Collab Engine v7.0                         ║")
    lines.append("║══════════════════════════════════════════════════════║")
    lines.append(f"║   {'Agent':<20} {'Status':<10} {'Latency':<8} {'Model':<20} ║")
    lines.append(f"║   {'─'*20} {'─'*10} {'─'*8} {'─'*20} ║")

    for h in results:
        if h.installed and h.reachable:
            status = "✅ 可用"
        elif h.installed and not h.reachable:
            status = "⚠️ 需配置"
        else:
            status = "❌ 未安装"

        latency = f"{h.latency_ms:.0f}ms" if h.latency_ms > 0 else "-"
        model = h.model[:18] if h.model else "-"
        lines.append(f"║   {h.display_name or h.name:<20} {status:<10} {latency:<8} {model:<20} ║")

    lines.append("║" + " " * 54 + "║")
    lines.append("║   Mode: [1] No-Leader  [2] Leader                    ║")
    lines.append("║   Enter mode number (default 1):                      ║")
    lines.append("╚══════════════════════════════════════════════════════╝")
    lines.append("")
    return "\n".join(lines)
