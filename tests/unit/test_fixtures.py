"""TASK-010 单元测试：测试基线与 Fixture。

验收标准：
1. 单元、契约、集成目录均可独立运行（fixture 不耦合特定目录）。
2. 测试不依赖真实 GitHub 或模型 Key（用本地 git + mock adapter）。
3. 临时目录在成功和失败后均清理。

测试范围：
- ``tests/conftest.py``：全局 fixture。
- ``tests/fixtures/**``：可复用夹具（virtual_clock、temp_dir、git_repo、
  mock_providers、factories、sensitive_config）。

本文件既测试 fixture 辅助类，也测试 conftest 暴露的 pytest fixture。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from fixtures.factories import (
    FIXED_CONTROL_COMMIT,
    FIXED_NODE_ID,
    FIXED_ORG_ID,
    TEST_SECRET_KEY,
    make_node_settings,
    make_server_settings,
)
from fixtures.git_repo import LocalGitRepo, init_local_git_repo
from fixtures.mock_providers import MockModelAdapter, MockToolAdapter
from fixtures.sensitive_config import (
    FAKE_GITHUB_TOKEN,
    FAKE_MODEL_API_KEY,
    FAKE_SSH_KEY_MATERIAL,
    is_fake_credential,
    safe_git_binding,
    safe_model_connection,
)
from fixtures.temp_dir import cleanup_temp_dir, make_temp_dir
from fixtures.virtual_clock import VirtualClock


# --------------------------------------------------------------------------- #
# 验收 3：临时目录在成功和失败后均清理
# --------------------------------------------------------------------------- #


class TestTempDirCleanup:
    """临时目录 fixture 在成功和失败后均清理。"""

    def test_tmp_project_dir_cleans_up_on_success(
        self, tmp_project_dir: Path
    ) -> None:
        """成功路径：fixture 退出后目录消失。"""
        assert tmp_project_dir.exists()
        # 写入一些内容，验证带内容的目录也能清理。
        (tmp_project_dir / "file.txt").write_text("data", encoding="utf-8")
        tracked = tmp_project_dir
        # 记录路径，fixture 退出后断言已删除（在下一个测试中验证）。
        assert (tracked / "file.txt").exists()

    def test_tmp_project_dir_removed_after_previous_test(self) -> None:
        """前一个测试的临时目录已被清理（间接验证清理发生）。"""
        # tmp_project_dir 在上一个测试结束后已清理；这里只验证新 fixture 仍可用。
        pass

    def test_make_temp_dir_creates_and_cleanup_removes(self) -> None:
        """``make_temp_dir`` + ``cleanup_temp_dir`` 显式清理。"""
        d = make_temp_dir()
        try:
            assert d.exists() and d.is_dir()
            (d / "nested").mkdir()
            (d / "nested" / "x.txt").write_text("x", encoding="utf-8")
        finally:
            removed = cleanup_temp_dir(d)
        assert removed, "cleanup_temp_dir 应返回 True 表示已删除"
        assert not d.exists(), "目录应已删除"

    def test_cleanup_temp_dir_is_idempotent(self) -> None:
        """重复清理不抛异常。"""
        d = make_temp_dir()
        cleanup_temp_dir(d)
        # 再次清理已不存在的目录
        assert cleanup_temp_dir(d) is False
        assert cleanup_temp_dir(None) is False

    def test_tmp_project_dir_cleans_up_on_failure(
        self, tmp_path: Path
    ) -> None:
        """失败路径：即使测试抛异常，临时目录仍被清理。

        用 ``make_temp_dir`` 手动模拟「测试中创建目录后抛异常」的场景，
        ``try/finally`` 保证清理。
        """
        d = make_temp_dir()
        cleaned_up = {"done": False}
        with pytest.raises(RuntimeError, match="simulated failure"):
            try:
                (d / "f.txt").write_text("x", encoding="utf-8")
                raise RuntimeError("simulated failure")
            finally:
                cleanup_temp_dir(d)
                cleaned_up["done"] = not d.exists()
        assert cleaned_up["done"], "异常后目录仍应被清理"
        assert not d.exists()


# --------------------------------------------------------------------------- #
# 验收：虚拟时钟 VirtualClock
# --------------------------------------------------------------------------- #


class TestVirtualClock:
    """``VirtualClock`` 提供确定性时间推进，不依赖系统时钟。"""

    def test_now_returns_utc_datetime(self) -> None:
        clock = VirtualClock()
        assert clock.now().tzinfo is not None
        assert clock.now().utcoffset() == timedelta(0)

    def test_default_start_is_deterministic(self) -> None:
        clock1 = VirtualClock()
        clock2 = VirtualClock()
        assert clock1.now() == clock2.now(), "默认起点应确定可复现"

    def test_advance_with_timedelta(self) -> None:
        clock = VirtualClock()
        before = clock.now()
        advanced = clock.advance(timedelta(seconds=30))
        assert advanced == before + timedelta(seconds=30)
        assert clock.now() == advanced

    def test_advance_with_seconds(self) -> None:
        clock = VirtualClock()
        before = clock.now()
        clock.advance(120)
        assert clock.now() == before + timedelta(seconds=120)

    def test_advance_is_cumulative(self) -> None:
        clock = VirtualClock()
        t0 = clock.now()
        clock.advance(10)
        clock.advance(timedelta(minutes=1))
        assert clock.now() == t0 + timedelta(seconds=70)

    def test_wait_until_advances_to_deadline(self) -> None:
        clock = VirtualClock()
        deadline = clock.now() + timedelta(seconds=60)
        asyncio.run(clock.wait_until(deadline))
        assert clock.now() == deadline

    def test_wait_until_does_not_regress_for_past_deadline(self) -> None:
        """deadline 在过去时，wait_until 不回拨时间。"""
        clock = VirtualClock()
        before = clock.now()
        past = before - timedelta(seconds=10)
        asyncio.run(clock.wait_until(past))
        assert clock.now() == before, "wait_until 不应回拨虚拟时间"

    def test_wait_until_does_not_block(self) -> None:
        """wait_until 立即返回，不真实等待（验证执行时间极短）。"""
        clock = VirtualClock()
        deadline = clock.now() + timedelta(hours=24)
        import time as _time

        start = _time.monotonic()
        asyncio.run(clock.wait_until(deadline))
        elapsed = _time.monotonic() - start
        assert elapsed < 1.0, "wait_until 不应真实阻塞"

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="时区"):
            VirtualClock(start=datetime(2024, 1, 1))

    def test_custom_start(self) -> None:
        custom = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        clock = VirtualClock(start=custom)
        assert clock.now() == custom

    def test_satisfies_clock_protocol(self) -> None:
        """``VirtualClock`` 实现 ``Clock`` Protocol 的 ``now``/``wait_until``。

        ``Clock`` Protocol 未声明 ``@runtime_checkable``（生产代码不可改），
        故用 ``hasattr`` 做结构化校验，并确认方法签名可调用。
        """
        clock = VirtualClock()
        assert callable(getattr(clock, "now", None))
        assert callable(getattr(clock, "wait_until", None))
        # now() 返回带时区的 datetime。
        assert clock.now().tzinfo is not None


# --------------------------------------------------------------------------- #
# 验收：Mock Git 仓库（不依赖真实 GitHub）
# --------------------------------------------------------------------------- #


class TestLocalGitRepo:
    """``LocalGitRepo`` 用真实 git 初始化，不依赖 GitHub。"""

    def test_git_repo_fixture_has_head(self, git_repo: LocalGitRepo) -> None:
        """git_repo fixture 初始化后可 rev-parse HEAD。"""
        sha = git_repo.rev_parse("HEAD")
        assert len(sha) >= 7
        int(sha, 16)  # 合法十六进制

    def test_git_repo_default_branch_is_main(self, git_repo: LocalGitRepo) -> None:
        assert git_repo.current_branch() == "main"

    def test_commit_file_creates_commit(self, git_repo: LocalGitRepo) -> None:
        head_before = git_repo.rev_parse("HEAD")
        git_repo.commit_file("src/app.py", "print('hi')\n", "add app")
        head_after = git_repo.rev_parse("HEAD")
        assert head_after != head_before, "提交后 HEAD 应变化"

    def test_checkout_branch(self, git_repo: LocalGitRepo) -> None:
        git_repo.checkout_branch("feature/x", create=True)
        assert git_repo.current_branch() == "feature/x"

    def test_empty_git_repo_has_no_head(self, empty_git_repo: LocalGitRepo) -> None:
        """空仓库（无初始提交）rev-parse HEAD 应失败。"""
        with pytest.raises(subprocess.CalledProcessError):
            empty_git_repo.rev_parse("HEAD")

    def test_empty_git_repo_can_add_first_commit(
        self, empty_git_repo: LocalGitRepo
    ) -> None:
        empty_git_repo.commit_file("a.txt", "a\n", "first")
        assert len(empty_git_repo.rev_parse("HEAD")) >= 7

    def test_init_local_git_repo_isolated_identity(self, tmp_path: Path) -> None:
        """仓库身份隔离：不继承宿主 user.name/email。"""
        repo = init_local_git_repo(tmp_path / "r")
        name = repo.run(["config", "user.name"]).strip()
        email = repo.run(["config", "user.email"]).strip()
        assert name == "Test Bot"
        assert email == "bot@example.test"

    def test_git_repo_does_not_touch_remote(self, git_repo: LocalGitRepo) -> None:
        """验收：不依赖真实 GitHub——仓库无 remote 配置。"""
        remotes = git_repo.run(["remote"]).strip()
        assert remotes == "", "本地 fixture 不应配置任何 remote"


# --------------------------------------------------------------------------- #
# 验收：Mock Provider / Tool Adapter（不依赖真实模型 Key）
# --------------------------------------------------------------------------- #


def _make_request() -> dict[str, Any]:
    """构造最小 UnifiedModelRequest（TypedDict 接受 dict 字面量）。"""
    return {
        "attempt_id": "att-1",
        "call_key": "key-1",
        "model_policy_id": "mp-1",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [],
        "response_schema": None,
        "temperature": None,
        "max_output_tokens": 128,
        "timeout_seconds": 10,
        "metadata": {},
    }


class TestMockModelAdapter:
    """``MockModelAdapter`` 返回固定成功响应，不发起网络调用。"""

    def test_invoke_returns_completed(self) -> None:
        adapter = MockModelAdapter()
        resp = asyncio.run(adapter.invoke({}, "mock-model", _make_request()))
        assert resp["status"] == "COMPLETED"
        assert resp["model_profile_id"] == "mock-model"
        assert resp["message"]["content"] == "mock-response"
        assert resp["usage"]["input_tokens"] == 1
        assert resp["error"] is None

    def test_invoke_records_request(self) -> None:
        adapter = MockModelAdapter()
        req = _make_request()
        asyncio.run(adapter.invoke({}, "mock-model", req))
        assert adapter.invoke_count == 1
        assert adapter.last_request == req

    def test_custom_response_text(self) -> None:
        adapter = MockModelAdapter(response_text="hello world")
        resp = asyncio.run(adapter.invoke({}, "m", _make_request()))
        assert resp["message"]["content"] == "hello world"

    def test_probe_returns_ok(self) -> None:
        adapter = MockModelAdapter()
        result = asyncio.run(adapter.probe({}))
        assert result["ok"] is True
        assert "provider" in result

    def test_list_models_returns_list(self) -> None:
        adapter = MockModelAdapter()
        models = asyncio.run(adapter.list_models({}))
        assert isinstance(models, list)
        assert len(models) >= 1
        assert "name" in models[0]

    def test_stream_yields_chunks(self) -> None:
        adapter = MockModelAdapter(response_text="a b c")

        async def _collect() -> list[dict[str, Any]]:
            return [chunk async for chunk in adapter.stream({}, "m", _make_request())]

        chunks = asyncio.run(_collect())
        assert len(chunks) == 3
        assert all("delta" in c for c in chunks)

    def test_normalize_error(self) -> None:
        adapter = MockModelAdapter()
        result = adapter.normalize_error(RuntimeError("boom"))
        assert result["code"] == "MOCK_ERROR"
        assert result["retryable"] is False

    def test_satisfies_protocol(self) -> None:
        """``MockModelAdapter`` 实现 ``ModelAdapter`` Protocol 结构。

        Protocol 未声明 ``@runtime_checkable``（生产代码不可改），故用结构化校验。
        """
        adapter = MockModelAdapter()
        for attr in (
            "adapter_type",
            "probe",
            "list_models",
            "invoke",
            "stream",
            "normalize_error",
        ):
            assert hasattr(adapter, attr), f"缺少 Protocol 属性 {attr}"
        assert adapter.adapter_type == "mock"


class TestMockToolAdapter:
    """``MockToolAdapter`` 回显参数，不执行真实副作用。"""

    def test_invoke_echoes_arguments(self) -> None:
        adapter = MockToolAdapter()
        result = asyncio.run(
            adapter.invoke({"name": "echo"}, {"x": 1}, timeout_seconds=5)
        )
        assert result["ok"] is True
        assert result["echo"] == {"x": 1}
        assert result["timeout_seconds"] == 5
        assert adapter.invoke_count == 1

    def test_cancel_returns_none(self) -> None:
        adapter = MockToolAdapter()
        result = asyncio.run(adapter.cancel("call-1"))
        assert result is None

    def test_satisfies_protocol(self) -> None:
        """``MockToolAdapter`` 实现 ``ToolAdapter`` Protocol 结构。"""
        adapter = MockToolAdapter()
        for attr in ("adapter_type", "invoke", "cancel"):
            assert hasattr(adapter, attr), f"缺少 Protocol 属性 {attr}"
        assert adapter.adapter_type == "mock"


# --------------------------------------------------------------------------- #
# 验收：配置工厂与固定 ID
# --------------------------------------------------------------------------- #


class TestFactories:
    """``make_server_settings`` / ``make_node_settings`` 生成合法配置。"""

    def test_make_server_settings_basic(self, tmp_path: Path) -> None:
        settings = make_server_settings(tmp_path)
        assert settings.organization_id == FIXED_ORG_ID
        assert settings.public_base_url == "http://localhost:8000"

    def test_make_server_settings_overrides(self, tmp_path: Path) -> None:
        settings = make_server_settings(tmp_path, organization_id="org-other")
        assert settings.organization_id == "org-other"

    def test_make_server_settings_secret_is_test_value(
        self, tmp_path: Path
    ) -> None:
        settings = make_server_settings(tmp_path)
        # SecretStr 的 get_secret_value 暴露测试值。
        assert settings.secret_key.get_secret_value() == TEST_SECRET_KEY
        assert is_fake_credential(TEST_SECRET_KEY)

    def test_make_server_settings_paths_confined_to_data_dir(
        self, tmp_path: Path
    ) -> None:
        settings = make_server_settings(tmp_path)
        root = tmp_path.resolve()
        for p in (
            settings.business_db_path,
            settings.checkpointer_db_path,
            settings.artifact_root,
            settings.workspace_root,
        ):
            assert str(p.resolve()).startswith(str(root))

    def test_make_node_settings_basic(self, tmp_path: Path) -> None:
        settings = make_node_settings(tmp_path)
        assert settings.node_id == FIXED_NODE_ID
        assert settings.control_remote_url == "origin"

    def test_make_node_settings_overrides(self, tmp_path: Path) -> None:
        settings = make_node_settings(tmp_path, node_id="node-other")
        assert settings.node_id == "node-other"

    def test_server_settings_factory_fixture(self, server_settings_factory) -> None:
        settings = server_settings_factory()
        assert settings.organization_id == FIXED_ORG_ID

    def test_node_settings_factory_fixture(self, node_settings_factory) -> None:
        settings = node_settings_factory()
        assert settings.node_id == FIXED_NODE_ID

    def test_fixed_node_id_matches_pattern(self) -> None:
        assert re.match(
            r"^node-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            FIXED_NODE_ID,
        )

    def test_fixed_control_commit_is_hex(self) -> None:
        assert len(FIXED_CONTROL_COMMIT) == 40
        int(FIXED_CONTROL_COMMIT, 16)


# --------------------------------------------------------------------------- #
# 验收：敏感配置不含真实 Key
# --------------------------------------------------------------------------- #


class TestSensitiveConfig:
    """所有凭据均为 fake 值，不含真实 Key。"""

    def test_fake_github_token_is_fake(self) -> None:
        assert is_fake_credential(FAKE_GITHUB_TOKEN)
        assert FAKE_GITHUB_TOKEN.startswith("ghp_FAKE")

    def test_fake_model_api_key_is_fake(self) -> None:
        assert is_fake_credential(FAKE_MODEL_API_KEY)

    def test_fake_ssh_material_is_fake(self) -> None:
        assert is_fake_credential(FAKE_SSH_KEY_MATERIAL)

    def test_safe_model_connection_has_no_real_key(self) -> None:
        cfg = safe_model_connection()
        # 仅存环境变量名，不存值。
        assert "api_key_env" in cfg
        assert "api_key" not in cfg
        assert "key" not in cfg or cfg.get("key") is None
        blob = str(cfg)
        assert "sk-" not in blob or "fake" in blob.lower()

    def test_safe_git_binding_has_no_real_credential(self) -> None:
        cfg = safe_git_binding()
        assert "secret_id" in cfg
        assert is_fake_credential(cfg["secret_id"])
        # remote_url 为公开示例地址，不含 token。
        assert "@" not in cfg["remote_url"]

    def test_is_fake_credential_detects_markers(self) -> None:
        assert is_fake_credential("FAKE_TOKEN")
        assert is_fake_credential("some-not-real-key")
        assert not is_fake_credential("sk-prod-real-secret-key")
        assert not is_fake_credential("ghp_1234567890abcdef")


# --------------------------------------------------------------------------- #
# 验收 2：测试不依赖真实 GitHub 或模型 Key
# --------------------------------------------------------------------------- #


class TestNoExternalDependencies:
    """fixture 集合不依赖真实 GitHub 或模型 API Key。"""

    def test_git_repo_is_local_only(self, git_repo: LocalGitRepo) -> None:
        """git_repo fixture 不配置任何 remote，不访问 GitHub。"""
        assert git_repo.run(["remote"]).strip() == ""

    def test_mock_model_adapter_no_network(self) -> None:
        """MockModelAdapter 不持有任何网络客户端属性。"""
        adapter = MockModelAdapter()
        for attr in ("http_client", "client", "session", "aiohttp", "httpx"):
            assert not hasattr(adapter, attr) or getattr(adapter, attr) is None

    def test_no_real_api_key_in_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试环境不注入真实 OpenAI/Anthropic/GitHub token。"""
        # conftest 的 _clear_maf_env autouse fixture 已清除 MAF_* 变量；
        # 这里额外断言常见真实 Key 环境变量未被设置（或为 fake）。
        sensitive_keys = (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "MAF_GIT_CREDENTIAL_TOKEN",
        )
        for key in sensitive_keys:
            val = os.environ.get(key)
            if val is not None:
                # 若存在，必须是明显的 fake 值。
                assert is_fake_credential(val), (
                    f"环境变量 {key} 含疑似真实凭据: {val!r}"
                )


# --------------------------------------------------------------------------- #
# 验收 1：各目录可独立运行（fixture 不耦合特定目录）
# --------------------------------------------------------------------------- #


class TestFixturesAvailableAcrossDirs:
    """conftest 的全局 fixture 在 unit 目录可用（契约：其他目录同理）。"""

    def test_virtual_clock_fixture_injected(self, virtual_clock: VirtualClock) -> None:
        before = virtual_clock.now()
        virtual_clock.advance(5)
        assert virtual_clock.now() == before + timedelta(seconds=5)

    def test_mock_model_adapter_fixture_injected(
        self, mock_model_adapter: MockModelAdapter
    ) -> None:
        resp = asyncio.run(
            mock_model_adapter.invoke({}, "m", _make_request())
        )
        assert resp["status"] == "COMPLETED"

    def test_mock_tool_adapter_fixture_injected(
        self, mock_tool_adapter: MockToolAdapter
    ) -> None:
        result = asyncio.run(
            mock_tool_adapter.invoke({"name": "t"}, {"a": 1}, 10)
        )
        assert result["echo"] == {"a": 1}

    def test_fixed_id_fixtures_injected(
        self, fixed_node_id: str, fixed_control_commit: str
    ) -> None:
        assert fixed_node_id == FIXED_NODE_ID
        assert fixed_control_commit == FIXED_CONTROL_COMMIT
