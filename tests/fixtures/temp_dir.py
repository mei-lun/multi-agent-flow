"""临时目录工具：保证成功和失败后均清理。

pytest 内置 ``tmp_path`` 已在会话后清理，但部分场景需要显式可控的临时目录
（如跨 fixture 共享、断言清理行为、验证清理语义）。本模块提供配套工具，
配套 conftest 的 ``tmp_project_dir`` fixture 使用 ``try/finally`` 保证清理。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path


def make_temp_dir(prefix: str = "maf-test-") -> Path:
    """创建一个临时目录并返回路径；调用方负责调用 ``cleanup_temp_dir`` 清理。"""
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup_temp_dir(path: Path | None) -> bool:
    """幂等清理临时目录，不存在时静默返回。

    返回是否执行了删除。清理失败不抛异常（用 ``ignore_errors``），
    避免阻断测试退出；调用方可在测试中显式断言目录已消失。
    """
    if path is None:
        return False
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


__all__ = ["make_temp_dir", "cleanup_temp_dir"]
