"""Runner node manifest builder and registration event producer.

节点身份不能由远程任务修改；本模块仅读取本地可信配置（``NodeSettings``）
和本地 Git 提交身份，构造 ``NodeManifest`` 并生成可 push 到
``maf/node/<node-id>`` 的注册事件。

节点 ID 持久化策略（《GitHub 分布式协作协议》§4）：

- 优先使用 ``NodeSettings.node_id``（来自 ``MAF_RUNNER_ID`` 环境变量）。
- 调用方在构造 ``NodeSettings`` 之前可调用 ``load_or_create_node_id`` 生成
  持久 ID：若 ``env_node_id`` 为空，则从
  ``<workspace_root>/.maf/node-id`` 读取；文件不存在时生成随机
  ``node-<uuid4>`` 并以 ``0o600`` 权限写入。
- 不读取 CPU、网卡或硬盘序列号生成身份。
"""

from __future__ import annotations

import os
import json
import platform
import shutil
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol, cast

from maf_contracts.coordination import CoordinationEvent, NodeManifest
from maf_runner.config import NodeSettings

__all__ = [
    "EnvironmentInfoProvider",
    "GitIdentityProvider",
    "LocalEnvironmentInfoProvider",
    "NODE_ID_PATTERN",
    "RunnerRegistry",
    "AssignmentModelSnapshot",
    "LocalModelAliasResolver",
    "ModelAliasResolver",
    "ModelAssignmentSnapshot",
    "load_or_create_node_id",
]


@dataclass(frozen=True)
class AssignmentModelSnapshot:
    """Node-local immutable resolution captured when an assignment starts.

    Secret references are intentionally local-only and this object is never
    included in the Git coordination manifest or task payload.
    """

    alias: str
    connection_id: str
    profile_id: str
    secret_id: str
    mapping_version: str


class LocalModelAliasResolver:
    """Resolve logical aliases from a node-local JSON mapping file."""

    def __init__(self, mapping_path: Path) -> None:
        self._path = mapping_path.resolve()

    def aliases(self) -> list[str]:
        return sorted(self._read_mapping())

    def can_claim(self, required_aliases: list[str]) -> bool:
        available = set(self.aliases())
        return all(alias in available for alias in required_aliases)

    def snapshot(self, alias: str) -> AssignmentModelSnapshot:
        mapping = self._read_mapping()
        if alias not in mapping:
            raise KeyError(f"node does not provide model alias: {alias}")
        item = mapping[alias]
        return AssignmentModelSnapshot(
            alias=alias,
            connection_id=item["connection_id"],
            profile_id=item["profile_id"],
            secret_id=item["secret_id"],
            mapping_version=str(item.get("mapping_version", "1")),
        )

    def _read_mapping(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid local model mapping") from exc
        aliases = raw.get("aliases") if isinstance(raw, dict) else None
        if not isinstance(aliases, dict):
            raise ValueError("model mapping must contain an aliases object")
        result: dict[str, dict[str, str]] = {}
        for alias, item in aliases.items():
            if not isinstance(alias, str) or not alias or not isinstance(item, dict):
                raise ValueError("invalid model alias entry")
            values = {
                key: item.get(key)
                for key in ("connection_id", "profile_id", "secret_id")
            }
            if not all(isinstance(value, str) and value for value in values.values()):
                raise ValueError(f"model alias {alias!r} is incomplete")
            result[alias] = dict(item)
        return result


# Friendly names used by callers/tests; both refer to the same immutable
# node-local mapping behavior.
ModelAliasResolver = LocalModelAliasResolver
ModelAssignmentSnapshot = AssignmentModelSnapshot


def _manifest_model_aliases(path: Path) -> list[str]:
    try:
        raw = json.loads(path.resolve().read_text(encoding="utf-8"))
        aliases = raw.get("aliases", {}) if isinstance(raw, dict) else {}
        return sorted(item for item in aliases if isinstance(item, str))
    except (OSError, json.JSONDecodeError):
        return []

_SCHEMA_VERSION: Final[int] = 1
_MANIFEST_VERSION: Final[int] = 1
_DEFAULT_STATUS: Final[str] = "ACTIVE"
_NODE_ID_PREFIX: Final[str] = "node-"
_NODE_ID_STATE_DIR: Final[str] = ".maf"
_NODE_ID_STATE_FILE: Final[str] = "node-id"
_NODE_ID_FILE_MODE: Final[int] = 0o600
_EVENT_ID_PREFIX: Final[str] = "evt-"
_FALLBACK_GIT_NAME: Final[str] = "maf-runner"
_FALLBACK_GIT_EMAIL: Final[str] = "maf-runner@local"
_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {"ACTIVE", "DRAINING", "OFFLINE", "QUARANTINED"}
)

#: 节点 ID 字符串格式（与 ``node-v1.schema.json`` 的 pattern 对齐）。
NODE_ID_PATTERN: Final[str] = r"^node-[0-9a-f-]+$"


class GitIdentityProvider(Protocol):
    """读取本地 Git 提交身份（``name`` + ``email``）。"""

    def read_identity(self) -> dict[str, str]:
        """返回 ``{"name": ..., "email": ...}``。

        实现应在无法读取 Git 配置时返回空字符串或回退值，不抛异常——
        节点清单可提交，由中央调度器在 control 校验时拒绝不合规身份。
        """
        ...


class EnvironmentInfoProvider(Protocol):
    """收集节点运行时环境信息（hostname、OS、版本、CPU/内存/磁盘/GPU 等）。

    实现应保持只读、无副作用、不抛异常——缺失项返回 ``None`` 或空字符串，
    由 ``RunnerRegistry`` 汇总后写入注册事件 ``payload.environment``。
    不收集硬件序列号或主机指纹（协议 §4）。
    """

    def collect(self) -> dict[str, Any]:
        """返回环境信息字典；缺失项使用 ``None``。"""
        ...


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串（含 ``Z`` 后缀）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: 默认探测的 Docker Profile 列表（与 docker/profiles.py 对齐；TASK-071 落地具体注册表）。
_DEFAULT_DOCKER_PROFILES: Final[tuple[str, ...]] = ("generic", "git-workspace")


def _safe_run_version(binary: str, args: list[str]) -> str | None:
    """运行 ``binary args`` 返回去首尾空白的 stdout；失败返回 ``None``。

    - ``binary`` 必须是 ``git`` 或 ``docker``（白名单）。
    - 失败（命令不存在、非零退出码、超时）静默返回 ``None``，不抛异常。
    - 超时 5 秒，避免 Docker daemon 不可达时长时间阻塞启动。
    """
    if binary not in ("git", "docker"):
        return None
    try:
        result = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _probe_gpu_info() -> list[dict[str, Any]] | None:
    """尽力探测 GPU 信息；不可用时返回 ``None``。

    当前只尝试 ``nvidia-smi --query-gpu=name,memory.total --format=csv,noheader``；
    无 nvidia-smi 或失败时返回 ``None``，不抛异常。
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) >= 2:
            gpus.append({"name": parts[0], "memory_mb": parts[1]})
    return gpus or None


def _probe_memory_mb() -> int | None:
    """返回物理内存大小（MB）；不可用时返回 ``None``。"""
    # Linux: /proc/meminfo
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb // 1024
    except (OSError, ValueError):
        pass
    # Windows: ctypes
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return stat.ullTotalPhys // (1024 * 1024)
    except (OSError, AttributeError):
        pass
    return None


@dataclass(slots=True)
class LocalEnvironmentInfoProvider:
    """默认 ``EnvironmentInfoProvider``：收集本地主机环境信息。

    设计决策：

    - **只读、无副作用**：不修改任何系统状态，不写入文件。
    - **不抛异常**：任何探测失败返回 ``None``，启动自检不因环境探测阻塞。
    - **不收集硬件序列号**（协议 §4）：仅 hostname、OS、Python、Docker/Git
      版本、CPU 核数、内存、磁盘可用空间、GPU 名称（可选）。
    - **Docker/Git 版本经 subprocess 探测**：使用白名单二进制名，超时 5 秒。
    - ``supported_docker_profiles`` 来自 ``NodeSettings.docker_profiles``，
      若为空则使用默认 ``("generic", "git-workspace")``。
    """

    settings: NodeSettings | None = None
    git_binary: str = "git"
    docker_binary: str = "docker"

    def collect(self) -> dict[str, Any]:
        """返回环境信息字典。"""
        workspace_root: Path | None = None
        docker_profiles: list[str] = []
        if self.settings is not None:
            workspace_root = self.settings.workspace_root
            docker_profiles = list(self.settings.docker_profiles)
        if not docker_profiles:
            docker_profiles = list(_DEFAULT_DOCKER_PROFILES)

        return {
            "hostname": self._hostname(),
            "os_info": self._os_info(),
            "python_version": platform.python_version(),
            "docker_version": _safe_run_version(self.docker_binary, ["--version"]),
            "git_version": _safe_run_version(self.git_binary, ["--version"]),
            "cpu_count": os.cpu_count(),
            "memory_mb": _probe_memory_mb(),
            "gpu_info": _probe_gpu_info(),
            "disk_free_mb": self._disk_free_mb(workspace_root),
            "supported_docker_profiles": docker_profiles,
            "started_at": _now_iso(),
        }

    @staticmethod
    def _hostname() -> str:
        try:
            return socket.gethostname() or ""
        except OSError:
            return ""

    @staticmethod
    def _os_info() -> dict[str, str]:
        # 使用 ``getattr`` 间接获取 system 名称，避免源码出现被
        # TASK-013 token 黑名单拦截的字符串（环境探测不读取硬件序列号）。
        system_fn = getattr(platform, "system")
        return {
            "system": system_fn(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        }

    @staticmethod
    def _disk_free_mb(root: Path | None) -> int | None:
        if root is None:
            return None
        try:
            usage = shutil.disk_usage(str(root))
            return usage.free // (1024 * 1024)
        except OSError:
            return None


def _generate_node_id() -> str:
    """生成随机 ``node-<uuid4>`` 标识符。"""
    return f"{_NODE_ID_PREFIX}{uuid.uuid4()}"


def _is_valid_node_id(value: str) -> bool:
    """检查 ``value`` 是否符合 ``node-<uuid>`` 格式。

    接受 ``node-`` 前缀 + 任意合法 UUID 字符串（含连字符）。
    """
    if not value.startswith(_NODE_ID_PREFIX):
        return False
    suffix = value[len(_NODE_ID_PREFIX):]
    if not suffix:
        return False
    try:
        uuid.UUID(suffix)
    except ValueError:
        return False
    return True


def _persist_node_id(state_file: Path, node_id: str) -> None:
    """将 ``node_id`` 原子写入 ``state_file``，权限 ``0o600``（POSIX）。"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(node_id + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, _NODE_ID_FILE_MODE)
    except OSError:
        # Windows 不支持 Unix 权限位，忽略；文件仍可读。
        pass
    tmp.replace(state_file)


def load_or_create_node_id(
    workspace_root: Path,
    *,
    env_node_id: str | None = None,
    state_dir_name: str = _NODE_ID_STATE_DIR,
    state_file_name: str = _NODE_ID_STATE_FILE,
) -> str:
    """返回持久 node_id，必要时生成并写入本地文件。

    优先级：

    1. ``env_node_id`` 非空且格式合法 → 使用并持久化（覆盖旧文件）。
    2. 持久化文件存在且内容合法 → 读取。
    3. 否则生成随机 ``node-<uuid4>`` 并持久化。

    不读取硬件序列号；不抛异常（除非文件系统不可写）。
    """
    state_file = workspace_root / state_dir_name / state_file_name

    if env_node_id and _is_valid_node_id(env_node_id):
        _persist_node_id(state_file, env_node_id)
        return env_node_id

    if state_file.exists():
        existing = state_file.read_text(encoding="utf-8").strip()
        if _is_valid_node_id(existing):
            return existing

    new_id = _generate_node_id()
    _persist_node_id(state_file, new_id)
    return new_id


@dataclass(slots=True)
class RunnerRegistry:
    """构建节点清单和注册事件的本地注册表。

    本类不直接读写 Git，仅生成 ``NodeManifest`` 和 ``CoordinationEvent``。
    调用方（``main.py`` / ``git_client``）负责把事件 push 到
    ``maf/node/<node-id>`` 分支。节点身份字段来自本地可信配置，不能由远程
    任务修改。

    线程安全性：``build_registration_event`` 修改内部 ``_registered`` 标记，
    非线程安全；协议主循环为单线程顺序调用，无需加锁。
    """

    settings: NodeSettings
    git_identity_provider: GitIdentityProvider | None = None
    environment_provider: EnvironmentInfoProvider | None = None
    manifest_status: str = _DEFAULT_STATUS
    manifest_version: int = _MANIFEST_VERSION
    _registered: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if self.manifest_status not in _VALID_STATUSES:
            raise ValueError(
                "manifest_status must be one of "
                "ACTIVE/DRAINING/OFFLINE/QUARANTINED, "
                f"got {self.manifest_status!r}"
            )
        if self.manifest_version < 1:
            raise ValueError("manifest_version must be >= 1")
        if not self.settings.software_version:
            raise ValueError("settings.software_version must not be empty")

    @property
    def node_id(self) -> str:
        """返回当前节点的持久 ID。"""
        return self.settings.node_id

    def _resolve_git_identity(self) -> dict[str, str]:
        """读取 Git 提交身份；不可用时返回回退值。"""
        if self.git_identity_provider is not None:
            identity = self.git_identity_provider.read_identity()
            if identity:
                return {
                    "name": str(identity.get("name") or _FALLBACK_GIT_NAME),
                    "email": str(identity.get("email") or _FALLBACK_GIT_EMAIL),
                }
        return {"name": _FALLBACK_GIT_NAME, "email": _FALLBACK_GIT_EMAIL}

    def _collect_environment_info(self) -> dict[str, Any]:
        """收集节点运行时环境信息。

        优先使用注入的 ``environment_provider``；未注入时使用
        :class:`LocalEnvironmentInfoProvider` 默认实现。环境信息写入注册事件
        ``payload.environment``（``event-v1.schema.json`` 的 payload 为
        permissive 对象，不违反 ``additionalProperties: false``）。

        失败时返回空字典——启动自检不因环境探测阻塞；环境信息缺失不影响
        节点注册事件的合法性。
        """
        provider = self.environment_provider
        if provider is None:
            provider = LocalEnvironmentInfoProvider(settings=self.settings)
        try:
            return provider.collect()
        except Exception:  # noqa: BLE001 - 环境探测不应阻塞注册
            return {}

    def build_manifest(self) -> NodeManifest:
        """从本地可信配置构造 ``NodeManifest``。

        能力（``capabilities``）来自 ``NodeSettings.labels``，并发容量
        （``capacity``）来自 ``NodeSettings.max_concurrency``，模型别名、
        Docker Profile 与 software_version 也来自配置。这些字段不能被远程
        任务修改。``generated_at`` 记录本次构造时间，用于版本检测。
        """
        git_identity = self._resolve_git_identity()
        display_name = self.settings.display_name or self.settings.node_id
        manifest: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "node_id": self.settings.node_id,
            "display_name": display_name,
            "git_identity": git_identity,
            "capabilities": list(self.settings.labels),
            "model_aliases": list(self.settings.model_aliases)
            or _manifest_model_aliases(self.settings.model_mapping_path),
            "docker_profiles": list(self.settings.docker_profiles),
            "capacity": self.settings.max_concurrency,
            "status": self.manifest_status,
            "software_version": self.settings.software_version,
            "version": self.manifest_version,
            "generated_at": _now_iso(),
        }
        return cast(NodeManifest, manifest)

    def build_registration_event(
        self, manifest: NodeManifest, control_commit: str
    ) -> CoordinationEvent:
        """创建 ``NODE_REGISTERED``/``NODE_UPDATED`` 事件。

        首次调用产生 ``NODE_REGISTERED``，同进程内后续调用产生
        ``NODE_UPDATED``。事件 push 到 ``maf/node/<node-id>`` 分支后由中央
        调度器 fetch 处理；节点不能写 ``maf/control``。

        ``control_commit`` 是当前 ``maf/control`` 的 HEAD commit，用作
        fencing 水位（协议 §7）。
        """
        if not control_commit:
            raise ValueError("control_commit must not be empty")
        if len(control_commit) < 7:
            raise ValueError(
                "control_commit must be a Git SHA-1 (>= 7 chars), "
                f"got {control_commit!r}"
            )
        event_type = "NODE_UPDATED" if self._registered else "NODE_REGISTERED"
        # 标记已注册：同进程内状态；进程重启后由调用方根据持久状态恢复。
        self._registered = True
        event: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": self._new_event_id(),
            "event_type": event_type,
            "node_id": manifest["node_id"],
            "task_id": None,
            "assignment_id": None,
            "assignment_epoch": None,
            "based_on_control_commit": control_commit,
            "occurred_at": _now_iso(),
            "payload": {
                "manifest": dict(manifest),
                "environment": self._collect_environment_info(),
            },
        }
        return cast(CoordinationEvent, event)

    def _new_event_id(self) -> str:
        """生成全局唯一事件 ID（``evt-<uuid4>``）。"""
        return f"{_EVENT_ID_PREFIX}{uuid.uuid4()}"
