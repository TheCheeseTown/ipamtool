"""
Business logic for subnet and range operations.

All rules that decide *what happens* when the user adds, resizes, moves,
or deletes a subnet or range live here.  tui.py and cli.py call these
functions; they never reconstruct the logic themselves.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .storage import Subnet, IPRange
from .ip_utils import (
    auto_gateway,
    check_conflicts,
    find_best_fit,
    find_next_available,
    find_next_in_subnet,
)


# ── Tree helpers ──────────────────────────────────────────────────────────────

def ancestor_cidrs(start_cidr: str, subnets: list[Subnet]) -> set[str]:
    """CIDRs of start_cidr and every ancestor in the parent_subnet chain."""
    by_cidr = {s.cidr_notation(): s for s in subnets}
    result: set[str] = set()
    current = start_cidr
    while current and current not in result:
        result.add(current)
        s = by_cidr.get(current)
        current = s.parent_subnet if s else ""
    return result


def descendant_cidrs(root_cidr: str, subnets: list[Subnet]) -> set[str]:
    """CIDRs of every subnet nested (directly or indirectly) under root_cidr."""
    by_parent: dict[str, list[Subnet]] = {}
    for s in subnets:
        by_parent.setdefault(s.parent_subnet, []).append(s)
    result: set[str] = set()

    def _walk(cidr: str) -> None:
        for s in by_parent.get(cidr, []):
            child = s.cidr_notation()
            if child not in result:
                result.add(child)
                _walk(child)

    _walk(root_cidr)
    return result


def descendant_range_names(root_name: str, ranges: list[IPRange]) -> set[str]:
    """Names of every range nested (directly or indirectly) under root_name."""
    by_parent: dict[str, list[str]] = {}
    for r in ranges:
        by_parent.setdefault(r.parent, []).append(r.name)
    result: set[str] = set()

    def _walk(name: str) -> None:
        for child in by_parent.get(name, []):
            if child not in result:
                result.add(child)
                _walk(child)

    _walk(root_name)
    return result


def exclusion_set(cidr: str, parent_subnet: str, subnets: list[Subnet]) -> set[str]:
    """Standard conflict-check exclusion set: ancestors + descendants + self."""
    return ancestor_cidrs(parent_subnet, subnets) | descendant_cidrs(cidr, subnets) | {cidr}


# ── Relocation ────────────────────────────────────────────────────────────────

def relocate_children(
    old_parent_cidr: str,
    new_parent_net: ipaddress.IPv4Network,
    subnets: list[Subnet],
    _visited: set[str] | None = None,
) -> None:
    """Cascade a parent resize/relocation to all descendants in-place.

    Children already inside new_parent_net have only their parent_subnet
    reference updated.  Children that fall outside are shifted by the same
    offset as the parent and their gateway is recalculated if it was
    auto-derived.  _visited prevents infinite loops on corrupt data.
    """
    if _visited is None:
        _visited = set()
    if old_parent_cidr in _visited:
        return
    _visited.add(old_parent_cidr)

    old_start = int(ipaddress.IPv4Network(old_parent_cidr, strict=False).network_address)
    new_start = int(new_parent_net.network_address)
    new_str = str(new_parent_net)

    children = [
        (s, s.cidr_notation())
        for s in subnets
        if s.parent_subnet == old_parent_cidr
    ]

    for s, old_child_cidr in children:
        if old_child_cidr in _visited:
            continue
        child_net = ipaddress.IPv4Network(f"{s.subnet}/{s.cidr}", strict=False)
        if child_net.subnet_of(new_parent_net):
            s.parent_subnet = new_str
            relocate_children(old_child_cidr, child_net, subnets, _visited)
        else:
            offset = int(ipaddress.IPv4Address(s.subnet)) - old_start
            new_raw = new_start + offset
            if not (0 <= new_raw <= 0xFFFFFFFF):
                import sys
                print(
                    f"[ipam] relocate: child {old_child_cidr} would shift outside IPv4 space — skipped",
                    file=sys.stderr,
                )
                continue
            new_addr = ipaddress.IPv4Address(new_raw)
            old_auto_gw = auto_gateway(child_net)
            s.subnet = str(new_addr)
            s.parent_subnet = new_str
            new_child_net = ipaddress.IPv4Network(f"{s.subnet}/{s.cidr}", strict=False)
            if s.gateway == old_auto_gw:
                s.gateway = auto_gateway(new_child_net)
            relocate_children(old_child_cidr, new_child_net, subnets, _visited)


# ── Resize planning ───────────────────────────────────────────────────────────

@dataclass
class ResizeOutcome:
    """What would happen if a subnet were resized to a given prefix.

    Callers inspect this and decide what to display / whether to apply.
    No side effects.
    """
    ok: bool
    direct: bool                                    # True → apply immediately
    new_net: ipaddress.IPv4Network | None = None    # the network to apply
    n_relocate: int = 0                             # children that will move
    message: str = ""                               # human-readable reason


def plan_resize(
    s: Subnet,
    new_prefix: int,
    subnets: list[Subnet],
    ranges: list[IPRange],
) -> ResizeOutcome:
    """Decide what would happen if *s* were resized to *new_prefix*.

    Returns a ResizeOutcome.  No data is modified.
    """
    old_cidr = s.cidr_notation()

    try:
        new_net = ipaddress.IPv4Network(f"{s.subnet}/{new_prefix}", strict=False)
    except ValueError as exc:
        return ResizeOutcome(ok=False, direct=False, message=str(exc))

    excluded = exclusion_set(old_cidr, s.parent_subnet, subnets)
    conflict = check_conflicts(new_net, subnets, exclude_cidrs=excluded)
    addr_shifted = str(new_net.network_address) != s.subnet

    if not conflict and not addr_shifted:
        return ResizeOutcome(ok=True, direct=True, new_net=new_net)

    # Natural boundary is blocked — search for the best available slot
    scope = s.parent_subnet
    if not scope:
        pr = next((r for r in ranges if r.name == s.parent_range), None)
        scope = pr.network if pr else None

    best = (
        find_best_fit(new_prefix, scope, subnets, exclude_cidrs=excluded)
        if scope else None
    )

    if not best:
        reason = (
            f"overlaps {conflict.cidr_notation()}"
            if conflict else f"address shifts to {new_net}"
        )
        return ResizeOutcome(ok=False, direct=False, message=f"No free /{new_prefix} in scope ({reason})")

    n_relocate = sum(
        1 for c in subnets
        if c.parent_subnet == old_cidr
        and not ipaddress.IPv4Network(f"{c.subnet}/{c.cidr}", strict=False).subnet_of(best)
    )
    action = "no space at natural boundary" if conflict else "address shifted"
    return ResizeOutcome(ok=True, direct=False, new_net=best,
                         n_relocate=n_relocate, message=action)


def apply_resize(
    s: Subnet,
    new_net: ipaddress.IPv4Network,
    subnets: list[Subnet],
) -> tuple[str, str] | None:
    """Apply a resize to *s* in-place and cascade to all descendants.

    Returns *(old_auto_gw, new_auto_gw)* when the network address changed
    so the caller can decide whether to update the parent's own gateway
    after reading the form (the form value may differ from the auto-derived
    one if the user typed something custom).

    Returns None when the address did not change (prefix-only resize).
    """
    old_cidr = s.cidr_notation()
    old_net = ipaddress.IPv4Network(old_cidr, strict=False)
    old_auto_gw = auto_gateway(old_net)
    addr_changed = str(old_net.network_address) != str(new_net.network_address)

    s.subnet = str(new_net.network_address)
    s.cidr = new_net.prefixlen
    new_cidr = s.cidr_notation()

    if old_cidr != new_cidr:
        relocate_children(old_cidr, new_net, subnets)

    if addr_changed:
        return old_auto_gw, auto_gateway(new_net)
    return None
