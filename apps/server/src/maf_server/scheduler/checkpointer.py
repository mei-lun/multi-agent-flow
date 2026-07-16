"""Create and manage the dedicated SQLite LangGraph checkpointer."""

from typing import Any


def create_checkpointer(database_path: str) -> Any:
    """Build the checkpoint adapter without exposing it to business modules."""
    raise NotImplementedError

