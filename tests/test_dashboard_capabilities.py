"""Tests for dashboard skill and tool preview payloads."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.hermes_collab_engine.server import DashboardServer


class DashboardCapabilityPayloadTests(unittest.TestCase):
    def test_dashboard_skills_payload_lists_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = DashboardServer("127.0.0.1", 0, str(Path(tmp) / "db.sqlite3"), tmp)
            payload = server.skills_payload()

        names = {item["name"] for item in payload}
        self.assertIn("implementation-focus", names)
        self.assertIn("test-verify", names)

    def test_dashboard_skills_payload_previews_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = DashboardServer("127.0.0.1", 0, str(Path(tmp) / "db.sqlite3"), tmp)
            payload = server.skills_payload("implementation", "implement code and verify with unittest")

        names = [item["name"] for item in payload]
        self.assertIn("implementation-focus", names)
        self.assertIn("test-verify", names)

    def test_dashboard_tools_payload_previews_mcp_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = DashboardServer("127.0.0.1", 0, str(Path(tmp) / "db.sqlite3"), tmp)
            payload = server.tools_payload("verification", "verify an MCP read-only tool integration")

        names = [item["name"] for item in payload]
        mcp_payload = next(item for item in payload if item["name"] == "mcp-readonly")
        self.assertIn("mcp-readonly", names)
        self.assertIn("mcp__filesystem__read_file", mcp_payload["mcp_tools"])


if __name__ == "__main__":
    unittest.main()
