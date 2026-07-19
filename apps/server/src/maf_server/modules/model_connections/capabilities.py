"""Measured model profiles and immutable fallback policies.

This module deliberately keeps discovery separate from inference routing.  A
profile only advertises a capability after the corresponding fixed probe has
actually run and produced evidence; provider or model names are never used as
capability heuristics.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Final


MODEL_CAPABILITIES: Final[tuple[str, ...]] = (
    "chat",
    "stream",
    "tool",
    "json_schema",
    "vision",
)

_PROBE_CASES: Final[dict[str, dict[str, Any]]] = {
    "chat": {"messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 2},
    "stream": {"messages": [{"role": "user", "content": "Reply OK"}], "stream": True, "max_tokens": 2},
    "tool": {"messages": [{"role": "user", "content": "Call ping"}], "tools": [{"name": "ping", "input_schema": {"type": "object"}}], "max_tokens": 2},
    "json_schema": {"messages": [{"role": "user", "content": "Return ok"}], "response_schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}, "max_tokens": 4},
    "vision": {"messages": [{"role": "user", "content": [{"type": "text", "text": "Describe"}, {"type": "image", "mime_type": "image/png", "data": "iVBORw0KGgo="}]}], "max_tokens": 2},
}


@dataclass(frozen=True)
class CapabilityEvidence:
    capability: str
    supported: bool
    checked_at: str
    summary: str


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    connection_id: str
    model_name: str
    evidence: tuple[CapabilityEvidence, ...]
    probed_at: str

    def supports(self, capability: str) -> bool:
        """Return true only for a successful measured capability."""
        return any(
            item.capability == capability and item.supported for item in self.evidence
        )


ProbeExecutor = Callable[[str, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ModelProfileService:
    """Run deterministic low-cost probes and retain their evidence."""

    def __init__(self, executor: ProbeExecutor) -> None:
        self._executor = executor
        self._profiles: dict[str, ModelProfile] = {}

    async def probe_model(self, connection_id: str, model_name: str) -> ModelProfile:
        if not connection_id or not model_name:
            raise ValueError("connection_id and model_name are required")
        evidence: list[CapabilityEvidence] = []
        for capability in MODEL_CAPABILITIES:
            checked_at = datetime.now(timezone.utc).isoformat()
            try:
                raw = await self._executor(
                    connection_id,
                    model_name,
                    capability,
                    copy.deepcopy(_PROBE_CASES[capability]),
                )
                supported = bool(raw.get("supported", raw.get("ok", False)))
                summary = str(raw.get("summary") or ("probe passed" if supported else "probe failed"))
            except Exception as exc:
                supported = False
                summary = _safe_probe_error(exc)
            evidence.append(
                CapabilityEvidence(capability, supported, checked_at, summary[:240])
            )
        profile_id = f"profile-{uuid.uuid4()}"
        profile = ModelProfile(
            profile_id=profile_id,
            connection_id=connection_id,
            model_name=model_name,
            evidence=tuple(evidence),
            probed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._profiles[profile_id] = profile
        return profile

    def get_profile(self, profile_id: str) -> ModelProfile | None:
        return self._profiles.get(profile_id)

    def supports(self, profile_id: str, required: set[str]) -> bool:
        profile = self._profiles.get(profile_id)
        return profile is not None and all(profile.supports(item) for item in required)


def _safe_probe_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if any(word in lowered for word in ("key", "token", "secret", "authorization", "bearer")):
        return "probe failed (redacted)"
    return f"probe failed: {text}"[:240]


@dataclass(frozen=True)
class ModelPolicyVersion:
    policy_id: str
    version_id: str
    version: int
    primary_profile_id: str
    fallback_profile_ids: tuple[str, ...]
    max_retries: int
    fallback_error_categories: frozenset[str]
    content_hash: str

    @property
    def ordered_profiles(self) -> tuple[str, ...]:
        return (self.primary_profile_id, *self.fallback_profile_ids)


class ModelPolicyService:
    """Create immutable policy versions and make deterministic fallback decisions."""

    _NON_FALLBACK_CATEGORIES: Final[frozenset[str]] = frozenset(
        {"policy", "budget", "permission", "validation", "authentication"}
    )

    def __init__(self) -> None:
        self._versions: dict[str, ModelPolicyVersion] = {}
        self._counts: dict[str, int] = {}

    def create_policy(
        self,
        policy_id: str,
        primary_profile_id: str,
        fallback_profile_ids: list[str],
        *,
        max_retries: int = 0,
        fallback_error_categories: set[str] | None = None,
    ) -> ModelPolicyVersion:
        if not policy_id or not primary_profile_id:
            raise ValueError("policy_id and primary_profile_id are required")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        ordered = [primary_profile_id, *fallback_profile_ids]
        if len(ordered) != len(set(ordered)):
            raise ValueError("model profiles in a policy must be unique")
        categories = frozenset(fallback_error_categories or {"rate_limit", "timeout", "network", "server"})
        version = self._counts.get(policy_id, 0) + 1
        body = {
            "policy_id": policy_id,
            "version": version,
            "primary_profile_id": primary_profile_id,
            "fallback_profile_ids": fallback_profile_ids,
            "max_retries": max_retries,
            "fallback_error_categories": sorted(categories),
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = ModelPolicyVersion(
            policy_id=policy_id,
            version_id=f"{policy_id}:v{version}",
            version=version,
            primary_profile_id=primary_profile_id,
            fallback_profile_ids=tuple(fallback_profile_ids),
            max_retries=max_retries,
            fallback_error_categories=categories,
            content_hash=digest,
        )
        self._versions[result.version_id] = result
        self._counts[policy_id] = version
        return result

    def snapshot(self, version_id: str) -> ModelPolicyVersion:
        """Return the exact policy version captured by a started assignment."""
        return self._versions[version_id]

    def should_fallback(self, version_id: str, error: dict[str, Any]) -> bool:
        policy = self._versions[version_id]
        category = str(error.get("category", "")).lower()
        if category in self._NON_FALLBACK_CATEGORIES:
            return False
        return bool(error.get("retryable")) and category in policy.fallback_error_categories


__all__ = [
    "CapabilityEvidence",
    "MODEL_CAPABILITIES",
    "ModelPolicyService",
    "ModelPolicyVersion",
    "ModelProfile",
    "ModelProfileService",
]
