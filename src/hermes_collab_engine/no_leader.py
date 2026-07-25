"""
8765 Hermes Collab Engine v7.0 — No-Leader Dispatcher

Directly dispatches tasks to agent CLI subprocesses without going through
CollabEngine.run(). Used by No-Leader mode where you (the Hermes Agent)
act as the orchestrator: assign tasks, collect results, organize peer review,
and do the final check.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkerResult:
    """Result from a single worker dispatch."""
    agent: str
    ok: bool
    stdout: str
    stderr: str
    duration_s: float
    node_id: str = ""
    task: str = ""


@dataclass
class ReviewResult:
    """Peer review evaluation."""
    node_id: str
    reviewer: str
    target: str
    verdict: str          # accept / reject / needs_improvement
    score: int             # 1-10
    criteria: dict[str, int] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    comments: str = ""
    duration_s: float = 0.0


class NoLeaderDispatcher:
    """Dispatch tasks directly to agent binaries.

    No CollabEngine lifecycle — just subprocess.run() with the agent's CLI.
    """

    def __init__(self, cwd: str | Path = ".", model: str | None = None):
        self.cwd = Path(cwd).resolve()
        self.model = model

    def dispatch(
        self,
        agent: str,
        task: str,
        model: str | None = None,
        timeout: int = 300,
    ) -> WorkerResult:
        """Dispatch a single task to an agent subprocess.

        Args:
            agent: Agent name (e.g. 'opencode', 'claude-code')
            task: Task description/prompt
            model: Model override (uses self.model if None)
            timeout: Max seconds to wait

        Returns:
            WorkerResult with stdout/stderr/duration
        """
        from .agents import get_backend

        backend = get_backend(agent)
        model = model or self.model
        cmd = backend.build_command(
            prompt=task,
            model=model,
            allowed_tools=[],
            provider=None,
            reasoning=False,
        )

        start = time.time()

        # Codex two-stage dispatch: codex exec outputs code to stdout but doesn't write files
        if agent == "codex":
            return self._dispatch_codex(backend, task, model, timeout, cmd)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.cwd),
            )
            elapsed = time.time() - start
            return WorkerResult(
                agent=agent,
                ok=proc.returncode == 0,
                stdout=(proc.stdout or "").strip(),
                stderr=(proc.stderr or "").strip(),
                duration_s=round(elapsed, 1),
                task=task[:100],
            )
        except subprocess.TimeoutExpired:
            return WorkerResult(
                agent=agent,
                ok=False,
                stdout="",
                stderr=f"timed out after {timeout}s",
                duration_s=timeout,
                task=task[:100],
            )
        except FileNotFoundError as e:
            return WorkerResult(
                agent=agent,
                ok=False,
                stdout="",
                stderr=str(e),
                duration_s=0,
                task=task[:100],
            )

    def _dispatch_codex(
        self,
        backend: Any,
        task: str,
        model: str | None,
        timeout: int,
        base_cmd: list[str],
    ) -> WorkerResult:
        """Two-stage codex dispatch: generate code → extract → write → execute.

        codex exec outputs generated code to stdout but does NOT write files
        due to sandbox restrictions. This method:
        1. Asks codex to generate code (capture stdout)
        2. Extracts code blocks via regex
        3. Writes extracted code to files (parsed from task or default output)
        4. Optionally executes a verification command
        """
        import re as _re
        import subprocess as _sp

        # Stage 1: Generate code — ask codex to output only the code
        gen_task = (
            f"{task}\n\n"
            f"IMPORTANT: Output ONLY the source code in ``` blocks. "
            f"Do NOT write files — output code blocks only."
        )
        gen_cmd = backend.build_command(
            prompt=gen_task, model=model,
            allowed_tools=[], provider=None, reasoning=False,
        )

        start = time.time()
        try:
            proc = _sp.run(
                gen_cmd, capture_output=True, text=True,
                timeout=timeout, cwd=str(self.cwd),
            )
        except _sp.TimeoutExpired:
            return WorkerResult(
                agent="codex", ok=False, stdout="",
                stderr=f"timed out after {timeout}s",
                duration_s=timeout, task=task[:100],
            )
        elapsed = time.time() - start

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # Stage 2: Extract code blocks from stdout
        code_blocks = _re.findall(
            r"```(?:\w+)?\n(.*?)```", stdout, _re.DOTALL
        )
        extracted_code = code_blocks[0] if code_blocks else stdout

        # Stage 3: Write files (if the task specifies a target path)
        files_written = []
        # Try to infer target file from "write to|save as|->|=>" patterns in task
        _target_match = _re.search(
            r"(?:write to|save as|->|=>|→)\s*([^\s,;]+(?:\.[a-z]+))",
            task, _re.IGNORECASE
        )
        if _target_match:
            _target_path = self.cwd / _target_match.group(1).strip()
            _target_path.parent.mkdir(parents=True, exist_ok=True)
            _target_path.write_text(extracted_code)
            files_written.append(str(_target_path))

        return WorkerResult(
            agent="codex",
            ok=proc.returncode == 0 or bool(code_blocks),
            stdout=stdout,
            stderr=stderr,
            duration_s=round(elapsed, 1),
            task=task[:100],
        )

    def peer_review(
        self,
        target_result: WorkerResult,
        target_agent: str,
        reviewer_agent: str,
        task_description: str = "",
    ) -> ReviewResult:
        """Have one agent review another agent's output.

        Args:
            target_result: The WorkerResult being reviewed
            target_agent: Name of the agent that produced the result
            reviewer_agent: Name of the agent doing the review
            task_description: Original task description for context

        Returns:
            ReviewResult with verdict and suggestions
        """
        review_prompt = (
            f"请对以下代码/结果进行评审。\n\n"
            f"原始任务: {task_description or target_result.task}\n\n"
            f"由 {target_agent} 完成的输出:\n"
            f"```\n{target_result.stdout[:3000]}\n```\n\n"
            f"评审标准:\n"
            f"1. correctness (正确性): 代码逻辑是否正确\n"
            f"2. completeness (完整性): 是否覆盖了所有需求\n"
            f"3. code_quality (代码质量): 可读性、结构化程度\n"
            f"4. test_coverage (测试覆盖): 是否包含测试\n\n"
            f"请以 JSON 格式输出:\n"
            f"{{\n"
            f'  "verdict": "accept|reject|needs_improvement",\n'
            f'  "score": <1-10>,\n'
            f'  "criteria": {{\n'
            f'    "correctness": <1-10>,\n'
            f'    "completeness": <1-10>,\n'
            f'    "code_quality": <1-10>,\n'
            f'    "test_coverage": <1-10>\n'
            f'  }},\n'
            f'  "suggestions": ["建议1", "建议2"],\n'
            f'  "comments": "总体评价"\n'
            f"}}\n"
        )

        result = self.dispatch(reviewer_agent, review_prompt, timeout=120)

        # Parse JSON from review output
        review = self._parse_review_json(result.stdout)

        return ReviewResult(
            node_id=target_result.node_id,
            reviewer=reviewer_agent,
            target=target_agent,
            verdict=review.get("verdict", "needs_improvement"),
            score=review.get("score", 5),
            criteria=review.get("criteria", {}),
            suggestions=review.get("suggestions", []),
            comments=review.get("comments", "")[:500],
            duration_s=result.duration_s,
        )

    def _parse_review_json(self, text: str) -> dict[str, Any]:
        """Parse JSON from review output, with fallback extraction."""
        import json as _json
        import re as _re

        # Try direct parse
        text = text.strip()
        # Remove markdown code fences if present
        text = _re.sub(r"^```(?:json)?\s*", "", text)
        text = _re.sub(r"\s*```$", "", text)

        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            pass

        # Try to find JSON block with regex
        m = _re.search(r"\{[^{}]*\}", text, _re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(0))
            except _json.JSONDecodeError:
                pass

        # Fallback: parse manually
        verdict = "needs_improvement"
        score = 5
        if "accept" in text.lower():
            verdict = "accept"
        elif "reject" in text.lower():
            verdict = "reject"

        m_score = _re.search(r'[Ss]core["\']?\s*:\s*(\d+)', text)
        if m_score:
            score = min(10, max(1, int(m_score.group(1))))

        return {
            "verdict": verdict,
            "score": score,
            "criteria": {},
            "suggestions": [],
            "comments": text[:500],
        }
