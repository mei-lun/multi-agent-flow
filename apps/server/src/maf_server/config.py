"""Server configuration loaded from environment variables.

Reads and validates ``MAF_*`` environment variables, resolves relative paths
against the central data directory, and protects secret material via
``SecretStr`` so that ``repr``/``str`` never expose plaintext. This module must
not read business tables or decrypt secrets (that is the SecretStore's job).

Key environment variables (see ``.env.example`` for the full list):

- ``MAF_ENV``, ``MAF_ORGANIZATION_ID``
- ``MAF_DATA_DIR`` (confinement root for data sub-paths)
- ``MAF_DATABASE_PATH``, ``MAF_CHECKPOINT_DATABASE_PATH``
- ``MAF_ARTIFACT_ROOT``, ``MAF_WORKSPACE_ROOT``
- ``MAF_CONTROL_BRANCH`` (default ``maf/control``, single-writer)
- ``MAF_GIT_REPO_ROOT`` (local Git working tree for coordination)
- ``MAF_SERVER_HOST``, ``MAF_SERVER_PORT``, ``MAF_PUBLIC_BASE_URL``
- ``MAF_MASTER_KEY_FILE``, ``MAF_CAPABILITY_SIGNING_KEY_FILE``
- ``MAF_SECRET_KEY`` (SecretStr, bootstrap session signing secret)
- ``MAF_LOG_LEVEL``, ``MAF_LOG_FILE``
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_DEFAULT_CONTROL_BRANCH: Final[str] = "maf/control"


class ServerSettings(BaseSettings):
    """Central server settings sourced from environment variables.

    Required fields without a default cause field-level validation errors at
    startup when missing. Relative data sub-paths are resolved against
    ``data_dir`` and must remain inside it (path-traversal guard). Secret
    material uses ``SecretStr`` and is never rendered as plaintext.
    """

    model_config = SettingsConfigDict(
        env_prefix="MAF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- environment & organization ------------------------------------- #
    env: str = Field(default="dev", description="Deployment environment name.")
    organization_id: str = Field(
        ..., description="Single-organization boundary ID; required."
    )

    # --- data directory (confinement root) ------------------------------- #
    data_dir: Path = Field(
        default=Path("./data"),
        description="Central data directory; relative paths are confined here.",
    )

    # --- data sub-paths (confined to data_dir) --------------------------- #
    business_db_path: Path = Field(
        ...,
        validation_alias=AliasChoices("MAF_DATABASE_PATH"),
        description="Business SQLite database path (relative to data_dir).",
    )
    checkpointer_db_path: Path = Field(
        ...,
        validation_alias=AliasChoices("MAF_CHECKPOINT_DATABASE_PATH"),
        description="LangGraph checkpoint SQLite path (relative to data_dir).",
    )
    artifact_root: Path = Field(
        ..., description="Artifact store root directory (relative to data_dir)."
    )
    workspace_root: Path = Field(
        ..., description="Server-side workspace root directory (relative to data_dir)."
    )

    # --- Git coordination ------------------------------------------------ #
    control_branch: str = Field(
        default=_DEFAULT_CONTROL_BRANCH,
        description="Git coordination control branch; server is the single writer.",
    )
    git_repo_root: Path = Field(
        ..., description="Local Git working tree root for coordination commits."
    )

    # --- HTTP / FastAPI -------------------------------------------------- #
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("MAF_SERVER_HOST"),
        description="FastAPI listen host.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("MAF_SERVER_PORT"),
        description="FastAPI listen port.",
    )
    public_base_url: str = Field(
        ..., description="External base URL for API/SSE endpoints."
    )

    # --- logging --------------------------------------------------------- #
    log_level: str = Field(default="INFO", description="Logging level.")
    log_file: Path | None = Field(default=None, description="Optional log file path.")

    # --- secret material (SecretStr / key file paths) ------------------- #
    master_key_file: Path | None = Field(
        default=None,
        description="Path to the master key file for SecretStore AES-GCM fallback.",
    )
    capability_signing_key_file: Path | None = Field(
        default=None,
        description="Path to the capability token signing key file.",
    )
    secret_key: SecretStr = Field(
        ...,
        description="Bootstrap secret for session/JWT signing; never logged.",
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

    # ------------------------------------------------------------------ #
    # path confinement
    # ------------------------------------------------------------------ #

    @model_validator(mode="after")
    def _resolve_and_confine_paths(self) -> ServerSettings:
        root = self.data_dir.resolve()
        self.business_db_path = self._confine(
            self.business_db_path, root, "business_db_path"
        )
        self.checkpointer_db_path = self._confine(
            self.checkpointer_db_path, root, "checkpointer_db_path"
        )
        self.artifact_root = self._confine(self.artifact_root, root, "artifact_root")
        self.workspace_root = self._confine(
            self.workspace_root, root, "workspace_root"
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
                f"allowed data root {root}"
            ) from exc
        return resolved


__all__ = ["ServerSettings"]
