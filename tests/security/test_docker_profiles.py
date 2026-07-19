"""TASK-071 安全测试：Docker Profile 注册表。

验收标准覆盖：

1. **Profile 注册/解析/列表**：``register`` 注册自定义 Profile，``resolve``
   按名称返回 Profile，``list_profiles`` 列出所有已注册名称。
2. **未知 Profile 拒绝**：``resolve`` 对未知名称抛清晰错误。
3. **任意镜像拒绝**：``validate_profile`` 拒绝不在白名单内的镜像。
4. **浮动 latest 拒绝**：以 ``:latest`` 结尾的镜像引用被拒绝。
5. **privileged 拒绝**：``privileged=True`` 的 Profile 被拒绝。
6. **host 网络拒绝**：``network_mode="host"`` 的 Profile 被拒绝。
7. **Docker socket 挂载拒绝**：``mounts`` 包含 Docker socket 路径被拒绝。
8. **宿主机根目录挂载拒绝**：``mounts`` 挂载 ``/`` 被拒绝。
9. **read_only 默认 True 且强制为 True**：预定义 Profile 根文件系统只读。
10. **预定义 Profile 验证**：``generic`` 和 ``git-workspace`` 通过校验。
11. **resolve 返回副本**：调用方修改不影响注册表原始定义（Task 只能收紧）。
12. **DockerProfileSettings 配置加载**：``allowed_images`` 白名单可经环境变量覆盖。

与设计文档 §13.2、TASK-071 验收标准对齐。
"""

from __future__ import annotations

import os

import pytest

from maf_runner.config import DockerProfileSettings
from maf_runner.docker.profiles import (
    DEFAULT_ALLOWED_IMAGES,
    DEFAULT_CPU_QUOTA,
    DEFAULT_MEMORY_LIMIT,
    DockerProfile,
    DockerProfileRegistry,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 白名单内的 generic 镜像（digest-pinned）。
_GENERIC_IMAGE = DEFAULT_ALLOWED_IMAGES[0]
#: 白名单内的 git 镜像（digest-pinned）。
_GIT_IMAGE = DEFAULT_ALLOWED_IMAGES[1]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def registry() -> DockerProfileRegistry:
    """返回默认注册表（含 ``generic`` 和 ``git-workspace`` 预定义 Profile）。"""
    return DockerProfileRegistry()


def _make_valid_profile(
    *,
    name: str = "test-profile",
    image: str = _GENERIC_IMAGE,
    **overrides,
) -> DockerProfile:
    """构造一个通过校验的合法 Profile。"""
    kwargs: dict = dict(
        name=name,
        image=image,
        command=["sleep", "infinity"],
        env={"TEST": "true"},
        network_mode="bridge",
        capabilities=[],
        security_opts=["no-new-privileges"],
        read_only=True,
        memory_limit="256m",
        cpu_quota=50000,
        working_dir="/workspace",
        mounts=["tmpfs:/tmp:rw,size=32m"],
        privileged=False,
    )
    kwargs.update(overrides)
    return DockerProfile(**kwargs)


# --------------------------------------------------------------------------- #
# 验收 1：Profile 注册/解析/列表
# --------------------------------------------------------------------------- #


class TestProfileRegisterResolveList:
    """``register`` / ``resolve`` / ``list_profiles`` 基本行为。"""

    def test_predefined_profiles_registered(self, registry: DockerProfileRegistry) -> None:
        """构造时自动注册 ``generic`` 和 ``git-workspace``。"""
        profiles = registry.list_profiles()
        assert "generic" in profiles
        assert "git-workspace" in profiles

    def test_list_profiles_sorted(self, registry: DockerProfileRegistry) -> None:
        """``list_profiles`` 返回按字母序排列的名称列表。"""
        profiles = registry.list_profiles()
        assert profiles == sorted(profiles)

    def test_resolve_returns_profile(self, registry: DockerProfileRegistry) -> None:
        """``resolve`` 返回对应名称的 ``DockerProfile``。"""
        profile = registry.resolve("generic")
        assert isinstance(profile, DockerProfile)
        assert profile.name == "generic"
        assert profile.image == _GENERIC_IMAGE

    def test_resolve_git_workspace(self, registry: DockerProfileRegistry) -> None:
        """``resolve`` 返回 ``git-workspace`` Profile。"""
        profile = registry.resolve("git-workspace")
        assert profile.name == "git-workspace"
        assert profile.image == _GIT_IMAGE
        assert profile.network_mode == "bridge"

    def test_register_custom_profile(self, registry: DockerProfileRegistry) -> None:
        """``register`` 注册自定义 Profile 后可 ``resolve``。"""
        custom = _make_valid_profile(name="custom-python")
        registry.register(custom)
        assert "custom-python" in registry.list_profiles()
        resolved = registry.resolve("custom-python")
        assert resolved.name == "custom-python"

    def test_register_overwrites_existing(self, registry: DockerProfileRegistry) -> None:
        """同名 Profile 注册时覆盖旧定义。"""
        v1 = _make_valid_profile(name="custom", memory_limit="128m")
        v2 = _make_valid_profile(name="custom", memory_limit="256m")
        registry.register(v1)
        registry.register(v2)
        resolved = registry.resolve("custom")
        assert resolved.memory_limit == "256m"


# --------------------------------------------------------------------------- #
# 验收 2：未知 Profile 拒绝
# --------------------------------------------------------------------------- #


class TestUnknownProfileRejected:
    """``resolve`` 对未知名称抛清晰错误。"""

    def test_resolve_unknown_raises(self, registry: DockerProfileRegistry) -> None:
        with pytest.raises(ValueError, match="unknown docker profile"):
            registry.resolve("nonexistent")

    def test_resolve_unknown_lists_available(self, registry: DockerProfileRegistry) -> None:
        """错误消息列出可用 Profile 以便调试。"""
        with pytest.raises(ValueError, match="available profiles"):
            registry.resolve("missing")
        try:
            registry.resolve("missing")
        except ValueError as exc:
            msg = str(exc)
            assert "generic" in msg
            assert "git-workspace" in msg

    def test_resolve_empty_name_raises(self, registry: DockerProfileRegistry) -> None:
        with pytest.raises(ValueError, match="unknown docker profile"):
            registry.resolve("")


# --------------------------------------------------------------------------- #
# 验收 3：任意镜像拒绝（白名单约束）
# --------------------------------------------------------------------------- #


class TestImageWhitelistEnforced:
    """``validate_profile`` 拒绝不在白名单内的镜像。"""

    def test_arbitrary_image_rejected(self, registry: DockerProfileRegistry) -> None:
        """任意用户指定镜像被拒绝。"""
        profile = _make_valid_profile(image="ubuntu:22.04")
        with pytest.raises(ValueError, match="not in the allowed_images whitelist"):
            registry.validate_profile(profile)

    def test_arbitrary_digest_image_rejected(self, registry: DockerProfileRegistry) -> None:
        """即使 digest-pinned，不在白名单内的镜像也被拒绝。"""
        profile = _make_valid_profile(
            image="evil/runner@sha256:"
            "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        )
        with pytest.raises(ValueError, match="not in the allowed_images whitelist"):
            registry.validate_profile(profile)

    def test_whitelisted_image_accepted(self, registry: DockerProfileRegistry) -> None:
        """白名单内的镜像通过校验。"""
        profile = _make_valid_profile(image=_GENERIC_IMAGE)
        registry.validate_profile(profile)

    def test_register_with_arbitrary_image_rejected(
        self, registry: DockerProfileRegistry
    ) -> None:
        """``register`` 对任意镜像的 Profile 抛错且不注册。"""
        profile = _make_valid_profile(name="bad", image="ubuntu:22.04")
        with pytest.raises(ValueError, match="whitelist"):
            registry.register(profile)
        assert "bad" not in registry.list_profiles()

    def test_custom_whitelist_accepts_image(self) -> None:
        """自定义白名单内的镜像通过校验。"""
        custom_image = "custom/image@sha256:" + "a" * 64
        reg = DockerProfileRegistry(allowed_images=[custom_image])
        profile = _make_valid_profile(image=custom_image)
        reg.validate_profile(profile)

    def test_empty_whitelist_rejected(self) -> None:
        """空白名单构造失败。"""
        with pytest.raises(ValueError, match="allowed_images must not be empty"):
            DockerProfileRegistry(allowed_images=[])


# --------------------------------------------------------------------------- #
# 验收 4：浮动 latest 拒绝
# --------------------------------------------------------------------------- #


class TestFloatingLatestRejected:
    """以 ``:latest`` 结尾的镜像引用被拒绝（TASK-071 验收标准）。"""

    def test_latest_tag_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(image="maf/runner-generic:latest")
        with pytest.raises(ValueError, match="floating :latest"):
            registry.validate_profile(profile)

    def test_latest_tag_rejected_even_in_whitelist(self) -> None:
        """即使 ``:latest`` 镜像在白名单内，仍被浮动标签检查拒绝。"""
        reg = DockerProfileRegistry(
            allowed_images=["maf/runner-generic:latest", _GENERIC_IMAGE]
        )
        profile = _make_valid_profile(image="maf/runner-generic:latest")
        with pytest.raises(ValueError, match="floating :latest"):
            reg.validate_profile(profile)

    def test_digest_pinned_image_not_flagged_as_latest(
        self, registry: DockerProfileRegistry
    ) -> None:
        """digest-pinned 镜像（含 ``@sha256:``）不被 ``:latest`` 检查拦截。"""
        profile = _make_valid_profile(image=_GENERIC_IMAGE)
        # 不抛错即通过。
        registry.validate_profile(profile)


# --------------------------------------------------------------------------- #
# 验收 5：privileged 拒绝
# --------------------------------------------------------------------------- #


class TestPrivilegedRejected:
    """``privileged=True`` 的 Profile 被拒绝。"""

    def test_privileged_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(privileged=True)
        with pytest.raises(ValueError, match="privileged mode is forbidden"):
            registry.validate_profile(profile)

    def test_privileged_false_accepted(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(privileged=False)
        registry.validate_profile(profile)

    def test_predefined_profiles_not_privileged(
        self, registry: DockerProfileRegistry
    ) -> None:
        for name in registry.list_profiles():
            assert registry.resolve(name).privileged is False


# --------------------------------------------------------------------------- #
# 验收 6：host 网络拒绝
# --------------------------------------------------------------------------- #


class TestHostNetworkRejected:
    """``network_mode="host"`` 的 Profile 被拒绝。"""

    def test_host_network_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(network_mode="host")
        with pytest.raises(ValueError, match="host network mode is forbidden"):
            registry.validate_profile(profile)

    def test_bridge_network_accepted(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(network_mode="bridge")
        registry.validate_profile(profile)

    def test_none_network_accepted(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(network_mode="none")
        registry.validate_profile(profile)

    def test_empty_network_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(network_mode="")
        with pytest.raises(ValueError, match="network_mode must not be empty"):
            registry.validate_profile(profile)

    def test_predefined_profiles_not_host(
        self, registry: DockerProfileRegistry
    ) -> None:
        for name in registry.list_profiles():
            assert registry.resolve(name).network_mode != "host"


# --------------------------------------------------------------------------- #
# 验收 7：Docker socket 挂载拒绝
# --------------------------------------------------------------------------- #


class TestDockerSocketMountRejected:
    """``mounts`` 包含 Docker socket 路径被拒绝。"""

    @pytest.mark.parametrize(
        "mount",
        [
            "/var/run/docker.sock:/var/run/docker.sock",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "/run/docker.sock:/var/run/docker.sock",
            "/var/run/docker:/var/run/docker",
        ],
    )
    def test_docker_socket_mount_rejected(
        self, registry: DockerProfileRegistry, mount: str
    ) -> None:
        profile = _make_valid_profile(mounts=["tmpfs:/tmp:rw", mount])
        with pytest.raises(ValueError, match="Docker socket is forbidden"):
            registry.validate_profile(profile)

    def test_safe_mount_accepted(self, registry: DockerProfileRegistry) -> None:
        """非 Docker socket 的挂载通过校验。"""
        profile = _make_valid_profile(
            mounts=["workspace:/workspace:rw", "tmpfs:/tmp:rw,size=64m"]
        )
        registry.validate_profile(profile)

    def test_empty_mount_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(mounts=[""])
        with pytest.raises(ValueError, match="mount entry must not be empty"):
            registry.validate_profile(profile)

    def test_host_root_mount_rejected(self, registry: DockerProfileRegistry) -> None:
        """挂载宿主机根目录被拒绝。"""
        profile = _make_valid_profile(mounts=["/:/host"])
        with pytest.raises(ValueError, match="host root directory is forbidden"):
            registry.validate_profile(profile)


# --------------------------------------------------------------------------- #
# 验收 8 & 9：read_only 默认 True 且强制为 True
# --------------------------------------------------------------------------- #


class TestReadOnlyEnforced:
    """``read_only`` 默认 ``True`` 且 ``validate_profile`` 强制为 ``True``。"""

    def test_read_only_default_true(self) -> None:
        """``DockerProfile`` 的 ``read_only`` 默认为 ``True``。"""
        profile = DockerProfile(name="x", image=_GENERIC_IMAGE)
        assert profile.read_only is True

    def test_read_only_false_rejected(self, registry: DockerProfileRegistry) -> None:
        profile = _make_valid_profile(read_only=False)
        with pytest.raises(ValueError, match="read_only must be True"):
            registry.validate_profile(profile)

    def test_predefined_profiles_read_only(
        self, registry: DockerProfileRegistry
    ) -> None:
        for name in registry.list_profiles():
            assert registry.resolve(name).read_only is True


# --------------------------------------------------------------------------- #
# 验收 10：预定义 Profile 验证
# --------------------------------------------------------------------------- #


class TestPredefinedProfilesValid:
    """``generic`` 和 ``git-workspace`` 预定义 Profile 通过全部校验。"""

    def test_generic_profile_fields(self, registry: DockerProfileRegistry) -> None:
        profile = registry.resolve("generic")
        assert profile.name == "generic"
        assert profile.image == _GENERIC_IMAGE
        assert profile.network_mode == "bridge"
        assert profile.privileged is False
        assert profile.read_only is True
        assert "no-new-privileges" in profile.security_opts
        # 通用工作区有 tmpfs /tmp 挂载。
        assert any("tmpfs" in m and "/tmp" in m for m in profile.mounts)

    def test_git_workspace_profile_fields(
        self, registry: DockerProfileRegistry
    ) -> None:
        profile = registry.resolve("git-workspace")
        assert profile.name == "git-workspace"
        assert profile.image == _GIT_IMAGE
        assert profile.network_mode == "bridge"
        assert profile.privileged is False
        assert profile.read_only is True
        assert "no-new-privileges" in profile.security_opts
        # Git 工作区挂载工作目录。
        assert any("workspace" in m for m in profile.mounts)

    def test_predefined_profiles_pass_validation(
        self, registry: DockerProfileRegistry
    ) -> None:
        """预定义 Profile 本身通过 ``validate_profile``（构造时已校验）。"""
        for name in registry.list_profiles():
            profile = registry.resolve(name)
            registry.validate_profile(profile)

    def test_generic_has_no_capabilities(self, registry: DockerProfileRegistry) -> None:
        """``generic`` Profile 不追加 Linux capabilities。"""
        assert registry.resolve("generic").capabilities == []

    def test_git_workspace_has_no_capabilities(
        self, registry: DockerProfileRegistry
    ) -> None:
        assert registry.resolve("git-workspace").capabilities == []

    def test_predefined_memory_limit_matches_default(
        self, registry: DockerProfileRegistry
    ) -> None:
        for name in registry.list_profiles():
            assert registry.resolve(name).memory_limit == DEFAULT_MEMORY_LIMIT

    def test_predefined_cpu_quota_matches_default(
        self, registry: DockerProfileRegistry
    ) -> None:
        for name in registry.list_profiles():
            assert registry.resolve(name).cpu_quota == DEFAULT_CPU_QUOTA


# --------------------------------------------------------------------------- #
# 验收 11：resolve 返回副本（Task 只能收紧）
# --------------------------------------------------------------------------- #


class TestResolveReturnsCopy:
    """``resolve`` 返回 Profile 副本，调用方修改不影响注册表原始定义。"""

    def test_resolve_returns_independent_copy(
        self, registry: DockerProfileRegistry
    ) -> None:
        """修改 ``resolve`` 返回的 Profile 不影响注册表。"""
        profile = registry.resolve("generic")
        profile.memory_limit = "9999g"
        profile.capabilities.append("CAP_SYS_ADMIN")
        profile.mounts.append("/etc:/etc:ro")

        # 再次 resolve 应返回原始定义，不受修改影响。
        fresh = registry.resolve("generic")
        assert fresh.memory_limit == DEFAULT_MEMORY_LIMIT
        assert fresh.capabilities == []
        assert "CAP_SYS_ADMIN" not in fresh.capabilities
        assert "/etc:/etc:ro" not in fresh.mounts

    def test_resolve_command_list_is_copy(
        self, registry: DockerProfileRegistry
    ) -> None:
        profile = registry.resolve("generic")
        profile.command.append("injected")
        fresh = registry.resolve("generic")
        assert "injected" not in fresh.command

    def test_resolve_env_dict_is_copy(self, registry: DockerProfileRegistry) -> None:
        profile = registry.resolve("generic")
        profile.env["INJECTED"] = "yes"
        fresh = registry.resolve("generic")
        assert "INJECTED" not in fresh.env

    def test_resolve_mounts_list_is_copy(
        self, registry: DockerProfileRegistry
    ) -> None:
        profile = registry.resolve("git-workspace")
        original_mounts = list(profile.mounts)
        profile.mounts.clear()
        fresh = registry.resolve("git-workspace")
        assert fresh.mounts == original_mounts


# --------------------------------------------------------------------------- #
# 验收 12：DockerProfileSettings 配置加载
# --------------------------------------------------------------------------- #


class TestDockerProfileSettings:
    """``DockerProfileSettings`` 从环境变量加载白名单与默认内存上限。"""

    def test_defaults_match_registry(self) -> None:
        """``DockerProfileSettings`` 默认白名单与注册表默认一致。"""
        settings = DockerProfileSettings(_env_file=None)
        assert set(settings.allowed_images) == set(DEFAULT_ALLOWED_IMAGES)
        assert settings.default_memory_limit == DEFAULT_MEMORY_LIMIT

    def test_allowed_images_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MAF_DOCKER_PROFILE_ALLOWED_IMAGES`` 以 JSON 数组注入白名单。"""
        import json

        custom = "custom/img@sha256:" + "b" * 64
        # pydantic-settings 对 list[str] 类型环境变量要求 JSON 数组格式。
        monkeypatch.setenv(
            "MAF_DOCKER_PROFILE_ALLOWED_IMAGES",
            json.dumps([custom, _GENERIC_IMAGE]),
        )
        settings = DockerProfileSettings(_env_file=None)
        assert custom in settings.allowed_images
        assert _GENERIC_IMAGE in settings.allowed_images

    def test_default_memory_limit_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAF_DOCKER_PROFILE_DEFAULT_MEMORY_LIMIT", "1024m")
        settings = DockerProfileSettings(_env_file=None)
        assert settings.default_memory_limit == "1024m"

    def test_registry_uses_custom_settings(self) -> None:
        """``DockerProfileRegistry`` 可使用 ``DockerProfileSettings`` 的白名单。"""
        custom_image = "custom/image@sha256:" + "c" * 64
        settings = DockerProfileSettings(_env_file=None)
        # 模拟从 settings 构造注册表。
        reg = DockerProfileRegistry(
            allowed_images=list(settings.allowed_images) + [custom_image],
            default_memory_limit=settings.default_memory_limit,
        )
        profile = _make_valid_profile(image=custom_image)
        reg.validate_profile(profile)

    def test_default_memory_limit_applied_to_predefined(self) -> None:
        """自定义 ``default_memory_limit`` 应用到预定义 Profile。"""
        reg = DockerProfileRegistry(default_memory_limit="1024m")
        for name in reg.list_profiles():
            assert reg.resolve(name).memory_limit == "1024m"


# --------------------------------------------------------------------------- #
# 验收：边界测试（register 校验失败不污染注册表）
# --------------------------------------------------------------------------- #


class TestRegisterValidationBoundaries:
    """``register`` 校验失败时不注册，注册表状态不变。"""

    def test_register_invalid_profile_not_added(
        self, registry: DockerProfileRegistry
    ) -> None:
        """校验失败的 Profile 不进入注册表。"""
        initial = set(registry.list_profiles())
        bad = _make_valid_profile(name="bad", image="ubuntu:22.04")
        with pytest.raises(ValueError):
            registry.register(bad)
        assert set(registry.list_profiles()) == initial
        with pytest.raises(ValueError, match="unknown docker profile"):
            registry.resolve("bad")

    def test_register_valid_after_invalid(
        self, registry: DockerProfileRegistry
    ) -> None:
        """一次校验失败后仍可注册合法 Profile。"""
        bad = _make_valid_profile(name="bad", image="ubuntu:22.04")
        with pytest.raises(ValueError):
            registry.register(bad)
        good = _make_valid_profile(name="good")
        registry.register(good)
        assert "good" in registry.list_profiles()

    def test_multiple_security_violations_reported(
        self, registry: DockerProfileRegistry
    ) -> None:
        """多个安全违规时，第一个违规被报告（fail-fast）。"""
        profile = _make_valid_profile(
            image="ubuntu:22.04",
            network_mode="host",
            privileged=True,
            read_only=False,
        )
        with pytest.raises(ValueError):
            registry.validate_profile(profile)
