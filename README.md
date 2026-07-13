
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

`load_acd()` extracts the ACD into a temporary directory (system temp, auto-deleted when the call
returns — see "Low-level access via ExportL5x" below if you want the extracted files left on disk):

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
            comment = routine._rung_comments.get(i)
            print(f"    Rung {i}: {rung}" + (f"  // {comment}" if comment else ""))

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

These same per-element/per-bit comments are also emitted as a standalone `<Comments>` block
in `tag.to_xml()` / L5X output (right after `<Description>`, before `<Data>`), matching a real
Studio 5000 export — verified byte-exact (`Operand="..."` is the path above with the tag name
stripped and upper-cased, e.g. `Operand=".GAIN"`, `Operand="[2,2,1].BFRLUG.Z5_SAWPATTERN.3"`).
This only covers regular controller/program-scoped `<Tag>` elements — per-bit comments on I/O
module connections (`<Module><Connections><Connection><InputTag>/<OutputTag><Comments>`) are not
yet emitted (see the note under "Convert ACD to L5X" below).

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

> **Note** — The L5X serialisation captures tags, programs, routines, rungs (including rung-level `<Comment>`s), UDTs, and AOIs with their initial values, along with per-tag `<Description>` and `<Comments>` blocks. A whole-project structural comparison against a real, decades-old production Studio 5000 export found `<Tag>`, `<Module>`, `<Routine>`, `<Program>`, `<Rung>`, `<Task>`, `<DataType>`, and `<AddOnInstructionDefinition>` counts to be **exact matches**, and both tag-level `<Comments>` and rung-level `<Comment>` content checked comment-by-comment (not just counted) with **zero mismatches**. The only two known, fully-understood remaining gaps: hardware module metadata (catalog numbers, connection parameters) is not fully round-tripped because Rockwell stores those as opaque CIP identity records rather than as strings, and per-bit comments/descriptions on I/O module connections (`<Module><Connections><Connection><InputTag>/<OutputTag><Comments>`) are not yet emitted — see `CLAUDE.md`'s "Known limitations" for details.

---

### Comparing I/O addresses between two projects

Finding where I/O wiring changed between two ACDs (two saves of the same project, or two related
variants) without hand-writing a regex over rung text:

```python
from acd import load_acd, diff_io_addresses

project_a = load_acd("MyController_v1.ACD")
project_b = load_acd("MyController_v2.ACD")

diff = diff_io_addresses(project_a, project_b)
for (program_name, routine_name), changes in diff.items():
    print(program_name, routine_name)
    print("  removed:", changes["removed"])
    print("  added:  ", changes["added"])
```

Only routines with an actual I/O address difference appear in the result. This compares by
*address set*, not by rung position — two routines with a different rung count (very common even
between two saves of "the same" logic) still diff correctly instead of raising `IndexError`. For a
single rung/ST-line, `find_io_addresses(text)` returns the raw list of addresses found
(`"IO024:I.Data[0].13"`, `"Remote_GraderConsole:3:I.Pt13.Data"`, `"Local:10:I.Data.11"`, ...); for
a whole project's routine-by-routine breakdown without diffing, use `io_addresses_by_routine(project)`.

---

### Editing a project and getting the change into Studio 5000

**`save_acd()` alone will NOT produce a file real Studio 5000 accepts if anything changed.** Studio
enforces a `FileInfo.Dat` checksum on open, seeded by a per-installation signing key this library
does not have (and cannot derive from the ACD itself — see `acd/integrity/` and `CLAUDE.md`'s "ACD
write-back" section). A `save_acd()` round-trip with **zero edits** reproduces the original file
byte-for-byte, which is a useful sanity check, but any real edit needs a different path:

**Use `export_routine()` to export a single routine as a partial L5X, then import it via Studio
5000's own native "Import Routine" feature** (right-click a Routines folder → *Import Routine…*).
Studio does the actual binary write and re-signing itself, so `FileInfo.Dat` is never a concern.
This is verified end-to-end against real Studio 5000 for three edit classes: editing a rung,
editing an existing tag's fields (description, value, …), and creating a brand-new tag.

**Editing a rung:**

```python
from acd.api import load_acd, export_routine

project = load_acd("MyController.ACD")
routine = project.controller.programs[0].routines[0]

routine.rungs[0] = "XIC(MySensor)OTE(MyOutput);"
export_routine(project, routine, "MyRoutine.L5X")
# Then in Studio 5000: right-click the Routines folder -> Import Routine... -> MyRoutine.L5X
```

**Editing a tag** (description, value, …) works the same way, via a "carrier" routine that
already references the tag — `export_routine()` embeds a full `<Tag>` definition for every tag a
routine's rungs reference, and Studio's Import Routine dialog offers to overwrite a tag when the
imported copy differs from the project's own:

```python
tag = next(t for t in project.controller.tags if t.name == "MyTag")
tag._comments = [("", "New description")]  # ("", text) is the tag's own whole-tag description

# Find (or pick) any routine whose rung text already references "MyTag"
routine = project.controller.programs[0].routines[0]
export_routine(project, routine, "MyRoutine.L5X")
# Import in Studio; accept the prompt to overwrite MyTag's description.
```

A tag with no reference anywhere in ladder/ST logic (HMI-only or legacy tags, commonly ~30-60% of
a real project) can't be carried this way — there's no routine to attach it to.

**Creating a brand-new tag** uses the identical mechanism — construct a new `Tag`, reference it
from a rung (a real one, or a harmless one guarded by an always-false condition if you don't want
to change actual logic), and export/import the same way. Studio decides create-vs-overwrite based
on whether the tag name already exists in the project, so no special-casing is needed.

See `export_routine()`'s own docstring and `CLAUDE.md`'s "Native-import escape hatches" section for
the full mechanism, verified dependency-closure behavior (UDTs, AOIs, Modules, JSR-called
routines), and known limitations.

---

### Patching rung text directly into the ACD binary (limited)

`patch_rungs()`/`save_acd()` can rewrite `SbRegion.Dat` (rung text) in place without going through
Studio at all — useful for a byte-exact round-trip check, but **the output will not open in real
Studio 5000** unless you've registered a valid `FileInfo.Dat` signing key (see "Integrity / project
key" below):

```python
from acd.api import load_acd, save_acd, patch_rungs

project = load_acd("MyController.ACD")
routine = project.controller.programs[0].routines[0]
changes = {routine._rung_ids[0]: "XIC(MySensor)OTE(MyOutput);"}

patch_rungs(project, changes)
save_acd(project, "MyController_modified.ACD")
```

Only `SbRegion.Dat` (rung text) is re-serialised. Other object types (tags, data types, AOI definitions, modules) pass through as raw bytes and are preserved verbatim — there is no binary serializer for `Comps.Dat`, so editing those structures in the Python object model has no write-back path via `save_acd()` at all (use `export_routine()` above instead).

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

**Working-directory default differs from `load_acd()`.** With no `temp_dir` argument, `ExportL5x`
extracts into a folder *next to the source file* (`MyController.ACD` → `MyController/`, in the same
directory) and leaves it there — handy for inspecting the raw `.Dat`/`.Idx` files or the SQLite DB
afterward. `load_acd()` (above) instead uses a system temp directory and deletes it automatically,
so a one-shot load doesn't clutter your project folder. Pass `temp_dir=` explicitly to either one
to control this yourself.

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
