from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from nexapilot.config import (
    ModelGatewayConfig,
    ModelProviderConfig,
    ModelRouteConfig,
    ModelTargetConfig,
    OpenAIConfig,
    ProviderBudgetConfig,
    ProviderCircuitConfig,
    ProviderFallbackConfig,
    ProviderPricingConfig,
    ProviderResilienceConfig,
    ProviderRetryConfig,
    ProviderTimeoutConfig,
)
from nexapilot.llm.capabilities import CapabilityResolver
from nexapilot.llm.errors import ProviderCallFailed, ProviderUpstreamError
from nexapilot.llm.gateway import ProviderGateway
from nexapilot.llm.openai_chat import (
    Finish,
    LLMEvent,
    ResponseStarted,
    TextDelta,
    Usage,
)
from nexapilot.llm.routing import ModelRouter
from nexapilot.model import ModelRef, Session
from nexapilot.store.sqlite import SQLiteStore


class _ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"code": "test_error", "message": message}}
        self.response = SimpleNamespace(
            headers={"Retry-After": retry_after} if retry_after else {}
        )


class _ScriptedProvider:
    def __init__(self, scripts: list[list[LLMEvent | BaseException | tuple]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any) -> AsyncIterator[LLMEvent]:
        self.calls.append(kwargs)
        if not self.scripts:
            raise AssertionError("unexpected provider attempt")
        script = self.scripts.pop(0)
        for action in script:
            if isinstance(action, BaseException):
                raise action
            if isinstance(action, tuple) and action[0] == "sleep":
                await asyncio.sleep(float(action[1]))
                continue
            yield action


class _MaximumRandom:
    @staticmethod
    def uniform(start: float, end: float) -> float:
        return end


class ProviderGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(str(Path(self.tmp.name) / "gateway.sqlite3"))
        await self.store.init()
        now = int(time.time() * 1000)
        self.session = Session(
            id=str(uuid4()),
            title="provider gateway",
            worktree=self.tmp.name,
            cwd=self.tmp.name,
            created_at=now,
            updated_at=now,
            permission_rules=[],
        )
        await self.store.create_session(self.session)
        self.run = await self.store.create_run(
            session_id=self.session.id,
            trigger_message_id=None,
            source="test",
            agent_name="primary",
            model=ModelRef(provider="openai", id="test-model"),
        )
        self.step = await self.store.create_run_step(
            run_id=self.run.id,
            kind="model",
        )

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def config(
        self,
        *,
        transport: str = "auto",
        reasoning_effort: str = "medium",
        retry: ProviderRetryConfig | None = None,
        timeout: ProviderTimeoutConfig | None = None,
        circuit: ProviderCircuitConfig | None = None,
        fallback: ProviderFallbackConfig | None = None,
        budgets: ProviderBudgetConfig | None = None,
        pricing: ProviderPricingConfig | None = None,
    ) -> OpenAIConfig:
        resilience = ProviderResilienceConfig(
            retry=retry or ProviderRetryConfig(max_attempts=3, base_delay_ms=1),
            timeout=timeout
            or ProviderTimeoutConfig(
                connect_ms=500,
                first_event_ms=500,
                idle_stream_ms=500,
                total_attempt_ms=2_000,
            ),
            circuit_breaker=circuit or ProviderCircuitConfig(enabled=False),
            fallback=fallback or ProviderFallbackConfig(same_model_transport=False),
        )
        return OpenAIConfig(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="test-model",
            transport=transport,  # type: ignore[arg-type]
            reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
            resilience=resilience,
            budgets=budgets or ProviderBudgetConfig(),
            pricing=pricing or ProviderPricingConfig(),
        )

    async def collect(
        self,
        gateway: ProviderGateway,
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        params: dict[str, object] | None = None,
        cancel_check=None,
    ) -> list[LLMEvent]:
        return [
            event
            async for event in gateway.stream(
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                tools=tools or [],
                model=model,
                params=params,
                run_id=self.run.id,
                step_id=self.step.id,
                cancel_check=cancel_check,
            )
        ]

    async def test_model_override_reaches_planner_adapter_and_call_record(self) -> None:
        responses = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "req-override"),
                    Finish("finish", "stop"),
                ]
            ]
        )
        gateway = ProviderGateway(
            config=self.config(),
            store=self.store,
            adapters={"responses": responses},
        )

        await self.collect(gateway, model="review-model")

        self.assertEqual(responses.calls[0]["model"], "review-model")
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(calls[0]["model"], "review-model")

    async def test_auto_selects_responses_for_reasoning_and_persists_usage(
        self,
    ) -> None:
        responses = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "req-1"),
                    TextDelta("text_delta", "done"),
                    Usage("usage", 100, 25, 10, 5, "req-1"),
                    Finish("finish", "stop"),
                ]
            ]
        )
        chat = _ScriptedProvider([])
        cfg = self.config(
            pricing=ProviderPricingConfig(
                input_per_million=2.0,
                cached_input_per_million=1.0,
                output_per_million=8.0,
                version="test-pricing",
            )
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": responses, "chat_completions": chat},
        )

        events = await self.collect(gateway, tools=[{"type": "function"}])

        self.assertTrue(any(isinstance(event, TextDelta) for event in events))
        self.assertEqual(len(responses.calls), 1)
        self.assertEqual(len(chat.calls), 0)
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(calls[0]["transport"], "responses")
        self.assertEqual(calls[0]["status"], "completed")
        self.assertEqual(calls[0]["provider_request_id"], "req-1")
        self.assertEqual(calls[0]["input_tokens"], 100)
        self.assertEqual(calls[0]["estimated_cost_microusd"], 390)

    async def test_incompatible_explicit_chat_fails_before_http_request(self) -> None:
        chat = _ScriptedProvider([])
        cfg = self.config(transport="chat_completions")
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"chat_completions": chat},
        )

        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(
                gateway,
                tools=[{"type": "function"}],
                params={"options": {"reasoning_effort": "medium"}},
            )

        self.assertEqual(caught.exception.error.code, "provider_capability_mismatch")
        self.assertEqual(chat.calls, [])
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(calls[0]["status"], "failed")
        self.assertEqual(calls[0]["error_code"], "provider_capability_mismatch")
        self.assertEqual(await self.store.list_llm_call_attempts(calls[0]["id"]), [])

    async def test_rate_limit_retries_before_output_and_succeeds(self) -> None:
        provider = _ScriptedProvider(
            [
                [_ProviderHTTPError("busy", status_code=429)],
                [
                    ResponseStarted("response_started", "req-2"),
                    TextDelta("text_delta", "ok"),
                    Finish("finish", "stop"),
                ],
            ]
        )
        cfg = self.config(transport="responses")
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": provider},
            random_source=_MaximumRandom(),
        )

        await self.collect(gateway)

        calls = await self.store.list_run_llm_calls(self.run.id)
        attempts = await self.store.list_llm_call_attempts(calls[0]["id"])
        self.assertEqual([item["status"] for item in attempts], ["failed", "completed"])
        self.assertEqual(attempts[0]["error_code"], "provider_rate_limited")
        self.assertEqual(calls[0]["status"], "completed")

    async def test_stream_rate_limit_event_retries_and_succeeds(self) -> None:
        provider = _ScriptedProvider(
            [
                [
                    ProviderUpstreamError(
                        "Rate limit reached on tokens per min. Please try again in 0.001s.",
                        code="rate_limit_exceeded",
                    )
                ],
                [
                    ResponseStarted("response_started", "req-2"),
                    TextDelta("text_delta", "ok"),
                    Finish("finish", "stop"),
                ],
            ]
        )
        gateway = ProviderGateway(
            config=self.config(transport="responses"),
            store=self.store,
            adapters={"responses": provider},
            random_source=_MaximumRandom(),
        )

        await self.collect(gateway)

        calls = await self.store.list_run_llm_calls(self.run.id)
        attempts = await self.store.list_llm_call_attempts(calls[0]["id"])
        self.assertEqual([item["status"] for item in attempts], ["failed", "completed"])
        self.assertEqual(attempts[0]["error_code"], "provider_rate_limited")

    async def test_never_retries_after_semantic_output(self) -> None:
        provider = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "req-partial"),
                    TextDelta("text_delta", "partial"),
                    ConnectionError("stream broke"),
                ],
                [Finish("finish", "stop")],
            ]
        )
        gateway = ProviderGateway(
            config=self.config(transport="responses"),
            store=self.store,
            adapters={"responses": provider},
        )

        received: list[LLMEvent] = []
        with self.assertRaises(ProviderCallFailed) as caught:
            async for event in gateway.stream(
                system="s",
                messages=[],
                tools=[],
                run_id=self.run.id,
                step_id=self.step.id,
            ):
                received.append(event)

        self.assertTrue(caught.exception.partial_output)
        self.assertTrue(any(isinstance(event, TextDelta) for event in received))
        self.assertEqual(len(provider.calls), 1)
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(calls[0]["status"], "interrupted")

    async def test_first_event_timeout_is_classified_and_retried(self) -> None:
        provider = _ScriptedProvider(
            [
                [ResponseStarted("response_started", "req-slow"), ("sleep", 0.2)],
                [
                    ResponseStarted("response_started", "req-fast"),
                    TextDelta("text_delta", "ok"),
                    Finish("finish", "stop"),
                ],
            ]
        )
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=2, base_delay_ms=1),
            timeout=ProviderTimeoutConfig(
                connect_ms=1_000,
                first_event_ms=50,
                idle_stream_ms=1_000,
                total_attempt_ms=5_000,
            ),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": provider},
        )

        await self.collect(gateway)

        calls = await self.store.list_run_llm_calls(self.run.id)
        attempts = await self.store.list_llm_call_attempts(calls[0]["id"])
        self.assertEqual(attempts[0]["error_code"], "provider_first_event_timeout")
        self.assertEqual(attempts[1]["status"], "completed")

    async def test_context_rebuild_is_a_new_linked_call_not_an_attempt(self) -> None:
        overflow = _ProviderHTTPError(
            "maximum context length exceeded",
            status_code=400,
        )
        overflow.body["error"]["code"] = "context_length_exceeded"
        provider = _ScriptedProvider(
            [
                [overflow],
                [
                    ResponseStarted("response_started", "req-rebuilt"),
                    TextDelta("text_delta", "recovered"),
                    Finish("finish", "stop"),
                ],
            ]
        )
        gateway = ProviderGateway(
            config=self.config(transport="responses"),
            store=self.store,
            adapters={"responses": provider},
        )

        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(gateway)
        self.assertEqual(
            caught.exception.error.code,
            "provider_context_overflow",
        )
        self.assertIsNotNone(caught.exception.call_id)

        events = [
            event
            async for event in gateway.stream(
                system="smaller system",
                messages=[{"role": "user", "content": "smaller input"}],
                tools=[],
                run_id=self.run.id,
                step_id=self.step.id,
                parent_call_id=caught.exception.call_id,
                retry_reason="context_rebuild",
            )
        ]

        self.assertTrue(any(isinstance(event, TextDelta) for event in events))
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(len(calls), 2)
        first, rebuilt = calls
        self.assertEqual(len(await self.store.list_llm_call_attempts(first["id"])), 1)
        self.assertEqual(rebuilt["parent_call_id"], first["id"])
        self.assertEqual(rebuilt["retry_reason"], "context_rebuild")
        self.assertNotEqual(rebuilt["request_hash"], first["request_hash"])

    async def test_cancel_during_retry_backoff_terminalizes_call(self) -> None:
        provider = _ScriptedProvider(
            [[_ProviderHTTPError("busy", status_code=429, retry_after="1")]]
        )
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=3, max_retry_after_ms=1_000),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": provider},
        )
        started = time.monotonic()

        async def cancelled() -> bool:
            return time.monotonic() - started > 0.04

        with self.assertRaises(asyncio.CancelledError):
            await self.collect(gateway, cancel_check=cancelled)

        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(calls[0]["status"], "cancelled")
        self.assertEqual(calls[0]["error_code"], "provider_cancelled")

    async def test_circuit_opens_and_later_call_fails_without_http(self) -> None:
        provider = _ScriptedProvider(
            [[ConnectionError("down")], [ConnectionError("still down")]]
        )
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=1),
            circuit=ProviderCircuitConfig(
                enabled=True,
                failure_threshold=2,
                failure_window_ms=60_000,
                cooldown_ms=60_000,
            ),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": provider},
        )

        for _ in range(2):
            with self.assertRaises(ProviderCallFailed):
                await self.collect(gateway)
        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(gateway)

        self.assertEqual(caught.exception.error.code, "provider_circuit_open")
        self.assertEqual(len(provider.calls), 2)

    async def test_run_call_budget_blocks_second_call_before_http(self) -> None:
        provider = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "req-budget"),
                    Finish("finish", "stop"),
                ]
            ]
        )
        cfg = self.config(
            transport="responses",
            budgets=ProviderBudgetConfig(max_calls_per_run=1),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={"responses": provider},
        )

        await self.collect(gateway)
        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(gateway)

        self.assertEqual(caught.exception.error.code, "provider_budget_exceeded")
        self.assertEqual(len(provider.calls), 1)

    def routed_config(
        self,
        *,
        max_total_attempts: int = 4,
        backup_tools: bool = True,
    ) -> ModelGatewayConfig:
        return ModelGatewayConfig(
            enabled=True,
            default_route="coding",
            providers={
                "primary": ModelProviderConfig(
                    provider_type="openai",
                    base_url="https://primary.example/v1",
                    api_key_env="PRIMARY_KEY",
                    transports=("responses",),
                    capability_profile="openai",
                ),
                "backup": ModelProviderConfig(
                    provider_type="openai_compatible",
                    base_url="https://backup.example/v1",
                    api_key_env="BACKUP_KEY",
                    transports=("responses",),
                    capability_profile="openai_compatible",
                ),
            },
            models={
                "premium": ModelTargetConfig(
                    provider="primary",
                    model="premium-model",
                    transport="responses",
                    context_window=100_000,
                ),
                "balanced": ModelTargetConfig(
                    provider="backup",
                    model="balanced-model",
                    transport="responses",
                    context_window=50_000,
                    tools=backup_tools,
                ),
            },
            routes={
                "coding": ModelRouteConfig(
                    candidates=("premium", "balanced"),
                    fallback_on=("rate_limit", "connection", "timeout"),
                    max_fallback_hops=1,
                    max_total_attempts=max_total_attempts,
                )
            },
        )

    async def test_route_falls_back_to_another_provider_and_model(self) -> None:
        primary = _ScriptedProvider(
            [[_ProviderHTTPError("busy", status_code=429)]]
        )
        backup = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "backup-request"),
                    TextDelta("text_delta", "fallback worked"),
                    Finish("finish", "stop"),
                ]
            ]
        )
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=1),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={
                "primary:responses": primary,
                "backup:responses": backup,
            },
            router=ModelRouter(self.routed_config(), cfg),
        )

        events = await self.collect(gateway)

        self.assertEqual(
            [event.text for event in events if isinstance(event, TextDelta)],
            ["fallback worked"],
        )
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(backup.calls), 1)
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual([call["provider"] for call in calls], ["primary", "backup"])
        self.assertEqual(
            [call["model"] for call in calls],
            ["premium-model", "balanced-model"],
        )
        self.assertEqual(calls[1]["fallback_from_call_id"], calls[0]["id"])
        self.assertEqual(calls[1]["metadata"]["fallback_kind"], "model_fallback")

    async def test_route_never_falls_back_after_semantic_output(self) -> None:
        primary = _ScriptedProvider(
            [
                [
                    ResponseStarted("response_started", "partial-request"),
                    TextDelta("text_delta", "partial"),
                    ConnectionError("stream failed"),
                ]
            ]
        )
        backup = _ScriptedProvider([])
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=1),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={
                "primary:responses": primary,
                "backup:responses": backup,
            },
            router=ModelRouter(self.routed_config(), cfg),
        )

        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(gateway)

        self.assertTrue(caught.exception.partial_output)
        self.assertEqual(len(backup.calls), 0)
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "interrupted")

    async def test_route_attempt_budget_does_not_leave_a_dangling_call(self) -> None:
        primary = _ScriptedProvider(
            [[_ProviderHTTPError("busy", status_code=429)]]
        )
        backup = _ScriptedProvider([])
        cfg = self.config(
            transport="responses",
            retry=ProviderRetryConfig(max_attempts=1),
        )
        gateway = ProviderGateway(
            config=cfg,
            store=self.store,
            adapters={
                "primary:responses": primary,
                "backup:responses": backup,
            },
            router=ModelRouter(
                self.routed_config(max_total_attempts=1),
                cfg,
            ),
        )

        with self.assertRaises(ProviderCallFailed) as caught:
            await self.collect(gateway)

        self.assertEqual(caught.exception.error.code, "provider_budget_exceeded")
        self.assertEqual(len(backup.calls), 0)
        calls = await self.store.list_run_llm_calls(self.run.id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "failed")

    def test_capability_resolver_exposes_profile_without_credentials(self) -> None:
        cfg = replace(self.config(), api_key="super-secret")
        payload = CapabilityResolver(cfg).resolve().to_dict()
        self.assertEqual(payload["profile"], "openai")
        self.assertNotIn("super-secret", str(payload))


if __name__ == "__main__":
    unittest.main()
