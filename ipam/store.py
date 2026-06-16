"""
Session-level in-memory state for the TUI.

IPAMStore is the single source of truth for a running TUI session.
Display methods read from it (no disk I/O on every refresh).
Write operations modify it in-place and call commit() to persist.
"""
from __future__ import annotations

from .storage import (
    IPRange,
    Subnet,
    load_ranges,
    load_subnets,
    save_ranges,
    save_subnets,
    snapshot,
    acquire_session_lock,
    release_session_lock,
)


class IPAMStore:
    """Holds subnets and ranges in memory for the lifetime of a TUI session."""

    def __init__(self) -> None:
        self.subnets: list[Subnet] = []
        self.ranges: list[IPRange] = []
        self._lock = None
        self._lock_contested = False
        try:
            self._lock = acquire_session_lock()
        except OSError:
            self._lock_contested = True
        self.reload()

    @property
    def lock_contested(self) -> bool:
        """True when another TUI session is already running on this directory."""
        return self._lock_contested

    def close(self) -> None:
        """Release the session lock. Call when the TUI exits."""
        release_session_lock(self._lock)
        self._lock = None

    def reload(self) -> None:
        """Read both files from disk, replacing in-memory state."""
        self.subnets = load_subnets()
        self.ranges = load_ranges()

    def commit(self, *, subnets: bool = True, ranges: bool = True) -> None:
        """Snapshot current on-disk state, then persist whatever changed."""
        snapshot()
        if subnets:
            save_subnets(self.subnets)
        if ranges:
            save_ranges(self.ranges)
