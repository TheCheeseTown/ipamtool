import ipaddress
from pathlib import Path
import click
from .storage import (
    Subnet, IPRange, load_subnets, save_subnets,
    load_ranges, save_ranges, STATUSES,
    snapshot, undo, backup_count, set_data_dir,
)
from .ip_utils import find_next_available, find_next_in_subnet, check_conflicts, auto_gateway, auto_dhcp_range, validate_subrange, range_tree, subnet_tree, subnet_tree_for_range
from .ops import (
    ancestor_cidrs as _ancestor_cidrs,
    descendant_cidrs as _descendant_cidrs,
    descendant_range_names as _descendant_range_names,
)
from .export import export_csv, export_xlsx


@click.group()
def cli():
    """IPAM - IP Address Management Tool"""


# ── Range commands ─────────────────────────────────────────────────────────────

@cli.group()
def range():
    """Manage parent IP ranges."""


@range.command("add")
@click.argument("network")
@click.option("--name", required=True, help="Human-readable name for this range")
@click.option("--parent", default="", help="Parent range name (to nest inside another range)")
def range_add(network, name, parent):
    """Add an IP range or sub-range (e.g. 10.0.0.0/8)."""
    if ":" in network:
        raise click.ClickException("IPv6 is not supported — please use an IPv4 CIDR (e.g. 10.0.0.0/8).")
    try:
        net = ipaddress.IPv4Network(network, strict=False)
    except ValueError as e:
        raise click.ClickException(str(e))

    if str(net) != network:
        click.echo(f"Note: network address normalised to {net}")

    ranges = load_ranges()
    if any(r.name == name for r in ranges):
        raise click.ClickException(f"Range named '{name}' already exists.")

    if parent:
        parent_range = next((r for r in ranges if r.name == parent), None)
        if not parent_range:
            raise click.ClickException(f"Parent range '{parent}' not found.")
        err = validate_subrange(str(net), parent_range, ranges)
        if err:
            raise click.ClickException(err)

    ranges.append(IPRange(name=name, network=str(net), parent=parent))
    snapshot()
    save_ranges(ranges)
    parent_label = f" (inside {parent})" if parent else ""
    click.echo(f"Added range: {name} → {net}{parent_label}")


@range.command("list")
def range_list():
    """List all registered ranges (tree view)."""
    ranges = load_ranges()
    if not ranges:
        click.echo("No ranges registered.")
        return
    click.echo(f"{'NAME':<28} {'NETWORK':<20} {'PARENT'}")
    click.echo("-" * 60)
    for r, depth in range_tree(ranges):
        indent = "  " * depth + ("└ " if depth else "")
        name_col = (indent + r.name)[:26]
        click.echo(f"{name_col:<28} {r.network:<20} {r.parent or '—'}")


@range.command("delete")
@click.argument("name")
def range_delete(name):
    """Delete a parent range by name (cascades to child ranges and subnets)."""
    ranges = load_ranges()
    if not any(r.name == name for r in ranges):
        raise click.ClickException(f"Range '{name}' not found.")

    subnets = load_subnets()
    all_names = {name} | _descendant_range_names(name, ranges)
    child_ranges = [r for r in ranges if r.name in all_names and r.name != name]
    affected = [s for s in subnets if s.parent_range in all_names]

    parts = [f"range '{name}'"]
    if child_ranges:
        n = len(child_ranges)
        parts.append(f"{n} child range{'s' if n != 1 else ''}")
    if affected:
        n = len(affected)
        parts.append(f"{n} subnet{'s' if n != 1 else ''}")
    click.confirm(f"Delete {', '.join(parts)}?", abort=True)

    snapshot()
    save_ranges([r for r in ranges if r.name not in all_names])
    save_subnets([s for s in subnets if s.parent_range not in all_names])
    click.echo(f"Deleted range '{name}' and {len(child_ranges)} child range(s), {len(affected)} subnet(s).")


# ── Subnet commands ────────────────────────────────────────────────────────────

@cli.group()
def subnet():
    """Manage subnets."""


@subnet.command("add")
@click.option("--range", "range_name", required=True, help="Parent range name")
@click.option("--prefix", required=True, type=int, help="Prefix length (e.g. 24 for /24)")
@click.option("--name", "segment_name", default="", help="Segment name")
@click.option("--vlan", default="", help="VLAN ID")
@click.option("--purpose", default="", help="Purpose description")
@click.option("--gateway", default=None, help="Override gateway IP")
@click.option("--dhcp", is_flag=True, default=False, help="Auto-calculate DHCP range")
@click.option("--dhcp-start", default=None, help="Override DHCP start IP")
@click.option("--dhcp-end", default=None, help="Override DHCP end IP")
@click.option("--static-range", default="", help="Static IP range description")
@click.option("--location", default="", help="Physical/logical location")
@click.option("--owner", default="", help="Owner or team")
@click.option("--status", default="planned", type=click.Choice(STATUSES), help="Status")
@click.option("--notes", default="", help="Free-text notes")
@click.option("--parent-subnet", default="", help="Parent subnet CIDR to nest inside (e.g. 10.0.0.0/22)")
def subnet_add(range_name, prefix, segment_name, vlan, purpose, gateway,
               dhcp, dhcp_start, dhcp_end, static_range, location, owner, status, notes, parent_subnet):
    """Auto-assign next available subnet block in a range or parent subnet."""
    if not (0 <= prefix <= 31):
        raise click.ClickException(f"Prefix must be between 0 and 31 (got {prefix}). Use 'address add' for /32 hosts.")

    ranges = load_ranges()
    parent_range = next((r for r in ranges if r.name == range_name), None)
    if not parent_range:
        raise click.ClickException(f"Range '{range_name}' not found.")

    subnets = load_subnets()

    if parent_subnet:
        # Validate parent subnet exists
        ps = next((s for s in subnets if s.cidr_notation() == parent_subnet or s.segment_name == parent_subnet), None)
        if not ps:
            raise click.ClickException(f"Parent subnet '{parent_subnet}' not found.")
        parent_cidr = ps.cidr_notation()
        try:
            network = find_next_in_subnet(parent_cidr, prefix, subnets)
        except ValueError as e:
            raise click.ClickException(str(e))
    else:
        try:
            network = find_next_available(parent_range, prefix, subnets, ranges)
        except ValueError as e:
            raise click.ClickException(str(e))
        parent_cidr = ""

    # Exclude all ancestors — a child legitimately overlaps its parents
    ancestors = _ancestor_cidrs(parent_cidr, subnets) if parent_cidr else set()
    conflict = check_conflicts(network, subnets, exclude_cidrs=ancestors)
    if conflict:
        raise click.ClickException(
            f"Conflict: {network} overlaps with {conflict.cidr_notation()} ({conflict.segment_name})"
        )

    if gateway:
        try:
            ipaddress.IPv4Address(gateway)
        except ValueError:
            raise click.ClickException(f"Invalid gateway IP: {gateway}")

    gw = gateway if gateway else auto_gateway(network)

    dhcp_s, dhcp_e = "", ""
    if dhcp:
        dhcp_s, dhcp_e = auto_dhcp_range(network)
    if dhcp_start:
        try:
            ipaddress.IPv4Address(dhcp_start)
        except ValueError:
            raise click.ClickException(f"Invalid DHCP start IP: {dhcp_start}")
        dhcp_s = dhcp_start
    if dhcp_end:
        try:
            ipaddress.IPv4Address(dhcp_end)
        except ValueError:
            raise click.ClickException(f"Invalid DHCP end IP: {dhcp_end}")
        dhcp_e = dhcp_end
    # If start was set (manually or via --dhcp) but end is still missing, auto-fill end
    if dhcp_s and not dhcp_e:
        _, dhcp_e = auto_dhcp_range(network)

    s = Subnet(
        subnet=str(network.network_address),
        cidr=prefix,
        segment_name=segment_name,
        vlan_id=vlan,
        purpose=purpose,
        gateway=gw,
        dhcp_start=dhcp_s,
        dhcp_end=dhcp_e,
        static_range=static_range,
        location=location,
        owner=owner,
        status=status,
        notes=notes,
        parent_range=range_name,
        parent_subnet=parent_cidr,
    )

    subnets.append(s)
    snapshot()
    save_subnets(subnets)
    click.echo(f"Added subnet: {network} (gateway: {gw})")


@subnet.command("list")
@click.option("--range", "range_name", default=None, help="Show only one folder (range name)")
def subnet_list(range_name):
    """List subnets grouped by folder (range), with folder/subnet labels."""
    ranges  = load_ranges()
    subnets = load_subnets()

    if not ranges and not subnets:
        click.echo("No data.")
        return

    # Filter to a single range when requested
    if range_name:
        target = next((r for r in ranges if r.name == range_name), None)
        if not target:
            raise click.ClickException(f"Range '{range_name}' not found.")
        _print_range_block(target, 0, ranges, subnets)
        return

    click.echo(f"  {'TYPE':<8} {'NAME':<36} {'CIDR':<20} {'STATUS':<10} {'GATEWAY'}")
    click.echo("  " + "-" * 85)
    for r, rd in range_tree(ranges):
        _print_range_block(r, rd, ranges, subnets)


def _print_range_block(r, rd: int, ranges: list, subnets: list):
    rindent = "  " * rd
    click.echo(f"{rindent}  {'folder':<8} {r.name:<36} {r.network:<20}")
    for s, depth in subnet_tree_for_range(r.name, subnets):
        sindent = "  " * (rd + 1 + depth)
        prefix  = "└ " if depth > 0 else ""
        if s.is_host:
            label = s.device_name or s.subnet
            tag   = "host"
        else:
            label = s.segment_name or s.cidr_notation()
            tag   = "subnet"
        click.echo(
            f"{sindent}  {tag:<8} {(prefix + label):<36} {s.cidr_notation():<20}"
            f" {s.status:<10} {s.gateway or '—'}"
        )


@subnet.command("delete")
@click.argument("identifier")
def subnet_delete(identifier):
    """Delete a subnet by address (10.0.1.0/24) or segment name (cascades to children)."""
    subnets = load_subnets()
    target = _find_subnet(identifier, subnets)
    if not target:
        raise click.ClickException(f"Subnet '{identifier}' not found.")

    desc = _descendant_cidrs(target.cidr_notation(), subnets)
    label = f"{target.cidr_notation()}" + (f" ({target.segment_name})" if target.segment_name else "")
    if desc:
        n = len(desc)
        click.confirm(f"Delete {label} and {n} descendant{'s' if n != 1 else ''}?", abort=True)
    else:
        click.confirm(f"Delete {label}?", abort=True)

    all_cidrs = desc | {target.cidr_notation()}
    snapshot()
    save_subnets([s for s in subnets if s.cidr_notation() not in all_cidrs])
    click.echo(f"Deleted {label}" + (f" and {len(desc)} descendant(s)." if desc else "."))


@subnet.command("edit")
@click.argument("identifier")
def subnet_edit(identifier):
    """Edit a subnet in the TUI (by address or segment name)."""
    from .tui import IPAMApp
    subnets = load_subnets()
    target = _find_subnet(identifier, subnets)
    if not target:
        raise click.ClickException(f"Subnet '{identifier}' not found.")
    app = IPAMApp(edit_target=target.cidr_notation())
    app.run()


# ── Address commands ───────────────────────────────────────────────────────────

@cli.group()
def address():
    """Manage specific host addresses (/32)."""


@address.command("add")
@click.argument("ip")
@click.option("--range", "range_name", required=True, help="Parent range name")
@click.option("--device", default="TBD", show_default=True, help="Device name")
@click.option("--parent-subnet", default="", help="Parent subnet CIDR or name")
@click.option("--vlan", default="", help="VLAN ID")
@click.option("--location", default="", help="Location")
@click.option("--owner", default="", help="Owner or team")
@click.option("--status", default="planned", type=click.Choice(STATUSES))
@click.option("--notes", default="", help="Notes")
def address_add(ip, range_name, device, parent_subnet, vlan, location, owner, status, notes):
    """Register a specific host IP address."""
    if ":" in ip:
        raise click.ClickException("IPv6 is not supported — please use an IPv4 address.")
    try:
        ipaddress.IPv4Address(ip)
    except ValueError as e:
        raise click.ClickException(str(e))

    ranges = load_ranges()
    if not any(r.name == range_name for r in ranges):
        raise click.ClickException(f"Range '{range_name}' not found.")

    subnets = load_subnets()
    cidr = f"{ip}/32"
    if any(s.cidr_notation() == cidr for s in subnets):
        raise click.ClickException(f"{ip} is already registered.")

    parent_cidr = ""
    if parent_subnet:
        ps = next((s for s in subnets if s.cidr_notation() == parent_subnet or s.segment_name == parent_subnet), None)
        if not ps:
            raise click.ClickException(f"Parent subnet '{parent_subnet}' not found.")
        parent_cidr = ps.cidr_notation()

    import ipaddress as _ip
    new_net = _ip.IPv4Network(cidr)
    ancestors = _ancestor_cidrs(parent_cidr, subnets) if parent_cidr else set()
    conflict = check_conflicts(new_net, subnets, exclude_cidrs=ancestors)
    if conflict:
        raise click.ClickException(f"Conflict: overlaps {conflict.cidr_notation()} ({conflict.segment_name})")

    subnets.append(Subnet(
        subnet=ip,
        cidr=32,
        device_name=device,
        vlan_id=vlan,
        location=location,
        owner=owner,
        status=status,
        notes=notes,
        parent_range=range_name,
        parent_subnet=parent_cidr,
    ))
    snapshot()
    save_subnets(subnets)
    click.echo(f"Added address: {ip}  device: {device}")


@address.command("list")
@click.option("--range", "range_name", default=None, help="Filter by range")
def address_list(range_name):
    """List all registered host addresses."""
    subnets = load_subnets()
    hosts = [s for s in subnets if s.is_host]
    if range_name:
        hosts = [s for s in hosts if s.parent_range == range_name]
    if not hosts:
        click.echo("No addresses registered.")
        return
    click.echo(f"{'IP':<18} {'DEVICE':<24} {'STATUS':<12} {'OWNER':<16} {'PARENT SUBNET'}")
    click.echo("-" * 85)
    for s in hosts:
        click.echo(f"{s.subnet:<18} {s.device_name:<24} {s.status:<12} {s.owner:<16} {s.parent_subnet or '—'}")


# ── Export commands ────────────────────────────────────────────────────────────

@cli.command("export")
@click.option("--format", "fmt", default="xlsx", type=click.Choice(["xlsx", "csv"]), show_default=True)
@click.option("--output", "-o", default=None, help="Output file path")
@click.option("--range", "range_name", default=None, help="Filter by parent range")
def export(fmt, output, range_name):
    """Export IPAM data to Excel or CSV."""
    subnets = load_subnets()
    if range_name:
        subnets = [s for s in subnets if s.parent_range == range_name]

    if output is None:
        output = f"ipam_export.{fmt}"
    path = Path(output)

    click.echo(f"Exporting {len(subnets)} records to {path}…")
    if fmt == "xlsx":
        export_xlsx(subnets, path)
    else:
        export_csv(subnets, path)

    click.echo(f"Done → {path}")


# ── Undo ───────────────────────────────────────────────────────────────────────

@cli.command("undo")
def undo_cmd():
    """Undo the last change (up to 128 steps)."""
    if undo():
        left = backup_count()
        click.echo(f"Undone. {left} undo step{'s' if left != 1 else ''} remaining.")
    else:
        click.echo("Nothing to undo.")


# ── TUI launcher ───────────────────────────────────────────────────────────────

@cli.command("tui")
@click.argument("project", required=False, default=None, metavar="[PROJECT]")
def tui(project):
    """Open the interactive TUI.

    Pass a project name or path to use a separate data directory instead of ~/.ipam/.

    \b
    Examples:
      ipam tui                  # uses ~/.ipam/
      ipam tui datacenter       # uses ./datacenter/
      ipam tui datacenter.csv   # also uses ./datacenter/
    """
    if project:
        p = Path(project)
        data_dir = (p.parent / p.stem) if p.suffix else p
        set_data_dir(data_dir.resolve())
    from .tui import IPAMApp
    IPAMApp().run()


# ── New project ────────────────────────────────────────────────────────────────

@cli.command("new")
@click.argument("name")
def new(name):
    """Create a new project directory with a datacenter template.

    \b
    Example:
      ipam new datacenter
      ipam tui datacenter
    """
    p = Path(name)
    if p.suffix:
        p = p.parent / p.stem
    if p.exists():
        raise click.ClickException(f"'{p}' already exists.")
    p.mkdir(parents=True)

    import csv as _csv, ipaddress as _ip

    def _gw(subnet, cidr):
        net = _ip.IPv4Network(f"{subnet}/{cidr}", strict=False)
        h = list(net.hosts())
        return str(h[0]) if h else ""

    def _dhcp(subnet, cidr):
        net = _ip.IPv4Network(f"{subnet}/{cidr}", strict=False)
        h = list(net.hosts())
        if len(h) < 4:
            return "", ""
        return str(h[1]), str(h[len(h) // 2])

    def sub(name, subnet, cidr, vlan="", purpose="", status="planned",
            owner="", location="DC-1", parent_range="", parent_subnet="",
            device="", dhcp=False, notes=""):
        gw = _gw(subnet, cidr) if cidr < 32 else ""
        ds, de = (_dhcp(subnet, cidr) if dhcp else ("", ""))
        return {
            "vlan_id": vlan, "segment_name": name, "device_name": device,
            "purpose": purpose, "subnet": subnet, "cidr": cidr,
            "gateway": gw, "dhcp_start": ds, "dhcp_end": de,
            "static_range": "", "location": location, "owner": owner,
            "status": status, "notes": notes,
            "parent_range": parent_range, "parent_subnet": parent_subnet,
        }

    def host(ip, device, status="active", owner="", location="DC-1",
             parent_range="", parent_subnet="", notes=""):
        return {
            "vlan_id": "", "segment_name": "", "device_name": device,
            "purpose": "", "subnet": ip, "cidr": 32,
            "gateway": "", "dhcp_start": "", "dhcp_end": "",
            "static_range": "", "location": location, "owner": owner,
            "status": status, "notes": notes,
            "parent_range": parent_range, "parent_subnet": parent_subnet,
        }

    ranges = [
        {"name": "Datacenter", "network": "192.168.0.0/16",   "parent": ""},
        {"name": "prod",       "network": "192.168.0.0/17",   "parent": "Datacenter"},
        {"name": "prod-core",  "network": "192.168.0.0/18",   "parent": "prod"},
        {"name": "prod-dmz",   "network": "192.168.64.0/18",  "parent": "prod"},
        {"name": "nonprod",    "network": "192.168.128.0/17", "parent": "Datacenter"},
        {"name": "dev",        "network": "192.168.128.0/18", "parent": "nonprod"},
        {"name": "test",       "network": "192.168.192.0/18", "parent": "nonprod"},
    ]

    CS  = "prod-core"
    DMZ = "prod-dmz"
    DEV = "dev"
    TST = "test"

    subnets = [
        # ── prod-core ─────────────────────────────────────────────────────────
        sub("dc-prod-core-web",     "192.168.0.0",   22, vlan="10",  purpose="Web servers",            status="active",   owner="Web Team",   parent_range=CS),
        sub("dc-prod-core-web-fe",  "192.168.0.0",   24, vlan="10",  purpose="NGINX front-ends",       status="active",   owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/22"),
        host("192.168.0.10",  "nginx-01",      status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/24"),
        host("192.168.0.11",  "nginx-02",      status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/24"),
        host("192.168.0.12",  "nginx-03",      status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/24"),
        host("192.168.0.20",  "nginx-lb-vip",  status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/24", notes="Virtual IP"),
        sub("dc-prod-core-web-be",  "192.168.1.0",   24, vlan="10",  purpose="Node.js services",       status="active",   owner="Web Team",   parent_range=CS, parent_subnet="192.168.0.0/22"),
        host("192.168.1.10",  "node-01",       status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.1.0/24"),
        host("192.168.1.11",  "node-02",       status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.1.0/24"),
        host("192.168.1.12",  "node-03",       status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.1.0/24"),
        host("192.168.1.13",  "node-04",       status="active", owner="Web Team",   parent_range=CS, parent_subnet="192.168.1.0/24"),

        sub("dc-prod-core-app",     "192.168.4.0",   22, vlan="20",  purpose="Application layer",      status="active",   owner="App Team",   parent_range=CS),
        sub("dc-prod-core-app-pri", "192.168.4.0",   24, vlan="20",  purpose="Primary app nodes",      status="active",   owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/22"),
        host("192.168.4.10",  "app-01",        status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/24"),
        host("192.168.4.11",  "app-02",        status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/24"),
        host("192.168.4.20",  "redis-01",      status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/24", notes="Cache"),
        host("192.168.4.21",  "redis-02",      status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/24", notes="Cache"),
        sub("dc-prod-core-app-sec", "192.168.5.0",   24, vlan="20",  purpose="Secondary app nodes",    status="active",   owner="App Team",   parent_range=CS, parent_subnet="192.168.4.0/22"),
        host("192.168.5.10",  "app-03",        status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.5.0/24"),
        host("192.168.5.11",  "app-04",        status="active", owner="App Team",   parent_range=CS, parent_subnet="192.168.5.0/24"),

        sub("dc-prod-core-db",      "192.168.8.0",   22, vlan="30",  purpose="Database layer",         status="active",   owner="DB Team",    parent_range=CS),
        sub("dc-prod-core-db-pri",  "192.168.8.0",   24, vlan="30",  purpose="Primary DB cluster",     status="active",   owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/22"),
        host("192.168.8.10",  "pg-primary-01", status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/24", notes="PostgreSQL primary"),
        host("192.168.8.11",  "pg-replica-01", status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/24", notes="PostgreSQL replica"),
        host("192.168.8.12",  "pg-replica-02", status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/24", notes="PostgreSQL replica"),
        host("192.168.8.20",  "mongo-01",      status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/24"),
        host("192.168.8.21",  "mongo-02",      status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/24"),
        sub("dc-prod-core-db-bak",  "192.168.9.0",   24, vlan="30",  purpose="Backup & snapshots",     status="reserved", owner="DB Team",    parent_range=CS, parent_subnet="192.168.8.0/22"),
        host("192.168.9.10",  "backup-srv-01", status="active", owner="DB Team",    parent_range=CS, parent_subnet="192.168.9.0/24"),

        sub("dc-prod-core-stor",    "192.168.12.0",  23, vlan="40",  purpose="NFS / object storage",   status="reserved", owner="Infra",      parent_range=CS),
        host("192.168.12.10", "nas-01",        status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.12.0/23"),
        host("192.168.12.11", "nas-02",        status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.12.0/23"),
        host("192.168.12.20", "s3-gateway",    status="planned",owner="Infra",      parent_range=CS, parent_subnet="192.168.12.0/23"),

        sub("dc-prod-core-mgmt",    "192.168.14.0",  24, vlan="50",  purpose="Out-of-band management", status="active",   owner="Infra",      parent_range=CS),
        host("192.168.14.1",  "jumphost-01",   status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.14.0/24"),
        host("192.168.14.2",  "monitoring-01", status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.14.0/24", notes="Prometheus + Grafana"),
        host("192.168.14.3",  "ansible-ctrl",  status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.14.0/24"),
        host("192.168.14.10", "sw-core-01",    status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.14.0/24", notes="Core switch"),
        host("192.168.14.11", "sw-core-02",    status="active", owner="Infra",      parent_range=CS, parent_subnet="192.168.14.0/24", notes="Core switch"),
        host("192.168.14.20", "pdu-rack-01",   status="active", owner="Facilities", parent_range=CS, parent_subnet="192.168.14.0/24"),
        host("192.168.14.21", "pdu-rack-02",   status="active", owner="Facilities", parent_range=CS, parent_subnet="192.168.14.0/24"),

        # ── prod-dmz ──────────────────────────────────────────────────────────
        sub("dc-prod-dmz-web",   "192.168.64.0",  24, vlan="100", purpose="Internet-facing LBs",    status="active", owner="NetOps", parent_range=DMZ),
        host("192.168.64.1",  "fw-outside",    status="active", owner="NetOps",     parent_range=DMZ, parent_subnet="192.168.64.0/24", notes="Firewall outside interface"),
        host("192.168.64.10", "lb-ext-01",     status="active", owner="NetOps",     parent_range=DMZ, parent_subnet="192.168.64.0/24"),
        host("192.168.64.11", "lb-ext-02",     status="active", owner="NetOps",     parent_range=DMZ, parent_subnet="192.168.64.0/24"),

        sub("dc-prod-dmz-proxy", "192.168.65.0",  24, vlan="110", purpose="TLS termination / WAF",  status="active", owner="NetOps", parent_range=DMZ),
        host("192.168.65.10", "proxy-01",      status="active", owner="NetOps",     parent_range=DMZ, parent_subnet="192.168.65.0/24"),
        host("192.168.65.11", "proxy-02",      status="active", owner="NetOps",     parent_range=DMZ, parent_subnet="192.168.65.0/24"),

        sub("dc-prod-dmz-fw",    "192.168.66.0",  30, vlan="120", purpose="Firewall transit link",  status="active", owner="NetOps", parent_range=DMZ),

        # ── dev ───────────────────────────────────────────────────────────────
        sub("dc-dev-srv", "192.168.128.0", 22, vlan="200", purpose="Developer VMs",      status="active",  owner="Dev Team", parent_range=DEV),
        sub("dc-dev-web", "192.168.128.0", 24, vlan="200", purpose="Dev web tier",        status="active",  owner="Dev Team", parent_range=DEV, parent_subnet="192.168.128.0/22", dhcp=True),
        host("192.168.128.10","dev-web-01", status="planned", owner="Dev Team",  parent_range=DEV, parent_subnet="192.168.128.0/24"),
        host("192.168.128.11","dev-web-02", status="planned", owner="Dev Team",  parent_range=DEV, parent_subnet="192.168.128.0/24"),
        sub("dc-dev-app", "192.168.129.0", 24, vlan="200", purpose="Dev app tier",        status="active",  owner="Dev Team", parent_range=DEV, parent_subnet="192.168.128.0/22", dhcp=True),
        host("192.168.129.10","dev-app-01", status="planned", owner="Dev Team",  parent_range=DEV, parent_subnet="192.168.129.0/24"),
        sub("dc-dev-db",  "192.168.132.0", 24, vlan="210", purpose="Dev databases",       status="reserved",owner="Dev Team", parent_range=DEV),
        host("192.168.132.10","dev-pg-01",  status="planned", owner="Dev Team",  parent_range=DEV, parent_subnet="192.168.132.0/24"),

        # ── test ──────────────────────────────────────────────────────────────
        sub("dc-test-srv", "192.168.192.0", 22, vlan="300", purpose="Test / QA VMs",      status="planned", owner="QA Team",  parent_range=TST),
        sub("dc-test-web", "192.168.192.0", 24, vlan="300", purpose="Test web tier",       status="planned", owner="QA Team",  parent_range=TST, parent_subnet="192.168.192.0/22"),
        sub("dc-test-app", "192.168.193.0", 24, vlan="300", purpose="Test app tier",       status="planned", owner="QA Team",  parent_range=TST, parent_subnet="192.168.192.0/22"),
        sub("dc-test-db",  "192.168.196.0", 24, vlan="310", purpose="Test databases",      status="planned", owner="QA Team",  parent_range=TST),
    ]

    from .storage import FIELDNAMES as _FN, RANGE_FIELDNAMES as _RFN

    with open(p / "ranges.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=_RFN)
        w.writeheader()
        w.writerows(ranges)

    with open(p / "data.csv", "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=_FN)
        w.writeheader()
        w.writerows(subnets)

    # Validate the template for overlaps so future edits are caught early
    from .storage import load_subnets as _ls, load_ranges as _lr, set_data_dir as _sdd
    _sdd(p.resolve())
    _template_subnets = _ls()
    _seen: list = []
    _warnings: list[str] = []
    for s in _template_subnets:
        ancestors = {x.cidr_notation() for x in _seen if s.network.subnet_of(x.network) or x.network.subnet_of(s.network)}
        conflict = check_conflicts(s.network, _seen, exclude_cidrs=ancestors)
        if conflict:
            _warnings.append(f"  overlap: {s.cidr_notation()} ↔ {conflict.cidr_notation()}")
        _seen.append(s)
    if _warnings:
        click.echo("WARNING: template contains overlapping subnets:")
        for w in _warnings:
            click.echo(w)

    click.echo(f"Created project '{p}/' with datacenter template.")
    click.echo(f"Open with:  ipam tui {name}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _find_subnet(identifier: str, subnets: list[Subnet]):
    # Try exact CIDR match first
    for s in subnets:
        if s.cidr_notation() == identifier or s.subnet == identifier:
            return s
    # Try segment name (case-insensitive)
    identifier_lower = identifier.lower()
    for s in subnets:
        if s.segment_name.lower() == identifier_lower:
            return s
    return None


def main():
    cli()
