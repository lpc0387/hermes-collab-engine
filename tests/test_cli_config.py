"""Tests for the new `hermes-collab doctor` and `hermes-collab config {show,set,add-provider}` subcommands.

These commands were added to integrate config_store (atomic write, backup rotation,
mask_token) with the user-facing CLI. They are thin wrappers; the heavy lifting
(atomicity, masking, migration) is tested in test_config_store.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, cwd: Path | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run the engine CLI as a subprocess and capture output."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_collab_engine.cli", *args],
        cwd=str(cwd) if cwd else str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_json_emits_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({"worker_model": "test-model", "leader_model": "leader-x"}))
            r = _run_cli("doctor", "--config", str(cfg), "--json")
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            report = json.loads(r.stdout)
            self.assertTrue(report["valid_json"])
            self.assertEqual(report["worker_model"], "test-model")
            self.assertEqual(report["leader_model"], "leader-x")
            self.assertIn("backup_count", report)
            self.assertIn("providers", report["loaded_keys"] or [])  # defaulted

    def test_doctor_human_readable_includes_health_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({"worker_model": "m"}))
            r = _run_cli("doctor", "--config", str(cfg))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            # First line is the ✓/✗ marker plus the path
            first = r.stdout.splitlines()[0]
            self.assertTrue(first.startswith("✓") or first.startswith("✗"))
            self.assertIn(str(cfg), first)

    def test_doctor_flags_missing_or_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "does-not-exist.json"
            r = _run_cli("doctor", "--config", str(cfg), "--json")
            # Should not crash; non-zero exit when invalid
            report = json.loads(r.stdout)
            self.assertFalse(report["exists"])
            self.assertIn("File not found", report["errors"])
            self.assertNotEqual(r.returncode, 0)

    def test_doctor_migrates_legacy_format(self) -> None:
        """A legacy config (no 'providers' field) should still be readable by doctor."""
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            legacy = {
                "leader": {"base_url": "https://x", "api_key": "sk-test-1234567890", "model": "leader-m"},
                "worker": {"base_url": "https://x", "api_key": "sk-test-0987654321", "model": "worker-m"},
                "worker_model": "worker-m",
                "leader_model": "leader-m",
            }
            cfg.write_text(json.dumps(legacy))
            r = _run_cli("doctor", "--config", str(cfg))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("worker_model : worker-m", r.stdout)


class ConfigShowCommandTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_config_show_masks_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({
                "providers": [{"name": "p1", "api_key": "sk-ant-real-ABCD1234"}],
                "worker_model": "m",
            }))
            r = _run_cli("config", "show", "--config", str(cfg))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            data = json.loads(r.stdout)
            # Original api_key preserved, masked copy added
            self.assertEqual(data["providers"][0]["api_key"], "sk-ant-real-ABCD1234")
            self.assertEqual(data["providers"][0]["api_key_masked"], "sk-ant-...1234")


class ConfigSetCommandTests(unittest.TestCase):
    def test_config_set_writes_backup_and_updates_field(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({"worker_model": "old"}))
            r = _run_cli("config", "set", "worker-model", "new", "--config", str(cfg))
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            after = json.loads(cfg.read_text())
            self.assertEqual(after["worker_model"], "new")
            # Backup was created
            backups = list((Path(td) / ".backups").glob(".runtime-config.json.*.bak"))
            self.assertEqual(len(backups), 1)
            # Backup is 600 perms
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_config_set_rejects_unknown_field(self) -> None:
        r = _run_cli("config", "set", "bogus-field", "v")
        self.assertNotEqual(r.returncode, 0)


class ConfigAddProviderCommandTests(unittest.TestCase):
    def test_add_provider_appends_and_sets_active_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({}))
            r = _run_cli(
                "config", "add-provider", "alpha",
                "--base-url", "https://api.example.com",
                "--api-key", "sk-test-1234567890",
                "--protocol", "anthropic",
                "--config", str(cfg),
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            after = json.loads(cfg.read_text())
            self.assertEqual(len(after["providers"]), 1)
            self.assertEqual(after["providers"][0]["name"], "alpha")
            self.assertEqual(after["active_provider"], "alpha")

    def test_add_provider_overwrites_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({
                "providers": [{"name": "p1", "api_key": "old-key-aaaaaaaa", "base_url": "u1"}],
                "active_provider": "p1",
            }))
            _run_cli(
                "config", "add-provider", "p1",
                "--base-url", "u2",
                "--api-key", "new-key-bbbbbbbb",
                "--protocol", "openai",
                "--config", str(cfg),
            )
            after = json.loads(cfg.read_text())
            # Should still be 1 provider (overwrite, not append)
            self.assertEqual(len(after["providers"]), 1)
            self.assertEqual(after["providers"][0]["base_url"], "u2")
            self.assertEqual(after["providers"][0]["protocol"], "openai")

    def test_add_provider_does_not_overwrite_existing_active(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text(json.dumps({
                "providers": [{"name": "first", "api_key": "k-aaaaaaaa"}],
                "active_provider": "first",
            }))
            _run_cli(
                "config", "add-provider", "second",
                "--base-url", "u",
                "--api-key", "k-bbbbbbbb",
                "--config", str(cfg),
            )
            after = json.loads(cfg.read_text())
            # Already-active provider should not be replaced
            self.assertEqual(after["active_provider"], "first")


class DoctorConfigAcceptsBadJSONGracefullyTests(unittest.TestCase):
    def test_doctor_does_not_crash_on_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".runtime-config.json"
            cfg.write_text("{ this is not valid json")
            r = _run_cli("doctor", "--config", str(cfg), "--json")
            report = json.loads(r.stdout)
            self.assertTrue(report["exists"])
            self.assertFalse(report["valid_json"])
            self.assertTrue(any("Invalid JSON" in e for e in report["errors"]))
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
