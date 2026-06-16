import ipaddress
from typing import Optional
from .storage import Subnet, IPRange


def find_next_available(
    parent: IPRange,
    prefix_len: int,
    subnets: list[Subnet],
    all_ranges: list[IPRange] | None = None,
) -> ipaddress.IPv4Network:
    parent_net = ipaddress.IPv4Network(parent.network, strict=True)
    occupied = [s.network for s in subnets if s.parent_range == parent.name and not s.parent_subnet]

    if all_ranges:
        for r in all_ranges:
            if r.parent == parent.name:
                try:
                    occupied.append(ipaddress.IPv4Network(r.network, strict=True))
                except ValueError:
                    pass

    for candidate in parent_net.subnets(new_prefix=prefix_len):
        if not any(_overlaps(candidate, e) for e in occupied):
            return candidate

    raise ValueError(f"No free /{prefix_len} block in {parent.network}")


def find_next_in_subnet(
    parent_cidr: str,
    prefix_len: int,
    all_subnets: list[Subnet],
) -> ipaddress.IPv4Network:
    boundary = ipaddress.IPv4Network(parent_cidr, strict=True)

    if prefix_len <= boundary.prefixlen:
        raise ValueError(f"Prefix /{prefix_len} must be larger than parent /{boundary.prefixlen}")

    # All subnets strictly inside this boundary are occupied space
    occupied = []
    for s in all_subnets:
        try:
            if s.network.subnet_of(boundary) and s.network != boundary:
                occupied.append(s.network)
        except TypeError:
            pass

    for candidate in boundary.subnets(new_prefix=prefix_len):
        if not any(_overlaps(candidate, e) for e in occupied):
            return candidate

    raise ValueError(f"No free /{prefix_len} block in {parent_cidr}")


def find_best_fit(
    prefix_len: int,
    scope_cidr: str,
    all_subnets: list[Subnet],
    exclude_cidrs: set[str] | None = None,
    all_ranges: list[IPRange] | None = None,
) -> ipaddress.IPv4Network | None:
    """Find first available block of prefix_len within scope_cidr, skipping excluded CIDRs."""
    try:
        scope = ipaddress.IPv4Network(scope_cidr, strict=False)
    except ValueError:
        return None

    if prefix_len <= scope.prefixlen:
        return None

    occupied = []
    for s in all_subnets:
        if exclude_cidrs and s.cidr_notation() in exclude_cidrs:
            continue
        try:
            if s.network.subnet_of(scope) and s.network != scope:
                occupied.append(s.network)
        except TypeError:
            pass

    if all_ranges:
        for r in all_ranges:
            if exclude_cidrs and r.network in exclude_cidrs:
                continue
            try:
                rnet = ipaddress.IPv4Network(r.network, strict=True)
                if rnet.subnet_of(scope) and rnet != scope:
                    occupied.append(rnet)
            except (ValueError, TypeError):
                pass

    for candidate in scope.subnets(new_prefix=prefix_len):
        if not any(_overlaps(candidate, o) for o in occupied):
            return candidate

    return None


def check_conflicts(
    new_net: ipaddress.IPv4Network,
    subnets: list[Subnet],
    exclude_cidrs: set[str] | None = None,
) -> Optional[Subnet]:
    for s in subnets:
        if exclude_cidrs and s.cidr_notation() in exclude_cidrs:
            continue
        if _overlaps(new_net, s.network):
            return s
    return None


def validate_subrange(
    child_network: str,
    parent: IPRange,
    all_ranges: list[IPRange],
) -> str | None:
    try:
        child_net = ipaddress.IPv4Network(child_network, strict=False)
    except ValueError as e:
        return str(e)

    parent_net = ipaddress.IPv4Network(parent.network, strict=True)

    if not child_net.subnet_of(parent_net):
        return f"{child_network} is not within {parent.network}"

    if child_net.prefixlen <= parent_net.prefixlen:
        return f"Sub-range prefix /{child_net.prefixlen} must be larger than parent /{parent_net.prefixlen}"

    for r in all_ranges:
        if r.parent == parent.name and r.network != child_network:
            try:
                sibling = ipaddress.IPv4Network(r.network, strict=True)
                if child_net.overlaps(sibling):
                    return f"Overlaps with sibling range '{r.name}' ({r.network})"
            except ValueError:
                pass

    return None


def check_range_conflicts(
    new_net: ipaddress.IPv4Network,
    ranges: list[IPRange],
    exclude_name: str = "",
) -> Optional[IPRange]:
    for r in ranges:
        if r.name == exclude_name:
            continue
        try:
            rnet = ipaddress.IPv4Network(r.network, strict=True)
            if new_net.overlaps(rnet):
                return r
        except ValueError:
            pass
    return None


def _overlaps(a: ipaddress.IPv4Network, b: ipaddress.IPv4Network) -> bool:
    return a.overlaps(b)


def auto_gateway(network: ipaddress.IPv4Network) -> str:
    if network.prefixlen >= 32:
        return ""
    hosts = list(network.hosts())
    if not hosts:
        return ""
    return str(hosts[0])


def auto_dhcp_range(network: ipaddress.IPv4Network) -> tuple[str, str]:
    hosts = list(network.hosts())
    if len(hosts) < 4:
        return "", ""
    start_idx = 1
    end_idx = start_idx + max(1, (len(hosts) - 1) // 2)
    return str(hosts[start_idx]), str(hosts[min(end_idx, len(hosts) - 1)])


def range_tree(ranges: list[IPRange]) -> list[tuple[IPRange, int]]:
    by_parent: dict[str, list[IPRange]] = {}
    for r in ranges:
        by_parent.setdefault(r.parent, []).append(r)

    result: list[tuple[IPRange, int]] = []

    def walk(parent_name: str, depth: int):
        children = by_parent.get(parent_name, [])
        for r in sorted(children, key=lambda x: ipaddress.IPv4Network(x.network, strict=False).network_address):
            result.append((r, depth))
            walk(r.name, depth + 1)

    walk("", 0)
    return result


def subnet_tree_for_range(range_name: str, subnets: list[Subnet]) -> list[tuple[Subnet, int]]:
    """Subnet tree filtered to one range; subnets whose parent_subnet lives in a different range become roots."""
    in_range = [s for s in subnets if s.parent_range == range_name]
    range_cidrs = {s.cidr_notation() for s in in_range}
    by_parent: dict[str, list[Subnet]] = {}
    for s in in_range:
        key = s.parent_subnet if s.parent_subnet in range_cidrs else ""
        by_parent.setdefault(key, []).append(s)
    result: list[tuple[Subnet, int]] = []
    def _ip_key(s: Subnet):
        return (int(s.network.network_address), s.network.prefixlen)

    def walk(parent_cidr: str, depth: int):
        for s in sorted(by_parent.get(parent_cidr, []), key=_ip_key):
            result.append((s, depth))
            walk(s.cidr_notation(), depth + 1)
    walk("", 0)
    return result


def subnet_tree(subnets: list[Subnet]) -> list[tuple[Subnet, int]]:
    known_cidrs = {s.cidr_notation() for s in subnets}
    by_parent: dict[str, list[Subnet]] = {}
    for s in subnets:
        # If the declared parent no longer exists, surface at top level
        key = s.parent_subnet if s.parent_subnet in known_cidrs else ""
        by_parent.setdefault(key, []).append(s)

    result: list[tuple[Subnet, int]] = []

    def _ip_key(s: Subnet):
        return (int(s.network.network_address), s.network.prefixlen)

    def walk(parent_cidr: str, depth: int):
        for s in sorted(by_parent.get(parent_cidr, []), key=_ip_key):
            result.append((s, depth))
            walk(s.cidr_notation(), depth + 1)

    walk("", 0)
    return result
