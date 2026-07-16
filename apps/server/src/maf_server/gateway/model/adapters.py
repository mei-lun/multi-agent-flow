"""Provider adapter protocol implemented through the embedded LiteLLM SDK."""

from typing import Any, Protocol


class ModelAdapter(Protocol):
    def complete(self, *, model: str, messages: list[dict[str, Any]], **options: Any) -> Any: ...

