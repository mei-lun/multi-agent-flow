"""TASK-004 unit tests for unified logging and correlation IDs.

Covers the three acceptance criteria:

1. Server 和 Node 日志均为结构化格式（JSON Lines）。
2. Key、Token、密码和宿主机敏感路径会被脱敏。
3. 同一请求或 Git 事件可通过关联 ID 追踪。

Tests use ``structlog``'s ``LogCapture`` to inspect processor output without
writing to stdout, and reset ``CorrelationContext`` between tests to avoid
cross-test context leakage.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from maf_observability import (
    CORRELATION_FIELDS,
    REDACTED_PLACEHOLDER,
    CorrelationContext,
    configure_logging,
    correlation_context,
    get_logger,
    new_trace_id,
    redact_sensitive,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_correlation() -> Iterator[None]:
    """Clear correlation context before each test."""
    CorrelationContext.clear()
    yield
    CorrelationContext.clear()


@pytest.fixture()
def captured_events() -> list[dict[str, Any]]:
    """Configure structlog to capture processor output into a list.

    Returns a list that will be populated with each log event's ``event_dict``
    (after all processors except the final JSONRenderer). Use ``configure``
    with a capturing processor appended.
    """
    events: list[dict[str, Any]] = []

    def _capture(
        _logger: Any, _method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        # Make a copy so downstream JSONRenderer doesn't mutate captured dict.
        events.append(dict(event_dict))
        return event_dict

    # Build chain with JSONRenderer at the end so PrintLogger receives a
    # string. ``_capture`` runs before the renderer to inspect the structured
    # dict (post-redaction) without serializing it.
    chain = [
        structlog.contextvars.merge_contextvars,
        # correlation injection
        _inject_for_test,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor_for_test,
        _capture,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=chain,
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return events


def _inject_for_test(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Test-only correlation injector (mirrors production processor)."""
    snapshot = CorrelationContext.snapshot()
    for key, value in snapshot.items():
        if key not in event_dict:
            event_dict[key] = value
    return event_dict


def _redact_processor_for_test(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Test-only redact processor wrapping ``redact_sensitive``."""
    return redact_sensitive(event_dict)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# acceptance 1: structured JSON output
# --------------------------------------------------------------------------- #


class TestStructuredJsonOutput:
    """Server 和 Node 日志均为结构化格式（JSON Lines）。"""

    def test_configure_logging_emits_json_via_stdout(self) -> None:
        """``configure_logging`` produces JSON Lines on stdout."""
        configure_logging(level="INFO")
        logger = get_logger("maf_server.test")
        # structlog PrintLoggerFactory writes to stdout; capture it.
        import sys
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            logger.info("server_started", host="127.0.0.1", port=8000)
        line = buf.getvalue().strip()
        assert line, "expected at least one log line on stdout"
        parsed = json.loads(line)
        assert parsed["event"] == "server_started"
        assert parsed["host"] == "127.0.0.1"
        assert parsed["port"] == 8000
        assert parsed["level"] == "info"
        assert "timestamp" in parsed

    def test_captured_event_has_level_and_timestamp(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger("maf_runner.test")
        logger.info("node_started", node_id="node-abc")
        assert len(captured_events) == 1
        event = captured_events[0]
        assert event["event"] == "node_started"
        assert event["level"] == "info"
        assert event["node_id"] == "node-abc"
        assert "timestamp" in event

    def test_extra_log_methods_carry_level(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")
        levels = [e["level"] for e in captured_events]
        # debug is filtered out at INFO level (filtering bound logger).
        assert levels == ["info", "warning", "error"]

    def test_configure_logging_rejects_invalid_level(self) -> None:
        with pytest.raises(ValueError, match="level must be one of"):
            configure_logging(level="VERBOSE")


# --------------------------------------------------------------------------- #
# acceptance 2: redaction of Key/Token/password/sensitive paths
# --------------------------------------------------------------------------- #


class TestRedaction:
    """Key、Token、密码和宿主机敏感路径会被脱敏。"""

    def test_api_key_redacted(self, captured_events: list[dict[str, Any]]) -> None:
        logger = get_logger()
        logger.info("model_call", api_key="sk-abc123XYZ", model="gpt-4")
        event = captured_events[0]
        assert event["api_key"] == REDACTED_PLACEHOLDER
        assert "sk-abc123XYZ" not in json.dumps(event)
        assert event["model"] == "gpt-4"

    def test_token_redacted(self, captured_events: list[dict[str, Any]]) -> None:
        logger = get_logger()
        logger.info("git_push", token="ghp_abcdef123456")
        event = captured_events[0]
        assert event["token"] == REDACTED_PLACEHOLDER
        assert "ghp_abcdef123456" not in json.dumps(event)

    def test_password_redacted(self, captured_events: list[dict[str, Any]]) -> None:
        logger = get_logger()
        logger.info("auth", password="hunter2", username="alice")
        event = captured_events[0]
        assert event["password"] == REDACTED_PLACEHOLDER
        assert event["username"] == "alice"
        assert "hunter2" not in json.dumps(event)

    def test_secret_redacted(self, captured_events: list[dict[str, Any]]) -> None:
        logger = get_logger()
        logger.info("config", secret_key="topsecret", secret="another")
        event = captured_events[0]
        assert event["secret_key"] == REDACTED_PLACEHOLDER
        assert event["secret"] == REDACTED_PLACEHOLDER

    def test_authorization_header_redacted(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        logger.info("http_request", authorization="Bearer abc.def.ghi")
        event = captured_events[0]
        assert event["authorization"] == REDACTED_PLACEHOLDER

    def test_nested_dict_redacted(self) -> None:
        payload = {
            "user": "alice",
            "credentials": {"api_key": "sk-nested", "token": "tok-nested"},
            "metadata": {"safe": "ok"},
        }
        redacted = redact_sensitive(payload)
        assert redacted["user"] == "alice"
        assert redacted["credentials"] == REDACTED_PLACEHOLDER
        assert redacted["metadata"] == {"safe": "ok"}

    def test_list_with_sensitive_items_redacted(self) -> None:
        payload = [{"api_key": "sk-1"}, {"name": "ok"}]
        redacted = redact_sensitive(payload)
        assert redacted[0]["api_key"] == REDACTED_PLACEHOLDER
        assert redacted[1]["name"] == "ok"

    @pytest.mark.parametrize(
        "path",
        [
            r"C:\Users\alice\.ssh\id_rsa",
            r"C:\Users\bob\data\secrets\master.key",
            "/home/alice/.aws/credentials",
            "/Users/bob/.kube/config",
            "~/.ssh/id_ed25519",
            "$HOME/.aws/credentials",
            "/root/.gnupg/secring.gpg",
            "/etc/ssl/private/server.pem",
            "/var/keys/server.pfx",
        ],
    )
    def test_sensitive_host_path_redacted(
        self,
        captured_events: list[dict[str, Any]],
        path: str,
    ) -> None:
        logger = get_logger()
        logger.info("file_access", path=path)
        event = captured_events[0]
        assert event["path"] == REDACTED_PLACEHOLDER, (
            f"expected {path!r} to be redacted"
        )
        assert path not in json.dumps(event)

    def test_non_sensitive_path_preserved(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        logger.info("file_access", path="/data/artifacts/sha256/ab/cd/1234")
        event = captured_events[0]
        assert event["path"] == "/data/artifacts/sha256/ab/cd/1234"

    def test_non_sensitive_field_names_preserved(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        logger.info(
            "task_dispatched",
            task_id="t-1",
            run_id="r-1",
            event_id="e-1",
            assignment_epoch=3,
        )
        event = captured_events[0]
        assert event["task_id"] == "t-1"
        assert event["run_id"] == "r-1"
        assert event["event_id"] == "e-1"
        assert event["assignment_epoch"] == 3

    def test_redact_sensitive_does_not_mutate_input(self) -> None:
        payload = {"api_key": "sk-orig", "name": "x"}
        original = dict(payload)
        redacted = redact_sensitive(payload)
        assert payload == original, "input must not be mutated"
        assert redacted["api_key"] == REDACTED_PLACEHOLDER


# --------------------------------------------------------------------------- #
# acceptance 3: correlation ID tracking
# --------------------------------------------------------------------------- #


class TestCorrelationContext:
    """同一请求或 Git 事件可通过关联 ID 追踪。"""

    def test_snapshot_empty_by_default(self) -> None:
        assert CorrelationContext.snapshot() == {}

    def test_bind_and_snapshot(self) -> None:
        CorrelationContext.bind(trace_id="trace-1", run_id="run-1")
        snap = CorrelationContext.snapshot()
        assert snap["trace_id"] == "trace-1"
        assert snap["run_id"] == "run-1"

    def test_get_returns_none_when_unbound(self) -> None:
        assert CorrelationContext.get("trace_id") is None

    def test_get_returns_value_when_bound(self) -> None:
        CorrelationContext.bind(trace_id="trace-1")
        assert CorrelationContext.get("trace_id") == "trace-1"

    def test_get_unknown_field_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown correlation field"):
            CorrelationContext.get("not_a_field")

    def test_bind_unknown_field_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown correlation fields"):
            CorrelationContext.bind(unknown_field="x")

    def test_bind_none_clears_field(self) -> None:
        CorrelationContext.bind(trace_id="trace-1")
        assert CorrelationContext.get("trace_id") == "trace-1"
        CorrelationContext.bind(trace_id=None)
        assert CorrelationContext.get("trace_id") is None

    def test_clear_resets_all(self) -> None:
        CorrelationContext.bind(trace_id="t", run_id="r")
        CorrelationContext.clear()
        assert CorrelationContext.snapshot() == {}

    def test_context_manager_binds_and_restores(self) -> None:
        CorrelationContext.bind(trace_id="outer")
        with correlation_context(run_id="r-1") as snap:
            assert snap["trace_id"] == "outer"
            assert snap["run_id"] == "r-1"
            assert CorrelationContext.get("run_id") == "r-1"
        # After exit, run_id should be gone but trace_id preserved.
        assert CorrelationContext.get("run_id") is None
        assert CorrelationContext.get("trace_id") == "outer"

    def test_context_manager_nested(self) -> None:
        with correlation_context(trace_id="t-1"):
            with correlation_context(run_id="r-1"):
                snap = CorrelationContext.snapshot()
                assert snap["trace_id"] == "t-1"
                assert snap["run_id"] == "r-1"
            # Inner run_id cleared after inner exit.
            assert CorrelationContext.get("run_id") is None
            assert CorrelationContext.get("trace_id") == "t-1"

    def test_context_manager_overrides_field(self) -> None:
        CorrelationContext.bind(trace_id="outer")
        with correlation_context(trace_id="inner"):
            assert CorrelationContext.get("trace_id") == "inner"
        assert CorrelationContext.get("trace_id") == "outer"

    def test_new_trace_id_is_unique_hex(self) -> None:
        t1 = new_trace_id()
        t2 = new_trace_id()
        assert t1 != t2
        assert len(t1) == 32  # uuid4 hex
        int(t1, 16)  # valid hex

    def test_logs_within_same_context_share_trace_id(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        """Two log calls inside the same correlation context share trace_id."""
        trace = new_trace_id()
        logger = get_logger()
        with correlation_context(trace_id=trace, run_id="run-42"):
            logger.info("step_one")
            logger.info("step_two")
        assert len(captured_events) == 2
        assert captured_events[0]["trace_id"] == trace
        assert captured_events[1]["trace_id"] == trace
        assert captured_events[0]["run_id"] == "run-42"
        assert captured_events[1]["run_id"] == "run-42"

    def test_logs_outside_context_have_no_trace_id(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        logger.info("no_context")
        assert len(captured_events) == 1
        assert "trace_id" not in captured_events[0]

    def test_logs_after_context_exit_drop_correlation(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        logger = get_logger()
        with correlation_context(trace_id="transient"):
            logger.info("inside")
        logger.info("outside")
        assert len(captured_events) == 2
        assert captured_events[0]["trace_id"] == "transient"
        assert "trace_id" not in captured_events[1]

    def test_git_event_correlation_fields_propagate(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        """Git event fields (event_id, assignment_epoch, control_commit) propagate."""
        logger = get_logger()
        with correlation_context(
            event_id="evt-019-abc",
            node_id="node-001",
            assignment_id="assign-1",
            assignment_epoch=7,
            control_commit="abc1234",
        ):
            logger.info("claim_requested", task_id="task-9")
        event = captured_events[0]
        assert event["event_id"] == "evt-019-abc"
        assert event["node_id"] == "node-001"
        assert event["assignment_id"] == "assign-1"
        assert event["assignment_epoch"] == 7
        assert event["control_commit"] == "abc1234"
        assert event["task_id"] == "task-9"

    def test_correlation_fields_complete(self) -> None:
        """All expected correlation fields are exposed."""
        expected = {
            "trace_id",
            "request_id",
            "actor_id",
            "organization_id",
            "run_id",
            "task_id",
            "attempt_id",
            "event_id",
            "node_id",
            "assignment_id",
            "assignment_epoch",
            "control_commit",
        }
        assert set(CORRELATION_FIELDS) == expected

    def test_explicit_bind_overrides_context(
        self, captured_events: list[dict[str, Any]]
    ) -> None:
        """Explicit ``logger.bind(trace_id=...)`` wins over context snapshot."""
        with correlation_context(trace_id="ctx-trace"):
            logger = get_logger().bind(trace_id="explicit-trace")
            logger.info("event")
        event = captured_events[0]
        assert event["trace_id"] == "explicit-trace"
