# acd-tools — Claude Code Entry Point

This file is for AI agents (Claude Code, OpenCode, etc.) working **on this repository's own
source code** — i.e. maintaining/extending the ACD parser itself. For user-facing API docs
and usage examples, see `README.md` instead; this file is about internals, gotchas, and how
to safely make changes here.

## Purpose

`acd-tools` parses Rockwell `.ACD` project files (Studio 5000 / RSLogix 5000) directly from
their proprietary binary format — no Studio 5000 installation or L5X export required — and
exposes the contents as Python objects (`Controller`, `Tag`, `Program`, `Routine`, `DataType`,
`AOI`, `Module`, ...). It can also serialize the parsed project back to L5X XML, and patch
rung text back into a working `.ACD` file.

The `.ACD` file is a zip-like archive containing several proprietary binary databases:
`Comps.Dat` (all project objects: tags, datatypes, programs, modules, AOIs, ...),
`SbRegion.Dat` (ladder rung text), `Comments.Dat` (tag/element descriptions and comments),
`Nameless.Dat`, plus `QuickInfo.XML` / `TagInfo.XML` (some metadata is in ordinary XML).

## Commands

```bash
pip install -e ".[dev]"
pytest                    # runs from repo root; test/conftest.py chdir's into test/ automatically
```

- Run a single test: `pytest test/test_elements_helpers.py -q`
- The sample fixture ACD used throughout the test suite is `resources/CuteLogix.ACD` (paths
  in test files are relative to `test/`, e.g. `"../resources/CuteLogix.ACD"` — this works from
  any invocation directory because of the `conftest.py` autouse fixture).
- Formatting: `black` (via pre-commit, see `.pre-commit-config.yaml`).

## Architecture

```
acd/
├── api.py                  # Public API (load_acd, save_acd, patch_rungs, ImportProjectFromFile, ...)
├── l5x/
│   ├── export_l5x.py       # ACD zip -> extracted .Dat files -> SQLite tables -> ControllerBuilder
│   └── elements.py         # Dataclasses (Tag, Program, DataType, ...) + *Builder classes that
│                            #   read from the SQLite cursor and construct them (~3600 lines)
├── database/                # Generic binary .Dat file reader (DbExtract, DatRecord)
├── record/                  # Per-database-file record parsers (CompsRecord, SbRegionRecord,
│                            #   CommentsRecord, NamelessRecord) — thin wrappers that call into
│                            #   acd/generated/ Kaitai parsers and normalize into SQL row tuples
├── generated/                # Kaitai Struct (.ksy) generated binary parsers (do not hand-edit;
│                            #   see "Developing" in README for regeneration)
├── integrity/                # FileInfo.Dat checksum / project-key handling (SDK compatibility)
└── zip/                      # ACD container (un)zipping and rewriting
```

**Data flow:** `ExportL5x.__init__` unzips the ACD, reads each `.Dat` file via `DbExtract`,
runs each raw record through its `record/*.py` parser, and bulk-inserts the normalized tuples
into an in-memory-ish SQLite DB (`comps`, `rungs`, `region_map`, `comments`, `nameless` tables).
`ControllerBuilder` (in `elements.py`) then queries that SQLite DB to build the full object
graph. Builder classes (`TagBuilder`, `ProgramBuilder`, `DataTypeBuilder`, `AoiBuilder`, ...)
all follow the same pattern: take a cursor + `object_id`, `SELECT` the raw record, parse fixed
byte offsets out of it (via `struct.unpack_from`), and construct the corresponding dataclass.

**Everything is binary-offset-driven, not name-driven.** A new/unfamiliar UDT, tag, or AOI
needs zero code changes to parse correctly — `DataTypeBuilder`/`MemberBuilder` read `dimension`,
`data_type`, `bit_number`, etc. from fixed offsets in the raw record for every UDT, whatever
it's called, including Rockwell "ProductDefined"/module-defined types and string-family types
(`STRING`, or a custom type like `STRING_20` detected via the `family` flag, never by matching
the type's own name — a type could just as easily be named `ASCII_TWENTY`). The **only**
name-based heuristics anywhere in the parsing pipeline are:
- `ControllerBuilder`'s I/O comment-resolution block (`elements.py`, search for `"FAULT",
  "STATUS"`), which excludes members literally named `Fault`/`Status` when guessing which
  member of an I/O module's UDT is "the data member" for legacy bit-comment resolution —
  scoped narrowly to that one use case, not to tag/UDT typing in general.
- `ModuleBuilder`'s connection-type name-heuristic fallback (see `_CONNECTION_TYPE_BY_CODE`),
  used only when a connection record's type-code byte is unrecognized or the record is too
  short — the primary path reads a real binary enum (see below), logging a warning when it
  falls back so unrecognized codes don't silently get mis-guessed.

**When you find a classification that currently has to guess from a name** (like the
connection-type heuristic did until it was replaced), look harder for a real discriminating
byte/flag before accepting the heuristic as final — see "Connection Type" below for the method
that worked, and consider adding a `log.warning()` (loguru, already used in this file) on the
fallback path so future unrecognized cases are visible instead of silently mis-guessed.

## Comment / description resolution — read this before touching `comments.py` or `_comments`

This is the trickiest, most bug-prone part of the codebase. `Comments.Dat` stores per-tag and
per-element/per-bit descriptions (what Studio 5000 shows as `<Comment Operand="...">` in an
L5X, or the tag's `<Description>`). Getting the full address (`Tag[3].Flags.2`, `Tag.Member.Bit`,
`Local:10:I.Data.13`, etc.) right requires resolving several layers of indirection:

1. **Container key.** Each tag's comments are found via `parent = (comment_id << 16) | cip_type`,
   where `comment_id`/`cip_type` are read from the tag's own comps record (`RxGeneric`).
2. **Scope collisions.** Multiple *unrelated* tags can share the exact same `(comment_id,
   cip_type)` key (e.g. tags that never got their own unique `comment_id` assigned — this can
   affect hundreds of tags in a single large project). **`comments` table has a `scope_id` column**
   (a 2-byte discriminator at absolute byte offset 16 in both the tag's own raw record and every
   comment record) that must be matched in addition to `parent`, or comments from completely
   different tags get merged together and mislabeled. `TagBuilder` already does this — if you
   add any *new* query against the `comments` table, make sure to filter by `scope_id` too.
3. **Record types.** `Comments.Dat` uses several different binary record layouts depending on
   what's being described (see `record_type` handling in `acd/record/comments.py`):
   - `1`/`2` (AsciiRecord): whole-tag/whole-object descriptions and rung comments.
   - `3`/`4`/`13`/`14` (Kaitai `Utf16Record`): standard structured Kaitai-dispatched types.
   - `5`/`6`/`7`/`8`/`11`/`15`/`19`/`24`/`29`/`30`/`37`/`39`: array/bit operand descriptions with
     an identical hand-parsed layout (`unknown(8, scope_id at [2:4]) + obj_id(u4 at [8:12]) +
     unknown(4) + utf16 tag_ref + ascii text`) — **not** dispatched by the Kaitai `.ksy` file,
     parsed by hand in `comments.py`. This list was built incrementally by finding real examples
     of each shape in an actual project and confirming the byte layout matched; if you find a
     new numbered type with this same byte shape (8-byte header, obj_id at offset 8), just add
     it to this tuple — don't assume the list above is exhaustive, more probably exist.
     `tag_reference` can be an arbitrarily long chain of `!HEXOID` references (one per nesting
     level) plus array indices (including multi-dimensional, comma-separated: `[2,2,1]`) and a
     trailing bit number, e.g. `"[1].!HEXOID1[1].!HEXOID2.9"` for
     `Tag[1].Member1[1].Member2.9` — already handled correctly by the existing multi-match
     hex-OID regex once the record type itself is recognized; no per-shape resolution logic
     needed, just recognize the type.
   - `19` is **overloaded**: most instances are genuine tag comments (as above), but some carry
     AOI edit-history metadata instead (a literal `tag_reference` of `"UDI_LAST_EDITED_BY"` with
     a username/computer string as the text, parallel to the `12`/`UDI_HISTORY` handling below).
     Verified these don't collide with any real tag's `(parent, scope_id)`, so no extra
     filtering was added — but re-check this if you ever see a `UDI_LAST_EDITED_BY` string leak
     into `Tag._comments`.
   - `16`/`17`: similar but with `obj_id` at a different offset (6, not 8).
   - `12`: UDI metadata (AOI RevisionNote) — different, unrelated layout.
4. **Hex-OID resolution.** References like `!06DC4E61` or `.!06DC4E61.!0751B500` are object IDs
   into `RxTypeMemberCollection`; `_build_hex_oid_map()` in `elements.py` resolves them to member
   names. This map is built **globally per-project**, not scoped to the specific tag's own
   DataType — a theoretical (not yet observed) risk if two unrelated members ever share an OID.
5. **Path normalization** (`TagBuilder.build()`, the `normalized = []` loop in `elements.py`):
   stitches the tag name + resolved ref into a final address string. Watch out for:
   - Refs that already carry their own leading `.` (from `.!HEXOID` resolution) — don't add a
     second dot (`sep = "" if ref.startswith(".") else "."`).
   - Multi-digit array indices — any regex touching bracket/digit patterns here (see
     `ExportL5x._normalize_comment`'s bare-`"N]"` → `"[N]"` fix) must use a lookbehind that
     excludes *both* `[` and digits, or it can match mid-way through a 2+-digit number and
     corrupt it (this was a real regression: `(?<!\[)` alone matched inside `"[10]"`, producing
     `"[1[0]"`).
   - Comma-separated multi-dimensional indices (`[2,2,1]`) — the same lookbehind must *also*
     exclude a comma, or it mis-fires on the last component of an already-correct index (`"1]"`
     preceded by `,` looks just like a bare missing-bracket case otherwise), corrupting
     `"[2,2,1]"` into `"[2,2,[1]"`.
   - String-family values (`STRING`, or a custom type like `STRING_20`) are represented
     identically at every nesting level — top-level tag, array element, or a member nested
     inside another struct — as `{"LEN": int, "DATA": str}`, rendered as a `Structure`/
     `StructureMember` with separate `LEN` (DINT) and `DATA` (ASCII) `DataValueMember`s. Never
     represent a string value as a bare/flat string internally — every consumer (XML rendering,
     comment-path matching for `.DATA[N]`/`.DATA[N].bit`) expects this dict shape, verified
     against a real non-blank custom string-family array tag. The `DATA` member's own text
     content is further wrapped as `<![CDATA['text']]>` (quoted L5K-style literal) when
     non-empty, or bare `<![CDATA[]]>` (no quotes) when empty — see `_string_literal_cdata()`.

**When verifying comment/description output, don't trust any pre-built "reference" JSON/index
a downstream project might hand you** (e.g. something like `ref.json` derived from an L5X/CSV
by another script) **blindly** — it's typically hand-built by a separate AI/script pass and can
silently encode the very same bugs it's meant to catch. It may also not exist at all, or be
stale relative to the ACD you're actually testing against. The only trustworthy ground truth is
a real Studio 5000 export: an L5X's `<Comment Operand="...">` / `<Description>` elements, or a
Studio 5000 "Export Tags" CSV's `COMMENT`/`TAG` rows. Don't assume either is already present in
the working directory — if you need to verify comment/description output and don't have one,
ask the user to export a fresh L5X (File > Save As / Export) and/or tag CSV report from Studio
5000 for the specific ACD under test.

## Connection Type / RPI (Module builder)

`ModuleBuilder` reads each I/O connection's Type (Input/Output/DiagnosticInput/MotionSync/...)
from a real u16le CIP enum at raw offset 90, and its RPI (microseconds) from a u32le
immediately after it at offset 92 — not from the connection's name. The connection's own name
(e.g. `"Standard"`) gives **no reliable signal**: in a real project, most `"Standard"`
connections were `Type="Output"` while a couple were `Type="Input"`. If you ever need to
reverse-engineer a similar "guess from name" situation, the method that worked here: collect
every real `<Connection Name=... RPI=... Type=...>` from a project's own L5X export, match each
one to its raw ACD record (RPI is a convenient unique-ish key to match on), then scan every byte
offset for one whose value is constant within each `Type=` group and differs across groups — a
real 1-byte/2-byte enum will show up as a clean, zero-exception discriminator immediately.
`_CONNECTION_TYPE_BY_CODE` only has the codes seen so far (5/6/7/23); unrecognized codes log a
`log.warning()` and fall back to the old name heuristic rather than silently guessing — check
the logs if you ever suspect a module's connection Type is wrong.

## Known limitations / things not implemented

- `Comps.Dat` binary serialization is not implemented — `save_acd()`/`patch_rungs()` only
  re-serializes `SbRegion.Dat` (rung text); tags/datatypes/AOIs/modules round-trip as raw bytes.
- `acd/l5x/catalog_numbers.py` and `acd/l5x/port_structures.py` are hand-maintained lookup
  tables (vendor/product-type/product-code → catalog number / port layout) because that
  information isn't stored as strings in the ACD binary. Only relevant for **new hardware
  module models**, not new UDTs/tags/AOIs.
- Module (I/O) metadata is not fully round-tripped to L5X (opaque CIP identity records).
- `_decode_udt_initial_value`/`_decode_single_udt_element` (initial-*value* decoding from the
  data-table blob, `elements.py`) has a hardcoded recursion depth limit of 3 nested structs —
  this is a generic safety cap (not tied to any specific type/module), separate from the
  *structure*-generation recursion (`_struct_members_xml` and friends), which has no depth
  limit at all. If you ever see a deeply-nested UDT's initial value silently come back empty,
  check this limit first.

## Testing gotchas

- `test/conftest.py` chdir's into `test/` for the whole session — needed because many tests
  reference `resources/CuteLogix.ACD` via `"../resources/..."` relative paths. If you add a new
  test file, you can rely on cwd already being `test/`.
- Some AB module DataType names contain `:` (e.g. `CHANNEL_DI_TIMESTAMP:O:0`), which is invalid
  in Windows paths — anything that turns a comp name into a filename/directory (see
  `DumpCompsRecords` in `elements.py`) needs to sanitize it first.
- The full suite (`pytest` from repo root) should show `65 passed, 2 skipped, 0 failed`. If you
  see `FileNotFoundError`s or `PermissionError`s across many unrelated test files, first check
  you're not missing the `conftest.py` chdir behavior or that a previous test crashed and left
  a locked SQLite file/build artifact behind.
