import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.hermes_collab_engine.engine import CollabEngine
from src.hermes_collab_engine.models import WBSNode


class WorkerSkillInjectionTest(unittest.TestCase):
    def test_worker_prompt_includes_selected_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CollabEngine(db_path=Path(tmp) / "collab.sqlite3", cwd=tmp)
            node = WBSNode(
                id="wbs-impl",
                title="Implement feature",
                description="Write code changes and run unittest verification.",
                capability="implementation",
                complexity=2,
                dependencies=[],
                parallelizable=True,
                deliverable="Working implementation",
            )

            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd

                class Proc:
                    returncode = 0
                    stdout = '{"result":"done\\nHERMES-COLLAB-RESULT:{\\\"status\\\":\\\"ok\\\",\\\"summary\\\":\\\"done\\\",\\\"files_modified\\\":[],\\\"verification\\\":[]}","session_id":"s1","is_error":false}'
                    stderr = ""

                return Proc()

            with patch("subprocess.run", side_effect=fake_run):
                result = engine._run_worker("run_test", node, timeout=30)

            self.assertTrue(result.ok)
            prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
            self.assertIn("Relevant skills injected by Hermes", prompt)
            self.assertIn("Focused Implementation", prompt)
            self.assertIn("Test & Verification", prompt)
            logs = engine.store.recent_logs()
            self.assertTrue(any(log["message"] == "worker skills selected" for log in logs))


if __name__ == "__main__":
    unittest.main()
