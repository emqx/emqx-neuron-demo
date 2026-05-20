# /// script
# requires-python = ">=3.11"
# dependencies = ["openpyxl>=3.1"]
# ///
"""Convert a BACnet EDE (Engineering Data Exchange) CSV into a Neuron
tag-import sheet.

EDE format (per BACnet Interest Group spec): semicolon-delimited, with a
header row starting with `#`. Columns we use:
  1  keyname                 # ignored — vendor-specific path
  2  device obj.-instance    # ignored — we don't filter by device
  3  object-name             # often dirty (spaces, hyphens) — discarded
  4  object-type             # numeric BACnet type code (0..23)
  5  object-instance         # the N in AIN, AVN, BVN, MSVN, ...
  6  description             # carried into Neuron's description column
  10 commandable             # Y -> Read Write, N -> Read

Neuron import format (matches what `Export` produces from a configured node):
  group, interval, name, address, attribute, type,
  description, decimal, precision, bias

Object-type → Neuron type / address-prefix mapping (only the types the
BACnet/IP driver supports). Everything else (file, loop, schedule,
trend-log, notification-class, device) is dropped with a count.

Names need to be unique within a group and contain no special chars.
We generate `<prefix><instance>` (e.g. BV2097192) rather than reusing the
EDE object-name, which is frequently non-ASCII or contains spaces/hyphens.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook


# BACnet object-type numeric code → (Neuron address prefix, Neuron data type).
# Source: ASHRAE 135 + EMQX Neuron BACnet/IP driver supported areas.
TYPE_MAP: dict[int, tuple[str, str]] = {
    0:  ("AI",  "FLOAT"),
    1:  ("AO",  "FLOAT"),
    2:  ("AV",  "FLOAT"),
    3:  ("BI",  "BIT"),
    4:  ("BO",  "BIT"),
    5:  ("BV",  "BIT"),
    13: ("MSI", "UINT8"),
    14: ("MSO", "UINT8"),
    19: ("MSV", "UINT8"),
    23: ("ACC", "UINT8"),
}

# Object-type codes we know about but skip (not exposed as tags).
UNSUPPORTED = {
    8:  "device",
    9:  "file",
    12: "loop",
    15: "notification-class",
    17: "schedule",
    20: "trend-log",
}

NEURON_COLUMNS = [
    "group", "interval", "name", "address", "attribute", "type",
    "description", "decimal", "precision", "bias",
]


def parse_ede(path: Path) -> list[dict]:
    """Read an EDE CSV. The file may have a BOM and the first non-blank
    line is a `#`-prefixed header naming the columns."""
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        # Skip blank / header lines (the EDE header starts with `#`).
        reader = csv.reader(f, delimiter=";")
        for raw in reader:
            if not raw or not raw[0]:
                continue
            if raw[0].startswith("#"):
                continue
            # Some EDE producers include extra fields; index by position.
            if len(raw) < 10:
                continue
            try:
                obj_type = int(raw[3])
                obj_inst = int(raw[4])
            except (ValueError, IndexError):
                continue
            rows.append({
                "device_instance": raw[1],
                "object_type":     obj_type,
                "object_instance": obj_inst,
                "description":     raw[5] if len(raw) > 5 else "",
                "commandable":     (raw[9] or "").strip().upper() == "Y",
            })
    return rows


def to_neuron_rows(
    ede_rows: list[dict],
    group: str,
    interval_ms: int,
    include_types: set[int] | None,
    force_read_only: bool,
) -> tuple[list[dict], Counter]:
    """Map EDE rows to Neuron tag rows. Returns (rows, skip_reasons).

    De-duplicates on the generated name — EDE files can contain repeated
    object references when the same point appears under multiple groupings.
    """
    out: list[dict] = []
    skipped: Counter = Counter()
    seen: set[str] = set()

    for r in ede_rows:
        t = r["object_type"]
        if include_types is not None and t not in include_types:
            skipped[f"filtered (type {t})"] += 1
            continue
        mapping = TYPE_MAP.get(t)
        if mapping is None:
            label = UNSUPPORTED.get(t, f"type-{t}")
            skipped[f"unsupported ({label})"] += 1
            continue
        prefix, neuron_type = mapping
        name = f"{prefix}{r['object_instance']}"
        if name in seen:
            skipped["duplicate name"] += 1
            continue
        seen.add(name)

        if force_read_only:
            attr = "Read"
        else:
            # Commandable EDE objects map to read/write; non-commandable to
            # read-only. AI/BI/MSI are inherently read-only at the protocol
            # level — we still write `Read` for clarity.
            attr = "Read Write" if r["commandable"] and prefix in (
                "AO", "AV", "BO", "BV", "MSO", "MSV", "ACC"
            ) else "Read"

        out.append({
            "group":       group,
            "interval":    interval_ms,
            "name":        name,
            "address":     name,            # in BACnet plugin, address == AREA+instance
            "attribute":   attr,
            "type":        neuron_type,
            "description": r["description"],
            "decimal":     0,
            "precision":   0,
            "bias":        0,
        })

    return out, skipped


def write_xlsx(rows: list[dict], out_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"  # matches Neuron's downloadable upload-tag-template.xlsx
    ws.append(NEURON_COLUMNS)
    for r in rows:
        ws.append([r[c] for c in NEURON_COLUMNS])
    wb.save(out_path)


def write_csv(rows: list[dict], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NEURON_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def parse_types(arg: str | None) -> set[int] | None:
    """Accept either numeric codes or shorthand abbrevs: `AI,AV,BV,MSV` or
    `0,2,5,19`. Returns None when nothing is specified (all supported)."""
    if not arg:
        return None
    abbrev_to_code = {abbr: code for code, (abbr, _) in TYPE_MAP.items()}
    out: set[int] = set()
    for token in arg.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token.isdigit():
            out.add(int(token))
        elif token in abbrev_to_code:
            out.add(abbrev_to_code[token])
        else:
            raise SystemExit(f"unknown type: {token!r}")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ede", type=Path, help="path to the source EDE CSV")
    p.add_argument("-o", "--out", type=Path, required=True,
                   help="output .xlsx or .csv (extension picks format)")
    p.add_argument("-g", "--group", default="bacnet",
                   help="Neuron group name (default: bacnet)")
    p.add_argument("-i", "--interval", type=int, default=1000,
                   help="group polling interval in ms (default: 1000)")
    p.add_argument("-t", "--types", default=None,
                   help="comma-separated object types to keep, by abbrev "
                        "(AI,AV,BV,MSV...) or numeric code. Default: all "
                        "supported types.")
    p.add_argument("--read-only", action="store_true",
                   help="force every tag to Read (ignore EDE commandable flag)")
    p.add_argument("--limit", type=int, default=0,
                   help="cap the output at N tags (0 = unlimited). Useful "
                        "for splitting large EDE files across multiple groups.")
    args = p.parse_args()

    ede_rows = parse_ede(args.ede)
    print(f"parsed {len(ede_rows)} EDE rows from {args.ede.name}",
          file=sys.stderr)

    rows, skipped = to_neuron_rows(
        ede_rows,
        group=args.group,
        interval_ms=args.interval,
        include_types=parse_types(args.types),
        force_read_only=args.read_only,
    )

    if args.limit and len(rows) > args.limit:
        print(f"truncating {len(rows)} -> {args.limit} tags", file=sys.stderr)
        rows = rows[:args.limit]

    if args.out.suffix.lower() == ".csv":
        write_csv(rows, args.out)
    else:
        write_xlsx(rows, args.out)

    by_type: Counter = Counter()
    for r in rows:
        by_type[r["address"][:3].rstrip("0123456789")] += 1

    print(f"wrote {len(rows)} tags to {args.out}", file=sys.stderr)
    print("  by type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())),
          file=sys.stderr)
    if skipped:
        print("  skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.most_common()),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
