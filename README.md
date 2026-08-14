
![PyPI](https://img.shields.io/pypi/v/acd-tools?label=acd-tools)
![PyPI - Downloads](https://img.shields.io/pypi/dm/acd-tools)
![ACD Tools](https://github.com/hutcheb/acd/actions/workflows/acd-tools.yml/badge.svg)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=hutcheb_acd&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=hutcheb_acd)

## Rockwell ACD Project File Tools

The Rockwell `.ACD` file is an archive that contains all the files used by RSLogix / Studio 5000 Logix Designer. It consists of version text files, compressed XML metadata, and several proprietary binary database files (`Comps.Dat`, `SbRegion.Dat`, `Comments.Dat`, `Nameless.Dat`).

This library parses those binary databases and exposes the project contents — controller tags, programs, ladder rungs, data types (UDTs), add-on instructions (AOIs), and hardware modules — as Python objects. It can also serialise the parsed project to an **L5X XML file** that Studio 5000 can import.

> **Compatibility** — Tested against Studio 5000 firmware versions 20–35, plus V38.02 (schema
> revision 1.0) for rung/routine parsing specifically (see `CLAUDE.md` "Region Map format
> change (V38.02)" for what's version-gated there). Python 3.8+ is supported; Python 3.12+ is
> recommended.

> **For AI agents using this library** — if a task requires writing more than a few lines of
> boilerplate to get at something small (e.g. manually walking `controller.programs`/`.routines`
> to find one routine by name, hand-scanning rung text for which tags it references, filtering
> the full tag list down to just what one routine uses), or you notice yourself about to repeat
> that same workaround again later in the task — stop and tell the user. Name the specific
> operation and suggest it as a candidate for a small helper function in this library, instead of
> silently repeating the workaround. This is a flag for the user to decide on, not a task to
> self-assign — don't add the helper yourself unless asked.

---

### Installing

Install this fork directly (includes the persistent project DB / `db_*` API described below —
not yet on PyPI under this name, so `pip install acd-tools` would get a different, older
upstream project instead):

```bash
pip install git+https://github.com/PascalGodin/acd.git@main
```

For local development (editable install, so changes to a checked-out copy take effect
immediately):

```bash
pip install -e ".[dev]"
```

---

### Quick start — the `db_*` API

**This is the recommended way to use this library, especially for an AI agent** — each `db_*`
function takes the `.ACD` path directly, does one thing against a real, persistent SQLite file
kept next to the ACD, and returns. There's no `load_acd()`/`project` object to hold onto and no
separate "save" step — an edit is durable the instant the call returns, and stays visible to
every later `db_*` call against that same file (including from a different script/process),
until the DB is rebuilt from the real `.ACD` (a new Studio 5000 save, detected automatically, or
an explicit `rebuild=True`). See `CLAUDE.md`'s "Persistent project DB" section for the full
design rationale if you're modifying this library itself.

```python
import acd

acd.db_get_project_summary("MyController.ACD")     # names/counts overview -- call this first
acd.db_list_routines("MyController.ACD")             # routine names/types, no content
acd.db_get_routine("MyController.ACD", "MyRoutine")   # one routine's actual rungs/comments
acd.db_list_tags("MyController.ACD")                   # tag names/types, no value
acd.db_get_tag_value("MyController.ACD", "MyTag")        # one tag's value, paginated if a large array

acd.db_new_tag("MyController.ACD", "MyNewTag", "DINT", value=42)
acd.db_insert_rung("MyController.ACD", "MyRoutine", 0, "XIC(Always_Off)OTE(MyNewTag);")
acd.db_export_routine("MyController.ACD", "MyRoutine", "out.L5X")
# Studio 5000: right-click Routines -> Import Routine... -> select out.L5X
```

Run `help(acd)` (or read `acd/__init__.py`'s module docstring directly) for the complete `db_*`
function list, including multi-step atomic edits (`db_transaction`), deleting a
tag/routine/member, and comparing two projects/routines/saves — that docstring is written to be
the single source of truth for this API, kept in sync with the actual functions.

The lower-level, single-process API this is built on (`load_acd()`, an in-memory `Controller`/
`Tag`/`Routine` object graph, `export_routine()`, ...) is still fully supported via
`acd.api`/`acd.l5x.elements` directly — useful for read-heavy scripts that don't need edits to
survive across separate process invocations, or for advanced/internal work on this library
itself. Everything below this point uses that lower-level API.

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
#   MyStatusTag[10].11: Some bit-level description
#   Local:10:I.Data.13: Photo-eye 13
#   IO074:I.Data[0].0: Tray 4 / Accumulation / Motor Aux.
#   ProcessStatus.Feedback.UsingBackupSolution: Should be 1 if...
```

These same per-element/per-bit comments are also emitted as a standalone `<Comments>` block
in `tag.to_xml()` / L5X output (right after `<Description>`, before `<Data>`), matching a real
Studio 5000 export — verified byte-exact (`Operand="..."` is the path above with the tag name
stripped and upper-cased, e.g. `Operand=".GAIN"`, `Operand="[2,2,1].MYSUBSTRUCT.SOME_MEMBER.3"`).
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

### Comparing two projects

For a **generic** "what changed between these two ACDs?" comparison (two saves of the same
project, or two related variants), use `diff_project()` — it covers routines (rung/ST-line text),
tags (value/description/data type), data types, modules, and AOIs, all by name/content, not by
position:

```python
from acd import load_acd, diff_project

project_a = load_acd("MyController_v1.ACD")
project_b = load_acd("MyController_v2.ACD")

diff = diff_project(project_a, project_b)
for (program_name, routine_name), routine_diff in diff.get("routines", {}).items():
    print(program_name, routine_name, "->", routine_diff["status"])
    for change in routine_diff.get("changes", []):
        print(" ", change["op"], change["old"], "->", change["new"])

for key, tag_diff in diff.get("tags", {}).items():
    print(key, "->", tag_diff)
```

Routine content is compared with `difflib.SequenceMatcher` alignment, not a naive index-by-index
zip — two routines with a different rung count (very common even between two saves of "the same"
logic) still diff correctly instead of raising `IndexError`. Data types/modules/AOIs are compared
by name presence only (added/removed), not deep member/parameter layout. A tag's `"value"` entry
is shown in full for small/scalar values, but a large container (typically a UDT array tag's
decoded value) is summarized instead of dumped in full — e.g.
`{"summary": "list[64] vs list[64]: 3 of 64 common elements differ", "differing_indices": [3, 10, 41]}`
— so comparing two large, genuinely different projects doesn't produce an unreadable wall of raw
array values.

**If the request is specifically about I/O address wiring** (not a general diff), use the
narrower `diff_io_addresses()` instead — it reports *only* I/O tag address changes
(`"IO024:I.Data[0].13"`, `"Remote_Rack1:3:I.Pt13.Data"`, `"Local:10:I.Data.11"`, ...) and
nothing else, so it's the wrong default for a broad comparison:

```python
from acd import diff_io_addresses, find_io_addresses, io_addresses_by_routine

diff = diff_io_addresses(project_a, project_b)
for (program_name, routine_name), changes in diff.items():
    print(program_name, routine_name)
    print("  removed:", changes["removed"])
    print("  added:  ", changes["added"])
```

**Already have two specific `Routine` objects** (e.g. you found "the same" routine by name in two
different projects) and just want that one routine's diff? Use `diff_routine()` instead of a
whole-project scan — and don't manually zip/print `routine_a.rungs` and `routine_b.rungs` side by
side by index to eyeball it: a single rung inserted or removed anywhere shifts every later rung's
index, making an otherwise-unchanged tail look completely different in a naive side-by-side
printout even though nothing there actually changed:

```python
from acd import diff_routine

print(diff_routine(routine_a, routine_b))
# {'status': 'changed', 'changes': [{'op': 'delete',
#   'old': ['JSR(P_Landing,0);', 'JSR(Storage_Table,0)...', 'JSR(Planer_Outfeed,0);'],
#   'new': []}]}
# -- the other rungs (which shifted position by 3) are correctly recognized
# as unchanged and don't appear in "changes" at all.
```

`find_io_addresses(text)` extracts the raw list of I/O addresses from a single rung/ST-line;
`io_addresses_by_routine(project)` gives the whole project's routine-by-routine breakdown without
diffing.

---

### Lazy / summary-first lookups

For a caller operating under a context budget (e.g. an MCP tool wrapping this library, or any
agent that shouldn't have to pay for a whole project's worth of content just to check one thing),
each of these has a cheap "list names/counts only" step and a separate "fetch this ONE thing in
full" step — never walk `project.controller...` yourself and return everything you find:

```python
from acd import get_project_summary, list_routines, list_tags, get_tag_value

# Names/counts only -- the first thing to ask for, before drilling into anything specific.
summary = get_project_summary(project)
# {"controller_name": "...", "programs": [...], "tasks": [...], "data_types": [...],
#  "aois": [...], "modules": [...], "controller_tag_count": N, "program_tag_counts": {...},
#  "routine_count": N}

# Name/type/line-count for every routine (or one program's) -- no rung/line content.
for r in list_routines(project, program_name="MyProgram"):
    print(r["routine"], r["type"], r["line_count"])
# Then get_routine(project, r["routine"], ...) for one routine's actual logic.

# Name/data_type/dimensions/description for tags in one scope -- WITHOUT the decoded value
# (a UDT array tag's value can be large enough on its own to matter).
for t in list_tags(project):
    print(t["name"], t["data_type"], t["dimensions"])

# One tag's value, paginated if it's a large array instead of dumped in full.
page = get_tag_value(project, "MyArrayTag", offset=0, limit=50)
# {"name": ..., "data_type": ..., "dimensions": "50", "total_elements": 50,
#  "offset": 0, "returned": 50, "value": [...]}
```

---

### Lookup and rung-editing helpers

A few small helpers exist specifically to avoid hand-rolled patterns that are easy to get subtly
wrong — reach for these instead of writing your own version:

```python
from acd import get_routine, tag_exists, find_tag_references, replace_rung_safe, new_tag, diff_lines

# Instead of the nested program -> routine double-lookup. A routine name is only
# unique WITHIN a program (many projects have a "Main" in several programs) --
# raises ValueError (listing every matching program) if the name is ambiguous
# and no program_name was given, rather than silently picking one.
routine = get_routine(project, "MyRoutine", program_name="MyProgram")

# Pre-creation collision check, in a given scope (controller by default).
if not tag_exists(project, "NewTagName"):
    ...

# Every (program_name, routine_name, rung_index, text) referencing a name,
# project-wide -- e.g. to check whether a name is already used before
# reusing/repurposing it. Word-boundary matched by default (won't match
# "TrimPattern" inside "TrimPattern2"); pass regex=True for your own pattern.
for program_name, routine_name, idx, text in find_tag_references(project, "Lift_Skid"):
    print(program_name, routine_name, idx, text)

# Edit an EXISTING rung, but only if it still matches what you last read --
# raises with a readable diff (not a bare AssertionError) if it changed
# since then (e.g. hand-edited in Studio in the meantime).
replace_rung_safe(routine, 0, expected_old="NOP();", new_text="XIC(MySensor)OTE(MyOutput);")

# Construct a new controller-/program-scope Tag to append to .tags, instead of
# hand-rolling Tag(_name=..., name=..., tag_type="Base", ...) positional
# construction (an easy field-order mistake) every time.
new = new_tag("Current_Origin", "DINT", value=0, description="Added via acd-tools")
project.controller.tags.append(new)

# Verify your OWN in-memory edit to routine.rungs/._st_lines did what you
# expected before exporting -- e.g. assert an edit was insert-only. Aligns
# with difflib the same way diff_routine() does, just for two plain line
# lists instead of two Routine objects.
assert all(c["op"] == "insert" for c in diff_lines(old_rungs, routine.rungs))
```

Inserting or deleting a rung means shifting every `_rung_comments` key at/after that index to
match — `Routine.insert_rung()`/`Routine.delete_rung()` do this atomically instead of you
re-deriving the index arithmetic by hand:

```python
routine.insert_rung(3, "XIC(NewCondition)OTE(NewOutput);", comment="Explains the new interlock")
# ...
routine.delete_rung(3)
```

A rung inserted this way has no real ACD object_id yet (`routine._rung_ids[3]` is `None`) --
`export_routine()` (below) doesn't care, but `patch_rungs()` can only edit an *existing* rung's
text, never create a new one.

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

**`export_routine(..., validate=True)`** checks, before writing any XML, that every struct-typed
name reachable from a referenced tag's own DataType tree actually resolves — catches (with a clear
error naming the tag/member/type) the class of bug where mutating a UDT's members and exporting a
routine referencing an existing instance of it, in the same session, silently renders a member as
a bare zero instead of its real nested structure (previously only ever caught by a Studio 5000
import rejection). Off by default; it's an extra pass over the whole referenced-type graph.

```python
export_routine(project, routine, "MyRoutine.L5X", validate=True)
```

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

Sections of the code are generated from kaitai template (.ksy) files in the resources/templates
folder. The generated python scripts are checked into `acd/generated/` -- a normal `pip install`
does NOT regenerate them (this used to happen automatically during install, which meant `pip
install .`/`pip install` from a git URL failed on any machine without the external Kaitai Struct
compiler on PATH, even though the already-committed generated output didn't need rebuilding at
all). If you've changed a `.ksy` template, regenerate manually:

```bash
python scripts/regenerate_kaitai.py
```

Requires the [Kaitai Struct compiler](https://kaitai.io/#download)
(`kaitai-struct-compiler.bat`/`ksc`) on PATH -- a separate external tool, not a Python dependency.

### Contributing

Contributions are welcome. Open an issue or pull request on GitHub.

The sample ACD file used by the tests is `resources/CuteLogix.ACD`.
