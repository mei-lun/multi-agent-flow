"""Resolve connection policy, decrypt credentials, call a model, and audit use."""


class ModelGateway:
    def invoke(self, invocation_id: str) -> str:
        """Execute a persisted invocation and return its result artifact ID."""
        raise NotImplementedError

