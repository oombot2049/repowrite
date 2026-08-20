from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from nexapilot.config import _build_config, config_to_dict
from nexapilot.llm.routing import ModelRouter


def _raw_gateway_config() -> dict[str, object]:
    return {
        "openai": {
            "base_url": "https://bootstrap.example/v1",
            "api_key": "bootstrap-key",
            "model": "bootstrap-model",
            "reasoning_effort": "medium",
        },
        "model_gateway": {
            "enabled": True,
            "default_route": "coding",
            "providers": {
                "primary": {
                    "type": "openai",
                    "base_url": "https://primary.example/v1",
                    "api_key_env": "PRIMARY_API_KEY",
                    "transports": ["responses"],
                    "capability_profile": "openai",
                },
                "backup": {
                    "type": "openai_compatible",
                    "base_url": "https://backup.example/v1",
                    "api_key_env": "BACKUP_API_KEY",
                    "transports": ["chat_completions"],
                    "capability_profile": "openai_compatible",
                },
            },
            "models": {
                "premium": {
                    "provider": "primary",
                    "model": "premium-model",
                    "transport": "responses",
                    "context_window": 100000,
                    "tools": True,
                    "provider_state": True,
                    "reasoning_efforts": ["low", "medium", "high"],
                },
                "backup": {
                    "provider": "backup",
                    "model": "backup-model",
                    "transport": "chat_completions",
                    "context_window": 50000,
                    "tools": False,
                    "provider_state": False,
                    "reasoning_efforts": ["none", "medium"],
                },
            },
            "routes": {
                "coding": {
                    "candidates": ["premium", "backup"],
                    "fallback_on": ["rate_limit", "timeout", "server"],
                    "max_fallback_hops": 1,
                    "max_total_attempts": 4,
                    "allow_cross_provider": True,
                }
            },
        },
    }


class ModelGatewayConfigTests(unittest.TestCase):
    def test_documented_example_is_a_valid_enabled_gateway(self) -> None:
        example = Path("docs/examples/model-gateway.yaml")
        raw = yaml.safe_load(example.read_text(encoding="utf-8"))

        cfg = _build_config(raw)

        self.assertTrue(cfg.model_gateway.enabled)
        self.assertEqual(
            cfg.model_gateway.routes["coding-interactive"].max_total_attempts,
            6,
        )

    def test_config_builds_registry_without_resolving_secret_values(self) -> None:
        cfg = _build_config(_raw_gateway_config())

        self.assertTrue(cfg.model_gateway.enabled)
        self.assertEqual(cfg.model_gateway.default_route, "coding")
        self.assertEqual(
            cfg.model_gateway.providers["primary"].api_key_env,
            "PRIMARY_API_KEY",
        )
        self.assertEqual(
            cfg.model_gateway.routes["coding"].candidates,
            ("premium", "backup"),
        )
        serialized = config_to_dict(cfg)
        self.assertNotIn("PRIMARY_API_KEY_VALUE", str(serialized))

    def test_config_rejects_unknown_route_candidate(self) -> None:
        raw = _raw_gateway_config()
        gateway = raw["model_gateway"]
        assert isinstance(gateway, dict)
        routes = gateway["routes"]
        assert isinstance(routes, dict)
        coding = routes["coding"]
        assert isinstance(coding, dict)
        coding["candidates"] = ["missing"]

        with self.assertRaisesRegex(RuntimeError, "unknown models: missing"):
            _build_config(raw)

    def test_router_filters_incompatible_tool_candidate_before_http(self) -> None:
        cfg = _build_config(_raw_gateway_config())
        plan = ModelRouter(cfg.model_gateway, cfg.openai).plan(
            system="system",
            messages=[{"role": "user", "content": "edit the repository"}],
            tools=[{"type": "function", "function": {"name": "write"}}],
            params={"options": {"reasoning_effort": "medium"}},
            requested_model=None,
        )

        self.assertEqual([item.model_alias for item in plan.candidates], ["premium"])
        self.assertEqual(plan.rejected[0].model_alias, "backup")
        self.assertEqual(plan.rejected[0].reason, "tools_unsupported")

    def test_router_does_not_move_opaque_state_across_provider_boundary(self) -> None:
        raw = _raw_gateway_config()
        gateway = raw["model_gateway"]
        assert isinstance(gateway, dict)
        models = gateway["models"]
        assert isinstance(models, dict)
        backup = models["backup"]
        assert isinstance(backup, dict)
        backup["provider_state"] = True
        cfg = _build_config(raw)
        plan = ModelRouter(cfg.model_gateway, cfg.openai).plan(
            system="system",
            messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "provider_state": [{"type": "reasoning", "id": "opaque"}],
                }
            ],
            tools=[],
            params={"options": {"reasoning_effort": "medium"}},
            requested_model=None,
        )

        self.assertEqual([item.model_alias for item in plan.candidates], ["premium"])
        self.assertEqual(plan.rejected[0].model_alias, "backup")
        self.assertEqual(plan.rejected[0].reason, "provider_state_not_portable")


if __name__ == "__main__":
    unittest.main()
