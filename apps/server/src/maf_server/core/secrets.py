"""Secret references backed by OS keyring with encrypted fallback."""

from __future__ import annotations

from typing import Protocol


class SecretStore(Protocol):
    async def create(self, name: str, plaintext: str) -> str: ...
    async def resolve(self, secret_id: str) -> str: ...
    async def revoke(self, secret_id: str) -> None: ...

