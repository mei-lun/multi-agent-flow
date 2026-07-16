"""Requeue or fail jobs whose Runner lease has expired."""


def reap_expired_leases(now_iso: str) -> int:
    """Return the number of jobs recovered in one bounded scan."""
    raise NotImplementedError

