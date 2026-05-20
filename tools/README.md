# tools/

Standalone helpers. Each script declares its deps inline (PEP-723), so
`uv run tools/<name>.py` Just Works without a separate environment.

## `provision_neuron.py`

Configure all three neurons via the EMQX Neuron REST API.

- neuron-1, neuron-2 → OPC UA drivers pointing at `plc-1` and `plc-2`
- neuron-3 → BACnet/IP driver pointing at the `hvac-1` simulator

Idempotent: existing nodes/groups/subscriptions are left alone. Run after
`make up` (or via `make provision`).

```bash
uv run tools/provision_neuron.py
```

## `place_order.py`

Publish an order JSON to `<ent>/<site>/orders/<id>`. MES picks it up and
dispatches phase commands to a free PLC.

```bash
uv run tools/place_order.py --recipe program-a --qty 1
uv run tools/place_order.py --recipe program-quick --qty 3
```

Also wired up as `make place-order` for the default recipe.

## `ede_to_neuron.py`

Convert a BACnet EDE (Engineering Data Exchange) CSV into a tag-import
sheet you can upload to Neuron under **South Devices → Group List → Import**.

> Full walkthrough — file formats, object-type mapping, manual conversion,
> and the Neuron import steps — is in
> [`docs/ede-to-neuron-import.md`](../docs/ede-to-neuron-import.md).

Use this when the device exposes too many objects for the in-UI `Scan` to
be practical (anything north of a few hundred), or when you want to keep
the engineering descriptions from the EDE export instead of the bare
addresses that `Scan` returns.

The script accepts the EDE format defined by the BACnet Interest Group
(semicolon-delimited, `#`-prefixed header). Only object types supported by
Neuron's BACnet/IP driver are emitted (AI/AO/AV/BI/BO/BV/MSI/MSO/MSV/ACC);
everything else (trend-log, file, loop, schedule, notification-class,
device) is skipped with a count.

Tag names are regenerated as `<prefix><instance>` (e.g. `BV2097192`)
because EDE's `object-name` column often contains spaces, hyphens, or
non-ASCII characters that Neuron's name validator rejects. The EDE
`description` column is carried through verbatim.

The `commandable` flag from EDE drives the Neuron attribute: `Y` →
`Read Write` for writable object types (AO/AV/BO/BV/MSO/MSV/ACC),
otherwise `Read`. AI/BI/MSI are always `Read` regardless.

### Examples

```bash
# Convert the whole file into one Neuron group
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210.xlsx -g controller-210

# Subset by object type — analog values + multi-state values only
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210-av.xlsx -g controller-210 -t AV,MSV

# Subset by numeric BACnet type code (equivalent to AV,BV)
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210-rw.xlsx -g controller-210 -t 2,5

# Cap output size — split a large device across multiple groups
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210-1.xlsx -g controller-210-part1 --limit 500

# Force everything to Read (ignore EDE's commandable flag)
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210-ro.xlsx -g controller-210 --read-only

# Faster polling for a small curated subset
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210-fast.xlsx -g hvac-fast -t AI,AV --interval 250

# CSV output instead of xlsx (Neuron import accepts both)
uv run tools/ede_to_neuron.py "Controller 210_EDE.csv" \
    -o controller-210.csv -g controller-210
```

### Output column layout

Matches what Neuron's `Export` produces for a configured BACnet node:

| Column      | Source / value                                    |
|-------------|---------------------------------------------------|
| group       | `-g` argument                                     |
| interval    | `-i` argument (default 1000 ms)                   |
| name        | `<prefix><instance>` e.g. `BV2097192`             |
| address     | same as name (BACnet address is `AREA<index>`)    |
| attribute   | `Read` or `Read Write`                            |
| type        | `FLOAT` / `BIT` / `UINT8` per BACnet object type  |
| description | EDE `description` column                          |
| decimal     | 0                                                 |
| precision   | 0                                                 |
| bias        | 0                                                 |

### Importing into Neuron

In the NeuronEX UI:

1. **South Devices → src-bacnet → Group List**
2. Hover **Import** (top right of the group list), download the template
   once if you want to compare layouts.
3. Upload the generated `.xlsx` (or `.csv`).
4. New tags land under the group named in column `group`; the group is
   created if it doesn't exist.

Reprovisioning trap: Neuron returns transient HTML 4xx/5xx during daemon
reloads. If the import UI returns a generic error, refresh and retry once.
