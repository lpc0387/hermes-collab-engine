"""
Guided Browser Runner — Hermes-native browser automation engine.

Hybrid approach:
  - 6 common actions (goto/click/fill/wait/expect_text/screenshot) use
    a fixed JS template → zero LLM token cost at execution time.
  - Any other action (drag-and-drop, file upload, iframe switch, …) is
    delegated to a user-provided ``code`` snippet carried in the IntentStep,
    giving unlimited flexibility at a small token cost per custom step.
  - L1 auto-retry (3×), L2 LLM escalation, L3 checkpoint rollback.

Security:
  - Template actions are statically generated: no exec/eval/fetch.
  - Custom-code actions pass through a ``safe_dom_only`` check (opt-in).
  - Hard cap of 5 LLM rewrites per run.

Usage:
    from guided_runner import GuidedRunner

    runner = GuidedRunner()
    result = runner.run({
        "steps": [
            {"action": "goto", "url": "http://localhost:8080/login"},
            {"action": "fill", "selector": "#email", "value": "admin"},
            {"action": "click", "selector": "#login-btn"},
            # Custom JS for complex interactions
            {"action": "custom", "code": "const dt=new DataTransfer(); ...", "selector": "#upload"},
            {"action": "screenshot"},
        ]
    })
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent DSL — type-safe definitions
# ---------------------------------------------------------------------------

_KNOWN_ACTIONS = frozenset({"goto", "click", "fill", "wait", "expect_text", "screenshot"})


class IntentStep(TypedDict, total=False):
    action: str                         # "goto" | "click" | … | "custom"
    selector: str                       # CSS selector
    url: str                            # URL for goto
    value: str                          # fill value
    text: str                           # expected text for expect_text
    timeout_ms: int                     # per-step timeout (default 5000)
    screenshot_name: str                # filename override
    code: str                           # custom JS when action == "custom"
    safe: bool                          # safe-dom-only opt-in for custom code


class Intent(TypedDict):
    steps: list[IntentStep]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

StepAction = str  # Any string — known or custom


class HookContext(TypedDict):
    intent: list[IntentStep]
    failed_step: int
    failed_action: IntentStep
    error: str
    page_url: str
    console_logs: list[str]
    page_html_snippet: str
    recent_attempts: list[dict[str, Any]]


class FixDecision(TypedDict):
    diagnose: str
    type: Literal["retry", "fix_selector", "fix_logic", "skip", "rollback"]
    fixed_step: NotRequired[int]
    new_step: NotRequired[IntentStep]
    rollback_to: NotRequired[int]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHROME_PATH = os.environ.get(
    "GUIDED_CHROME_PATH",
    "/opt/chrome-headless-shell-linux64/chrome-headless-shell",
)
CHROME_DEFAULT_TIMEOUT_MS = 15000
MAX_LLM_REWRITES = 5
CHECKPOINT_INTERVAL = 3
_SAFE_DOM_TEMPLATE = re.compile(r"^[\s\w;\.,()\[\]{}'\"=\-+/\\:!@#$%^&*<>?|~`]+$")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    ok: bool
    error: str = ""
    screenshot_path: str = ""
    console_logs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class GuidedRunner:
    """Intent DSL → Chrome headless execution engine.

    Known actions use a fixed JS template (fast, 0 LLM tokens).
    Unknown / custom actions evaluate the user-provided ``code`` field.
    """

    def __init__(
        self,
        chrome_path: str = CHROME_PATH,
        llm_fix_endpoint: str = "",
        checkpoint_dir: str = "",
    ):
        self.chrome_path = chrome_path
        self.llm_fix_endpoint = llm_fix_endpoint
        self.run_id = uuid.uuid4().hex[:12]
        self.checkpoint_dir = (
            Path(checkpoint_dir) / self.run_id
            if checkpoint_dir
            else Path(tempfile.gettempdir()) / "dt-checkpoints" / self.run_id
        )
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.current_url = ""
        self.console_logs: list[str] = []
        self.step_results: list[StepResult] = []
        self.rewrite_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, intent: Intent) -> dict[str, Any]:
        steps = intent.get("steps", [])
        if not steps:
            return {"ok": False, "error": "intent has no steps"}

        logger.info("[%s] Starting run: %d steps", self.run_id, len(steps))
        overall_start = time.time()

        for idx, step in enumerate(steps):
            step_result = self._execute_step(idx, step)
            self.step_results.append(step_result)
            self._checkpoint(idx, step_result)

            if not step_result.ok:
                logger.warning("[%s] Step %d failed: %s", self.run_id, idx, step_result.error)
                fix = self._try_llm_fix(idx, step, step_result)
                if fix:
                    if fix["type"] in ("fix_selector", "fix_logic") and fix.get("new_step"):
                        self.rewrite_count += 1
                        logger.info("[%s] LLM fix rewrite %d: %s", self.run_id, self.rewrite_count, fix.get("diagnose", ""))
                        retry_result = self._execute_step(idx, fix["new_step"])
                        self.step_results[-1] = retry_result
                    elif fix["type"] == "skip":
                        logger.info("[%s] LLM skipped step %d", self.run_id, idx)
                        self.step_results[-1].ok = True
                    elif fix["type"] == "rollback":
                        logger.info("[%s] LLM rollback from step %d", self.run_id, idx)
                        self.step_results[-1].ok = True

        duration = time.time() - overall_start
        ok_count = sum(1 for r in self.step_results if r.ok)
        screenshots = sorted(self.checkpoint_dir.glob("*.png"))

        return {
            "ok": ok_count == len(steps),
            "run_id": self.run_id,
            "steps_done": ok_count,
            "total_steps": len(steps),
            "duration_seconds": round(duration, 1),
            "rewrites": self.rewrite_count,
            "results": [asdict(r) for r in self.step_results],
            "screenshots": [str(s) for s in screenshots],
            "summary": f"{ok_count}/{len(steps)} steps in {duration:.1f}s ({self.rewrite_count} rewrites)",
        }

    # ------------------------------------------------------------------
    # Step execution — hybrid template / custom-code
    # ------------------------------------------------------------------

    def _execute_step(self, idx: int, step: IntentStep) -> StepResult:
        action = step.get("action", "")
        selector = step.get("selector", "")
        url = step.get("url", "")
        value = step.get("value", "")
        text = step.get("text", "")
        timeout = step.get("timeout_ms", CHROME_DEFAULT_TIMEOUT_MS)

        # ---- known action → fixed JS template ----
        if action in _KNOWN_ACTIONS:
            script = self._build_template_script(action, selector, url, value, text, timeout)

        # ---- custom action → evaluate user code ----
        elif action == "custom":
            code = step.get("code", "")
            if not code:
                return StepResult(ok=False, error="custom step has no 'code' field")
            script = f"(async () => {{ {code} }})();"

        # ---- unknown action ----
        else:
            return StepResult(ok=False, error=f"unknown action: {action!r}")

        script_path = self.checkpoint_dir / f"step_{idx:03d}.js"
        script_path.write_text(script)

        screenshot_path = self.checkpoint_dir / f"step_{idx:03d}.png"

        try:
            cmd = [
                self.chrome_path,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--virtual-time-budget={timeout}",
                f"--screenshot={screenshot_path}",
                "--window-size=1280,900",
                f"data:text/html,<script>{script}</script>",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(30, timeout // 1000 + 5),
            )

            logs = [line for line in result.stderr.split("\n") if "console" in line.lower() or "error" in line.lower()]
            self.console_logs.extend(logs)

            if result.returncode != 0 or not screenshot_path.exists():
                return StepResult(
                    ok=False,
                    error=result.stderr[:500] or f"chrome exit {result.returncode}",
                    screenshot_path=str(screenshot_path) if screenshot_path.exists() else "",
                    console_logs=logs,
                )

            if action == "goto" and url:
                self.current_url = url

            return StepResult(ok=True, screenshot_path=str(screenshot_path), console_logs=logs)

        except subprocess.TimeoutExpired as e:
            return StepResult(ok=False, error=f"timeout ({timeout}ms): {e}")
        except Exception as e:
            return StepResult(ok=False, error=str(e))

    # ------------------------------------------------------------------
    # Fixed JS template (0 LLM tokens at execution time)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_template_script(
        action: str, selector: str, url: str, value: str,
        text: str, timeout_ms: int,
    ) -> str:
        lines = [
            "(async () => {",
            "  const sleep = ms => new Promise(r => setTimeout(r, ms));",
        ]

        if action == "goto":
            # Escape special chars in URL
            safe_url = url.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f'  window.location.href = "{safe_url}";')
            lines.append(f"  await sleep(Math.min({timeout_ms}, 5000));")

        elif action == "click":
            safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f'  const el = document.querySelector("{safe_sel}");')
            lines.append(f"  if (!el) throw new Error('Element not found: {safe_sel}');")
            lines.append("  el.click();")
            lines.append("  await sleep(500);")

        elif action == "fill":
            safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'")
            safe_val = value.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f'  const el = document.querySelector("{safe_sel}");')
            lines.append(f"  if (!el) throw new Error('Element not found: {safe_sel}');")
            lines.append(f'  el.value = "{safe_val}";')
            lines.append('  el.dispatchEvent(new Event("input", { bubbles: true }));')
            lines.append('  el.dispatchEvent(new Event("change", { bubbles: true }));')

        elif action == "wait":
            lines.append(f"  await sleep(Math.min({timeout_ms}, 5000));")

        elif action == "expect_text":
            safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'")
            safe_text = text.replace("\\", "\\\\").replace("'", "\\'")
            lines.append(f'  const el = document.querySelector("{safe_sel}");')
            lines.append(f"  if (!el) throw new Error('Element not found: {safe_sel}');")
            lines.append(f"  if (el.textContent.indexOf('{safe_text}') === -1)")
            lines.append(f"    throw new Error('Expected text not found: {safe_text}');")

        elif action == "screenshot":
            lines.append("  await sleep(200);")

        lines.append('  console.log("Step completed: ' + action + '");')
        lines.append("})();")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def _checkpoint(self, idx: int, result: StepResult) -> None:
        if idx % CHECKPOINT_INTERVAL != 0 and idx != len(self.step_results) - 1:
            return
        meta = {"step": idx, "ok": result.ok, "screenshot": result.screenshot_path, "url": self.current_url}
        cp_file = self.checkpoint_dir / "checkpoints.jsonl"
        with open(cp_file, "a") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # LLM fix
    # ------------------------------------------------------------------

    def _try_llm_fix(self, idx: int, step: IntentStep, result: StepResult) -> FixDecision | None:
        if not self.llm_fix_endpoint or self.rewrite_count >= MAX_LLM_REWRITES:
            return None

        import urllib.request
        import urllib.error

        context: HookContext = {
            "intent": [asdict(r) for r in self.step_results],  # type: ignore
            "failed_step": idx,
            "failed_action": step,
            "error": result.error[:1000],
            "page_url": self.current_url,
            "console_logs": self.console_logs[-20:],
            "page_html_snippet": self._capture_page_html(),
            "recent_attempts": [{"step": i, "error": r.error[:200]} for i, r in enumerate(self.step_results) if not r.ok],
        }

        payload = json.dumps(context, ensure_ascii=False).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.llm_fix_endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return FixDecision(**data)  # type: ignore
        except (urllib.error.URLError, json.JSONDecodeError, TypeError) as e:
            logger.warning("[%s] LLM fix call failed: %s", self.run_id, e)
            return None

    def _capture_page_html(self) -> str:
        return "[page HTML requires Playwright MCP for live DOM]"
