from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from nexapilot.model import Artifact
from nexapilot.store.sqlite import SQLiteStore


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedOutput:
    output: str
    metadata: dict[str, object]
    artifact: Artifact | None


class ArtifactStore:
    def __init__(
        self,
        store: SQLiteStore,
        root: str | Path,
        *,
        threshold_bytes: int = 64 * 1024,
        preview_head_chars: int = 12_000,
        preview_tail_chars: int = 4_000,
    ) -> None:
        self._store = store
        self._root = Path(root).resolve()
        self._threshold_bytes = threshold_bytes
        self._preview_head_chars = preview_head_chars
        self._preview_tail_chars = preview_tail_chars

    @property
    def root(self) -> Path:
        return self._root

    async def materialize_tool_output(
        self,
        *,
        session_id: str,
        run_id: str | None,
        message_id: str,
        tool_call_id: str,
        tool_name: str,
        output: str,
    ) -> MaterializedOutput:
        payload = output.encode("utf-8")
        if len(payload) <= self._threshold_bytes:
            return MaterializedOutput(output=output, metadata={}, artifact=None)
        artifact = await self.put_bytes(
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            kind="tool_output",
            name=f"{tool_name}-{tool_call_id}.txt",
            media_type="text/plain; charset=utf-8",
            content=payload,
        )
        preview = self._bounded_preview(output)
        reference = (
            f"\n\n[Full output stored as artifact {artifact.id}; "
            f"{artifact.size_bytes} bytes; sha256={artifact.sha256}]"
        )
        return MaterializedOutput(
            output=preview + reference,
            metadata={
                "artifact_id": artifact.id,
                "artifact_size_bytes": artifact.size_bytes,
                "artifact_sha256": artifact.sha256,
                "artifact_url": f"/artifacts/{artifact.id}/content",
                "output_offloaded": True,
            },
            artifact=artifact,
        )

    async def put_bytes(
        self,
        *,
        session_id: str,
        run_id: str | None,
        message_id: str | None,
        tool_call_id: str | None,
        kind: str,
        name: str,
        media_type: str,
        content: bytes,
    ) -> Artifact:
        artifact_id = str(uuid4())
        digest = hashlib.sha256(content).hexdigest()
        suffix = self._safe_suffix(name)
        relative = f"{artifact_id}{suffix}"
        self._root.mkdir(parents=True, exist_ok=True)
        destination = (self._root / relative).resolve()
        self._ensure_within_root(destination)
        temporary = self._root / f".{artifact_id}.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        artifact = Artifact(
            id=artifact_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            tool_call_id=tool_call_id,
            kind=kind,
            name=self._safe_name(name),
            media_type=media_type,
            size_bytes=len(content),
            sha256=digest,
            storage_path=relative,
            preview=self._bounded_preview(content.decode("utf-8", errors="replace")),
            created_at=int(time.time() * 1000),
        )
        try:
            return await self._store.add_artifact(artifact)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    async def read(self, artifact_id: str) -> tuple[Artifact, bytes]:
        artifact = await self._store.get_artifact(artifact_id)
        path = (self._root / artifact.storage_path).resolve()
        self._ensure_within_root(path)
        if not path.is_file():
            raise FileNotFoundError(f"artifact content missing: {artifact_id}")
        content = path.read_bytes()
        if len(content) != artifact.size_bytes:
            raise ArtifactIntegrityError(f"artifact size mismatch: {artifact_id}")
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.sha256:
            raise ArtifactIntegrityError(f"artifact hash mismatch: {artifact_id}")
        return artifact, content

    async def delete_session_content(self, session_id: str) -> None:
        for artifact in await self._store.list_artifacts(session_id=session_id):
            path = (self._root / artifact.storage_path).resolve()
            self._ensure_within_root(path)
            path.unlink(missing_ok=True)

    def _bounded_preview(self, text: str) -> str:
        limit = self._preview_head_chars + self._preview_tail_chars
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return (
            text[: self._preview_head_chars]
            + f"\n\n... [{omitted} characters offloaded] ...\n\n"
            + text[-self._preview_tail_chars :]
        )

    def _ensure_within_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise ArtifactIntegrityError("artifact path escapes storage root") from exc

    @staticmethod
    def _safe_name(name: str) -> str:
        cleaned = re.sub(r"[\\/\x00-\x1f\x7f]+", "_", name).strip(" .")
        return cleaned[:180] or "artifact.bin"

    @staticmethod
    def _safe_suffix(name: str) -> str:
        suffix = Path(name).suffix.lower()
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix or "") else ".bin"
