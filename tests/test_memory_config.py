from __future__ import annotations

import unittest

from nexapilot.config import _build_config, config_to_dict


def _minimal_config(**memory: object):
    return _build_config(
        {
            "openai": {
                "base_url": "https://example.test/v1",
                "api_key": "test-key",
                "model": "test-model",
            },
            "memory": memory,
        }
    )


class MemoryConfigTests(unittest.TestCase):
    def test_production_memory_features_are_safe_by_default(self) -> None:
        cfg = _minimal_config()

        self.assertTrue(cfg.memory.enabled)
        self.assertFalse(cfg.memory.processing.enabled)
        self.assertEqual(cfg.memory.processing.worker_interval_ms, 1_000)
        self.assertEqual(cfg.memory.processing.max_attempts, 5)
        self.assertFalse(cfg.memory.episodic.enabled)
        self.assertFalse(cfg.memory.semantic.enabled)
        self.assertFalse(cfg.memory.core.enabled)
        self.assertEqual(cfg.memory.core.max_tokens, 1_200)
        self.assertFalse(cfg.memory.context_manager.enabled)
        self.assertTrue(cfg.memory.context_manager.shadow_mode)
        self.assertEqual(cfg.memory.context_manager.max_input_tokens, 32_000)

    def test_production_memory_features_can_be_enabled_independently(self) -> None:
        cfg = _minimal_config(
            processing={"enabled": True, "worker_interval_ms": 250, "max_attempts": 8},
            episodic={"enabled": True},
            semantic={"enabled": True},
            core={"enabled": True, "max_tokens": 900},
            context_manager={
                "enabled": True,
                "shadow_mode": False,
                "max_input_tokens": 16_000,
                "reserved_output_tokens": 2_000,
            },
        )

        self.assertTrue(cfg.memory.processing.enabled)
        self.assertEqual(cfg.memory.processing.worker_interval_ms, 250)
        self.assertEqual(cfg.memory.processing.max_attempts, 8)
        self.assertTrue(cfg.memory.episodic.enabled)
        self.assertTrue(cfg.memory.semantic.enabled)
        self.assertTrue(cfg.memory.core.enabled)
        self.assertEqual(cfg.memory.core.max_tokens, 900)
        self.assertTrue(cfg.memory.context_manager.enabled)
        self.assertFalse(cfg.memory.context_manager.shadow_mode)
        self.assertEqual(cfg.memory.context_manager.max_input_tokens, 16_000)
        self.assertEqual(cfg.memory.context_manager.reserved_output_tokens, 2_000)

    def test_invalid_nested_values_fall_back_without_breaking_startup(self) -> None:
        cfg = _minimal_config(
            sync_interval_seconds="invalid",
            processing={"enabled": "invalid", "worker_interval_ms": "invalid", "max_attempts": 0},
            episodic="invalid",
            context_manager={"shadow_mode": "off"},
        )

        self.assertEqual(cfg.memory.sync_interval_seconds, 3)
        self.assertFalse(cfg.memory.processing.enabled)
        self.assertEqual(cfg.memory.processing.worker_interval_ms, 1_000)
        self.assertEqual(cfg.memory.processing.max_attempts, 1)
        self.assertFalse(cfg.memory.episodic.enabled)
        self.assertFalse(cfg.memory.context_manager.shadow_mode)

    def test_config_serialization_includes_nested_memory_controls(self) -> None:
        data = config_to_dict(_minimal_config())

        self.assertEqual(data["memory"]["processing"]["max_attempts"], 5)
        self.assertEqual(data["memory"]["core"]["max_tokens"], 1_200)
        self.assertTrue(data["memory"]["context_manager"]["shadow_mode"])


if __name__ == "__main__":
    unittest.main()
