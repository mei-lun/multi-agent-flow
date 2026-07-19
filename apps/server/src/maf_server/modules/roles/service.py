"""Role definition, complete dependency closure validation, and dry runs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Protocol

from maf_contracts.common import ActorContext
from .repository import InMemoryRoleRepository, RoleRepository
from .schemas import *


class RoleVersionValidator(Protocol):
    async def validate(self, draft: CreateRoleVersionRequest) -> ValidationReport: ...


class DependencyCatalog:
    """Optional catalog contract used by the validator; all methods are read-only."""

    async def model_policy(self, policy_id: str) -> dict[str, Any] | None: ...
    async def skill_version(self, version_id: str) -> dict[str, Any] | None: ...
    async def tool_version(self, key: str, version: int) -> dict[str, Any] | None: ...
    async def capability_policy(self, policy_id: str) -> dict[str, Any] | None: ...
    async def network_policy(self, policy_id: str) -> dict[str, Any] | None: ...


class DefaultRoleVersionValidator:
    def __init__(self, catalog: DependencyCatalog | None = None, *, system_limits: dict[str, int] | None = None) -> None:
        self.catalog = catalog
        self.system_limits = system_limits or {"max_steps": 1000, "max_tool_calls": 500, "timeout_seconds": 3600}

    async def validate(self, draft: CreateRoleVersionRequest) -> ValidationReport:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        limits = {
            "max_steps": draft.get("max_steps", 0),
            "max_tool_calls": draft.get("max_tool_calls", 0),
            "timeout_seconds": draft.get("timeout_seconds", 0),
        }
        for field, value in limits.items():
            if not isinstance(value, int) or value <= 0:
                errors.append({"code": "LIMIT_INVALID", "field": field})
            elif value > self.system_limits.get(field, value):
                errors.append({"code": "LIMIT_EXCEEDED", "field": field, "max": self.system_limits[field]})
        if not draft.get("system_prompt"):
            errors.append({"code": "PROMPT_EMPTY", "field": "system_prompt"})
        if self.catalog is None:
            return ValidationReport(valid=not errors, errors=errors, warnings=warnings)
        policy = await self.catalog.model_policy(draft.get("model_policy_id", ""))
        if policy is None or policy.get("status", "PUBLISHED") not in {"PUBLISHED", "ACTIVE"}:
            errors.append({"code": "MODEL_POLICY_UNAVAILABLE", "field": "model_policy_id"})
        bound_tools: set[str] = set()
        for grant in draft.get("tool_grants", []):
            if not isinstance(grant, dict) or not isinstance(grant.get("key"), str) or not isinstance(grant.get("version"), int):
                errors.append({"code": "TOOL_GRANT_INVALID", "field": "tool_grants"})
                continue
            key = f"{grant['key']}:{grant['version']}"
            bound_tools.add(key)
            tool = await self.catalog.tool_version(grant["key"], grant["version"])
            if tool is None or tool.get("status", "PUBLISHED") in {"DISABLED", "RETIRED"}:
                errors.append({"code": "TOOL_VERSION_UNAVAILABLE", "tool": key})
        for skill_id in draft.get("skill_version_ids", []):
            skill = await self.catalog.skill_version(skill_id)
            if skill is None or skill.get("status") != "PUBLISHED":
                errors.append({"code": "SKILL_VERSION_UNPUBLISHED", "skill_version_id": skill_id})
                continue
            for declared in skill.get("declared_tools", []):
                if declared not in bound_tools and not any(item.startswith(f"{declared}:") for item in bound_tools):
                    errors.append({"code": "SKILL_TOOL_NOT_GRANTED", "skill_version_id": skill_id, "tool": declared})
        if await self.catalog.capability_policy(draft.get("capability_policy_version_id", "")) is None:
            errors.append({"code": "CAPABILITY_POLICY_UNAVAILABLE"})
        if await self.catalog.network_policy(draft.get("network_policy_version_id", "")) is None:
            errors.append({"code": "NETWORK_POLICY_UNAVAILABLE"})
        return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


DryRunner = Callable[[RoleVersionView, DryRunRoleRequest, dict[str, Any]], Awaitable[dict[str, Any]]]


class RoleServiceImpl:
    def __init__(self, repository: RoleRepository | None = None, *, validator: RoleVersionValidator | None = None, runner: DryRunner | None = None, system_max_cost: str = "100") -> None:
        self.repository = repository or InMemoryRoleRepository()
        self.validator = validator or DefaultRoleVersionValidator()
        self.runner = runner
        self.system_max_cost = Decimal(system_max_cost)
        self._prompts: dict[str, str] = {}
        self._drafts: dict[str, CreateRoleVersionRequest] = {}
        self._runs: dict[str, DryRunRoleResult] = {}

    async def create_role(self, actor: ActorContext, request: CreateRoleRequest) -> RoleView:
        if not request.get("key") or not request.get("name"):
            raise ValueError("Role key and name are required")
        role_id = f"role-{request['key']}"
        if await self.repository.get_role(role_id) is not None:
            raise ValueError("Role key already exists")
        role = RoleView(
            id=role_id, key=request["key"], name=request["name"], description=request.get("description", ""),
            latest_published_version_id=None, version=1,
        )
        return await self.repository.save_role(role)

    async def create_version(self, actor: ActorContext, role_id: str, request: CreateRoleVersionRequest) -> RoleVersionView:
        if await self.repository.get_role(role_id) is None:
            raise KeyError(role_id)
        await self.validator.validate(request)
        prompt_hash = hashlib.sha256(request["system_prompt"].encode("utf-8")).hexdigest()
        previous = [item for item in getattr(self.repository, "versions", {}).values() if item["role_id"] == role_id]
        version_no = max((item["version"] for item in previous), default=0) + 1
        body = dict(request)
        body["system_prompt"] = prompt_hash
        content_hash = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        version_id = f"{role_id}:v{version_no}"
        view = RoleVersionView(
            id=version_id, role_id=role_id, version=version_no, status="DRAFT",
            system_prompt_hash=prompt_hash, model_policy_id=request["model_policy_id"],
            skill_version_ids=list(request["skill_version_ids"]), tool_grants=copy.deepcopy(request["tool_grants"]),
            capability_policy_version_id=request["capability_policy_version_id"], resource_profile=request["resource_profile"],
            network_policy_version_id=request["network_policy_version_id"],
            limits={"max_steps": request["max_steps"], "max_tool_calls": request["max_tool_calls"], "timeout_seconds": request["timeout_seconds"]},
            content_hash=content_hash,
        )
        self._prompts[version_id] = request["system_prompt"]
        self._drafts[version_id] = copy.deepcopy(request)
        await self.repository.save_version(view)
        return view

    async def publish(self, actor: ActorContext, version_id: str, request: PublishRoleRequest) -> RoleVersionView:
        version = await self.repository.get_version(version_id)
        draft = self._drafts.get(version_id)
        if version is None or draft is None:
            raise KeyError(version_id)
        if version["version"] != request["expected_version"]:
            raise ValueError("Role version conflict")
        report = await self.validator.validate(draft)
        if not report["valid"]:
            raise ValueError(json.dumps(report["errors"], sort_keys=True))
        version["status"] = "PUBLISHED"
        return await self.repository.save_version(version, expected_version=request["expected_version"])

    async def dry_run(self, actor: ActorContext, version_id: str, request: DryRunRoleRequest) -> DryRunRoleResult:
        version = await self.repository.get_version(version_id)
        if version is None or self.runner is None:
            raise ValueError("Role version or isolated runner is unavailable")
        try:
            requested_cost = Decimal(request["max_cost"])
        except (InvalidOperation, TypeError):
            raise ValueError("max_cost must be decimal string")
        if requested_cost < 0:
            raise ValueError("max_cost must be non-negative")
        limits = {**version["limits"], "max_cost": str(min(requested_cost, self.system_max_cost))}
        run_id = f"role-dry-run-{uuid.uuid4()}"
        try:
            output = await asyncio.wait_for(self.runner(version, request, limits), timeout=version["limits"]["timeout_seconds"])
            status = "COMPLETED"
            report = {"output": output, "limits": limits, "role_version_id": version_id}
            outputs = list(output.get("output_artifact_version_ids", [])) if isinstance(output, dict) else []
        except asyncio.TimeoutError:
            status, report, outputs = "FAILED", {"code": "TIMEOUT", "limits": limits}, []
        except Exception as exc:
            status, report, outputs = "FAILED", {"code": "DRY_RUN_FAILED", "message": str(exc)[:240], "limits": limits}, []
        result = DryRunRoleResult(run_id=run_id, status=status, output_artifact_version_ids=outputs, validation_report=report)
        self._runs[run_id] = result
        return result

    def get_dry_run(self, run_id: str) -> DryRunRoleResult | None:
        return copy.deepcopy(self._runs.get(run_id))


__all__ = ["DefaultRoleVersionValidator", "DependencyCatalog", "RoleServiceImpl", "RoleVersionValidator"]
