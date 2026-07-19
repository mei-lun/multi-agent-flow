"""Docker Profile 注册表：预定义容器配置模板与安全约束。

TASK-071 实现。管理预定义的 Docker 容器配置模板（``DockerProfile``），
强制镜像白名单、无 privileged、无 host 网络、无 Docker socket 挂载、
只读根文件系统，并拒绝浮动 ``:latest`` 镜像标签。

设计决策：

- **镜像白名单**：``DockerProfile.image`` 必须出现在
  ``DockerProfileSettings.allowed_images`` 白名单内；白名单默认包含
  digest-pinned 镜像引用，禁止任意用户指定镜像。
- **浮动 latest 拒绝**：以 ``:latest`` 结尾且未用 ``@sha256:`` digest 定址
  的镜像引用被拒绝（TASK-071 验收标准）。
- **预定义 Profile**：``generic``（通用工作区，bridge 网络，只读根文件系统 +
  tmpfs /tmp）和 ``git-workspace``（Git 任务工作区，bridge 网络，挂载工作目录）。
- **Task 只能收紧**：``resolve`` 返回不可变预定义 Profile 的副本，调用方只能
  在本地进一步收紧资源限制，不能放宽（如调高 memory_limit 或追加 capability）。
- **不创建容器**：本模块仅管理 Profile 定义与校验；容器创建由 TASK-072
  ``DockerManager`` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

__all__ = [
    "DockerProfile",
    "DockerProfileRegistry",
    "DEFAULT_ALLOWED_IMAGES",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_CPU_QUOTA",
]

# --------------------------------------------------------------------------- #
# 默认白名单镜像（digest-pinned，禁止浮动 latest）
# --------------------------------------------------------------------------- #

#: 默认 Docker Profile 镜像白名单。所有镜像均通过 ``@sha256:`` digest 定址，
#: 避免浮动标签（如 ``:latest``）被恶意替换。digest 为占位值，实际部署时
#: 通过 ``MAF_DOCKER_PROFILE_ALLOWED_IMAGES`` 环境变量注入真实 digest。
_DEFAULT_GENERIC_IMAGE: Final[str] = (
    "maf/runner-generic@sha256:"
    "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
)
_DEFAULT_GIT_IMAGE: Final[str] = (
    "maf/runner-git@sha256:"
    "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3"
)

#: 默认镜像白名单元组（``DockerProfileRegistry`` 构造时使用）。
DEFAULT_ALLOWED_IMAGES: Final[tuple[str, ...]] = (
    _DEFAULT_GENERIC_IMAGE,
    _DEFAULT_GIT_IMAGE,
)

#: 默认内存上限（与设计文档 §13.2 对齐）。
DEFAULT_MEMORY_LIMIT: Final[str] = "512m"

#: 默认 CPU 配额（微秒；100000 = 0.1 CPU，与 Docker ``--cpu-quota`` 对齐）。
DEFAULT_CPU_QUOTA: Final[int] = 100000

#: Docker socket 挂载特征，用于 ``validate_profile`` 拒绝 Docker socket 挂载。
_DOCKER_SOCKET_MARKERS: Final[tuple[str, ...]] = (
    "docker.sock",
    "/var/run/docker",
    "/run/docker.sock",
)

#: 预定义 Profile 名称。
_PROFILE_GENERIC: Final[str] = "generic"
_PROFILE_GIT_WORKSPACE: Final[str] = "git-workspace"


def _memory_bytes(value: str) -> int:
    raw = str(value).strip().lower()
    units = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2,
             "g": 1024**3, "gb": 1024**3}
    for suffix, multiplier in sorted(units.items(), key=lambda item: -len(item[0])):
        if raw.endswith(suffix):
            return int(float(raw[:-len(suffix)]) * multiplier)
    return int(raw)


# --------------------------------------------------------------------------- #
# DockerProfile dataclass
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DockerProfile:
    """预定义的 Docker 容器配置模板。

    字段对应 Docker 容器运行时安全参数。``validate_profile`` 强制以下安全约束：

    - ``image`` 必须在 ``DockerProfileRegistry`` 的白名单内；
    - ``image`` 不得使用浮动 ``:latest`` 标签（必须 digest-pinned）；
    - ``privileged`` 必须为 ``False``；
    - ``network_mode`` 不得为 ``"host"``；
    - ``mounts`` 不得挂载 Docker socket 或宿主机根目录；
    - ``read_only`` 必须为 ``True``（根文件系统只读）。

    ``capabilities`` 默认为空（不追加 Linux capabilities）；
    ``security_opts`` 默认包含 ``"no-new-privileges"``（由预定义 Profile 设置）。
    """

    name: str
    image: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    network_mode: str = "bridge"
    capabilities: list[str] = field(default_factory=list)
    security_opts: list[str] = field(default_factory=list)
    read_only: bool = True
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    cpu_quota: int = DEFAULT_CPU_QUOTA
    working_dir: str = "/workspace"
    mounts: list[str] = field(default_factory=list)
    #: ``privileged`` 字段仅用于 ``validate_profile`` 拒绝；预定义 Profile
    #: 与合法自定义 Profile 必须保持 ``False``。
    privileged: bool = False


# --------------------------------------------------------------------------- #
# DockerProfileRegistry
# --------------------------------------------------------------------------- #


class DockerProfileRegistry:
    """Docker Profile 注册表：注册、解析、列举和校验预定义 Profile。

    构造时自动注册 ``generic`` 和 ``git-workspace`` 两个预定义 Profile。
    自定义 Profile 可通过 :meth:`register` 注册（注册前经 ``validate_profile``
    校验安全约束）。

    线程安全性：注册表在构造后通常只读；``register`` 非线程安全，应在
    启动阶段单线程调用。``resolve`` 返回 Profile 副本，调用方修改不影响
    注册表内的原始定义。
    """

    def __init__(
        self,
        *,
        allowed_images: list[str] | tuple[str, ...] | None = None,
        default_memory_limit: str = DEFAULT_MEMORY_LIMIT,
    ) -> None:
        if allowed_images is None:
            self._allowed_images: frozenset[str] = frozenset(DEFAULT_ALLOWED_IMAGES)
        else:
            self._allowed_images = frozenset(allowed_images)
        if not self._allowed_images:
            raise ValueError("allowed_images must not be empty")
        self._default_memory_limit: str = default_memory_limit
        self._profiles: dict[str, DockerProfile] = {}
        self._register_predefined()

    # -- 预定义 Profile 注册 ------------------------------------------- #

    def _register_predefined(self) -> None:
        """注册 ``generic`` 和 ``git-workspace`` 预定义 Profile。

        仅注册镜像在当前白名单内的预定义 Profile；若自定义白名单排除了
        默认镜像，对应的预定义 Profile 不注册（其镜像不被允许）。
        """
        candidates = (
            DockerProfile(
                name=_PROFILE_GENERIC,
                image=_DEFAULT_GENERIC_IMAGE,
                command=["sleep", "infinity"],
                env={"MAF_PROFILE": "generic"},
                network_mode="bridge",
                capabilities=[],
                security_opts=["no-new-privileges"],
                read_only=True,
                memory_limit=self._default_memory_limit,
                cpu_quota=DEFAULT_CPU_QUOTA,
                working_dir="/workspace",
                mounts=["tmpfs:/tmp:rw,size=64m"],
                privileged=False,
            ),
            DockerProfile(
                name=_PROFILE_GIT_WORKSPACE,
                image=_DEFAULT_GIT_IMAGE,
                command=["sleep", "infinity"],
                env={
                    "MAF_PROFILE": "git-workspace",
                    "GIT_CONFIG_GLOBAL": "/tmp/gitconfig",
                },
                network_mode="bridge",
                capabilities=[],
                security_opts=["no-new-privileges"],
                read_only=True,
                memory_limit=self._default_memory_limit,
                cpu_quota=DEFAULT_CPU_QUOTA,
                working_dir="/workspace",
                mounts=[
                    "workspace:/workspace:rw",
                    "tmpfs:/tmp:rw,size=64m",
                ],
                privileged=False,
            ),
        )
        for profile in candidates:
            if profile.image not in self._allowed_images:
                continue
            self.validate_profile(profile)
            self._profiles[profile.name] = profile

    # -- 公共接口 ------------------------------------------------------ #

    def register(self, profile: DockerProfile) -> None:
        """注册一个 Profile。

        注册前调用 :meth:`validate_profile` 校验安全约束；校验失败抛
        ``ValueError``。同名 Profile 覆盖旧定义（用于启动阶段装配）。
        """
        self.validate_profile(profile)
        self._profiles[profile.name] = profile

    def resolve(self, name: str) -> DockerProfile:
        """按名称解析 Profile，返回副本。

        未知名称抛 ``ValueError``，错误消息列出所有可用 Profile 以便调试。
        返回的 Profile 是原始定义的深拷贝（通过 ``dataclasses.replace``），
        调用方只能进一步收紧资源限制，不能放宽注册表内的原始定义。
        """
        if name not in self._profiles:
            available = ", ".join(sorted(self._profiles.keys())) or "(empty)"
            raise ValueError(
                f"unknown docker profile: {name!r}; available profiles: {available}"
            )
        original = self._profiles[name]
        return replace(
            original,
            command=list(original.command),
            env=dict(original.env),
            capabilities=list(original.capabilities),
            security_opts=list(original.security_opts),
            mounts=list(original.mounts),
        )

    def constrain(self, name: str, *, memory_limit: str | None = None,
                  cpu_quota: int | None = None, capabilities: list[str] | None = None) -> DockerProfile:
        """返回只能收紧资源的 profile 副本。

        任务声明不能提高内存/CPU，也不能追加 Linux capability；空值表示沿用已发布值。
        """
        original = self.resolve(name)
        if memory_limit is not None and _memory_bytes(memory_limit) > _memory_bytes(original.memory_limit):
            raise ValueError("task may not increase memory limit")
        if cpu_quota is not None and cpu_quota > original.cpu_quota:
            raise ValueError("task may not increase cpu quota")
        if capabilities:
            if not set(capabilities).issubset(set(original.capabilities)):
                raise ValueError("task may not add capabilities")
        return replace(original, memory_limit=memory_limit or original.memory_limit,
                       cpu_quota=cpu_quota or original.cpu_quota,
                       capabilities=list(capabilities or original.capabilities))

    def list_profiles(self) -> list[str]:
        """返回所有已注册 Profile 名称（按字母序）。"""
        return sorted(self._profiles.keys())

    def validate_profile(self, profile: DockerProfile) -> None:
        """校验 Profile 安全约束；违反时抛 ``ValueError``。

        强制约束（与设计文档 §13.2、TASK-071 验收标准对齐）：

        1. **无浮动 latest**：镜像不得以 ``:latest`` 结尾（必须 digest-pinned）。
        2. **镜像白名单**：``image`` 必须在 ``allowed_images`` 白名单内。
        3. **无 privileged**：``privileged`` 必须为 ``False``。
        4. **无 host 网络**：``network_mode`` 不得为 ``"host"``。
        5. **无 Docker socket 挂载**：``mounts`` 不得包含 Docker socket 路径。
        6. **无宿主机根目录挂载**：``mounts`` 不得挂载 ``/`` 到容器。
        7. **只读根文件系统**：``read_only`` 必须为 ``True``。
        """
        # 1. 无浮动 latest 标签（格式检查优先于白名单内容检查）
        if profile.image.endswith(":latest"):
            raise ValueError(
                f"floating :latest tag is not allowed: {profile.image!r}; "
                "use a digest-pinned image (e.g. name@sha256:...)"
            )
        # 2. 镜像白名单
        if profile.image not in self._allowed_images:
            raise ValueError(
                f"image {profile.image!r} is not in the allowed_images whitelist; "
                f"allowed: {sorted(self._allowed_images)}"
            )
        # 3. 无 privileged 模式
        if profile.privileged:
            raise ValueError(
                "privileged mode is forbidden; DockerProfile.privileged must be False"
            )
        # 4. 无 host 网络模式
        if profile.network_mode == "host":
            raise ValueError(
                "host network mode is forbidden; use 'bridge' or 'none' instead"
            )
        if not profile.network_mode:
            raise ValueError("network_mode must not be empty")
        # 5 & 6. 挂载安全：禁止 Docker socket 和宿主机根目录
        for mount in profile.mounts:
            self._validate_mount(mount)
        # 7. 只读根文件系统
        if not profile.read_only:
            raise ValueError(
                "read_only must be True; writable areas should use tmpfs or "
                "scoped bind mounts, not a writable root filesystem"
            )

    @staticmethod
    def _validate_mount(mount: str) -> None:
        """校验单个挂载项：禁止 Docker socket 和宿主机根目录。"""
        if not mount:
            raise ValueError("mount entry must not be empty")
        lowered = mount.lower()
        for marker in _DOCKER_SOCKET_MARKERS:
            if marker in lowered:
                raise ValueError(
                    f"mounting Docker socket is forbidden: {mount!r}"
                )
        # 禁止挂载宿主机根目录（如 "/:/host" 或 "/:/"）。
        # 挂载规范形如 "src:dst[:mode]"；检查 src 部分。
        parts = mount.split(":")
        if len(parts) >= 2:
            src = parts[0]
            if src in ("/", ""):
                raise ValueError(
                    f"mounting host root directory is forbidden: {mount!r}"
                )
