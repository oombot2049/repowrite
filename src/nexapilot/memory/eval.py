from __future__ import annotations

import math
import time
from typing import Literal

from pydantic import BaseModel, Field

from nexapilot.store.sqlite import SQLiteStore


class MemoryEvalCase(BaseModel):
    name: str
    memory_type: Literal["semantic", "episodic"]
    workspace: str
    query: str
    expected_ids: list[str] = Field(default_factory=list)
    forbidden_ids: list[str] = Field(default_factory=list)
    top_k: int = 5


class MemoryEvaluator:
    def __init__(self, *, store: SQLiteStore) -> None:
        self._store = store

    async def evaluate(self, cases: list[MemoryEvalCase]) -> dict[str, object]:
        results: list[dict[str, object]] = []
        latencies: list[float] = []
        recalls: list[float] = []
        precisions: list[float] = []
        forbidden_hits = 0
        hit_cases = 0
        for case in cases:
            started = time.perf_counter()
            if case.memory_type == "semantic":
                hits = await self._store.search_semantic_memories(
                    case.workspace,
                    case.query,
                    limit=case.top_k,
                )
                hit_ids = [hit.memory.id for hit in hits]
            else:
                hits = await self._store.search_episodes(
                    case.workspace,
                    case.query,
                    limit=case.top_k,
                )
                hit_ids = [hit.episode.id for hit in hits]
            latency_ms = (time.perf_counter() - started) * 1_000
            latencies.append(latency_ms)
            expected = set(case.expected_ids)
            forbidden = set(case.forbidden_ids)
            returned = set(hit_ids)
            true_positives = expected & returned
            leaked = forbidden & returned
            recall = len(true_positives) / len(expected) if expected else 1.0
            precision = len(true_positives) / len(hit_ids) if hit_ids else (1.0 if not expected else 0.0)
            recalls.append(recall)
            precisions.append(precision)
            forbidden_hits += len(leaked)
            hit_cases += int(bool(true_positives) or not expected)
            results.append(
                {
                    "name": case.name,
                    "type": case.memory_type,
                    "query": case.query,
                    "hit_ids": hit_ids,
                    "missing_ids": sorted(expected - returned),
                    "forbidden_hit_ids": sorted(leaked),
                    "recall_at_k": round(recall, 4),
                    "precision_at_k": round(precision, 4),
                    "latency_ms": round(latency_ms, 3),
                }
            )
        count = len(cases)
        sorted_latencies = sorted(latencies)
        p95_index = max(0, math.ceil(len(sorted_latencies) * 0.95) - 1) if sorted_latencies else 0
        return {
            "summary": {
                "cases": count,
                "hit_rate": round(hit_cases / count, 4) if count else 1.0,
                "mean_recall_at_k": round(sum(recalls) / count, 4) if count else 1.0,
                "mean_precision_at_k": round(sum(precisions) / count, 4) if count else 1.0,
                "forbidden_hits": forbidden_hits,
                "mean_latency_ms": round(sum(latencies) / count, 3) if count else 0.0,
                "p95_latency_ms": round(sorted_latencies[p95_index], 3) if sorted_latencies else 0.0,
            },
            "cases": results,
        }
