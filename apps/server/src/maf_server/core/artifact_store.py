"""Local content-addressed Artifact file storage.

The implementation streams uploads to temporary files, computes SHA-256, and
atomically moves verified content under data/artifacts/sha256/.
"""

from __future__ import annotations

from typing import Protocol


class ArtifactFileStore(Protocol):
    async def put_stream(self, stream, expected_sha256: str | None = None): ...
    async def open(self, content_hash: str): ...
    async def exists(self, content_hash: str) -> bool: ...

