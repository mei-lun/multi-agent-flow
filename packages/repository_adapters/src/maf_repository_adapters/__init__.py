"""GitHub 与 Local Git Adapter 公共接口包。"""

from .base import RepositoryAdapter
from .git_cli import (
    ALLOWED_SUBCOMMANDS,
    FORBIDDEN_FLAGS,
    GitCommandError,
    SubprocessGitCli,
)

__all__ = [
    "ALLOWED_SUBCOMMANDS",
    "FORBIDDEN_FLAGS",
    "GitCommandError",
    "RepositoryAdapter",
    "SubprocessGitCli",
]
