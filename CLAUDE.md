# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

After `pip install -e .`, the `ipam` CLI is available in the venv.

## Running

```bash
ipam tui                   # interactive TUI (default ~/.ipam/)
ipam tui myproject         # TUI with project directory ./myproject/
ipam range list
ipam subnet list
ipam undo
```

## No Tests

There is no test suite. The `test/` directory contains only sample CSV data files used for manual testing with `ipam tui test/datacenter`.

## Architecture

The data model has two distinct types that must not be conflated:
- **`IPRange`** — administrative folder/container with a CIDR fence. Stored in `ranges.csv`. Never a routed network.
- **`Subnet`** — actual network or `/32` host entry. Stored in `data.csv`. `Subnet.is_host` is true when `cidr == 32`.

### Module responsibilities

| Module | Role |
|---|---|
| `storage.py` | `Subnet`/`IPRange` dataclasses, CSV read/write, undo/snapshot, session lock. Data paths are **module-level globals** mutated by `set_data_dir()` — this is how `ipam tui myproject` redirects all I/O to a project directory. |
| `ip_utils.py` | Pure IP math: allocation (`find_next_available`, `find_next_in_subnet`, `find_best_fit`), conflict checking, tree traversal helpers (`range_tree`, `subnet_tree`, `subnet_tree_for_range`). |
| `ops.py` | Business logic for multi-step operations: `plan_resize` / `apply_resize` (two-phase resize with child relocation), `relocate_children` (cascades address shifts to all descendants), `ancestor_cidrs` / `descendant_cidrs` / `descendant_range_names`. |
| `store.py` | `IPAMStore` — TUI-only in-memory session state. Wraps storage load/save. Holds the session lock. `commit()` calls `snapshot()` then saves. |
| `tui.py` | Textual TUI. Reads/writes exclusively through `IPAMStore`. All user interactions call into `ops.py` and `ip_utils.py`. |
| `cli.py` | Click CLI. Reads/writes directly through `storage.py` functions (no `IPAMStore`). Entry point: `ipam.cli:main`. |
| `export.py` | Excel (openpyxl) and CSV export. Uses `subnet_tree()` to produce hierarchically indented output. |

### Critical invariant: undo ordering

`snapshot()` **must be called before** any `save_subnets()` / `save_ranges()` call. It captures the current on-disk state as an undo point. Reversing the order silently breaks undo. The TUI uses `store.commit()` which enforces this automatically; CLI callers call `snapshot()` manually before each `save_*()`.

### Conflict checking and ancestry

Parent–child nesting is legitimate overlap. `check_conflicts()` accepts an `exclude_cidrs` set. When validating a new or resized subnet, always pass `ancestor_cidrs(parent_subnet, subnets)` as the exclusion set so parent CIDRs are not flagged as conflicts. See `ops.exclusion_set()` for the canonical pattern.

### Two-phase resize

When a user changes a subnet's prefix in the TUI, `plan_resize()` is called first (read-only, returns a `ResizeOutcome`). If the natural boundary is blocked, it finds the best available slot and reports how many children will relocate. The user confirms a second time; only then does `apply_resize()` mutate data and call `relocate_children()`.

### Storage paths are mutable globals

`storage.DATA_FILE`, `storage.RANGES_FILE`, and `storage.BACKUP_FILE` are module-level `Path` values. `set_data_dir(path)` reassigns all three. Code that caches these paths at import time will break project-directory support. Always reference them via the module (e.g. `from .storage import load_subnets`) rather than caching the path values directly.
