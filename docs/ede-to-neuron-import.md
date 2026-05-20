# Converting a BACnet EDE file to a Neuron tag-import sheet

This guide explains how to turn a BACnet **EDE** export (the object list a
controller vendor gives you) into the **Excel/CSV import sheet** that EMQX
Neuron accepts under *South Devices → Group List → Import*. It covers the
automated path (the `tools/ede_to_neuron.py` script) and the manual
column-by-column mapping if you'd rather convert by hand.

> Why import instead of Scan? Neuron's in-UI **Scan** for BACnet does not
> reliably enumerate tags in the version this demo ships (neuronex 2.14.1) —
> it floods the device and never completes, for any device. Importing an EDE
> sheet is the dependable way to load a large controller's points, and it
> keeps the engineering descriptions that Scan would throw away.

## The two file formats

### Input — EDE (`Controller 210_TZ09-1 (9)_EDE.csv`)

EDE (Engineering Data Exchange) is a BACnet Interest Group format: a
semicolon-delimited CSV whose first non-blank line is a `#`-prefixed header.
One row per BACnet object. The columns we care about:

| # | EDE column            | Use                                              |
|---|-----------------------|--------------------------------------------------|
| 1 | keyname               | ignored (vendor-specific path)                   |
| 2 | device obj.-instance  | ignored (we don't filter by device)              |
| 3 | object-name           | ignored — often has spaces / non-ASCII           |
| 4 | object-type           | **numeric BACnet type code** → Neuron type+area  |
| 5 | object-instance       | **the N** in `AI<N>`, `AV<N>`, `BV<N>` …         |
| 6 | description           | → Neuron `description` column                     |
| 10| commandable (Y/N)     | Y → `Read Write`, N → `Read`                      |

### Output — Neuron import sheet (`upload-tag-template.xlsx`)

Download the blank template from the Neuron UI (*Group List → hover Import →
Download Template*) to confirm the layout for your version. It is a single
sheet named `Sheet1` with this header row:

```
group | interval | name | address | attribute | type | description | decimal | precision | bias
```

| Column      | What goes in it                                          |
|-------------|----------------------------------------------------------|
| group       | the Neuron group name (created on import if new)         |
| interval    | group polling interval, ms (e.g. 1000)                   |
| name        | unique tag name within the group                         |
| address     | BACnet address `AREA<instance>` e.g. `AI0`, `AV5`        |
| attribute   | `Read` or `Read Write`                                    |
| type        | `FLOAT` (AI/AO/AV), `BIT` (BI/BO/BV), `UINT8` (MSI/MSO/MSV/ACC) |
| description | free text                                                |
| decimal     | 0                                                        |
| precision   | 0                                                        |
| bias        | 0                                                        |

## Object-type mapping

BACnet `object-type` code → Neuron address prefix + data type. Only the types
Neuron's BACnet/IP driver supports are emitted; everything else is dropped.

| EDE code | BACnet object        | Neuron address | Neuron type | Default attribute |
|----------|----------------------|----------------|-------------|-------------------|
| 0        | analog-input         | `AI<n>`        | FLOAT       | Read              |
| 1        | analog-output        | `AO<n>`        | FLOAT       | Read / Read Write |
| 2        | analog-value         | `AV<n>`        | FLOAT       | Read / Read Write |
| 3        | binary-input         | `BI<n>`        | BIT         | Read              |
| 4        | binary-output        | `BO<n>`        | BIT         | Read / Read Write |
| 5        | binary-value         | `BV<n>`        | BIT         | Read / Read Write |
| 13       | multi-state-input    | `MSI<n>`       | UINT8       | Read              |
| 14       | multi-state-output   | `MSO<n>`       | UINT8       | Read / Read Write |
| 19       | multi-state-value    | `MSV<n>`       | UINT8       | Read / Read Write |
| 23       | accumulator          | `ACC<n>`       | UINT8       | Read / Read Write |

Dropped (no Neuron tag): device (8), file (9), loop (12),
notification-class (15), schedule (17), trend-log (20).

Writable types (AO/AV/BO/BV/MSO/MSV/ACC) become `Read Write` when the EDE
`commandable` column is `Y`, else `Read`. Inputs (AI/BI/MSI) are always
`Read`.

## Automated conversion (recommended)

`tools/ede_to_neuron.py` does the mapping above. It declares its own deps
inline, so `uv run` needs no setup.

```bash
# Whole controller → one group
uv run tools/ede_to_neuron.py "Controller 210_TZ09-1 (9)_EDE.csv" \
    -o controller-210.xlsx -g controller-210
```

Output (stderr) reports what landed and what was skipped:

```
parsed 3091 EDE rows from Controller 210_TZ09-1 (9)_EDE.csv
wrote 2737 tags to controller-210.xlsx
  by type: AI=97, AO=37, AV=884, BI=330, BO=74, BV=991, MSV=324
  skipped: unsupported (trend-log)=179, unsupported (file)=97, ...
```

The generated file has the same sheet name (`Sheet1`) and header row as
`upload-tag-template.xlsx`, so it imports directly.

### Common variations

```bash
# Only analog + multi-state values
uv run tools/ede_to_neuron.py EDE.csv -o out.xlsx -g ctrl -t AV,MSV

# Equivalent using numeric type codes (AV=2, BV=5)
uv run tools/ede_to_neuron.py EDE.csv -o out.xlsx -g ctrl -t 2,5

# Cap at 500 tags — split a huge controller across several groups
uv run tools/ede_to_neuron.py EDE.csv -o ctrl-1.xlsx -g ctrl-part1 --limit 500

# Force every tag to Read (ignore the EDE commandable flag)
uv run tools/ede_to_neuron.py EDE.csv -o out.xlsx -g ctrl --read-only

# Faster polling for a curated subset
uv run tools/ede_to_neuron.py EDE.csv -o out.xlsx -g fast -t AI,AV --interval 250

# CSV instead of xlsx (Neuron import accepts both)
uv run tools/ede_to_neuron.py EDE.csv -o out.csv -g ctrl
```

Notes on the script's choices:

- **Tag names are regenerated** as `<prefix><instance>` (e.g. `BV2097192`),
  not copied from the EDE `object-name`, because those frequently contain
  spaces, hyphens, or non-ASCII that Neuron's name validator rejects.
- **`address` equals `name`** — for the BACnet driver the address *is* the
  area+instance string.
- **Descriptions are preserved verbatim**, including non-ASCII (Neuron
  accepts UTF-8 there). They're what makes the import worth doing over Scan.
- **Duplicate object references are de-duplicated** on the generated name.

## Manual conversion (no script)

If you'd rather convert by hand, open `upload-tag-template.xlsx` and, for
each EDE row whose `object-type` is in the mapping table above:

1. **address / name** — concatenate the area prefix and `object-instance`,
   e.g. object-type `2` (analog-value), instance `899` → `AV899`. Put the
   same string in both `name` and `address`.
2. **type** — FLOAT for analog (AI/AO/AV), BIT for binary (BI/BO/BV), UINT8
   for multi-state (MSI/MSO/MSV) and accumulator.
3. **attribute** — `Read Write` if `commandable` is `Y` *and* the type is
   writable (AO/AV/BO/BV/MSO/MSV/ACC); otherwise `Read`.
4. **description** — copy the EDE `description` column.
5. **group / interval** — your chosen group name and poll interval (ms).
6. **decimal / precision / bias** — `0`.

Delete the template's example row (`groupName / tagName / …`) before saving.

## Importing into Neuron

1. **South Devices → `<your BACnet driver>` → Group List**.
2. Hover **Import** → upload the generated `.xlsx` (or `.csv`).
3. Tags land under the group named in the `group` column; the group is
   created automatically if it doesn't exist.
4. If the import returns a generic error, the embedded daemon may be mid-
   reload (it serves transient HTML 4xx/5xx) — refresh and retry once.

After import, confirm the driver is actually reading: *South Devices →
driver* should show `running` / connected, and the group's tags should show
live values. If every tag reads `3002`/`3008`, re-check the driver's
`host` (numeric IP), `src_port` (must be `0`), and `device_network` (must be
`0` for a directly-reachable device) — see `CLAUDE.md` for those traps.
