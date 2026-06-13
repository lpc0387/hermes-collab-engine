import unittest
from unittest.mock import patch

import start


class StartupModeSelectionTest(unittest.TestCase):
    def test_choose_interaction_mode_defaults_to_web_panel(self):
        calls = []

        def fake_choose(label, items, default=1):
            calls.append((label, items, default))
            return items[0]

        with patch.object(start, "choose", fake_choose):
            mode = start.choose_interaction_mode()

        self.assertEqual(mode, "web")
        label, items, default = calls[0]
        self.assertIn("操作方式", label)
        self.assertEqual(default, 1)
        self.assertIn("Web", items[0])
        self.assertIn("Hermes 命令行", items[1])

    def test_choose_interaction_mode_can_select_hermes_cli(self):
        def fake_choose(label, items, default=1):
            return items[1]

        with patch.object(start, "choose", fake_choose):
            mode = start.choose_interaction_mode()

        self.assertEqual(mode, "cli")

    def test_get_config_from_hermes_merges_worker_default(self):
        env_cfg = {
            "source": "Hermes .env",
            "source_path": "/tmp/.env",
            "base_url": "https://env.example",
            "token": "token",
            "models": None,
            "default_leader": None,
            "default_worker": None,
        }
        yaml_cfg = {
            "source": "Hermes config.yaml",
            "source_path": "/tmp/config.yaml",
            "base_url": "https://yaml.example",
            "token": "token",
            "models": ["leader-model", "worker-model"],
            "default_leader": "leader-model",
            "default_worker": "worker-model",
        }
        with patch.object(start, "read_hermes_env", return_value=env_cfg), \
             patch.object(start, "read_hermes_config_yaml", return_value=yaml_cfg), \
             patch.object(start, "read_hermes_auth", return_value=None):
            cfg = start.get_config_from_hermes()

        self.assertEqual(cfg["base_url"], "https://env.example")
        self.assertEqual(cfg["default_leader"], "leader-model")
        self.assertEqual(cfg["default_worker"], "worker-model")


if __name__ == "__main__":
    unittest.main()
