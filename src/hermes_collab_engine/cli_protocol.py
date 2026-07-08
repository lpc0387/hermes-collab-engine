"""
8765 Hermes Collab Engine v7.0 — CLI Guardian Protocol

Replaces web SSE for guardian event delivery.
All events are printed to stdout with structured formatting.
Attention events prompt user via stdin for decision.
"""

from __future__ import annotations

import sys
from typing import Any


class CLIGuardianProtocol:
    """Terminal-based guardian protocol — push events to stdout, read decisions from stdin.

    Used by Leader mode to replace the web SSE channel.
    """

    # ANSI color codes for terminal output
    _CYAN = "\033[36m"
    _GREEN = "\033[32m"
    _YELLOW = "\033[33m"
    _RED = "\033[31m"
    _BOLD = "\033[1m"
    _RESET = "\033[0m"

    def __init__(self, quiet: bool = False):
        self.quiet = quiet

    def _print(self, msg: str, color: str = "") -> None:
        if self.quiet:
            return
        if color:
            print(f"{color}{msg}{self._RESET}", flush=True)
        else:
            print(msg, flush=True)

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Route an event to the appropriate handler based on type."""
        handler_map = {
            "guardian:stream": self._handle_stream,
            "guardian:leader_attention": self._handle_attention,
            "guardian:need_input": self._handle_need_input,
            "guardian:completed": self._handle_completed,
            "guardian:worker_error": self._handle_error,
            "guardian:worker_joined": self._handle_worker_joined,
            "guardian:interrupted": self._handle_interrupted,
            "lifecycle:worker_completed": self._handle_worker_completed,
        }
        handler = handler_map.get(event_type, self._handle_unknown)
        handler(data)

    def emit_stream(self, node_id: str, detail: str) -> None:
        """Quick helper for stream events."""
        self._print(f"  → [{self._CYAN}{node_id}{self._RESET}] {detail}")

    def emit_completed(self, node_id: str, duration: float) -> None:
        """Quick helper for completion events."""
        self._print(f"  → [{self._GREEN}{node_id}{self._RESET}] ✅ 完成 ({duration:.0f}s)", color=self._GREEN)

    def emit_error(self, node_id: str, error: str) -> None:
        """Quick helper for error events."""
        self._print(f"  → [{self._RED}{node_id}{self._RESET}] ❌ {error}", color=self._RED)

    def emit_aggregate(self, summary: str) -> None:
        """Print aggregation result with separator."""
        width = 55
        self._print("")
        self._print("═" * width, color=self._BOLD)
        self._print("  汇总结果:", color=self._BOLD)
        self._print("═" * width, color=self._BOLD)
        self._print(summary)

    # ── Internal handlers ──────────────────────────────────────

    def _handle_stream(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        detail = data.get("detail", "")
        self.emit_stream(node_id, detail)

    def _handle_attention(self, data: dict[str, Any]) -> bool | None:
        """Handle leader attention event — asks user for decision.

        Returns True if user chose to interrupt, False to continue, None if no decision needed.
        """
        node_id = data.get("node_id", "?")
        reason = data.get("reason", "attention")
        detail = data.get("detail", "")
        self._print(
            f"  → [{self._YELLOW}GUARDIAN{self._RESET}] "
            f"{self._YELLOW}⚠️ [{node_id}] {reason}: {detail}{self._RESET}"
        )
        try:
            choice = input("  是否中断 worker？[y/N] ").strip().lower()
            return choice in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _handle_need_input(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        detail = data.get("detail", "")
        self._print(
            f"  → [{self._YELLOW}HITL{self._RESET}] "
            f"{self._YELLOW}🖐️ [{node_id}] 需要用户介入: {detail}{self._RESET}"
        )

    def _handle_completed(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        duration = data.get("duration_seconds", 0)
        self.emit_completed(node_id, duration)

    def _handle_error(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        error = data.get("error", "") or data.get("detail", "")
        self.emit_error(node_id, error)

    def _handle_worker_joined(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        agent = data.get("agent", "?")
        self._print(f"  → [{self._CYAN}{node_id}{self._RESET}] 👷 {agent} 已接入")

    def _handle_interrupted(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        self._print(f"  → [{self._RED}{node_id}{self._RESET}] ⛔ 已中断", color=self._RED)

    def _handle_worker_completed(self, data: dict[str, Any]) -> None:
        node_id = data.get("node_id", "?")
        self._print(f"  → [{self._GREEN}{node_id}{self._RESET}] ✅ worker 完成", color=self._GREEN)

    def _handle_unknown(self, data: dict[str, Any]) -> None:
        pass  # silently ignore unknown event types
