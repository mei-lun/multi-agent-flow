"""创建和管理独立 LangGraph SQLite Checkpointer 的接口。"""

from typing import Any
from pathlib import Path
import sqlite3

try:
    from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaverBase  # type: ignore
except ImportError:  # pragma: no cover - exercised only without optional LangGraph
    _SqliteSaverBase = object  # type: ignore[misc,assignment]


def create_checkpointer(database_path: str) -> Any:
    """创建 checkpointer 并应用 busy_timeout 等轻量配置。

    database_path 必须位于 Server 数据目录，且不能等于 maf.db。返回对象只交给 Scheduler；
    Runner 和业务 Router 不得访问。函数不迁移业务表。
    """
    if not isinstance(database_path, str) or not database_path.strip():
        raise ValueError("database_path is required")
    path = Path(database_path).expanduser().resolve()
    if path.name == "maf.db":
        raise ValueError("scheduler checkpoint database must be separate from maf.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
    except ImportError:
        return _FallbackCheckpointer(conn)
    # SqliteSaver owns the schema initialization and can be reused across
    # graph compilations.  Keep the connection alive through the returned saver.
    return _SqliteSaverCompat(conn)


class _SqliteSaverCompat(_SqliteSaverBase):  # type: ignore[misc]
    """LangGraph saver with the project's small ``get`` compatibility API.

    LangGraph's ``get`` returns the complete checkpoint envelope.  Existing
    scheduler callers historically used ``get`` to inspect the persisted
    ``RunState`` directly (the fallback checkpointer does that as well).  The
    graph runtime itself reads checkpoints through ``get_tuple`` and therefore
    remains fully LangGraph-native; only the public inspection helper unwraps
    ``channel_values``.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)

    def get(self, config: dict[str, Any]) -> Any:
        checkpoint = super().get(config)
        if isinstance(checkpoint, dict) and "channel_values" in checkpoint:
            return checkpoint["channel_values"]
        return checkpoint


class _FallbackCheckpointer:
    """Tiny checkpoint store used when optional LangGraph packages are absent."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS scheduler_checkpoints "
            "(thread_id TEXT PRIMARY KEY, state BLOB NOT NULL)"
        )
        self.connection.commit()

    def put(self, config: dict[str, Any], state: Any, *args: Any, **kwargs: Any) -> None:
        import json
        thread_id = str(config.get("configurable", {}).get("thread_id", config.get("thread_id", "default")))
        payload = json.dumps(state, default=lambda o: getattr(o, "to_dict", lambda: str(o))())
        self.connection.execute("INSERT OR REPLACE INTO scheduler_checkpoints(thread_id,state) VALUES (?,?)", (thread_id, payload))
        self.connection.commit()

    def get(self, config: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        import json
        thread_id = str(config.get("configurable", {}).get("thread_id", config.get("thread_id", "default")))
        row = self.connection.execute("SELECT state FROM scheduler_checkpoints WHERE thread_id=?", (thread_id,)).fetchone()
        return json.loads(row[0]) if row else None
