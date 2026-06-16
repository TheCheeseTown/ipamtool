from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Input, Select, Label, Static, TabbedContent, TabPane
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.screen import ModalScreen
from textual.binding import Binding
from textual import on
from rich.text import Text

from .storage import Subnet, IPRange, load_subnets, save_subnets, load_ranges, save_ranges, STATUSES, snapshot, undo as _undo, backup_count
from .ip_utils import find_next_available, find_next_in_subnet, find_best_fit, check_conflicts, auto_gateway, auto_dhcp_range, validate_subrange, range_tree, subnet_tree, subnet_tree_for_range
from .ops import ancestor_cidrs as _ancestor_cidrs, descendant_cidrs as _descendant_cidrs, descendant_range_names as _descendant_range_names, relocate_children as _relocate_children, plan_resize, apply_resize
from .store import IPAMStore
from .export import export_xlsx, export_csv
from pathlib import Path


def _range_descendants(root_name: str, ranges: list) -> set[str]:
    by_parent: dict[str, list] = {}
    for r in ranges:
        by_parent.setdefault(r.parent, []).append(r)
    result = set()
    def walk(name: str):
        for r in by_parent.get(name, []):
            result.add(r.name)
            walk(r.name)
    walk(root_name)
    return result


def _validate_resize(new_network: str, range_name: str, ranges: list, subnets: list) -> str | None:
    import ipaddress
    new_net = ipaddress.IPv4Network(new_network, strict=True)
    for r in ranges:
        if r.parent == range_name:
            try:
                child = ipaddress.IPv4Network(r.network, strict=True)
                if not child.subnet_of(new_net):
                    return f"Child range '{r.name}' ({r.network}) doesn't fit in {new_network}"
            except ValueError:
                pass
    for s in subnets:
        if s.parent_range == range_name and not s.parent_subnet:
            if not s.network.subnet_of(new_net):
                return f"Subnet '{s.segment_name or s.cidr_notation()}' doesn't fit in {new_network}"
    return None


STATUS_STYLES = {
    "active":     "bold bright_green",
    "reserved":   "bold bright_yellow",
    "planned":    "bold bright_cyan",
    "deprecated": "bold bright_red",
}

CSS = """
Screen {
    background: #0a0a0a;
    color: ansi_bright_white;
}

Header {
    background: #111111;
    color: ansi_bright_cyan;
    text-style: bold;
}

Footer {
    background: #111111;
    color: #888888;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    padding: 0;
}

Tabs {
    background: #111111;
    border-bottom: tall #222222;
}

Tab {
    color: #666666;
    padding: 0 2;
}

Tab.-active {
    color: ansi_bright_cyan;
    text-style: bold;
    background: #0a0a0a;
}

DataTable {
    height: 1fr;
    background: #0a0a0a;
    color: ansi_bright_white;
}

DataTable > .datatable--header {
    background: #1a1a2e;
    color: ansi_bright_cyan;
    text-style: bold;
}

DataTable > .datatable--cursor {
    background: #1a3a1a;
    color: ansi_bright_white;
}

DataTable > .datatable--hover {
    background: #1a1a1a;
}

/* Modal dialogs */
ModalScreen {
    align: center middle;
    background: rgba(0,0,0,0.8);
}

#dialog {
    width: 58;
    max-height: 85%;
    background: #0f0f0f;
    border: tall #00e5ff;
    padding: 0 2 1 2;
}

#dialog-title {
    text-align: center;
    text-style: bold;
    color: ansi_bright_cyan;
    padding: 1 0;
    border-bottom: tall #222222;
    margin-bottom: 1;
}

.field-label {
    color: #888888;
    margin-top: 1;
}

#f-network-display {
    color: ansi_bright_cyan;
    padding: 0 1;
}

Input {
    background: #1a1a1a;
    border: tall #333333;
    color: ansi_bright_white;
    padding: 0 1;
}

Input:focus {
    border: tall #00e5ff;
}

Select {
    background: #1a1a1a;
    border: tall #333333;
    color: ansi_bright_white;
}

Select:focus {
    border: tall #00e5ff;
}

SelectOverlay {
    background: #1a1a1a;
    border: tall #00e5ff;
}

#dialog-hint {
    color: #555555;
    text-align: center;
    margin-top: 1;
    border-top: tall #222222;
    padding-top: 1;
}

#dialog-error {
    color: ansi_bright_red;
    text-align: center;
    margin-top: 1;
}

/* Confirm delete */
#confirm-dialog {
    width: 50;
    height: auto;
    background: #0f0f0f;
    border: tall #ff5555;
    padding: 1 2;
}

#confirm-label {
    text-align: center;
    color: ansi_bright_white;
    margin-bottom: 1;
}

#confirm-hint {
    text-align: center;
    color: #555555;
    margin-top: 1;
}

/* Export dialog */
#export-dialog {
    width: 50;
    height: auto;
    background: #0f0f0f;
    border: tall #00e5ff;
    padding: 1 2;
}

#export-title {
    text-align: center;
    text-style: bold;
    color: ansi_bright_cyan;
    margin-bottom: 1;
}

#export-hint {
    text-align: center;
    color: #555555;
    margin-top: 1;
}

#export-status {
    text-align: center;
    margin-top: 1;
}

.scroll {
    height: 1fr;
    max-height: 50;
}

#summary-bar {
    height: 1;
    background: #111111;
    color: #888888;
    padding: 0 1;
    border-top: tall #222222;
}
"""


def _range_slug(name: str) -> str:
    """First word of a range name, lowercased, keeping hyphens."""
    if not name or name in ("", "None"):
        return ""
    first = name.split()[0]
    return "".join(c for c in first.lower() if c.isalnum() or c == "-").strip("-")


def _range_full_slug(range_name: str, ranges: list) -> str:
    """Build prefix by walking the full hierarchy root→leaf, one slug per level."""
    path: list[str] = []
    current = range_name
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        r = next((x for x in ranges if x.name == current), None)
        if not r:
            break
        slug = _range_slug(r.name)
        if slug:
            path.append(slug)
        current = r.parent
    path.reverse()
    return "-".join(path)


class EditSubnetScreen(ModalScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, subnet: Subnet | None = None):
        super().__init__()
        self._subnet = subnet
        self._is_edit = subnet is not None
        self._pending_net: "ipaddress.IPv4Network | None" = None
        self._auto_prefix: str = ""  # last prefix we wrote into segment-name

    def compose(self) -> ComposeResult:
        s = self._subnet or Subnet(subnet="", cidr=24)
        is_host_edit = self._is_edit and s.is_host
        if is_host_edit:
            title = "── Edit Device ──"
        elif self._is_edit:
            title = "── Edit Subnet ──"
        else:
            title = "── Add Subnet ──"
        ranges = self.app.store.ranges
        range_options = [(r.name, r.name) for r in ranges] or [("(no ranges)", "")]
        all_subnets = self.app.store.subnets
        subnet_options = [("(none — top level)", "")] + [
            (f"{sn.segment_name or sn.cidr_notation()}  ({sn.cidr_notation()})", sn.cidr_notation())
            for sn in all_subnets
            if not self._is_edit or sn.cidr_notation() != s.cidr_notation()
        ]

        with Vertical(id="dialog"):
            yield Label(title, id="dialog-title")
            with ScrollableContainer(classes="scroll"):
                if not self._is_edit:
                    yield Label("Parent Range", classes="field-label")
                    yield Select(range_options, value=range_options[0][1], id="f-parent-range")
                    yield Label("Parent Subnet  (optional, for nesting)", classes="field-label")
                    yield Select(subnet_options, value="", id="f-parent-subnet")
                else:
                    yield Label("Network", classes="field-label")
                    yield Static(s.cidr_notation(), id="f-network-display")

                if not is_host_edit:
                    yield Label("Prefix Length  (e.g. 24 → /24)", classes="field-label")
                    yield Input(value=str(s.cidr), id="f-prefix")
                    yield Label("Segment Name", classes="field-label")
                    yield Input(value=s.segment_name, id="f-segment-name", placeholder="e.g. Office-WiFi")
                else:
                    yield Label("Device Name", classes="field-label")
                    yield Input(value=s.device_name, id="f-device-name", placeholder="TBD")

                yield Label("VLAN ID", classes="field-label")
                yield Input(value=s.vlan_id, id="f-vlan-id", placeholder="e.g. 10")

                if not is_host_edit:
                    yield Label("Purpose", classes="field-label")
                    yield Input(value=s.purpose, id="f-purpose")

                gw_label = "Gateway  (device's default router)" if is_host_edit else "Gateway  (blank = auto)"
                gw_ph    = "e.g. 10.0.0.1" if is_host_edit else "auto"
                yield Label(gw_label, classes="field-label")
                yield Input(value=s.gateway, id="f-gateway", placeholder=gw_ph)

                if not is_host_edit:
                    yield Label("DHCP Start  (blank = no DHCP)", classes="field-label")
                    yield Input(value=s.dhcp_start, id="f-dhcp-start", placeholder="auto if set")
                    yield Label("DHCP End", classes="field-label")
                    yield Input(value=s.dhcp_end, id="f-dhcp-end", placeholder="auto if start set")
                    yield Label("Static Range", classes="field-label")
                    yield Input(value=s.static_range, id="f-static-range")

                yield Label("Location", classes="field-label")
                yield Input(value=s.location, id="f-location")
                yield Label("Owner", classes="field-label")
                yield Input(value=s.owner, id="f-owner")
                yield Label("Status", classes="field-label")
                yield Select([(st, st) for st in STATUSES], value=s.status, id="f-status")
                yield Label("Notes", classes="field-label")
                yield Input(value=s.notes, id="f-notes")

            yield Label("ctrl+s  save    esc  cancel", id="dialog-hint")
            yield Static("", id="dialog-error")

    def _get(self, wid: str) -> str:
        try:
            return self.query_one(f"#{wid}", Input).value.strip()
        except Exception:
            return ""

    def _get_select(self, wid: str) -> str:
        try:
            return str(self.query_one(f"#{wid}", Select).value or "")
        except Exception:
            return ""

    def _show_error(self, msg: str):
        self.query_one("#dialog-error", Static).update(f"[bright_red]{msg}[/]")

    # ── Auto-prefix helpers ───────────────────────────────────────────────────

    @on(Select.Changed, "#f-parent-subnet")
    def _on_parent_subnet_changed(self, _: Select.Changed) -> None:
        self._apply_auto_prefix()

    @on(Select.Changed, "#f-parent-range")
    def _on_parent_range_changed(self, _: Select.Changed) -> None:
        self._apply_auto_prefix()

    def _apply_auto_prefix(self) -> None:
        if self._is_edit:
            return
        try:
            seg = self.query_one("#f-segment-name", Input)
        except Exception:
            return
        current = seg.value
        if current and current != self._auto_prefix:
            return  # user has typed something custom — don't overwrite

        range_name = self._get_select("f-parent-range")
        slug = _range_full_slug(range_name, self.app.store.ranges)
        self._auto_prefix = slug + "-" if slug else ""
        seg.value = self._auto_prefix

    def action_save(self):
        store   = self.app.store
        subnets = store.subnets
        ranges  = store.ranges

        if self._is_edit:
            s = next((x for x in subnets if x.cidr_notation() == self._subnet.cidr_notation()), None)
            if not s:
                self._show_error("Subnet not found — may have been deleted.")
                return

            gw_cascade = None

            if not s.is_host:
                try:
                    new_prefix = int(self._get("f-prefix"))
                except ValueError:
                    self._show_error("Prefix must be an integer (e.g. 24)")
                    return

                if new_prefix != s.cidr:
                    if self._pending_net is not None and self._pending_net.prefixlen == new_prefix:
                        gw_cascade = apply_resize(s, self._pending_net, subnets)
                        self._pending_net = None
                    else:
                        outcome = plan_resize(s, new_prefix, subnets, ranges)
                        if not outcome.ok:
                            self._show_error(outcome.message)
                            return
                        if not outcome.direct:
                            self._pending_net = outcome.new_net
                            extra = (
                                f"  [#888888]{outcome.n_relocate} child"
                                f"{'s' if outcome.n_relocate != 1 else ''} will relocate[/]"
                                if outcome.n_relocate else ""
                            )
                            self.query_one("#f-network-display", Static).update(
                                f"[ansi_bright_yellow]→ {outcome.new_net}[/]{extra}"
                            )
                            self._show_error(f"Best fit: {outcome.new_net}  ({outcome.message}) — save again to apply.")
                            return
                        gw_cascade = apply_resize(s, outcome.new_net, subnets)

            if s.is_host:
                s.device_name = self._get("f-device-name") or "TBD"
            else:
                s.segment_name = self._get("f-segment-name")
                s.purpose      = self._get("f-purpose")
            s.vlan_id = self._get("f-vlan-id")
            _form_gw  = self._get("f-gateway")
            if _form_gw:
                try:
                    import ipaddress as _ip
                    _ip.IPv4Address(_form_gw)
                except ValueError:
                    self._show_error(f"Invalid gateway IP: {_form_gw}")
                    return
            s.gateway = gw_cascade[1] if (gw_cascade and _form_gw == gw_cascade[0]) else _form_gw
            if not s.is_host:
                _dhcp_s = self._get("f-dhcp-start")
                _dhcp_e = self._get("f-dhcp-end")
                for _label, _val in (("DHCP start", _dhcp_s), ("DHCP end", _dhcp_e)):
                    if _val:
                        try:
                            import ipaddress as _ip
                            _ip.IPv4Address(_val)
                        except ValueError:
                            self._show_error(f"Invalid {_label} IP: {_val}")
                            return
                s.dhcp_start   = _dhcp_s
                s.dhcp_end     = _dhcp_e
                s.static_range = self._get("f-static-range")
            s.location = self._get("f-location")
            s.owner    = self._get("f-owner")
            s.status   = self._get_select("f-status")
            s.notes    = self._get("f-notes")
            store.commit(subnets=True, ranges=False)
            self.dismiss(True)
            return

        range_name = self._get_select("f-parent-range")
        parent_subnet_cidr = self._get_select("f-parent-subnet")
        if parent_subnet_cidr == "None":
            parent_subnet_cidr = ""

        try:
            prefix = int(self._get("f-prefix"))
        except ValueError:
            self._show_error("Prefix must be an integer (e.g. 24)")
            return
        if not (0 <= prefix <= 31):
            self._show_error("Prefix must be 0–31. Use 'Add Address' (i) for /32 hosts.")
            return

        parent_range = next((r for r in ranges if r.name == range_name), None)
        if not parent_range:
            self._show_error(f"Range '{range_name}' not found.")
            return

        try:
            if parent_subnet_cidr:
                network = find_next_in_subnet(parent_subnet_cidr, prefix, subnets)
            else:
                network = find_next_available(parent_range, prefix, subnets, ranges)
        except ValueError as e:
            self._show_error(str(e))
            return

        ancestors = _ancestor_cidrs(parent_subnet_cidr, subnets) if parent_subnet_cidr else set()
        conflict = check_conflicts(network, subnets, exclude_cidrs=ancestors)
        if conflict:
            self._show_error(f"Conflict: overlaps {conflict.cidr_notation()}")
            return

        import ipaddress as _ip
        _gw_raw = self._get("f-gateway")
        if _gw_raw:
            try:
                _ip.IPv4Address(_gw_raw)
            except ValueError:
                self._show_error(f"Invalid gateway IP: {_gw_raw}")
                return
        gw = _gw_raw or auto_gateway(network)
        dhcp_s = self._get("f-dhcp-start")
        dhcp_e = self._get("f-dhcp-end")
        for _label, _val in (("DHCP start", dhcp_s), ("DHCP end", dhcp_e)):
            if _val:
                try:
                    _ip.IPv4Address(_val)
                except ValueError:
                    self._show_error(f"Invalid {_label} IP: {_val}")
                    return
        if dhcp_s and not dhcp_e:
            _, dhcp_e = auto_dhcp_range(network)

        subnets.append(Subnet(
            subnet=str(network.network_address),
            cidr=prefix,
            segment_name=self._get("f-segment-name"),
            vlan_id=self._get("f-vlan-id"),
            purpose=self._get("f-purpose"),
            gateway=gw,
            dhcp_start=dhcp_s,
            dhcp_end=dhcp_e,
            static_range=self._get("f-static-range"),
            location=self._get("f-location"),
            owner=self._get("f-owner"),
            status=self._get_select("f-status"),
            notes=self._get("f-notes"),
            parent_range=range_name,
            parent_subnet=parent_subnet_cidr,
        ))
        store.commit(subnets=True, ranges=False)
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class EditRangeScreen(ModalScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, ipr: IPRange | None = None):
        super().__init__()
        self._range = ipr

    def compose(self) -> ComposeResult:
        import ipaddress
        r = self._range or IPRange(name="", network="")
        title = "── Edit Range ──" if self._range else "── Add Range ──"

        ranges = self.app.store.ranges
        parent_options = [("(none — top level)", "")] + [(rr.name, rr.name) for rr in ranges if not self._range or rr.name != self._range.name]
        current_parent = r.parent if r.parent else ""

        with Vertical(id="dialog"):
            yield Label(title, id="dialog-title")
            yield Label("Name", classes="field-label")
            yield Input(value=r.name, id="f-name", placeholder="e.g. Datacenter")
            yield Label("Network  (CIDR notation)", classes="field-label")
            yield Input(value=r.network, id="f-network", placeholder="e.g. 10.1.0.0/16")
            yield Label("Parent Range  (optional)", classes="field-label")
            yield Select(parent_options, value=current_parent or "", id="f-parent")
            yield Label("ctrl+s  save    esc  cancel", id="dialog-hint")
            yield Static("", id="dialog-error")

    def _show_error(self, msg: str):
        self.query_one("#dialog-error", Static).update(f"[bright_red]{msg}[/]")

    def action_save(self):
        import ipaddress
        name = self.query_one("#f-name", Input).value.strip()
        network_str = self.query_one("#f-network", Input).value.strip()
        parent_val = str(self.query_one("#f-parent", Select).value or "")
        if parent_val == "None":
            parent_val = ""

        if not name:
            self._show_error("Name required.")
            return
        if ":" in network_str:
            self._show_error("IPv6 is not supported — use an IPv4 CIDR (e.g. 10.0.0.0/8).")
            return
        try:
            net = ipaddress.IPv4Network(network_str, strict=False)
        except ValueError as e:
            self._show_error(str(e))
            return

        # Notify if address was normalised (host bits stripped)
        if str(net.network_address) != network_str.split("/")[0]:
            self._show_error(
                f"Address normalised to {net} — saving that. Save again to confirm."
            )
            self.query_one("#f-network", Input).value = str(net)
            return

        store   = self.app.store
        ranges  = store.ranges
        subnets = store.subnets

        if parent_val:
            parent_range = next((r for r in ranges if r.name == parent_val), None)
            if not parent_range:
                self._show_error(f"Parent '{parent_val}' not found.")
                return
            exclude = self._range.name if self._range else ""
            err = validate_subrange(str(net), parent_range, [r for r in ranges if r.name != exclude])
            if err:
                self._show_error(err)
                return

        if self._range:
            r = next((x for x in ranges if x.name == self._range.name), None)
            if not r:
                self._show_error("Range not found — may have been deleted.")
                return

            old_name    = r.name
            old_network = r.network

            if str(net) != old_network:
                err = _validate_resize(str(net), old_name, ranges, subnets)
                if err:
                    self._show_error(err)
                    return

            r.name    = name
            r.network = str(net)
            r.parent  = parent_val

            subnets_dirty = False
            if name != old_name:
                for s in subnets:
                    if s.parent_range == old_name:
                        s.parent_range = name
                        subnets_dirty = True
                for cr in ranges:
                    if cr.parent == old_name:
                        cr.parent = name

            store.commit(subnets=subnets_dirty, ranges=True)
        else:
            if any(r.name == name for r in ranges):
                self._show_error(f"Range '{name}' already exists.")
                return
            ranges.append(IPRange(name=name, network=str(net), parent=parent_val))
            store.commit(subnets=False, ranges=True)

        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class AddAddressScreen(ModalScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        ranges = self.app.store.ranges
        range_options = [(r.name, r.name) for r in ranges] or [("(no ranges)", "")]
        all_subnets = self.app.store.subnets
        subnet_options = [("(none — top level)", "")] + [
            (f"{s.segment_name or s.cidr_notation()}  ({s.cidr_notation()})", s.cidr_notation())
            for s in all_subnets if not s.is_host
        ]

        with Vertical(id="dialog"):
            yield Label("── Add Address ──", id="dialog-title")
            with ScrollableContainer(classes="scroll"):
                yield Label("IP Address", classes="field-label")
                yield Input(placeholder="e.g. 10.0.0.5", id="f-ip")
                yield Label("Device Name  (blank = TBD)", classes="field-label")
                yield Input(placeholder="TBD", id="f-device")
                yield Label("Parent Range", classes="field-label")
                yield Select(range_options, value=range_options[0][1], id="f-parent-range")
                yield Label("Parent Subnet  (optional)", classes="field-label")
                yield Select(subnet_options, value="", id="f-parent-subnet")
                yield Label("VLAN ID", classes="field-label")
                yield Input(id="f-vlan-id")
                yield Label("Gateway  (device's default router)", classes="field-label")
                yield Input(placeholder="e.g. 10.0.0.1", id="f-gateway")
                yield Label("Location", classes="field-label")
                yield Input(id="f-location")
                yield Label("Owner", classes="field-label")
                yield Input(id="f-owner")
                yield Label("Status", classes="field-label")
                yield Select([(st, st) for st in STATUSES], value="planned", id="f-status")
                yield Label("Notes", classes="field-label")
                yield Input(id="f-notes")
            yield Label("ctrl+s  save    esc  cancel", id="dialog-hint")
            yield Static("", id="dialog-error")

    def _get(self, wid: str) -> str:
        try:
            return self.query_one(f"#{wid}", Input).value.strip()
        except Exception:
            return ""

    def _get_select(self, wid: str) -> str:
        try:
            return str(self.query_one(f"#{wid}", Select).value or "")
        except Exception:
            return ""

    def _show_error(self, msg: str):
        self.query_one("#dialog-error", Static).update(f"[bright_red]{msg}[/]")

    @on(Select.Changed, "#f-parent-subnet")
    def _on_parent_subnet_changed(self, _: Select.Changed) -> None:
        try:
            gw_input = self.query_one("#f-gateway", Input)
        except Exception:
            return
        if gw_input.value:
            return
        parent_cidr = self._get_select("f-parent-subnet")
        if not parent_cidr or parent_cidr in ("", "None"):
            return
        parent = next(
            (s for s in self.app.store.subnets if s.cidr_notation() == parent_cidr),
            None,
        )
        if parent and parent.gateway:
            gw_input.value = parent.gateway

    def action_save(self):
        import ipaddress
        ip_str = self._get("f-ip")
        if not ip_str:
            self._show_error("IP address required.")
            return
        if ":" in ip_str:
            self._show_error("IPv6 is not supported — use an IPv4 address.")
            return
        try:
            ipaddress.IPv4Address(ip_str)
        except ValueError:
            self._show_error(f"Invalid IP address: {ip_str}")
            return

        range_name = self._get_select("f-parent-range")
        parent_subnet_cidr = self._get_select("f-parent-subnet")
        if parent_subnet_cidr in ("None", ""):
            parent_subnet_cidr = ""

        device = self._get("f-device") or "TBD"
        gw     = self._get("f-gateway")
        if gw:
            try:
                ipaddress.IPv4Address(gw)
            except ValueError:
                self._show_error(f"Invalid gateway IP: {gw}")
                return

        store   = self.app.store
        subnets = store.subnets
        cidr    = f"{ip_str}/32"

        if any(s.cidr_notation() == cidr for s in subnets):
            self._show_error(f"{ip_str} is already registered.")
            return

        ancestors = _ancestor_cidrs(parent_subnet_cidr, subnets) if parent_subnet_cidr else set()
        new_net = ipaddress.IPv4Network(cidr)
        conflict = check_conflicts(new_net, subnets, exclude_cidrs=ancestors)
        if conflict:
            self._show_error(f"Conflict: overlaps {conflict.cidr_notation()}")
            return

        subnets.append(Subnet(
            subnet=ip_str,
            cidr=32,
            segment_name="",
            device_name=device,
            gateway=gw,
            vlan_id=self._get("f-vlan-id"),
            location=self._get("f-location"),
            owner=self._get("f-owner"),
            status=self._get_select("f-status"),
            notes=self._get("f-notes"),
            parent_range=range_name,
            parent_subnet=parent_subnet_cidr,
        ))
        store.commit(subnets=True, ranges=False)
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class MoveRangeScreen(ModalScreen):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, ipr: IPRange):
        super().__init__()
        self._range = ipr

    def compose(self) -> ComposeResult:
        ranges = self.app.store.ranges
        excluded = {self._range.name} | _range_descendants(self._range.name, ranges)
        parent_options = [("(none — top level)", "")] + [
            (r.name, r.name) for r in ranges if r.name not in excluded
        ]
        current = self._range.parent or ""

        with Vertical(id="dialog"):
            yield Label("── Move Range ──", id="dialog-title")
            yield Static(
                f"[ansi_bright_cyan]{self._range.name}[/]  [#888888]{self._range.network}[/]",
                id="f-network-display",
            )
            yield Label("New Parent Range", classes="field-label")
            yield Select(parent_options, value=current or "", id="f-new-parent")
            yield Label("ctrl+s  save    esc  cancel", id="dialog-hint")
            yield Static("", id="dialog-error")

    def _show_error(self, msg: str):
        self.query_one("#dialog-error", Static).update(f"[bright_red]{msg}[/]")

    def action_save(self):
        new_parent = str(self.query_one("#f-new-parent", Select).value or "")
        if new_parent == "None":
            new_parent = ""

        store  = self.app.store
        ranges = store.ranges
        r = next((x for x in ranges if x.name == self._range.name), None)
        if not r:
            self._show_error("Range not found.")
            return

        if new_parent:
            parent_range = next((x for x in ranges if x.name == new_parent), None)
            if not parent_range:
                self._show_error(f"Parent '{new_parent}' not found.")
                return
            err = validate_subrange(r.network, parent_range, [x for x in ranges if x.name != r.name])
            if err:
                self._show_error(err)
                return

        r.parent = new_parent
        store.commit(subnets=False, ranges=True)
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class ConfirmDeleteScreen(ModalScreen):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, label: str):
        super().__init__()
        self._label = label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Delete [bright_red]{self._label}[/] ?", id="confirm-label")
            yield Label("y  confirm    n / esc  cancel", id="confirm-hint")

    def action_confirm(self):
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


class ExportScreen(ModalScreen):
    BINDINGS = [
        Binding("ctrl+s", "do_export", "Export"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="export-dialog"):
            yield Label("── Export ──", id="export-title")
            yield Label("Format", classes="field-label")
            yield Select([("Excel  .xlsx", "xlsx"), ("CSV  .csv", "csv")], value="xlsx", id="f-fmt")
            yield Label("Filename  (no extension)", classes="field-label")
            yield Input(value="ipam_export", id="f-name")
            yield Label("ctrl+s  export    esc  cancel", id="export-hint")
            yield Static("", id="export-status")

    def action_do_export(self):
        fmt = str(self.query_one("#f-fmt", Select).value)
        name = self.query_one("#f-name", Input).value.strip() or "ipam_export"
        path = Path(f"{name}.{fmt}")
        subnets = self.app.store.subnets
        status = self.query_one("#export-status", Static)
        status.update(f"[ansi_bright_yellow]Exporting {len(subnets)} records…[/]")
        try:
            if fmt == "xlsx":
                export_xlsx(subnets, path)
            else:
                export_csv(subnets, path)
            status.update(f"[bright_green]✓ {len(subnets)} subnets → {path}[/]")
        except Exception as exc:
            status.update(f"[bright_red]Export failed: {exc}[/]")

    def action_cancel(self):
        self.dismiss(None)


class IPAMApp(App):
    CSS = CSS
    TITLE = "IPAM"
    SUB_TITLE = "IP Address Management"

    BINDINGS = [
        Binding("a",      "add",         "Add Subnet"),
        Binding("i",      "add_address", "Add Address"),
        Binding("e",      "edit",        "Edit"),
        Binding("m",      "move_range",  "Move Range"),
        Binding("d",      "delete",      "Delete"),
        Binding("x",      "export",      "Export"),
        Binding("r",      "refresh",     "Refresh"),
        Binding("ctrl+z", "undo",        "Undo"),
        Binding("1",      "show_tab('ranges')",  "Ranges",  show=False),
        Binding("2",      "show_tab('subnets')", "Subnets", show=False),
        Binding("3",      "show_tab('info')",    "Info",    show=False),
        Binding("q",      "quit",        "Quit"),
    ]

    def __init__(self, edit_target: str | None = None):
        super().__init__()
        self._edit_target = edit_target
        self.store = IPAMStore()
        self._collapsed_ranges: set[str] = set()
        self._collapsed_host_groups: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="subnets"):
            with TabPane("Folders  [1]", id="ranges"):
                yield DataTable(id="ranges-table", cursor_type="row")
            with TabPane("Subnets  [2]", id="subnets"):
                yield DataTable(id="subnets-table", cursor_type="row")
            with TabPane("Info  [3]", id="info"):
                yield DataTable(id="info-table", cursor_type="row")
        yield Static("", id="summary-bar")
        yield Footer()

    def on_mount(self):
        rt = self.query_one("#ranges-table", DataTable)
        rt.add_columns("Name", "Network", "Sub-ranges", "Subnets", "Parent")

        st = self.query_one("#subnets-table", DataTable)
        st.add_columns(
            "Name / Device", "CIDR", "VLAN",
            "Total", "Usable",
            "Gateway", "DHCP Range", "Status",
            "Owner", "Location", "Range"
        )

        it = self.query_one("#info-table", DataTable)
        it.add_columns("Subnet / Host", "CIDR", "Total", "Carved", "Free", "Used %", "Hosts")

        self._load_ranges()
        self._load_subnets()
        self._load_info()

        if self.store.lock_contested:
            self.notify(
                "Another IPAM session is open on this directory — changes may conflict.",
                severity="warning",
                timeout=12,
            )

        if self._edit_target:
            self.call_after_refresh(self._open_edit_target)

    def on_unmount(self) -> None:
        self.store.close()

    def _update_summary(self):
        import ipaddress
        ranges = self.store.ranges
        subnets = self.store.subnets

        # Total IPs across all top-level ranges only (avoid double-counting nested ranges)
        top_ranges = [r for r in ranges if not r.parent]
        range_total = 0
        for r in top_ranges:
            try:
                range_total += ipaddress.IPv4Network(r.network, strict=False).num_addresses
            except ValueError:
                pass

        # Assigned = top-level subnets (no parent_subnet) — children are carved from these
        top_subnets = [s for s in subnets if not s.parent_subnet]
        assigned = sum(s.network.num_addresses for s in top_subnets)

        free = range_total - assigned
        pct = (assigned / range_total * 100) if range_total else 0

        self.query_one("#summary-bar", Static).update(
            f"  Ranges: [ansi_bright_cyan]{range_total:,}[/] IPs total  │  "
            f"Assigned: [ansi_bright_yellow]{assigned:,}[/]  │  "
            f"Free: [ansi_bright_green]{free:,}[/]  │  "
            f"Used: [ansi_bright_white]{pct:.1f}%[/]"
        )

    def _load_ranges(self):
        rt = self.query_one("#ranges-table", DataTable)
        rt.clear()
        ranges  = self.store.ranges
        subnets = self.store.subnets

        def _all_subnet_count(name: str) -> int:
            """Total subnets in this range and all its sub-ranges."""
            total = sum(1 for s in subnets if s.parent_range == name)
            for child in ranges:
                if child.parent == name:
                    total += _all_subnet_count(child.name)
            return total

        def _all_subfolder_count(name: str) -> int:
            direct = sum(1 for r in ranges if r.parent == name)
            total = direct
            for child in ranges:
                if child.parent == name:
                    total += _all_subfolder_count(child.name)
            return total

        for r in ranges:
            if r.parent:
                continue  # sub-ranges are shown as folder headers in Subnets tab
            rt.add_row(
                Text(r.name, style="ansi_bright_cyan"),
                r.network,
                str(_all_subfolder_count(r.name)),
                str(_all_subnet_count(r.name)),
                "—",
                key=r.name,
            )
        self._update_summary()

    # ── Range-section visibility helpers ─────────────────────────────────────

    def _rng_header_visible(self, name: str, ranges: list) -> bool:
        """Range header is visible only if every ancestor range is expanded."""
        r = next((x for x in ranges if x.name == name), None)
        if not r or not r.parent:
            return True
        if r.parent in self._collapsed_ranges:
            return False
        return self._rng_header_visible(r.parent, ranges)

    def _rng_content_visible(self, name: str, ranges: list) -> bool:
        """Subnets/sub-ranges under this range are visible only when not collapsed and header is visible."""
        if name in self._collapsed_ranges:
            return False
        return self._rng_header_visible(name, ranges)

    def _current_range_in_subnets(self) -> IPRange | None:
        """Return the range the cursor is on if it's a range header row in the subnets table."""
        table = self.query_one("#subnets-table", DataTable)
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
            if not row_key.startswith("range:"):
                return None
            range_name = row_key[6:]
            return next((r for r in self.store.ranges if r.name == range_name), None)
        except Exception:
            return None

    def _load_subnets(self):
        import ipaddress as _ip
        st = self.query_one("#subnets-table", DataTable)
        st.clear()
        all_subnets = self.store.subnets
        ranges = self.store.ranges

        # Child ranges indexed by parent name
        sub_ranges_by_parent: dict[str, list] = {}
        for r in ranges:
            sub_ranges_by_parent.setdefault(r.parent, []).append(r)

        _row_n = [0]

        def _render_subnet_row(s, depth, rd, hosts_by_parent):
            my_hosts = sorted(hosts_by_parent.get(s.cidr_notation(), []),
                              key=lambda x: (int(x.network.network_address), x.network.prefixlen))
            group_key  = f"hosts:{s.cidr_notation()}"
            multi_host = len(my_hosts) >= 2
            collapsed  = multi_host and group_key in self._collapsed_host_groups

            sindent    = "  " * (rd + 1 + depth)
            prefix_sym = "└ " if depth > 0 else ""
            label      = s.segment_name or s.cidr_notation()
            if multi_host:
                ind   = "▶" if collapsed else "▼"
                label = f"{ind} {label}"
            name_text = Text(sindent + prefix_sym + label,
                             style="ansi_bright_white" if depth > 0 else "ansi_bright_cyan")
            cidr_text = Text(s.cidr_notation(), style="#666666" if depth > 0 else "ansi_bright_white")
            dhcp = f"{s.dhcp_start}–{s.dhcp_end}" if s.dhcp_start else "—"
            net  = s.network
            st.add_row(
                name_text, cidr_text,
                s.vlan_id or "—", str(net.num_addresses), str(len(list(net.hosts()))),
                s.gateway or "—", dhcp,
                Text(s.status, style=STATUS_STYLES.get(s.status, "")),
                s.owner or "—", s.location or "—", s.parent_range,
                key=f"sub:{_row_n[0]}:{s.cidr_notation()}",
            )
            _row_n[0] += 1

            if not my_hosts:
                return
            hindent = "  " * (rd + 2 + depth)
            if len(my_hosts) == 1 or not collapsed:
                for h in my_hosts:
                    st.add_row(
                        Text(hindent + "● " + (h.device_name or h.subnet), style="ansi_bright_magenta"),
                        Text(h.subnet, style="#666666"),
                        h.vlan_id or "—", "1", "1", h.gateway or s.gateway or "—", "—",
                        Text(h.status, style=STATUS_STYLES.get(h.status, "")),
                        h.owner or "—", h.location or "—", h.parent_range,
                        key=f"host:{_row_n[0]}:{h.cidr_notation()}",
                    )
                    _row_n[0] += 1

        def _render_range(r, rd):
            if not self._rng_header_visible(r.name, ranges):
                return

            indicator = "▶" if r.name in self._collapsed_ranges else "▼"
            rindent = "  " * rd
            hdr_style = "bold ansi_bright_cyan" if rd == 0 else "bold ansi_bright_yellow"
            st.add_row(
                Text(f"{rindent}{indicator} {r.name}", style=hdr_style),
                Text(r.network, style="#555555"),
                Text(""), Text(""), Text(""),
                Text(""), Text(""),
                Text("folder", style="italic #555555"),
                Text(""), Text(""), Text(""),
                key=f"range:{r.name}",
            )

            if not self._rng_content_visible(r.name, ranges):
                return

            tree = subnet_tree_for_range(r.name, all_subnets)

            # Pre-group /32 hosts by their parent subnet CIDR
            hosts_by_parent: dict[str, list] = {}
            host_cidrs: set[str] = set()
            for s, _ in tree:
                if s.is_host:
                    hosts_by_parent.setdefault(s.parent_subnet, []).append(s)
                    host_cidrs.add(s.cidr_notation())

            # Group depth-0 subnets with their nested descendants
            chunks: list = []
            for s, depth in tree:
                if s.cidr_notation() in host_cidrs:
                    continue
                if depth == 0:
                    chunks.append((s, []))
                elif chunks:
                    chunks[-1][1].append((s, depth))

            # Merge depth-0 subnet chunks with direct child ranges, sort by IP
            child_ranges = sub_ranges_by_parent.get(r.name, [])

            def _sort_key(item):
                if isinstance(item, IPRange):
                    return int(_ip.IPv4Network(item.network, strict=False).network_address)
                s, _ = item
                return int(s.network.network_address)

            merged = list(chunks) + list(child_ranges)
            merged.sort(key=_sort_key)

            for item in merged:
                if isinstance(item, IPRange):
                    _render_range(item, rd + 1)
                else:
                    s, children = item
                    _render_subnet_row(s, 0, rd, hosts_by_parent)
                    for cs, cdepth in children:
                        _render_subnet_row(cs, cdepth, rd, hosts_by_parent)

        top_ranges = sorted(
            [r for r in ranges if not r.parent],
            key=lambda x: int(_ip.IPv4Network(x.network, strict=False).network_address),
        )
        for r in top_ranges:
            _render_range(r, 0)

        self._update_summary()

    def _load_info(self):
        import ipaddress
        it = self.query_one("#info-table", DataTable)
        it.clear()
        all_subnets = self.store.subnets
        ranges      = self.store.ranges

        def carved_for(cidr: str) -> int:
            return sum(s.network.num_addresses for s in all_subnets if s.parent_subnet == cidr)

        def host_count(cidr: str) -> int:
            total = 0
            for s in all_subnets:
                if s.parent_subnet == cidr:
                    total += 1 if s.is_host else host_count(s.cidr_notation())
            return total

        def range_carved(rname: str) -> int:
            direct = sum(s.network.num_addresses for s in all_subnets
                         if s.parent_range == rname and not s.parent_subnet)
            for child in ranges:
                if child.parent == rname:
                    try:
                        direct += ipaddress.IPv4Network(child.network, strict=True).num_addresses
                    except ValueError:
                        pass
            return direct

        def range_hosts(rname: str) -> int:
            total = sum(1 for s in all_subnets if s.parent_range == rname and s.is_host)
            for s in all_subnets:
                if s.parent_range == rname and not s.is_host:
                    total += host_count(s.cidr_notation())
            for child in ranges:
                if child.parent == rname:
                    total += range_hosts(child.name)
            return total

        def stat_color(pct: float) -> str:
            if pct >= 90: return "ansi_bright_red"
            if pct >= 70: return "ansi_bright_yellow"
            return "yellow3"

        def bar(pct: float, width: int = 12) -> str:
            filled = round(pct / 100 * width)
            return "█" * filled + "░" * (width - filled)

        _inf_n = 0
        for r, rd in range_tree(ranges):
            try:
                rnet = ipaddress.IPv4Network(r.network, strict=True)
            except ValueError:
                continue
            rindent    = "  " * rd
            hdr_style  = "bold ansi_bright_cyan" if rd == 0 else "bold ansi_bright_yellow"
            r_total    = rnet.num_addresses
            r_carved   = range_carved(r.name)
            r_free     = r_total - r_carved
            r_pct      = (r_carved / r_total * 100) if r_total else 0
            r_hosts    = range_hosts(r.name)
            r_color    = stat_color(r_pct)
            it.add_row(
                Text(f"{rindent}▸ {r.name}", style=hdr_style),
                Text(r.network, style="#555555"),
                Text(f"{r_total:,}",  style=r_color),
                Text(f"{r_carved:,}", style=r_color),
                Text(f"{r_free:,}", style="ansi_bright_green" if r_free > 0 else "ansi_bright_red"),
                Text(f"{bar(r_pct)}  {r_pct:.0f}%", style=r_color),
                Text(str(r_hosts) if r_hosts else "—", style="ansi_bright_magenta" if r_hosts else "#555555"),
                key=f"inf-range:{_inf_n}:{r.name}",
            )
            _inf_n += 1

            for s, depth in subnet_tree_for_range(r.name, all_subnets):
                if s.is_host:
                    continue  # hosts aggregated in the Hosts column
                sindent    = "  " * (rd + 1 + depth)
                prefix     = "└ " if depth > 0 else ""
                label      = s.segment_name or s.cidr_notation()
                total_ips  = s.network.num_addresses
                carved_ips = carved_for(s.cidr_notation())
                free_ips   = total_ips - carved_ips
                pct        = (carved_ips / total_ips * 100) if total_ips else 0
                hosts      = host_count(s.cidr_notation())
                color      = stat_color(pct)
                it.add_row(
                    Text(sindent + prefix + label,
                         style="ansi_bright_white" if depth > 0 else "ansi_bright_cyan"),
                    Text(s.cidr_notation(), style="#888888" if depth > 0 else "ansi_bright_white"),
                    Text(f"{total_ips:,}",  style=color),
                    Text(f"{carved_ips:,}", style=color),
                    Text(f"{free_ips:,}", style="ansi_bright_green" if free_ips > 0 else "ansi_bright_red"),
                    Text(f"{bar(pct)}  {pct:.0f}%", style=color),
                    Text(str(hosts) if hosts else "—", style="ansi_bright_magenta" if hosts else "#555555"),
                    key=f"inf-s:{_inf_n}:{s.cidr_notation()}",
                )
                _inf_n += 1

        it.add_row(Text(""), Text(""), Text(""), Text(""), Text(""), Text(""), Text(""), key="inf-sep")

        top_ranges  = [r for r in ranges if not r.parent]
        range_total = sum(ipaddress.IPv4Network(r.network, strict=True).num_addresses for r in top_ranges)
        top_subs    = [s for s in all_subnets if not s.parent_subnet]
        assigned    = sum(s.network.num_addresses for s in top_subs)
        free_total  = range_total - assigned
        pct         = (assigned / range_total * 100) if range_total else 0
        hosts_all   = sum(1 for s in all_subnets if s.is_host)
        color       = stat_color(pct)
        it.add_row(
            Text("TOTAL", style="bold ansi_bright_white"),
            Text(""),
            Text(f"{range_total:,}",  style=color),
            Text(f"{assigned:,}",     style=color),
            Text(f"{free_total:,}", style="ansi_bright_green" if free_total > 0 else "ansi_bright_red"),
            Text(f"{bar(pct)}  {pct:.0f}%", style=color),
            Text(str(hosts_all) if hosts_all else "—", style="ansi_bright_magenta" if hosts_all else "#555555"),
            key="inf-total",
        )

    def _active_tab(self) -> str:
        try:
            return self.query_one(TabbedContent).active
        except Exception:
            return "subnets"

    def action_show_tab(self, tab: str):
        self.query_one(TabbedContent).active = tab

    def action_refresh(self):
        self.store.reload()
        self._load_ranges()
        self._load_subnets()
        self._load_info()

    def action_undo(self):
        if _undo():
            self.store.reload()
            self._load_ranges()
            self._load_subnets()
            self._load_info()
            left = backup_count()
            self.notify(
                f"Undone  ({left} undo step{'s' if left != 1 else ''} left)",
                severity="information",
            )
        else:
            self.notify("Nothing to undo", severity="warning")


    # ── Add ──────────────────────────────────────────────────────────────────

    def action_add(self):
        if self._active_tab() == "ranges":
            self.push_screen(EditRangeScreen(), self._after_range_edit)
        else:
            self.push_screen(EditSubnetScreen(), self._after_subnet_edit)

    def action_add_address(self):
        self.push_screen(AddAddressScreen(), self._after_subnet_edit)

    def action_move_range(self):
        r = self._current_range()
        if r:
            self.push_screen(MoveRangeScreen(r), self._after_range_edit)

    # ── Edit ─────────────────────────────────────────────────────────────────

    def action_edit(self):
        if self._active_tab() == "ranges":
            r = self._current_range()
            if r:
                self.push_screen(EditRangeScreen(ipr=r), self._after_range_edit)
        else:
            r = self._current_range_in_subnets()
            if r:
                self.push_screen(EditRangeScreen(ipr=r), self._after_range_edit)
                return
            s = self._current_subnet()
            if s:
                self.push_screen(EditSubnetScreen(subnet=s), self._after_subnet_edit)

    # ── Delete ───────────────────────────────────────────────────────────────

    def action_delete(self):
        if self._active_tab() == "ranges":
            r = self._current_range()
            if r:
                ranges = self.store.ranges
                child_ranges = _descendant_range_names(r.name, ranges)
                affected = [s for s in self.store.subnets if s.parent_range in child_ranges | {r.name}]
                parts = [f"{r.name}  ({r.network})"]
                if child_ranges:
                    n = len(child_ranges)
                    parts.append(f"{n} child range{'s' if n != 1 else ''}")
                if affected:
                    n = len(affected)
                    parts.append(f"{n} subnet{'s' if n != 1 else ''}")
                self.push_screen(
                    ConfirmDeleteScreen(" + ".join(parts)),
                    lambda ok: self._delete_range(ok, r),
                )
        else:
            s = self._current_subnet()
            if s:
                desc = _descendant_cidrs(s.cidr_notation(), self.store.subnets)
                label = s.cidr_notation()
                if desc:
                    n = len(desc)
                    label += f" + {n} descendant{'s' if n != 1 else ''}"
                self.push_screen(
                    ConfirmDeleteScreen(label),
                    lambda ok: self._delete_subnet(ok, s),
                )

    # ── Export ───────────────────────────────────────────────────────────────

    def action_export(self):
        self.push_screen(ExportScreen())

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _after_range_edit(self, result):
        if result:
            self._load_ranges()
            self._load_subnets()
            self._load_info()

    def _after_subnet_edit(self, result):
        if result:
            self._load_ranges()
            self._load_subnets()
            self._load_info()

    def _delete_range(self, ok, r: IPRange):
        if ok:
            all_names = {r.name} | _descendant_range_names(r.name, self.store.ranges)
            self.store.ranges = [x for x in self.store.ranges if x.name not in all_names]
            self.store.subnets = [s for s in self.store.subnets if s.parent_range not in all_names]
            self.store.commit(subnets=True, ranges=True)
            self._load_ranges()
            self._load_subnets()
            self._load_info()

    def _delete_subnet(self, ok, s: Subnet):
        if ok:
            all_cidrs = _descendant_cidrs(s.cidr_notation(), self.store.subnets) | {s.cidr_notation()}
            self.store.subnets = [x for x in self.store.subnets if x.cidr_notation() not in all_cidrs]
            self.store.commit(subnets=True, ranges=False)
            self._load_ranges()
            self._load_subnets()
            self._load_info()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _current_range(self) -> IPRange | None:
        table = self.query_one("#ranges-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return next((r for r in self.store.ranges if r.name == row_key.value), None)
        except Exception:
            return None

    def _current_subnet(self) -> Subnet | None:
        table = self.query_one("#subnets-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
            if row_key.startswith("range:") or row_key.startswith("hosts:"):
                return None
            # Keys use "sub:{idx}:{cidr}" or "host:{idx}:{cidr}" format
            if row_key.startswith(("sub:", "host:")):
                cidr = row_key.split(":", 2)[2]
                return next((s for s in self.store.subnets if s.cidr_notation() == cidr), None)
            return None
        except Exception:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "subnets-table":
            return
        row_key = str(event.row_key.value or "")
        if row_key.startswith("range:"):
            name = row_key[6:]
            if name in self._collapsed_ranges:
                self._collapsed_ranges.discard(name)
            else:
                self._collapsed_ranges.add(name)
            self._load_subnets()
        elif row_key.startswith("sub:"):
            cidr = row_key.split(":", 2)[2]
            subnet_hosts = [s for s in self.store.subnets if s.is_host and s.parent_subnet == cidr]
            if len(subnet_hosts) >= 2:
                group_key = f"hosts:{cidr}"
                if group_key in self._collapsed_host_groups:
                    self._collapsed_host_groups.discard(group_key)
                else:
                    self._collapsed_host_groups.add(group_key)
                self._load_subnets()

    def _open_edit_target(self):
        target = next(
            (s for s in self.store.subnets if s.cidr_notation() == self._edit_target), None
        )
        if target:
            self.push_screen(EditSubnetScreen(subnet=target), self._after_subnet_edit)
