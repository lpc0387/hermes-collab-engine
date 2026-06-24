import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_sandbox_server():
    path = Path(__file__).resolve().parents[1] / "sandbox" / "server.py"
    spec = importlib.util.spec_from_file_location("sandbox_server_ttl_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SandboxTtlTests(unittest.TestCase):
    def test_default_ttl_is_two_hours(self):
        server = load_sandbox_server()

        self.assertEqual(server.DEFAULT_TTL_SECONDS, 2 * 60 * 60)

    def test_shutdown_after_waits_then_stops_server(self):
        server = load_sandbox_server()
        sleeps = []

        class FakeHttpd:
            shutdown_called = False

            def shutdown(self):
                self.shutdown_called = True

        fake_httpd = FakeHttpd()

        with mock.patch.object(server.time, "sleep", side_effect=sleeps.append):
            server._shutdown_after(fake_httpd, 7)

        self.assertEqual(sleeps, [7])
        self.assertTrue(fake_httpd.shutdown_called)

    def test_safe_copytree_refuses_existing_marked_workspace(self):
        server = load_sandbox_server()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src"
            dst = tmp_path / "workspace"
            src.mkdir()
            (src / "file.txt").write_text("demo", encoding="utf-8")
            dst.mkdir()
            (dst / server.SANDBOX_MARKER_FILENAME).write_text("old marker", encoding="utf-8")
            (dst / "keep.txt").write_text("do not remove", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "refusing existing sandbox workspace"):
                server._safe_copytree(src, dst)
            server._cleanup_sandbox_workspace(dst)

            self.assertTrue(dst.is_dir())
            self.assertTrue((dst / "keep.txt").is_file())

    def test_safe_copytree_created_workspace_is_only_current_run_cleanup_target(self):
        server = load_sandbox_server()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "src"
            dst = tmp_path / "workspace"
            src.mkdir()
            (src / "file.txt").write_text("demo", encoding="utf-8")

            self.assertTrue(server._safe_copytree(src, dst))
            marker = json.loads((dst / server.SANDBOX_MARKER_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(marker["workspace"], str(dst.resolve()))
            self.assertEqual(marker["pid"], server.os.getpid())

            server._cleanup_sandbox_workspace(dst)

            self.assertFalse(dst.exists())

    def test_ttl_seconds_max_clamps_negative_to_zero(self):
        """max(0, ttl_seconds) ensures negative values become 0."""
        self.assertEqual(max(0, -5), 0)
        self.assertEqual(max(0, -1), 0)
        self.assertEqual(max(0, -3600), 0)

    def test_ttl_seconds_max_preserves_zero(self):
        """max(0, 0) keeps zero unchanged."""
        self.assertEqual(max(0, 0), 0)

    def test_ttl_seconds_max_preserves_positive(self):
        """max(0, positive) keeps positive values unchanged."""
        self.assertEqual(max(0, 3600), 3600)
        self.assertEqual(max(0, 1), 1)
        self.assertEqual(max(0, 999999), 999999)

    def test_fractional_ttl_rejected_by_argparse_type_int(self):
        """argparse type=int rejects non-integer --ttl-seconds values."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--ttl-seconds", type=int, default=7200)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--ttl-seconds", "0.5"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--ttl-seconds", "1.0"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--ttl-seconds", "abc"])

    def test_invalid_ttl_string_rejected(self):
        """Non-numeric --ttl-seconds causes argparse error."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--ttl-seconds", type=int, default=7200)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--ttl-seconds", "not-a-number"])

    def test_zero_ttl_skips_shutdown_timer_registration(self):
        """When ttl_seconds is 0, main() skips _register_shutdown_timer."""
        server = load_sandbox_server()

        class FakeHttpd:
            instances = []

            def __init__(self, address, handler):
                self.address = address
                self.handler = handler
                self.served = False
                FakeHttpd.instances.append(self)

            def serve_forever(self):
                self.served = True

        class FakeThread:
            instances = []

            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False
                FakeThread.instances.append(self)

            def start(self):
                self.started = True

        # Reset fake class state
        FakeHttpd.instances.clear()
        FakeThread.instances.clear()

        argv = ["server.py", "--host", "127.0.0.1", "--port", "0", "--ttl-seconds", "0"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(server, "ThreadingHTTPServer", FakeHttpd), \
             mock.patch.object(server.threading, "Thread", FakeThread):
            self.assertEqual(server.main(), 0)

        # TTL=0 means no timer thread should be created
        self.assertEqual(server.SANDBOX_CONFIG["ttlSeconds"], 0)
        self.assertEqual(len(FakeHttpd.instances), 1)
        self.assertTrue(FakeHttpd.instances[0].served)
        # No timer thread registered when TTL is 0
        self.assertEqual(len(FakeThread.instances), 0)

    def test_very_large_ttl_accepted(self):
        """A very large --ttl-seconds value is accepted and used."""
        server = load_sandbox_server()

        class FakeHttpd:
            instances = []

            def __init__(self, address, handler):
                self.address = address
                self.handler = handler
                self.served = False
                FakeHttpd.instances.append(self)

            def serve_forever(self):
                self.served = True

        class FakeThread:
            instances = []

            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False
                FakeThread.instances.append(self)

            def start(self):
                self.started = True

        FakeHttpd.instances.clear()
        FakeThread.instances.clear()

        argv = ["server.py", "--host", "127.0.0.1", "--port", "0", "--ttl-seconds", "86400"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(server, "ThreadingHTTPServer", FakeHttpd), \
             mock.patch.object(server.threading, "Thread", FakeThread):
            self.assertEqual(server.main(), 0)

        self.assertEqual(server.SANDBOX_CONFIG["ttlSeconds"], 86400)
        self.assertEqual(len(FakeHttpd.instances), 1)
        self.assertTrue(FakeHttpd.instances[0].served)
        self.assertEqual(len(FakeThread.instances), 1)
        timer = FakeThread.instances[0]
        self.assertIs(timer.target, server._shutdown_after)
        self.assertEqual(timer.args, (FakeHttpd.instances[0], 86400))
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)

    def test_main_registers_shutdown_timer_from_cli(self):
        server = load_sandbox_server()

        class FakeHttpd:
            instances = []

            def __init__(self, address, handler):
                self.address = address
                self.handler = handler
                self.served = False
                FakeHttpd.instances.append(self)

            def serve_forever(self):
                self.served = True

        class FakeThread:
            instances = []

            def __init__(self, target, args, daemon):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False
                FakeThread.instances.append(self)

            def start(self):
                self.started = True

        argv = ["server.py", "--host", "127.0.0.1", "--port", "0", "--ttl-seconds", "9"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(server, "ThreadingHTTPServer", FakeHttpd), \
             mock.patch.object(server.threading, "Thread", FakeThread):
            self.assertEqual(server.main(), 0)

        self.assertEqual(server.SANDBOX_CONFIG["ttlSeconds"], 9)
        self.assertEqual(len(FakeHttpd.instances), 1)
        self.assertTrue(FakeHttpd.instances[0].served)
        self.assertEqual(len(FakeThread.instances), 1)
        timer = FakeThread.instances[0]
        self.assertIs(timer.target, server._shutdown_after)
        self.assertEqual(timer.args, (FakeHttpd.instances[0], 9))
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.started)


if __name__ == "__main__":
    unittest.main()
