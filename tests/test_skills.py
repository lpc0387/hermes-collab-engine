"""Tests for Hermes worker skill distribution."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode
from src.hermes_collab_engine.skills import SkillEntry, SkillRegistry, get_default_registry


def _extract_prompt(cmd: list[str]) -> str:
    for idx, token in enumerate(cmd):
        if token == "-p" and idx + 1 < len(cmd):
            return cmd[idx + 1]
    return max((arg for arg in cmd if isinstance(arg, str)), key=len)


class SkillRegistryTests(unittest.TestCase):
    def test_builtin_registry_contains_core_skills(self):
        registry = get_default_registry()
        names = {skill.name for skill in registry.list_all()}
        self.assertIn("implementation-focus", names)
        self.assertIn("test-verify", names)
        self.assertIn("search-verify", names)

    def test_select_for_implementation_prefers_coding_and_verification(self):
        registry = get_default_registry()
        skills = registry.select_for_node("implementation", "Implement code and add unittest verification")
        names = [skill.name for skill in skills]
        self.assertIn("implementation-focus", names)
        self.assertIn("test-verify", names)
        self.assertLessEqual(len(skills), 3)

    def test_select_for_node_normalizes_capability_and_empty_task(self):
        registry = get_default_registry()
        skills = registry.select_for_node(" Implementation ", None)
        names = [skill.name for skill in skills]
        self.assertIn("implementation-focus", names)
        self.assertIn("test-verify", names)

    def test_custom_registration_overrides_builtin_name(self):
        registry = SkillRegistry()
        registry.register(SkillEntry(
            name="implementation-focus",
            display_name="Custom Implementation",
            category="coding",
            description="Custom content",
            content="Custom skill body",
            applicable_node_types=["implementation"],
            priority=1,
            source="custom",
        ))
        skill = registry.get("implementation-focus")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.display_name, "Custom Implementation")
        self.assertEqual(skill.source, "custom")

    def test_render_for_prompt_includes_skill_heading_and_content(self):
        registry = SkillRegistry()
        skill = registry.get("test-verify")
        rendered = registry.render_for_prompt([skill])
        self.assertIn("Relevant skills injected by Hermes", rendered)
        self.assertIn("Test & Verification", rendered)
        self.assertIn("test-verify", rendered)
        self.assertIn("narrowest regression test", rendered)


class EngineSkillInjectionTests(unittest.TestCase):
    def test_run_worker_injects_selected_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp)
            node = WBSNode(
                id="wbs-impl",
                title="Implement feature",
                description="Implement the feature and run unittest verification.",
                capability="implementation",
                complexity=4,
                dependencies=[],
                parallelizable=True,
                deliverable="Working implementation",
            )
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False}),
                stderr="",
            )
            with patch("src.hermes_collab_engine.engine.subprocess.run", return_value=completed) as mock_run:
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            prompt = _extract_prompt(mock_run.call_args.args[0])
            self.assertIn("Relevant skills injected by Hermes", prompt)
            self.assertIn("Focused Implementation", prompt)
            self.assertIn("Test & Verification", prompt)

            log = engine.store._one(
                "SELECT data_json FROM logs WHERE run_id=? AND node_id=? AND message='worker skills selected'",
                ("run_test", "wbs-impl"),
            )
            self.assertIsNotNone(log)
            data = json.loads(log["data_json"])
            self.assertIn("implementation-focus", data["skills"])

    def test_run_worker_omits_skills_when_registry_selects_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_registry = SkillRegistry()
            empty_registry._skills = {}
            engine = CollabEngine(Path(tmp) / "db.sqlite3", tmp, skill_registry=empty_registry)
            node = WBSNode(
                id="wbs-general",
                title="General node",
                description="Do general work.",
                capability="general",
                complexity=2,
                dependencies=[],
                parallelizable=True,
                deliverable="General result",
            )
            completed = subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout=json.dumps({"result": "ok", "session_id": "s1", "is_error": False}),
                stderr="",
            )
            with patch("src.hermes_collab_engine.engine.subprocess.run", return_value=completed) as mock_run:
                engine._run_worker("run_test", node, timeout=30)

            prompt = _extract_prompt(mock_run.call_args.args[0])
            self.assertNotIn("Relevant skills injected by Hermes", prompt)


class CLISkillTests(unittest.TestCase):
    def test_skills_command_lists_registry(self):
        proc = subprocess.run(
            ["python3", "-m", "hermes_collab_engine.cli", "skills", "--json"],
            capture_output=True,
            text=True,
            cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        names = {item["name"] for item in data}
        self.assertIn("implementation-focus", names)

    def test_skills_command_can_preview_selection(self):
        proc = subprocess.run(
            [
                "python3", "-m", "hermes_collab_engine.cli", "skills",
                "--node-type", "implementation",
                "--task", "implement code and verify with unittest",
            ],
            capture_output=True,
            text=True,
            cwd="/root/hermes-collab-engine/src",
            env={**__import__("os").environ, "PYTHONPATH": "/root/hermes-collab-engine/src"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("implementation-focus", proc.stdout)
        self.assertIn("test-verify", proc.stdout)


if __name__ == "__main__":
    unittest.main()
