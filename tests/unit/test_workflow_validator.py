"""Deterministic static workflow graph validation."""

from maf_server.modules.workflows.service import StaticWorkflowValidator


def _node(key: str, kind: str = "GATE", **extra):
    value = {
        "key": key,
        "kind": kind,
        "input_contracts": [],
        "output_contracts": [],
        "retry_policy": {"max_retries": 0},
        "timeout_seconds": 30,
        "ui_position": {"x": 0, "y": 0},
    }
    value.update(extra)
    return value


def _edge(key: str, source: str, target: str, condition=None):
    return {
        "key": key,
        "source_node_key": source,
        "target_node_key": target,
        "condition": condition,
        "priority": 0,
    }


def test_valid_graph_is_deterministic():
    graph = {
        "start_node_key": "start",
        "nodes": [_node("end", "END_SUCCESS"), _node("start")],
        "edges": [_edge("e", "start", "end")],
    }
    result = StaticWorkflowValidator().validate(graph)
    assert result["valid"] is True
    assert result["reachable_node_keys"] == ["end", "start"]
    assert result["errors"] == []


def test_reports_unreachable_cycle_and_missing_role_together():
    graph = {
        "start_node_key": "start",
        "nodes": [
            _node("start"),
            _node("agent", "AGENT"),
            _node("end", "END_SUCCESS"),
            _node("orphan"),
        ],
        "edges": [
            _edge("a", "start", "agent"),
            _edge("b", "agent", "start"),
            _edge("c", "orphan", "orphan"),
        ],
    }
    result = StaticWorkflowValidator().validate(graph)
    codes = {item["code"] for item in result["errors"]}
    assert {"ROLE_VERSION_REQUIRED", "NODE_UNREACHABLE", "GRAPH_CYCLE"} <= codes


def test_condition_parser_rejects_calls_and_unknown_names():
    graph = {
        "start_node_key": "start",
        "nodes": [_node("start"), _node("end", "END_SUCCESS")],
        "edges": [
            _edge("call", "start", "end", "status == __import__('os')"),
            _edge("name", "start", "end", "secret == 1"),
        ],
    }
    result = StaticWorkflowValidator().validate(graph)
    codes = [item["code"] for item in result["errors"]]
    assert "CONDITION_UNSAFE" in codes
    assert "CONDITION_NAME" in codes


def test_contracts_must_flow_from_upstream_output_to_downstream_input():
    graph = {
        "start_node_key": "start",
        "nodes": [
            _node("start", output_contracts=[{"artifact_type": "requirements", "schema_version": "1"}]),
            _node("consumer", input_contracts=[{"artifact_type": "architecture", "schema_version": "1"}]),
            _node("end", "END_SUCCESS"),
        ],
        "edges": [_edge("a", "start", "consumer"), _edge("b", "consumer", "end")],
    }
    result = StaticWorkflowValidator().validate(graph)
    assert any(item["code"] == "CONTRACT_MISMATCH" for item in result["errors"])


def test_matching_contract_version_is_accepted():
    graph = {
        "start_node_key": "start",
        "nodes": [
            _node("start", output_contracts=[{"artifact_type": "requirements", "schema_version": "1"}]),
            _node("consumer", input_contracts=[{"artifact_type": "requirements", "schema_version": "1"}]),
            _node("end", "END_SUCCESS"),
        ],
        "edges": [_edge("a", "start", "consumer"), _edge("b", "consumer", "end")],
    }
    result = StaticWorkflowValidator().validate(graph)
    assert result["valid"] is True


def test_rework_policy_and_role_status_are_bounded_and_published():
    graph = {
        "start_node_key": "start",
        "nodes": [
            _node("start", "AGENT", role_version_id="role-v1", role_version_status="DRAFT", rework_policy={"max_reworks": 11}),
            _node("end", "END_SUCCESS"),
        ],
        "edges": [_edge("a", "start", "end")],
    }
    result = StaticWorkflowValidator().validate(graph)
    codes = {item["code"] for item in result["errors"]}
    assert {"ROLE_VERSION_NOT_PUBLISHED", "REWORK_LIMIT_INVALID"} <= codes


def test_non_object_retry_policy_is_reported_instead_of_coerced():
    graph = {
        "start_node_key": "start",
        "nodes": [_node("start", retry_policy=[]), _node("end", "END_SUCCESS")],
        "edges": [_edge("a", "start", "end")],
    }
    result = StaticWorkflowValidator().validate(graph)
    assert any(item["code"] == "RETRY_POLICY_INVALID" for item in result["errors"])
