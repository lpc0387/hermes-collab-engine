import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def load_sandbox_server():
    path = Path(__file__).resolve().parents[1] / "sandbox" / "server.py"
    spec = importlib.util.spec_from_file_location("sandbox_server_prod_sync_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SandboxProdFeatureSyncTests(unittest.TestCase):
    def test_sandbox_reuses_shared_skill_and_tool_registries(self):
        server = load_sandbox_server()

        skills = server.skills_payload("implementation", "implement code and verify with unittest")
        tools = server.tools_payload("verification", "verify an MCP read-only tool integration")

        self.assertIn("implementation-focus", {item["name"] for item in skills})
        self.assertIn("test-verify", {item["name"] for item in skills})
        mcp = next(item for item in tools if item["name"] == "mcp-readonly")
        self.assertIn("mcp__filesystem__read_file", mcp["mcp_tools"])

    def test_resume_context_is_sandbox_bounded(self):
        server = load_sandbox_server()
        server.RUNS[:] = [
            {"id": "sandbox-demo-999", "title": "Sandbox previous run", "status": "completed", "created_at": "2026-06-14T00:00:00+00:00"}
        ]
        server.RUN_DETAILS.clear()
        server.RUN_DETAILS["sandbox-demo-999"] = {
            "id": "sandbox-demo-999",
            "title": "Sandbox previous run",
            "status": "completed",
            "nodes": [{"id": "node-1", "title": "Done", "status": "completed", "result": "ok"}],
            "logs": [{"timestamp": "2026-06-14T00:00:01+00:00", "level": "info", "node_id": "node-1", "message": "done"}],
        }

        context = server.resume_context()
        prompt, prompt_context = server.resume_prompt("continue safely")

        self.assertIsNotNone(context)
        self.assertTrue(context["sandbox"])
        self.assertEqual(context["run"]["id"], "sandbox-demo-999")
        self.assertIn("Sandbox session resume context", prompt)
        self.assertIn("production context is not loaded", prompt)
        self.assertEqual(prompt_context["run"]["id"], "sandbox-demo-999")

    def test_compact_title_uses_ellipsis_without_overlong_truncation(self):
        server = load_sandbox_server()
        long_title = "任务" * 80

        title = server.compact_title(long_title)

        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.endswith("…"))

    def test_sandbox_real_workspace_still_rejects_repo_root(self):
        server = load_sandbox_server()

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            with self.assertRaisesRegex(RuntimeError, "protected sandbox workspace"):
                server._safe_copytree(src, server.REPO_ROOT)


if __name__ == "__main__":
    unittest.main()
