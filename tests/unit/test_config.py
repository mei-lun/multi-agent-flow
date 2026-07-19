"""TASK-003 unit tests for configuration loading and validation.

Covers the three acceptance criteria:

1. Missing required config produces a field-level error and stops startup.
2. Secrets never appear in config ``repr``/``str`` or in exceptions.
3. Relative paths that resolve outside the allowed root are rejected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from maf_runner.config import NodeSettings
from maf_server.config import ServerSettings

_SECRET_PLAINTEXT = "super-secret-value-12345"

# Fields whose env var name differs from the Python field name (via
# validation_alias). The ValidationError ``loc`` for such a field is the alias,
# so tests accept either the field name or the alias.
_SERVER_FIELD_ALIASES: dict[str, str] = {
    "business_db_path": "MAF_DATABASE_PATH",
    "checkpointer_db_path": "MAF_CHECKPOINT_DATABASE_PATH",
    "host": "MAF_SERVER_HOST",
    "port": "MAF_SERVER_PORT",
}
_NODE_FIELD_ALIASES: dict[str, str] = {
    "node_id": "MAF_RUNNER_ID",
    "labels": "MAF_RUNNER_LABELS",
    "max_concurrency": "MAF_RUNNER_MAX_CONCURRENCY",
    "control_remote_url": "MAF_CONTROL_REMOTE_URL",
    "workspace_root": "MAF_WORKSPACE_ROOT",
    "model_mapping_path": "MAF_MODEL_MAPPING_PATH",
    "capability_token_cache_path": "MAF_CAPABILITY_TOKEN_CACHE_PATH",
    "local_git_roots": "MAF_LOCAL_GIT_ROOTS",
    "allowed_docker_images": "MAF_ALLOWED_DOCKER_IMAGES",
    "poll_interval_seconds": "MAF_POLL_INTERVAL_SECONDS",
    "progress_interval_seconds": "MAF_PROGRESS_INTERVAL_SECONDS",
    "git_credentials_token": "MAF_GIT_CREDENTIALS_TOKEN",
}


def _acceptable_locs(field: str, aliases: dict[str, str]) -> set[tuple[str, ...]]:
    """Return the set of acceptable error ``loc`` tuples for a field."""
    return {(field,), (aliases.get(field, field),)}


def _server_acceptable(field: str) -> set[tuple[str, ...]]:
    return _acceptable_locs(field, _SERVER_FIELD_ALIASES)


def _node_acceptable(field: str) -> set[tuple[str, ...]]:
    return _acceptable_locs(field, _NODE_FIELD_ALIASES)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ``MAF_*`` env vars so tests start from a clean slate."""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _server_kwargs(tmp_path: Path, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        organization_id="org-001",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key=_SECRET_PLAINTEXT,
        data_dir=tmp_path,
        _env_file=None,
    )
    kwargs.update(overrides)
    return kwargs


def _make_server(tmp_path: Path, **overrides: object) -> ServerSettings:
    return ServerSettings(**_server_kwargs(tmp_path, **overrides))


def _node_kwargs(tmp_path: Path, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        node_id="node-abc123",
        control_remote_url="origin",
        workspace_root=tmp_path,
        model_mapping_path=tmp_path / "model-mapping.yaml",
        capability_token_cache_path=Path("capability-tokens.db"),
        _env_file=None,
    )
    kwargs.update(overrides)
    return kwargs


def _make_node(tmp_path: Path, **overrides: object) -> NodeSettings:
    return NodeSettings(**_node_kwargs(tmp_path, **overrides))


# --------------------------------------------------------------------------- #
# acceptance 1: missing required config → field-level error
# --------------------------------------------------------------------------- #


class TestServerMissingFields:
    """ServerSettings must report the exact missing field."""

    @pytest.mark.parametrize(
        "field",
        [
            "organization_id",
            "business_db_path",
            "checkpointer_db_path",
            "artifact_root",
            "workspace_root",
            "git_repo_root",
            "public_base_url",
            "secret_key",
        ],
    )
    def test_missing_required_field_is_reported_by_name(
        self, tmp_path: Path, field: str
    ) -> None:
        kwargs = _server_kwargs(tmp_path)
        del kwargs[field]
        with pytest.raises(ValidationError) as exc_info:
            ServerSettings(**kwargs)
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert _server_acceptable(field) & locs, (
            f"missing {field} should produce a field-level error; "
            f"got locs={locs}"
        )

    def test_missing_secret_key_still_blocks_startup(self, tmp_path: Path) -> None:
        kwargs = _server_kwargs(tmp_path)
        del kwargs["secret_key"]
        with pytest.raises(ValidationError):
            ServerSettings(**kwargs)

    def test_invalid_log_level_reports_field(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, log_level="VERBOSE")
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert ("log_level",) in locs

    def test_invalid_port_reports_field(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, port=99999)
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert _server_acceptable("port") & locs


class TestNodeMissingFields:
    """NodeSettings must report the exact missing field."""

    @pytest.mark.parametrize(
        "field",
        [
            "node_id",
            "control_remote_url",
            "workspace_root",
            "model_mapping_path",
            "capability_token_cache_path",
        ],
    )
    def test_missing_required_field_is_reported_by_name(
        self, tmp_path: Path, field: str
    ) -> None:
        kwargs = _node_kwargs(tmp_path)
        del kwargs[field]
        with pytest.raises(ValidationError) as exc_info:
            NodeSettings(**kwargs)
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert _node_acceptable(field) & locs, (
            f"missing {field} should produce a field-level error; "
            f"got locs={locs}"
        )

    def test_blank_node_id_reports_field(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_node(tmp_path, node_id="")
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert _node_acceptable("node_id") & locs

    def test_invalid_max_concurrency_reports_field(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_node(tmp_path, max_concurrency=0)
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert _node_acceptable("max_concurrency") & locs


# --------------------------------------------------------------------------- #
# acceptance 2: secret never leaks in repr/str or exceptions
# --------------------------------------------------------------------------- #


class TestSecretProtection:
    """SecretStr fields must not expose plaintext via repr/str."""

    def test_server_secret_key_is_secret_str(self, tmp_path: Path) -> None:
        settings = _make_server(tmp_path)
        assert isinstance(settings.secret_key, SecretStr)
        assert settings.secret_key.get_secret_value() == _SECRET_PLAINTEXT

    def test_server_repr_hides_secret(self, tmp_path: Path) -> None:
        settings = _make_server(tmp_path)
        assert _SECRET_PLAINTEXT not in repr(settings)
        assert _SECRET_PLAINTEXT not in str(settings)
        assert _SECRET_PLAINTEXT not in repr(settings.secret_key)
        assert _SECRET_PLAINTEXT not in str(settings.secret_key)

    def test_server_model_dump_hides_secret(self, tmp_path: Path) -> None:
        settings = _make_server(tmp_path)
        dumped = settings.model_dump()
        assert dumped["secret_key"] != _SECRET_PLAINTEXT
        assert _SECRET_PLAINTEXT not in str(dumped)

    def test_node_git_credentials_token_hidden(self, tmp_path: Path) -> None:
        token = "ghp_token_value_xyz"
        settings = _make_node(tmp_path, git_credentials_token=token)
        assert isinstance(settings.git_credentials_token, SecretStr)
        assert settings.git_credentials_token.get_secret_value() == token
        assert token not in repr(settings)
        assert token not in str(settings)
        assert token not in repr(settings.git_credentials_token)

    def test_secret_not_in_validation_exception(self, tmp_path: Path) -> None:
        # A failing validator must not embed the secret in the exception text.
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, log_level="BAD")
        message = str(exc_info.value)
        assert _SECRET_PLAINTEXT not in message


# --------------------------------------------------------------------------- #
# acceptance 3: relative paths must resolve inside the allowed root
# --------------------------------------------------------------------------- #


class TestServerPathConfinement:
    """Relative data sub-paths must stay inside ``data_dir``."""

    def test_relative_path_resolves_inside_root(self, tmp_path: Path) -> None:
        settings = _make_server(tmp_path, business_db_path=Path("nested/maf.db"))
        assert settings.business_db_path == (tmp_path / "nested" / "maf.db").resolve()
        assert tmp_path in settings.business_db_path.parents or (
            settings.business_db_path == tmp_path
        )

    def test_traversal_in_business_db_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, business_db_path=Path("../../etc/passwd"))
        assert "business_db_path" in str(exc_info.value)

    def test_traversal_in_checkpointer_db_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, checkpointer_db_path=Path("../outside.db"))
        assert "checkpointer_db_path" in str(exc_info.value)

    def test_traversal_in_artifact_root_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, artifact_root=Path("../../outside"))
        assert "artifact_root" in str(exc_info.value)

    def test_traversal_in_workspace_root_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, workspace_root=Path("../../outside"))
        assert "workspace_root" in str(exc_info.value)

    def test_absolute_path_allowed(self, tmp_path: Path) -> None:
        absolute = tmp_path / "external" / "maf.db"
        settings = _make_server(tmp_path, business_db_path=absolute)
        assert settings.business_db_path == absolute.resolve()


class TestNodePathConfinement:
    """Relative local data paths must stay inside ``workspace_root``."""

    def test_relative_path_resolves_inside_root(self, tmp_path: Path) -> None:
        settings = _make_node(
            tmp_path, capability_token_cache_path=Path("cache/tokens.db")
        )
        assert settings.capability_token_cache_path == (
            tmp_path / "cache" / "tokens.db"
        ).resolve()

    def test_traversal_in_capability_cache_blocked(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_node(tmp_path, capability_token_cache_path=Path("../../etc/shadow"))
        assert "capability_token_cache_path" in str(exc_info.value)

    def test_absolute_capability_cache_allowed(self, tmp_path: Path) -> None:
        absolute = tmp_path / "abs" / "tokens.db"
        settings = _make_node(tmp_path, capability_token_cache_path=absolute)
        assert settings.capability_token_cache_path == absolute.resolve()


# --------------------------------------------------------------------------- #
# control_branch validation
# --------------------------------------------------------------------------- #


class TestControlBranchValidation:
    @pytest.mark.parametrize("branch", ["maf/control", "maf/control-v2"])
    def test_valid_branch_accepted(self, tmp_path: Path, branch: str) -> None:
        settings = _make_server(tmp_path, control_branch=branch)
        assert settings.control_branch == branch

    @pytest.mark.parametrize(
        "branch",
        ["", "maf control", "maf/../escape", "/maf/control", "-maf/control"],
    )
    def test_invalid_branch_rejected(self, tmp_path: Path, branch: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _make_server(tmp_path, control_branch=branch)
        locs = {tuple(err["loc"]) for err in exc_info.value.errors()}
        assert ("control_branch",) in locs


# --------------------------------------------------------------------------- #
# comma-separated list parsing
# --------------------------------------------------------------------------- #


class TestCommaSeparatedParsing:
    def test_node_labels_split(self, tmp_path: Path) -> None:
        settings = _make_node(tmp_path, labels="python,docker,git")
        assert settings.labels == ["python", "docker", "git"]

    def test_node_docker_images_split(self, tmp_path: Path) -> None:
        settings = _make_node(
            tmp_path, allowed_docker_images="python:3.11,node:20"
        )
        assert settings.allowed_docker_images == ["python:3.11", "node:20"]

    def test_node_local_git_roots_split(self, tmp_path: Path) -> None:
        settings = _make_node(
            tmp_path, local_git_roots="/srv/repos/a,/srv/repos/b"
        )
        assert settings.local_git_roots == [Path("/srv/repos/a"), Path("/srv/repos/b")]
