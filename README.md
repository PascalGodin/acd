
![PyPI](https://img.shields.io/pypi/v/acd-tools?label=acd-tools)
![PyPI - Downloads](https://img.shields.io/pypi/dm/acd-tools)
![ACD Tools](https://github.com/hutcheb/acd/actions/workflows/acd-tools.yml/badge.svg)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=hutcheb_acd&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=hutcheb_acd)

## Rockwell ACD Project File Tools

The Rockwell `.ACD` file is an archive that contains all the files used by RSLogix / Studio 5000 Logix Designer. It consists of version text files, compressed XML metadata, and several proprietary binary database files (`Comps.Dat`, `SbRegion.Dat`, `Comments.Dat`, `Nameless.Dat`).

This library parses those binary databases and exposes the project contents — controller tags, programs, ladder rungs, data types (UDTs), add-on instructions (AOIs), and hardware modules — as Python objects. It can also serialise the parsed project to an **L5X XML file** that Studio 5000 can import.

> **Compatibility** — Tested against Studio 5000 firmware versions 20–35. Python 3.8+ is supported; Python 3.12+ is recommended.

---

### Installing

```bash
pip install acd-tools
```

---

### Quick start — read an ACD file

```python
from acd.api import load_acd

project = load_acd("MyController.ACD")
controller = project.controller

# Basic controller info
print(controller.name)            # controller name
print(controller.serial_number)   # e.g. "16#AB12_3456"
print(controller.modified_date)

# Iterate controller-scoped tags and their initial values
for tag in controller.tags:
    print(f"  {tag.name}  ({tag.data_type})")
    if tag._initial_value is not None:
        print(f"    initial value: {tag._initial_value}")

# Walk programs -> routines -> ladder rungs
for program in controller.programs:
    print(f"\nProgram: {program.name}")
    for routine in program.routines:
        print(f"  Routine: {routine.name}  [{routine.type}]")
        for i, rung in enumerate(routine.rungs):
            print(f"    Rung {i}: {rung}")

# Inspect user-defined data types and their members
for udt in controller.data_types:
    member_names = [m.name for m in udt.members]
    print(f"UDT {udt.name}: {member_names}")

# Inspect add-on instructions
for aoi in controller.aois:
    print(f"AOI {aoi.name}: {len(aoi.routines)} routines, {len(aoi.tags)} params")

# Inspect hardware modules
for module in controller.modules:
    print(f"Module {module.name}: vendor={module.vendor_id} "
          f"type={module.product_type} code={module.product_code} slot={module.slot_no}")
```

---

### Tag values (including UDT initial values)

Controller-scoped and program-scoped tags carry their initial values when the data table instance can be located in the binary database:

```python
# Scalar tag
tag = controller.tags[0]
print(tag._initial_value)        # e.g. 42 or "Hello" or {"Member1": 1, "Member2": 0}

# Array tag
for tg in controller.tags:
    if tg.dimensions:
        arr = tg._initial_value   # list of values, one per element
        print(f"{tg.name}[0]: {arr[0] if arr else None}")
```

For UDT-typed tags the initial value is a `dict` (scalar) or `list[dict]` (array) keyed by member name. BOOL members are decoded from their packed bit position; nested UDTs and STRING members are handled recursively. This also applies to Rockwell "module-defined" types (e.g. an I/O module tag typed `AB:1794_IB32:I:0`) — since every DataType's member layout is read from the ACD's own type definitions, decoding works identically whether the struct is a user-created UDT or an implicit module type:

```python
for tag in controller.io_tags:
    print(tag.name, tag.data_type, tag._initial_value)
    # e.g. Local:10:I AB:1756_DI:I:0 {'Fault': 0, 'Data': 15870}
```

---

### Tag and per-element/per-bit descriptions

Whole-tag descriptions are available via the `description` property:

```python
tag = controller.tags[0]
print(tag.description)   # e.g. "Bin Status" (multi-line text is word-wrapped to one line)
```

Per-element and per-bit comments (the same ones Studio 5000 shows as `<Comment Operand="...">`
entries in an L5X export) are available as `(path, text)` tuples on `tag._comments`, already
resolved to full Studio 5000 addresses:

```python
for path, text in tag._comments:
    if path:   # empty path == the tag-level description, already covered by .description
        print(f"{path}: {text}")

# e.g.:
#   HTV_BStatus_Status[10].11: Tally Package Accumulating
#   Local:10:I.Data.13: Grader 13' PE
#   IO074:I.Data[0].0: Tray 4 / Accumulation / Motor Aux.
#   SorterStatusToGradeScan.SorterFeedback.UsingScanSolution: Should Be 1 if...
```

I/O module tags (name contains `:`) are also available separately via `controller.io_tags`,
and alias tags via `controller.alias_tags`:

```python
for tag in controller.io_tags:
    print(tag.name, tag.data_type)

for alias in controller.alias_tags:
    print(f"{alias.name} -> {alias.target}")
```

---

### Convert ACD to L5X

Export the parsed project as an L5X XML file (importable by Studio 5000):

```python
from acd.api import ConvertAcdToL5x

ConvertAcdToL5x("MyController.ACD", "MyController.L5X").extract()
```

The output is pretty-printed by default. Pass `pretty_print=False` for a compact single-line file:

```python
ConvertAcdToL5x("MyController.ACD", "MyController.L5X", pretty_print=False).extract()
```

> **Note** — The L5X serialisation captures tags, programs, routines, rungs, UDTs, and AOIs with their initial values. Hardware module metadata (catalog numbers, connection parameters) is not fully round-tripped because Rockwell stores those as opaque CIP identity records rather than as strings.

---

### Edit rungs and save back to ACD

Ladder rung text can be modified and written back to a working `.ACD` file. The library handles the binary encoding, including resolving tag names to/from `@HEX_OBJECT_ID@` placeholders:

```python
from acd.api import load_acd, save_acd, patch_rungs

project = load_acd("MyController.ACD")
controller = project.controller

# Find the first rung of the first routine and change it
routine = controller.programs[0].routines[0]
changes = {routine.rung_ids[0]: "XIC(MySensor)OTE(MyOutput);"}

patch_rungs(project, changes)
save_acd(project, "MyController_modified.ACD")
```

Only `SbRegion.Dat` (rung text) is re-serialised. Other object types (tags, data types, AOI definitions, modules) pass through as raw bytes and are preserved verbatim. Editing those structures in the Python object model and saving back requires a binary serializer for `Comps.Dat`, which is not yet implemented.

---

### Extract raw database files

Unzip all embedded files (`.Dat`, `.XML`, etc.) to a directory for inspection:

```python
from acd.api import ExtractAcdDatabase

ExtractAcdDatabase("MyController.ACD", "output/").extract()
# output/ now contains Comps.Dat, SbRegion.Dat, Comments.Dat,
#   Nameless.Dat, QuickInfo.XML, TagInfo.XML, XRefs.Dat, ...
```

---

### Extract raw database records to files

Save every individual binary record from the Comps database as its own file, useful for reverse-engineering the record format:

```python
from acd.api import ExtractAcdDatabaseRecordsToFiles

ExtractAcdDatabaseRecordsToFiles("MyController.ACD", "output/").extract()
```

---

### Dump Comps database as a navigable folder tree

Writes the entire Comps database as a directory tree where each node is a `.dat` file. A log file records the CIP class and instance for each record:

```python
from acd.api import DumpCompsRecordsToFile

DumpCompsRecordsToFile("MyController.ACD", "output/").extract()
# Produces output/output.log  +  output/<comp_name>/<comp_name>.dat  (recursive)
```

---

### Integrity / project key

Studio 5000's SDK validates ACD containers using a `FileInfo.Dat` checksum seeded by a project-specific key. The library can read and write this key, recompute the checksum, and verify that a loaded project matches the source ACD:

```python
from acd.api import load_acd
from acd.integrity import get_fileinfo_key, set_fileinfo_key, verify_loaded_acd

project = load_acd("MyController.ACD")

# Check if a signing key is present
key = get_fileinfo_key(project)

# Register a key (32 bytes for modern Studio, 126 for older)
set_fileinfo_key(project, b"\\x00" * 32)

# Verify the loaded project matches the original ACD
ok = verify_loaded_acd(project, "MyController.ACD")
```

When a key is registered, `save_acd()` recomputes `FileInfo.Dat` so the SDK accepts the output. Without a registered key, the container is written as-is (byte-equal round-trip of unmodified streams).

---

### Low-level access via ExportL5x

For direct SQLite access to the parsed ACD databases:

```python
from acd.l5x.export_l5x import ExportL5x

export = ExportL5x("MyController.ACD")

# Raw SQLite cursor — full access to comps, rungs, region_map, comments, nameless tables
cur = export.cur
cur.execute("SELECT comp_name, object_id FROM comps WHERE parent_id=0 AND record_type=256")
row = cur.fetchone()
ctrl_name, ctrl_id = row[0], row[1]

# High-level objects
controller = export.controller
project    = export.project

export.close()   # release the SQLite connection
```

> Always call `close()` when you are done, especially on Windows, to release the file lock on the SQLite database.

---

### Project structure

```
acd/
├── api.py                  # Public API (load_acd, save_acd, patch_rungs, ...)
├── l5x/
│   ├── export_l5x.py       # ACD -> SQLite -> Python objects
│   └── elements.py         # Dataclasses + Builder classes for all project elements
├── database/               # Binary .Dat file reader
├── record/                 # Record parsers (Comps, SbRegion, Comments, Nameless)
├── generated/              # Kaitai Struct generated parsers (comps, comments, ...)
├── integrity/              # FileInfo.Dat checksum and project key management
└── zip/                    # ACD archive extraction and writing
```

---

### Running the tests

```bash
pip install -e ".[dev]"
pytest
```

---

### Developing

Sections of the code are generated from kaitai template (.ksy) files in the resources/templates folder. These are generated during the install phase. The python scripts which are generated are located in the acd/generated folder.

### Contributing

Contributions are welcome. Open an issue or pull request on GitHub.

The sample ACD file used by the tests is `resources/CuteLogix.ACD`.
