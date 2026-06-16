import csv
import json
import os
import ipaddress
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# All paths are module-level globals so set_data_dir() can reassign them.
DATA_FILE        = Path.home() / ".ipam" / "data.csv"
RANGES_FILE      = Path.home() / ".ipam" / "ranges.csv"
BACKUP_FILE      = Path.home() / ".ipam" / "backups.json"
MAX_BACKUPS      = 128


def _lock_path() -> Path:
    return DATA_FILE.parent / ".session.lock"


def acquire_session_lock():
    """Try to acquire an exclusive advisory lock for one TUI session.

    Returns an open file object on success, None if fcntl is unavailable
    (Windows), or raises OSError if another session already holds the lock.
    """
    try:
        import fcntl
    except ImportError:
        return None  # not POSIX — skip locking

    _ensure_data_dir()
    lp = _lock_path()
    f = open(lp, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f
    except OSError:
        f.close()
        raise


def release_session_lock(lock_obj) -> None:
    if lock_obj is None:
        return
    try:
        import fcntl
        fcntl.flock(lock_obj, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_obj.close()
    except Exception:
        pass

FIELDNAMES = [
    "vlan_id", "segment_name", "device_name", "purpose", "subnet", "cidr",
    "gateway", "dhcp_start", "dhcp_end", "static_range",
    "location", "owner", "status", "notes", "parent_range", "parent_subnet",
]

RANGE_FIELDNAMES = ["name", "network", "parent"]

STATUSES = ["active", "reserved", "deprecated", "planned"]


def set_data_dir(path: Path):
    """Switch all storage paths to a different directory (for project files)."""
    global DATA_FILE, RANGES_FILE, BACKUP_FILE
    p = Path(path)
    DATA_FILE   = p / "data.csv"
    RANGES_FILE = p / "ranges.csv"
    BACKUP_FILE = p / "backups.json"


@dataclass
class Subnet:
    subnet: str
    cidr: int
    segment_name: str = ""
    vlan_id: str = ""
    purpose: str = ""
    gateway: str = ""
    dhcp_start: str = ""
    dhcp_end: str = ""
    static_range: str = ""
    location: str = ""
    owner: str = ""
    device_name: str = ""
    status: str = "planned"
    notes: str = ""
    parent_range: str = ""
    parent_subnet: str = ""

    @property
    def is_host(self) -> bool:
        return self.cidr == 32

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.subnet}/{self.cidr}", strict=False)

    def cidr_notation(self) -> str:
        return str(self.network)


@dataclass
class IPRange:
    name: str
    network: str
    parent: str = ""


def _ensure_data_dir():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, text: str):
    """Write text to path atomically using a temp file + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _valid_subnet_row(row: dict) -> bool:
    """Return False for rows that would crash .network / cidr_notation()."""
    try:
        cidr = int(row.get("cidr", -1))
        if not (0 <= cidr <= 32):
            return False
        ipaddress.IPv4Network(f"{row['subnet']}/{cidr}", strict=False)
        return True
    except (ValueError, KeyError):
        return False


def load_subnets() -> list[Subnet]:
    _ensure_data_dir()
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, newline="") as f:
        reader = csv.DictReader(f)
        subnets = []
        for row in reader:
            if not _valid_subnet_row(row):
                import sys
                print(f"[ipam] skipping invalid row: {dict(row)}", file=sys.stderr)
                continue
            cidr_int = int(row.get("cidr", 24))
            net = ipaddress.IPv4Network(f"{row['subnet']}/{cidr_int}", strict=False)
            s = Subnet(
                subnet=str(net.network_address),
                cidr=cidr_int,
                segment_name=row.get("segment_name", ""),
                vlan_id=row.get("vlan_id", ""),
                purpose=row.get("purpose", ""),
                gateway=row.get("gateway", ""),
                dhcp_start=row.get("dhcp_start", ""),
                dhcp_end=row.get("dhcp_end", ""),
                static_range=row.get("static_range", ""),
                location=row.get("location", ""),
                owner=row.get("owner", ""),
                device_name=row.get("device_name", ""),
                status=row.get("status", "planned"),
                notes=row.get("notes", ""),
                parent_range=row.get("parent_range", ""),
                parent_subnet=row.get("parent_subnet", ""),
            )
            subnets.append(s)
    return subnets


def save_subnets(subnets: list[Subnet]):
    _ensure_data_dir()
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    for s in subnets:
        writer.writerow({
            "vlan_id": s.vlan_id,
            "segment_name": s.segment_name,
            "device_name": s.device_name,
            "purpose": s.purpose,
            "subnet": s.subnet,
            "cidr": s.cidr,
            "gateway": s.gateway,
            "dhcp_start": s.dhcp_start,
            "dhcp_end": s.dhcp_end,
            "static_range": s.static_range,
            "location": s.location,
            "owner": s.owner,
            "status": s.status,
            "notes": s.notes,
            "parent_range": s.parent_range,
            "parent_subnet": s.parent_subnet,
        })
    _atomic_write(DATA_FILE, buf.getvalue())


def load_ranges() -> list[IPRange]:
    _ensure_data_dir()
    if not RANGES_FILE.exists():
        return []
    ranges = []
    with open(RANGES_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                name = row["name"]
                network = row["network"]
                if not name or not network:
                    raise ValueError("empty name or network")
                ipaddress.IPv4Network(network, strict=False)
            except (KeyError, ValueError):
                import sys
                print(f"[ipam] skipping invalid range row: {dict(row)}", file=sys.stderr)
                continue
            ranges.append(IPRange(
                name=name,
                network=network,
                parent=row.get("parent", ""),
            ))
    return ranges


def save_ranges(ranges: list[IPRange]):
    _ensure_data_dir()
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RANGE_FIELDNAMES)
    writer.writeheader()
    for r in ranges:
        writer.writerow({"name": r.name, "network": r.network, "parent": r.parent})
    _atomic_write(RANGES_FILE, buf.getvalue())


def snapshot():
    """Save current state of both data files as one undo point."""
    _ensure_data_dir()
    data_text   = DATA_FILE.read_text()   if DATA_FILE.exists()   else ""
    ranges_text = RANGES_FILE.read_text() if RANGES_FILE.exists() else ""

    stack: list = _load_stack()
    stack.append({"data": data_text, "ranges": ranges_text})
    if len(stack) > MAX_BACKUPS:
        stack = stack[-MAX_BACKUPS:]
    _save_stack(stack)


def undo() -> bool:
    """Restore the most recent snapshot. Returns True on success."""
    stack = _load_stack()
    if not stack:
        return False

    entry = stack.pop()
    _ensure_data_dir()
    _atomic_write(DATA_FILE,   entry.get("data",   ""))
    _atomic_write(RANGES_FILE, entry.get("ranges", ""))
    _save_stack(stack)
    return True


def backup_count() -> int:
    """Return number of available undo steps."""
    return len(_load_stack())


def _load_stack() -> list:
    if not BACKUP_FILE.exists():
        return []
    import gzip
    raw = BACKUP_FILE.read_bytes()
    try:
        # Gzip magic bytes: 0x1f 0x8b
        if raw[:2] == b"\x1f\x8b":
            text = gzip.decompress(raw).decode()
        else:
            text = raw.decode()
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError, gzip.BadGzipFile, UnicodeDecodeError):
        return []


def _save_stack(stack: list):
    import gzip
    _ensure_data_dir()
    compressed = gzip.compress(json.dumps(stack).encode(), compresslevel=6)
    tmp = BACKUP_FILE.with_suffix(BACKUP_FILE.suffix + ".tmp")
    try:
        tmp.write_bytes(compressed)
        os.replace(tmp, BACKUP_FILE)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
