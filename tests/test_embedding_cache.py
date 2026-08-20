from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nexapilot.memory.store import MemoryIndexStore


class EmbeddingCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_is_scoped_by_provider_model_and_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryIndexStore(str(Path(tmp) / "memory.sqlite3"))
            await store.init()
            await store.put_cached_embeddings(
                provider="provider-a",
                model="model-a",
                embeddings={"hash-1": [1.0, 2.0]},
                now_ms=10,
            )

            self.assertEqual(
                await store.get_cached_embeddings(
                    provider="provider-a",
                    model="model-a",
                    content_hashes=["hash-1", "hash-missing"],
                ),
                {"hash-1": [1.0, 2.0]},
            )
            self.assertEqual(
                await store.get_cached_embeddings(
                    provider="provider-a",
                    model="model-b",
                    content_hashes=["hash-1"],
                ),
                {},
            )
            self.assertEqual(
                await store.get_cached_embeddings(
                    provider="provider-b",
                    model="model-a",
                    content_hashes=["hash-1"],
                ),
                {},
            )


if __name__ == "__main__":
    unittest.main()
