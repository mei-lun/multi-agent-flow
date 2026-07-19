"""In-memory Role Definition and immutable version repository."""

from __future__ import annotations

import copy
from typing import Protocol

from .schemas import RoleVersionView, RoleView


class RoleRepository(Protocol):
    async def get_role(self, role_id: str) -> RoleView | None: ...
    async def get_version(self, version_id: str) -> RoleVersionView | None: ...
    async def save_role(self, role: RoleView) -> RoleView: ...
    async def save_version(self, version: RoleVersionView, expected_version: int | None = None) -> RoleVersionView: ...


class InMemoryRoleRepository:
    def __init__(self) -> None:
        self.roles: dict[str, RoleView] = {}
        self.versions: dict[str, RoleVersionView] = {}

    async def get_role(self, role_id: str) -> RoleView | None:
        value = self.roles.get(role_id)
        return copy.deepcopy(value) if value else None

    async def get_version(self, version_id: str) -> RoleVersionView | None:
        value = self.versions.get(version_id)
        return copy.deepcopy(value) if value else None

    async def save_role(self, role: RoleView) -> RoleView:
        self.roles[role["id"]] = copy.deepcopy(role)
        return copy.deepcopy(role)

    async def save_version(self, version: RoleVersionView, expected_version: int | None = None) -> RoleVersionView:
        existing = self.versions.get(version["id"])
        if existing and existing["status"] == "PUBLISHED" and existing != version:
            raise ValueError("published Role versions are immutable")
        if expected_version is not None and existing and existing["version"] != expected_version:
            raise ValueError("Role version conflict")
        self.versions[version["id"]] = copy.deepcopy(version)
        return copy.deepcopy(version)


__all__ = ["InMemoryRoleRepository", "RoleRepository"]
