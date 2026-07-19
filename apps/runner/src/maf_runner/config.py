"""Runner node configuration loaded from environment variables.

The runner does **not** self-host an HTTP control-plane endpoint and does not
call the central server over HTTP for coordination (see the Git coordination
protocol). It fetches ``maf/control`` via Git, submits events to
``maf/node/<node-id>``, and reports progress through Git events. This module
reads ``MAF_*`` environment variables, validates required fields, resolves
relative local paths against the workspace root, and protects secret material
via ``SecretStr``.

Key environment variables (see ``.env.example`` for the full list):

- ``MAF_RUNNER_ID`` (node_id, required), ``MAF_RUNNER_LABELS``
- ``MAF_RUNNER_MAX_CONCURRENCY``
- ``MAF_CONTROL_REMOTE_URL`` (Git remote for control), ``MAF_CONTROL_BRANCH``
- ``MAF_WORKSPACE_ROOT`` (confinement root for local data paths)
- ``MAF_MODEL_MAPPING_PATH``, ``MAF_CAPABILITY_TOKEN_CACHE_PATH``
- ``MAF_LOCAL_GIT_ROOTS``, ``MAF_ALLOWED_DOCKER_IMAGES``
- ``MAF_POLL_INTERVAL_SECONDS``, ``MAF_PROGRESS_INTERVAL_SECONDS``
- ``MAF_GIT_CREDENTIALS_TOKEN`` (SecretStr)
- ``MAF_CAPABILITY_SIGNING_KEY_FILE``
- ``MAF_LOG_LEVEL``, ``MAF_LOG_FILE``
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from maf_runner.docker.profiles import (
    DEFAULT_ALLOWED_IMAGES,
    DEFAULT_MEMORY_LIMIT,
)

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_DEFAULT_CONTROL_BRANCH: Final[str] = "maf/control"
_DEFAULT_POLL_INTERVAL: Final[int] = 30
_DEFAULT_PROGRESS_INTERVAL: Final[int] = 900  # 15 minutes per protocol §8
#: Advertised runner version when ``MAF_RUNNER_SOFTWARE_VERSION`` is unset.
#: Aligned with the project version in ``pyproject.toml``.
_DEFAULT_SOFTWARE_VERSION: Final[str] = "maf-runner-0.0.0"


class NodeSettings(BaseSettings):
    """Runner node settings sourced from environment variables.

    Required fields without a default cause field-level validation errors at
    startup when missing. Relative local data paths are resolved against
    ``workspace_root`` and must remain inside it. Secret material uses
    ``SecretStr`` and is never rendered as plaintext.
    """

    model_config = SettingsConfigDict(
        env_prefix="MAF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- identity & capacity --------------------------------------------- #
    env: str = Field(default="dev", description="Deployment environment name.")
    node_id: str = Field(
        ...,
        validation_alias=AliasChoices("MAF_RUNNER_ID"),
        description="Persistent node identifier, format ``node-<uuid>``.",
    )
    labels: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("MAF_RUNNER_LABELS"),
        description="Capability labels for task matching.",
    )
    max_concurrency: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("MAF_RUNNER_MAX_CONCURRENCY"),
        description="Maximum concurrent assignments the node accepts.",
    )

    # --- node manifest (TASK-013) --------------------------------------- #
    # These fields populate ``NodeManifest`` via ``RunnerRegistry``. They are
    # optional so existing deployments continue to work; the registry fills
    # sensible defaults (e.g. ``display_name`` falls back to ``node_id``).
    display_name: str = Field(
        default="",
        validation_alias=AliasChoices("MAF_RUNNER_DISPLAY_NAME"),
        description="Human-readable name shown in the node manifest.",
    )
    model_aliases: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("MAF_RUNNER_MODEL_ALIASES"),
        description="Supported model aliases advertised in the node manifest.",
    )
    docker_profiles: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("MAF_RUNNER_DOCKER_PROFILES"),
        description="Supported Docker profile names advertised in the manifest.",
    )
    software_version: str = Field(
        default=_DEFAULT_SOFTWARE_VERSION,
        validation_alias=AliasChoices("MAF_RUNNER_SOFTWARE_VERSION"),
        description="Runner software version advertised in the node manifest.",
    )

    # --- Git coordination (no node HTTP) --------------------------------- #
    control_remote_url: str = Field(
        ...,
        validation_alias=AliasChoices("MAF_CONTROL_REMOTE_URL"),
        description="Git remote URL for fetching ``maf/control``.",
    )
    control_branch: str = Field(
        default=_DEFAULT_CONTROL_BRANCH,
        description="Git coordination control branch name.",
    )

    # --- local paths ---------------------------------------------------- #
    workspace_root: Path = Field(
        ...,
        validation_alias=AliasChoices("MAF_WORKSPACE_ROOT"),
        description="Root directory for task workspaces and local data.",
    )
    model_mapping_path: Path = Field(
        ...,
        validation_alias=AliasChoices("MAF_MODEL_MAPPING_PATH"),
        description="Path to the local model alias mapping file.",
    )
    capability_token_cache_path: Path = Field(
        ...,
        validation_alias=AliasChoices("MAF_CAPABILITY_TOKEN_CACHE_PATH"),
        description="Path to the capability token cache (relative to workspace).",
    )
    local_git_roots: list[Path] = Field(
        default_factory=list,
        validation_alias=AliasChoices("MAF_LOCAL_GIT_ROOTS"),
        description="Allowed Git working tree roots for task repos.",
    )

    # --- docker ---------------------------------------------------------- #
    allowed_docker_images: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("MAF_ALLOWED_DOCKER_IMAGES"),
        description="Whitelist of Docker image references for task containers.",
    )
    docker_socket: str = Field(
        default="/var/run/docker.sock",
        validation_alias=AliasChoices("MAF_DOCKER_SOCKET"),
        description="Docker daemon socket path used for environment self-checks.",
    )
    docker_binary: str = Field(
        default="docker",
        validation_alias=AliasChoices("MAF_DOCKER_BINARY"),
        description="Docker CLI binary name (whitelisted by subprocess boundary).",
    )
    git_binary: str = Field(
        default="git",
        validation_alias=AliasChoices("MAF_GIT_BINARY"),
        description="Git CLI binary name (whitelisted by subprocess boundary).",
    )

    # --- timing ---------------------------------------------------------- #
    poll_interval_seconds: int = Field(
        default=_DEFAULT_POLL_INTERVAL,
        ge=1,
        validation_alias=AliasChoices("MAF_POLL_INTERVAL_SECONDS"),
        description="Seconds between control fetch polls.",
    )
    progress_interval_seconds: int = Field(
        default=_DEFAULT_PROGRESS_INTERVAL,
        ge=1,
        validation_alias=AliasChoices("MAF_PROGRESS_INTERVAL_SECONDS"),
        description="Maximum seconds between progress events (protocol §8).",
    )

    # --- logging --------------------------------------------------------- #
    log_level: str = Field(default="INFO", description="Logging level.")
    log_file: Path | None = Field(default=None, description="Optional log file path.")

    # --- secret material (SecretStr / key file paths) ------------------- #
    git_credentials_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MAF_GIT_CREDENTIALS_TOKEN"),
        description="Optional Git credentials token for the control remote.",
    )
    capability_signing_key_file: Path | None = Field(
        default=None,
        description="Path to the capability token signing key file.",
    )

    # ------------------------------------------------------------------ #
    # field validators
    # ------------------------------------------------------------------ #

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
            )
        return upper

    @field_validator("control_branch")
    @classmethod
    def _validate_control_branch(cls, value: str) -> str:
        if not value:
            raise ValueError("control_branch must not be empty")
        if any(ch.isspace() for ch in value):
            raise ValueError("control_branch must not contain whitespace")
        if ".." in value:
            raise ValueError("control_branch must not contain '..'")
        if value.startswith("/") or value.startswith("-"):
            raise ValueError("control_branch must not start with '/' or '-'")
        return value

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, value: str) -> str:
        if not value:
            raise ValueError("node_id must not be empty")
        if any(ch.isspace() for ch in value):
            raise ValueError("node_id must not contain whitespace")
        return value

    @field_validator(
        "labels",
        "allowed_docker_images",
        "model_aliases",
        "docker_profiles",
        mode="before",
    )
    @classmethod
    def _parse_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("software_version")
    @classmethod
    def _validate_software_version(cls, value: str) -> str:
        if not value:
            raise ValueError("software_version must not be empty")
        if any(ch.isspace() for ch in value):
            raise ValueError("software_version must not contain whitespace")
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        # Empty is allowed: the registry falls back to ``node_id``.
        if value and any(ch.isspace() for ch in value):
            raise ValueError("display_name must not contain whitespace")
        return value

    @field_validator("local_git_roots", mode="before")
    @classmethod
    def _parse_git_roots(cls, value: object) -> object:
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
            return [Path(item) for item in items]
        return value

    # ------------------------------------------------------------------ #
    # path confinement
    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _resolve_and_confine_paths(self) -> NodeSettings:
        root = self.workspace_root.resolve()
        self.model_mapping_path = self._confine(
            self.model_mapping_path, root, "model_mapping_path"
        )
        self.capability_token_cache_path = self._confine(
            self.capability_token_cache_path, root, "capability_token_cache_path"
        )
        return self

    @staticmethod
    def _confine(candidate: Path, root: Path, field_name: str) -> Path:
        """Resolve ``candidate`` against ``root`` and ensure it stays inside.

        Absolute paths are returned resolved as-is. Relative paths are joined
        to ``root`` and the result must remain within ``root``; otherwise a
        ``ValueError`` is raised to block path traversal.
        """
        if candidate.is_absolute():
            return candidate.resolve()
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"{field_name} resolves to {resolved} which is outside the "
                f"allowed workspace root {root}"
            ) from exc
        return resolved


class DockerProfileSettings(BaseSettings):
    """Docker Profile 安全配置（TASK-071）。

    为 :class:`maf_runner.docker.profiles.DockerProfileRegistry` 提供可经
    环境变量覆盖的镜像白名单与默认资源上限。``allowed_images`` 是
    digest-pinned 镜像引用列表，``validate_profile`` 据此拒绝任意镜像。

    环境变量（前缀 ``MAF_DOCKER_PROFILE_``）：

    - ``MAF_DOCKER_PROFILE_ALLOWED_IMAGES``：逗号分隔的镜像白名单；
    - ``MAF_DOCKER_PROFILE_DEFAULT_MEMORY_LIMIT``：默认内存上限（如 ``512m``）。
    """

    model_config = SettingsConfigDict(
        env_prefix="MAF_DOCKER_PROFILE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    allowed_images: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_IMAGES),
        description="Whitelist of digest-pinned Docker image references.",
    )
    default_memory_limit: str = Field(
        default=DEFAULT_MEMORY_LIMIT,
        description="Default memory limit for Docker profiles (e.g. '512m').",
    )

    @field_validator("allowed_images", mode="before")
    @classmethod
    def _parse_comma_separated_images(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


__all__ = ["DockerProfileSettings", "NodeSettings"]
