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
into an in-memory-ish SQLite DB (`comps`, `rungs`, `region_map`, `comments`, `nameless`,
`regnlink`, `regnlink_idx` tables).
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

6. **L5X `<Comments>` emission** (`_build_comments_xml()` in `elements.py`, called from
   `Tag.to_xml()`): renders every non-empty-path entry in `tag._comments` as a standalone
   `<Comments><Comment Operand="...">` block, positioned after `<Description>` and before
   `<Data>` — verified against a real project to be the correct position/shape. `Operand=` is
   the path with the tag name prefix stripped and the remainder **fully upper-cased** (e.g.
   `Operand=".GAIN"`, `Operand="[2,2,1].BFRLUG.Z5_SAWPATTERN.3"`), even though member names keep
   their original casing everywhere else in the document. The comment text itself is **not**
   collapsed to one line the way `.description`/`<Description>` is — multi-line text is
   preserved as-is inside the CDATA. There used to be a second, separate mechanism
   (`_build_elem_comments`) that embedded `<Comment>` as an inline child of an array `<Element>`
   node; it was removed after confirming **zero** such occurrences in a real project's L5X —
   array-element/bit comments only ever appear in the standalone `<Comments>` block.
7. **AOI InOut-parameter binding metadata masquerading as a comment.** When a UDT member's
   DataType is itself an AOI (e.g. a `VFD` member typed as AOI `VAB_PowerFlex_753`), Rockwell
   records which of that AOI's InOut parameters is wired up using the *exact same* Comments.Dat
   record shape as a real per-element comment: the ref resolves to the whole member (e.g.
   `.VFD`), and the text is literally the AOI's own parameter name (e.g. `"Ethernet_Module"`).
   This is not a user-authored description — verified against a real project, the same text
   recurs identically across every tag instance of several different UDTs, regardless of the
   owning tag's own identity, and it never appears in Studio 5000's own L5X `<Comments>` output.
   `ControllerBuilder.build()` strips these (after `aois`/`data_types` are both available) with
   a narrow rule: a comment is dropped only if it's a whole-member reference (no bit/array
   suffix) **and** that member's own DataType is an AOI **and** the text exactly matches one of
   that AOI's own parameter names verbatim. If you ever see a real user comment go missing on an
   AOI-typed member, check whether it happens to collide with that AOI's parameter names first.

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

**Pitfalls when writing your own script to diff generated output against a real L5X** (all
caused three false "bugs" in one verification pass before being caught):
- `comp_name` is **not unique** in `Comps.Dat` — a `<Tag>` and a `<Routine>` (or other object)
  can share the exact same name. `SELECT ... WHERE comp_name=?` can silently grab the wrong
  object entirely. Always resolve by `object_id` (from the already-built `Controller`/`Program`
  object graph) or by `parent_id`/collection membership, never by name alone.
- Self-closing `<Tag Name="..." .../>` elements (e.g. Alias tags with no children) have no
  `</Tag>` to search for — a naive `content.index('</Tag>', start)` after matching `<Tag
  Name="...">` will walk past the self-close and grab the **next** tag's content instead. Match
  `(?:/>|>)` and branch on which one matched before searching for a closing tag.
- I/O tags (`":" in tag.name`) are already correctly excluded from the real `<Tags>` XML section
  via `Tag._l5x_exclude` — that exclusion only takes effect when a *parent* element serializes
  its `tags` list (see `_LIST_SECTION_NAMES`/`_l5x_exclude` handling in `L5xElement.to_xml()`).
  Calling `tag.to_xml()` directly on an I/O tag bypasses that filter and will make it look like
  I/O tags are wrongly emitting `<Tag>`/`<Comments>` content when they never actually would be
  in a real full-project export — filter by `not tag._l5x_exclude` first when spot-checking.

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
- **Module/Connection-level comments are not implemented at all.** Studio 5000 stores per-bit
  descriptions for I/O module connection points inside
  `<Module><Connections><Connection><InputTag>/<OutputTag><Comments>` (a completely different
  XML location from a regular `<Tag>`'s `<Comments>` block, with its own comment_id/scope_id
  resolution scheme that hasn't been reverse-engineered yet). Verified on one large real project:
  570 `<Comment Operand="...">` entries live there — 0 of them are currently emitted. This is
  separate from (and larger than) the regular per-`<Tag>` `<Comments>` block, which **is**
  implemented and was verified byte-exact against that same project (see comment-resolution
  section above).
- **Whole-project L5X fidelity — current status (as thoroughly verified as this project has ever
  been checked)**: a full whole-project element-count comparison against a real Studio 5000 L5X
  export (see "Whole-project element-count verification" below) found and fixed real bugs causing
  `Tag`/`Module`/`Program`/`Routine`/`Rung`/`Task` count mismatches — all six are now **exact
  matches** (0 diff), joining `DataType`/`AddOnInstructionDefinition`. `Comment` (rung-level) is
  also now an **exact match** (582/582, every one on the exactly right rung, not just the right
  count — see "Rung comments: attribution via RegnLink.Idx" below) after finding the authoritative
  fragment→rung mapping in `RegnLink.Idx`. The **only** two remaining, fully-understood (not
  mysterious) discrepancies against that same real project's L5X, both already covered above: the
  `Comment` total is short by exactly 570 (the un-implemented `InputTag`/`OutputTag`/
  `InAliasTag`/`OutAliasTag` module-connection comments) and `Description` is short by exactly 19
  (16 of the same module-connection kind + 3 un-implemented `<Trend>`/`<Pen>` descriptions) —
  verified by breaking down both totals element-by-element, not just diffing the raw counts.
  Tag-level `<Comments>` and rung `<Comment>` content were both independently checked
  comment-by-comment (not just aggregate counts) against the real export with zero mismatches.
  Don't assume this same level of fidelity holds for a *different* real project just because one
  project now checks out this cleanly — re-verify against a fresh real export if it matters.
- `_decode_udt_initial_value`/`_decode_single_udt_element` (initial-*value* decoding from the
  data-table blob, `elements.py`) has a hardcoded recursion depth limit of 3 nested structs —
  this is a generic safety cap (not tied to any specific type/module), separate from the
  *structure*-generation recursion (`_struct_members_xml` and friends), which has no depth
  limit at all. If you ever see a deeply-nested UDT's initial value silently come back empty,
  check this limit first.
- ~~`<Description>` may need to preserve multi-line text~~ — **fixed.** Confirmed via a real
  Studio 5000 Import Routine diff: a tag's existing `<Description>` was genuinely multi-line
  (`"Program \nBit \nFlags"`, 3 lines), and our collapsed single-line rendering
  (`"Program  Bit  Flags"`) was flagged by Studio 5000's own import comparison as a real
  difference, not just cosmetic. `_multiline_xml_text()` now preserves line breaks in every
  `to_xml()` Description/RevisionNote renderer (Member, DataType, Tag, LocalTag, Parameter,
  Module, AOI) — verified byte-for-byte identical to the real export afterward. The
  `.description` **Python property** (`Member.description`/`Tag.description`) still
  deliberately collapses to one line — that's documented, existing convenience-API behavior,
  separate from XML fidelity.
- FBD and SFC routine content is still not decoded — only `RLL` (ladder, via `SbRegion.Dat`) and
  `ST` (structured text, via `Nameless.Dat`, see below) routine bodies are exported; an FBD/SFC
  routine still exports as an empty `<Routine Type="FBD"/>`/`<Routine Type="SFC"/>` with no
  `<SheetContent>`/`<STContent>`-equivalent — nobody has reverse-engineered their storage format
  yet (adapted from an upstream `hutcheb/acd` PR that only covered ST).

## Structured Text (ST) routine content (`_st_routine_lines`)

ST routine bodies are **not** stored in `SbRegion.Dat` like ladder rungs — they live in
`Nameless.Dat`, one record per source line, found by walking the nameless `parent_id` tree
breadth-first from the routine's own object id (routine → map → region → line, up to 6 levels).
A source-line record is identified by record type `0x01000002` (u32 at offset 4) — other record
types under the same subtree (`0x7d6` compiled neutral text, `0x7d2` region stubs, `0x8a4`
bookkeeping, in Kaitai node-kind terms) are *not* source lines and must be filtered out; the
sequence number (u32 at offset 20) gives source order, and the line text itself is `fffeff`-encoded
UTF-16 starting at offset 24 (`_parse_fffeff`, extended to handle the long-line form where the
one-byte length is `0xFF` and the real length follows as a u16). `@hexid@` placeholders (an
object-id-in-hex tag reference, distinct from rung text's `&hexid:` form) are batch-resolved to
comp names the same way rung text resolves module references. Rendered as `<STContent><Line
Number="N"><![CDATA[...]]></Line>...</STContent>` — verified line-for-line against the
`ACDTestsNonRedundant.ACD`/`ACDTestsWithAOI.ACD`/`ACDTestsFilledRedundant.ACD` fixtures' own
`STRoutine`, including preserved blank lines and resolved tag references
(`test_st_routine_content`). AOI logic routines store ST the same way and are picked up
automatically wherever `RoutineBuilder` runs. Adapted from an open, unmerged PR against
`hutcheb/acd` (our upstream) after independently re-verifying the layout against our own fixtures.

## Ingestion robustness (`_parse_records` in `export_l5x.py`)

`Comps.Dat`/`SbRegion.Dat`/`Comments.Dat`/`Nameless.Dat` ingestion used to abort the *entire*
import if a single record failed to parse (one `UnicodeDecodeError`/`struct.error` on newer
firmware, e.g. V33+, previously made a whole ACD unloadable — matches symptoms reported against
upstream `hutcheb/acd` issues #14/#15). `_parse_records()` now parses each `.Dat` file's records
one at a time, skipping (and counting) any record whose parser raises, logging a single
`log.warning("<Table>: skipped N unparseable record(s) of M")` instead of propagating — a missing
or wholly unreadable `.Dat` file degrades to an empty table the same way rather than raising.
`TaskBuilder`'s scheduled-program list is also bounds-checked against the record buffer (a
firmware-version-dependent layout could otherwise read a garbage count past the end of the
buffer), and a single task that still can't decode is skipped with a warning rather than aborting
`ControllerBuilder.build()` entirely. Adapted from an open, unmerged PR against `hutcheb/acd`;
existing test suite (which only exercises files that already parse cleanly) is unaffected by
design — this only changes behavior on records/files that previously would have raised.

## Native-import escape hatches for write-back (routine L5X, tag CSV)

Because `FileInfo.Dat` is enforced on open (see "ACD write-back"), the sanctioned way to get an
edit into a project is to hand Studio 5000 a file it imports through its own UI — Studio then
does the binary write + re-sign. Two native import channels, two file formats:
- **Routine → partial L5X** via "Import Routine" — see `export_routine()` below (verified
  end-to-end).
- **Tags/descriptions/comments → CSV** via "Import Tags" (Tools ▸ Import, or right-click
  Tags ▸ Import) — see `export_tag()` (draft, calibration-pending) and the tag-CSV notes next.
  This is the path for the tag-creation/description-edit class (the `STUDIO_EDITED` project's
  new `BitsFlags` tag was exactly this class).

### Tag CSV import format (Rockwell "CSV-Import-Export")

Reverse-engineered from a real `...-Tags.CSV` "Export Tags" output and verified reproducible
from our own parsed object model (100% of controller-scope base-tag DATATYPE fields and 99.9%
of DESCRIPTION fields regenerated byte-exact for a real 2724-tag project; the last handful are
rare escape chars, still being chased). Layout:
- Preamble: five `remark,"..."` lines (`CSV-Import-Export`, Date, `Version = RSLogix 5000 vNN.NN`,
  Owner, Company), then a bare `0.3` version line, then the column header
  `TYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,ATTRIBUTES`. Encoding is latin-1, CRLF lines.
- Row TYPEs seen: `TAG` (base tag), `ALIAS` (SPECIFIER = the AliasFor operand, DATATYPE empty),
  `COMMENT` (per-element/bit description; SPECIFIER = the full operand *including* the tag name,
  e.g. `IO074:I.DATA[0].0`), `RCOMMENT` (rung comments — same 582 count our RegnLink.Idx work
  resolves), `TYPE` (datatype/UDT declarations).
- `SCOPE`: empty = controller; a program name for program-scope; `<AOIName>:AOI` for AOI-local
  tags.
- `DATATYPE` **folds the array dimension in** (`DINT[64]`, `STRING[960]`) — our model stores
  `data_type` and `dimensions` separately, so recombine them here.
- `DESCRIPTION`/comment text uses the **raw multi-line** description (NOT `Tag.description`,
  which deliberately collapses newlines — use the empty-path entry of `Tag._comments`), with
  Rockwell's `$` escapes: `$` → `$$` (do this first), newline → `$N`, tab → `$T`,
  apostrophe `'` → `$'`. The whole field is then CSV-quoted.
- `ATTRIBUTES`: `(RADIX := …, Constant := …, ExternalAccess := …)` for controller/program base
  tags; program/AOI tags add `Usage := Local/Input/Output/InOut` and `Required`/`Visible`; the
  key set present varies by tag kind (some omit `RADIX`, InOut params omit `Constant`).

Studio's Import Tags accepts a *subset* CSV (just the preamble + header + the changed rows), so
an edit doesn't require regenerating all rows.

**Deprioritized per user direction**: the user does not want to rely on CSV import/export as the
tag-edit mechanism. The format reverse-engineering above is kept for reference (it's real,
verified-reproducible knowledge), but the active path for tag edits is the routine-carrier
mechanism below, not `export_tags_csv()`.

### Tag edits via the routine-import overwrite prompt (the active mechanism)

Confirmed by the user: Studio 5000's **Import Routine** dialog offers to overwrite a tag's
description when the imported file's `<Tag>` context element differs from what's already in the
project. Since `export_routine()` already embeds a full `<Tag>` definition for every
controller-/program-scope tag a routine's rung text references (see below), a tag-level edit
(description, value, ...) can be pushed through the *already-verified* routine-import path with
no new binary/XML format to trust:

1. Find an existing routine whose rung text already references the target tag by name (a
   controller-scope tag can be referenced from any routine in the project; a program-scope tag
   only from routines in its own program).
2. Edit the tag's description (or other field) on the in-memory `Tag` object.
3. `export_routine()` that *unmodified* routine — the routine's own logic doesn't change, but the
   tag's context `<Tag>` element now carries the edit.
4. Import in Studio; accept the overwrite prompt for the tag.

**Real limitation, measured on the current project** (`BPM_TrimmerSorter_20260709.ACD`): a
sizeable fraction of tags are never referenced in any routine's ladder or ST text at all —
**35% of controller-scope base tags, 59% of program-scope base tags** (measured by building the
full set of identifier tokens across every routine's `rungs` + `_st_lines`, project-wide, and
checking which base — non-Alias, non-I/O — tags never appear; the project has no FBD/SFC
routines, ruling that out as an explanation). These are presumably HMI/SCADA-only or legacy tags.
**This is not a bug to work around**: per the user, this should replicate what Studio's own
"Export Routine"/"Export Component" does, which likewise only includes what a routine actually
references — a tag with no logic reference wouldn't be in Studio's own export either. Tags in
this category are simply out of scope for the routine-carrier mechanism; no fallback (like
synthesizing a dead-code reference) has been built, pending a decision on whether one is wanted.

## Partial/context L5X exports (`export_routine()`)

`export_routine()` (`acd/api.py`) exports a single routine as a standalone partial L5X file for
Studio 5000's native "Import Routine" feature, sidestepping the `save_acd()`/`patch_rungs()`
limitations entirely for the common case of editing/adding rungs (including rung comments) in
an existing routine — Studio 5000 itself handles all the internal consistency (cross-reference
index, object database, re-signing) that a raw binary write would otherwise require.

**Confirmed working end-to-end**: a real, edited `export_routine()` output (a routine with a new
rung instruction added, referencing one controller-scope and two program-scope tags, including
one array tag) was successfully imported into a real Studio 5000 project via native Import
Routine, with zero errors. This took several rounds of real-data verification to get right —
see below for the full list of bugs found and fixed along the way, most of which only surfaced
once an actual *import* (not just an export/shape comparison) was attempted:

1. **The wrapper shape** was calibrated against a real Studio 5000 "Export Routine" output (a
   2-rung routine referencing one controller-scope tag and two program-scope tags):
   `<DataTypes Use="Context">` (always present, even empty), `<Tags Use="Context">` at both
   Controller and Program scope (full `<Tag>` definitions, reusing `Tag.to_xml()`, for every tag
   the routine's rung text references — found via a simple identifier scan intersected against
   the project's known tag names, not a real ladder-logic parser), `<Programs Use="Context">`,
   and `<Routines Use="Context">` wrapping `<Routine Use="Target" ...>`.
2. **Program-scope tag shadowing.** A program-scope tag must shadow/exclude a same-named but
   unrelated controller-scope tag (standard Logix bare-name resolution) when resolving which
   tags a routine's rung text actually references — previously both were incorrectly included.
3. **THE actual crash root cause** (`0x80004003` "Invalid pointer" in Logix Designer, confirmed
   via the app's own fatal-error log): individual `<Tag>` elements must **never** carry a
   `Use=` attribute themselves — only the wrapping container elements (`<Controller
   Use="Context">`, `<Tags Use="Context">`, `<DataTypes Use="Context">`, `<Programs
   Use="Context">`, `<Routines Use="Context">`, `<Program Use="Context">`) and the routine
   actually being targeted (`<Routine Use="Target">`) do. This was found by the most reliable
   method available: making the identical edit directly in Studio 5000, exporting it natively,
   confirming *that* file imports successfully, then diffing our file against it
   attribute-by-attribute (not just child-element shape, which had already matched) — the one
   remaining difference was `Use="Context"` present on every `<Tag>` in ours, absent in the real
   one. This exactly explained every earlier experimental result: an empty `<Tags
   Use="Context"></Tags>` never crashed, but *any* populated `<Tag>` did, regardless of whether
   it was a scalar or array, regardless of whether it had `<Data>` content at all (even
   attributes-only `<Tag>` elements crashed) — because the bad attribute was on the Tag element
   itself in every case.
4. **Two more bugs found along the way, both affecting `Tag.to_xml()` generally (not specific to
   `export_routine()`)**, uncovered because building real context tags for this feature was the
   first time this session's verification touched a scalar-with-known-value tag and a real
   populated array tag: scalar primitive tags were missing their `<Data Format="L5K">` block
   entirely and used the wrong Decorated element shape (`<BOOL Name=...>` instead of `<DataValue
   DataType="BOOL"...>`), and primitive *array* tags were also missing their entire L5K block —
   see the "Rung patch write-back" section's sibling fixes below for `Tag.to_xml()` details, and
   the dedicated "BOOL array bit-packing" fix a few paragraphs down.
5. **Array trailing-zero truncation was removed entirely** (`Tag.to_xml()`'s primitive array
   branch and `_udt_array_to_xml`) — it was never actually verified against real Studio 5000
   output despite an existing docstring claiming otherwise, directly contradicted by a real
   Export Routine sample (a 256-element array shown in full, not truncated), and strongly
   suspected (though not proven, since fix #3 above turned out to be the actual root cause) as a
   contributing crash risk before that was found.
6. **A serious, unrelated data-correctness bug found while checking the imported tag's actual
   value against the project's live value**: BOOL *array* initial values were bit-unpacked
   incorrectly — see "BOOL array bit-packing" below. This affects every BOOL array tag's decoded
   value project-wide, not just `export_routine()`.

**Confirmed importing a real edit succeeds**: after fix #3, importing an `export_routine()` file
with a genuinely new rung instruction (referencing a controller-scope tag and two program-scope
tags, one an array) into a real Studio 5000 project completed with zero errors.

Verified against a **second**, more complex real routine (`Lug_Skip`: 6 rungs, a UDT array tag
`To_Skip[25]`, two Alias tags) by diffing against a real Studio 5000 export of the identical
routine, unmodified — 0 remaining differences (attributes and children) across every element.
This round found and fixed several more real gaps:
7. **`Routine._description`** — routines can have their own whole-routine description, rendered
   as a `<Description>` child of `<Routine>` before `<RLLContent>`, AND as a leading XML comment
   (`<!--description text-->`) right after the `<?xml ...?>` declaration in the partial-export
   wrapper. Root-caused via the comments table: the routine's own comment parent/scope_id key has
   an `AsciiRecord` (record_type=1) entry with `rung_content==0`, previously only understood as
   "internal metadata to exclude" — it's actually this description. See "Routine-level
   Description" below for the leading-XML-comment newline-doubling pitfall found along the way.
8. **UDT scalar/array tags were also missing their `<Data Format="L5K">` block** — same class of
   bug as the primitive scalar/array cases (fix #4 above), just not yet applied to UDTs. Verified
   against the real `To_Skip[25]` tag. See "UDT L5K rendering" below.
9. **A latent bug this exposed**: a raw NUL byte could end up inside a decoded string member's
   own text (not just its computed padding), producing non-well-formed XML when rendered via L5K.
   Fixed `_l5k_string_padded()` to escape any embedded NUL the same way as padding (`"$00"`).
10. **`Member.byte_offset` leaked into L5X output** as an unintended `ByteOffset="..."` XML
    attribute (real Studio 5000 output never has this) — it was a plain, non-underscore dataclass
    field used only for internal UDT decode offset calculations, and `L5xElement.to_xml()`
    auto-serializes any non-underscore field. Renamed to `_byte_offset`.
11. **An Alias tag's target must also be included as its own context `<Tag>`** — a routine using
    alias `Sort_Enc_Calibrated` (→ `HTV_ECal_SortPos`) needs the target tag's own full definition
    included too, even though the target's name never literally appears in the rung text (only
    the alias name does). Resolved iteratively in `export_routine()` (a target could itself be
    an alias) with the target name stripped of any trailing member/bit-index suffix.

Still open / not yet verified: whether the `Owner` attribute is actually required for import to
succeed (included as an optional parameter, omitted by default; both successful tests included
it, so its necessity hasn't been isolated), and scenarios beyond a single UDT array level
(nested UDTs within UDTs, AOI-typed members, multi-dimensional UDT arrays) haven't been
exercised through `export_routine()` specifically yet (though the underlying `_l5k_udt_literal`/
`_udt_scalar_to_xml` recursion has been separately verified for nested cases in other contexts).

12. **UDT/AOI dependency closure was single-level and AOI-blind — fixed, but the AOI part is
    unverified.** The user clarified the intent directly: replicate what Studio's own routine
    export does, "including UDT, AOI, MODULES, Etc" as transitive dependencies. Auditing the code
    found two real gaps: `referenced_data_types` only checked the data type of *directly*
    referenced tags (a UDT containing another project UDT as a member wouldn't pull that inner
    UDT in), and `project.controller.aois` was never consulted at all — an AOI instruction call in
    a rung (e.g. `AOI_RPMtoFPM(TestFPM,VFD_P_INTBL2:I.OutputFreq);`, a real rung in
    `Motors/Main_Motors`) has its AOI name resolved through the instance tag's own `data_type`
    field (here `TestFPM.data_type == "AOI_RPMtoFPM"`) exactly like a UDT tag, but the AOI
    collection was simply never searched. `_resolve_type_closure()` (`acd/api.py`) now does a
    proper worklist-based transitive closure over both `project.controller.data_types` and
    `project.controller.aois` (following a UDT's own members, and an AOI's own parameters/local
    tags, for further nested dependencies), and `export_routine()` emits a conditional
    `<AddOnInstructionDefinitions Use="Context">` block (only when at least one AOI is actually
    referenced — confirmed to leave the already-verified no-AOI case byte-for-byte unaffected).
    **The UDT-closure half is low-risk** (same mechanism already verified, just transitively
    closed). **The AOI section's placement (right after `</DataTypes>`, before `<Tags>`) and even
    whether Studio wants it wrapped in `Use="Context"` at all is a reasoned-by-analogy guess, NOT
    verified against a real Studio export that has an AOI dependency** — unlike literally
    everything else in this wrapper, which was calibrated against real Studio output before
    trusting an import (see the `Use=` crash story above — guessing at this exact class of
    structural placement has bitten this project before). **Module dependencies are not handled
    at all yet**: that same `Motors/Main_Motors` rung's second argument
    (`VFD_P_INTBL2:I.OutputFreq`) is an I/O tag reference, and whether/how a real Studio routine
    export includes the owning `<Module>` definition for this is completely unknown — I/O tag
    names contain a `:` and aren't picked up by the plain-identifier `_referenced_tag_names`
    scan at all currently, so this needs its own investigation once real ground truth exists.
    **The concrete next step**: a real Studio 5000 "Export Routine" of `Motors/Main_Motors` from
    `BPM_TrimmerSorter_20260709.ACD` exercises both open questions in one file — requested from
    the user, not yet obtained as of this writing. A generated (unverified) sample based on the
    current code is at `...\Bethel_Planer\source\WriteBack_Tests\CALIBRATION_Main_Motors.L5X`.

## Routine-level Description (leading XML comment newline pitfall)

The leading `<!--description-->` XML comment `export_routine()` emits (see item 7 above) must
have its line endings normalized to bare `"\n"` *before* being embedded, using the same
`_multiline_xml_text()` already used for `<Description>` child elements — NOT the raw
`routine._description` string as-is. `Path.write_text()`'s default text-mode newline translation
on Windows blindly replaces every `"\n"` with `"\r\n"`, including the `"\n"` half of an
already-present `"\r\n"` pair from the ACD's own raw text, which doubles into `"\r\r\n"` (renders
as a spurious blank line) if left un-normalized. Caught by comparing byte-for-byte against a real
export where line breaks were single, not doubled.

## Whole-project element-count verification, and a real Comments.Dat dedup bug

`export_routine()` and individual-tag/routine spot-checks had been the only verification method
until this investigation: exporting an entire real project's `to_xml()` and comparing element
counts (`<Tag>`, `<Module>`, `<Routine>`, `<Rung>`, `<Program>`, `<Description>`, `<Comment>`,
...) against that same project's own Studio 5000 L5X export. This surfaced several real bugs no
per-feature test had caught (see "Known limitations" for the ones still open):

- **Phantom `<Program>`/`<Module>`/`<Tag>`/`<Routine>` elements**: deleted-but-not-purged comps
  records with a distinct `record_type` (or, for Routine, a `routine_type_enum(0) ==
  "TypeLess"` CIP value) that don't appear in the real L5X at all. Fixed by filtering these out
  in `ControllerBuilder`/`ProgramBuilder`/`RoutineBuilder` — see each builder's own inline
  comments for the specific record_type values found.
- **`populate_region_map()`'s read loop silently dropped the table's last entry** (an erroneous
  `- 4` in the loop bound, present since the function was first written) — lost whichever single
  16-byte entry happened to be physically last in the whole table, which for one real project
  landed in the *middle* of one routine's own rung sequence, silently shifting every subsequent
  rung's number by one in that routine alone. Fixed by removing the `- 4` (verified: `region_length`
  is always an exact multiple of 16 across every local fixture and this real project).
- **A real comment-dedup bug, found via a routine's own missing `<Description>`**: the
  `seen[key]` dedup step in `export_l5x.py` (see the comment-resolution notes above) used
  `(parent, tag_reference, scope_id)` as its key, keeping whichever candidate had the longest
  text. A routine's own whole-routine Description (`rung_content == 0`) and one of its *rung*
  comments (`rung_content != 0`) can share the exact same `(parent, tag_reference="", scope_id,
  object_id)` — found via a real "Get_Bin" routine where the real Description ("Find bin for
  current set") was shorter than an unrelated rung comment sharing the same key, so the
  dedup step silently kept the rung comment and discarded the Description. Fixed by adding
  `rung_content` to the dedup key. This also means a **routine can have at most one dedup
  collision saved per (parent, tag_reference, scope_id, rung_content) tuple** — see the next
  section for a related, *unsolved* problem this investigation also uncovered.

## Rung comments: attribution via RegnLink.Idx (SOLVED — 582/582 exact on a real project)

**The mechanism, in one paragraph**: a rung comment's `rung_content` upper 16 bits are a
"fragment" ID. The authoritative fragment→rung mapping lives in **`RegnLink.Idx`** (never
examined until this was solved): B-tree-style index pages containing dense 16-byte entries,
all little-endian — `[0:2] fragment` (same value as a `RegnLink.Dat` record's `[18:20]`),
`[2:3]` the same 7-bit value as the `.Dat` record's `[20:22]` "unknown", `[3:4]` always `0x00`
(used as a validation byte when scanning), `[4:8] routine_id` (comps object_id), **`[8:12]
rung_object_id` — directly names the comment's target rung**, `[12:16] ptr` = file offset + 12
of the paired `RegnLink.Dat` record carrying the same fragment (used as a validation bound:
must be ≤ the `.Dat` size, which filters false-positive scan matches). Resolution:
`fragment = rung_content >> 16` → look up `(routine_id, fragment)` in these entries → that
entry's `rung_object_id`'s position in the routine's region_map-ordered rung list. Stale
entries from old/free index pages survive the file scan (a fragment can appear twice with
different rung UIDs — observed in a real project), so prefer the entry whose `rung_object_id`
is one of the routine's own live rungs; if Idx entries exist but none names a live rung, drop
the comment (genuinely stale) rather than falling back. See `populate_regnlink()` in
`export_l5x.py` and `RoutineBuilder.build()`.

**Verified**: 582/582 rung comments on exactly the right rung for a real, decades-old production
project, against that project's own Studio 5000 full L5X export (including AOI logic-routine rung
comments — remember AOIs when parsing L5X ground truth), plus every purpose-built staged edit test
(fresh comments, delete-then-recreate, rung inserted mid-routine shifting comments below it).

**History — how this was misunderstood twice, kept so nobody re-treads it**:
1. First theory: comment `object_id` − 1 = rung index. Wrong (`object_id` is constant 1 across
   every rung comment in a routine); only 98/582 of a real project's comments were emitted.
2. Second theory ("the chain reading", previously documented here as the real mechanism):
   resolve the fragment against **`RegnLink.Dat`** — a per-routine linked list of rungs
   (22-byte records: `[0:4]` routine, `[4:8]` own rung, `[8:12]` next rung, `[12:16]` type,
   `[16:18]` flags, `[18:20]` fragment, `[20:22]` unk) — as "the fragment belongs to the rung
   in `next_id`". This is only *coincidentally* correct, for routines whose rungs were never
   reordered/relinked (true of freshly-created test projects, which is why it verified clean at
   the time): 522/582 of the real project's comments were *emitted*, but that number hid that
   the fragment→`next_id` association is wrong whenever the chain was ever edited — scored for
   *placement* against Studio's own export, only 113/533 landed on the right rung (317 were off
   by exactly +2). A fragment sticks to its 22-byte *link record*, not to the rung: verified by
   a staged rung-insertion test where the record `own=rung3` had its `next_id` redirected to the
   new rung while keeping its old fragment. `RegnLink.Idx`'s `rung_object_id` is the field that
   tracks the *current* rung for each fragment.
3. The "Rockwell editor quirk" theory (four staged reproductions of delete-a-comment-then-
   create-one appearing to write the *preceding rung's* fragment) — **retracted, it was our own
   misreading**. The written fragment was correct all along per `RegnLink.Idx`; it merely looked
   like rung 2's fragment under the broken chain reading (in that test routine the rungs had
   been created out of order, so chain order ≠ link-record order for exactly three fragments).
   The user's observation that Studio 5000 shows the comment on the correct rung after a full
   close/reopen was the decisive clue that the answer had to be recoverable from disk.

**`RegnLink.Dat` facts worth keeping** (the `.Dat` chain reading is retained only as a fallback
when a fragment has no Idx entry at all, e.g. missing `RegnLink.Idx`):
- Records are **not reliably contiguous** for a long-lived project — scan the whole file for
  known comps object_ids in the `[0:4]` slot rather than assuming adjacency.
- Type `0xFFFF0000` marks a stale/deleted link (filter it); additionally the physically-last
  record of a routine's block can carry type `0xFFFFFFFF` with fragment `0xFFFF` — it is not
  dead, it's the not-yet-finalized tail link (its own/next fields are still live chain data;
  observed getting a real fragment assigned only when a later edit appended another record).
- Physical record order = rung *creation* order (independently confirmed by `SbRegion.Dat`
  record order), not current rung order.

**Comments.Dat deletion/reuse facts** (corrects an earlier claim that deletion changes no
bytes): deleting a comment flips its record marker `fa fa` → `fd fd` and zeroes a constant
`0x3A` u32 at body offset 0 (a live-record tag shared by every live comment record); the text
and the rest of the body stay intact. Deletion also appends a free-list entry in the `0xFF`
free space after the last record, containing the freed record's offset and length as
**big-endian** u32s; creating a new comment physically reuses the freed slot and zeroes parts
of that free-list entry. None of this carries rung-attribution information.

## UDT L5K rendering (`_l5k_udt_literal`)

Mirrors `_udt_scalar_to_xml`'s own member-iteration rules (skip hidden and `BIT` members, same
declaration order) but emits an L5K array literal instead of XML: `"[1,0,0,...]"` for a scalar
struct, `"[[...],[...],...]"` for an array of structs, recursing into nested
structs/arrays/string-family members. Shares `_l5k_prim_literal()` (BOOL/BIT → `"2#0"`/`"2#1"`,
REAL/LREAL → `_l5k_real_literal()`, else plain decimal) with the primitive-array literal builder.
Verified against a real 25-element UDT array tag (`To_Skip[25]`): every element's L5K literal
matches Studio 5000's own `<Data Format="L5K">` content exactly.

## Initial-value decoding offset bugs (`_read_tag_initial_value`)

Two separate, serious bugs were found here in the same investigation (verifying `export_routine()`
imports against a project's actual tag values) — both affected the decoded initial value of
primitive tags, one for arrays and one for scalars. **If you ever see a primitive tag's decoded
value look wrong, this function is the first place to check**, and don't trust a "looks
plausible" value without comparing against real Studio 5000 ground truth — both of these bugs
produced plausible-looking (but wrong) values for many tags before being caught.

**1. BOOL array bit-packing.** Every array element was read at its own naive per-element byte
offset (`offset + i * elem_size`). This is correct for every primitive type *except* BOOL/BIT
arrays, which Rockwell bit-packs 32 bits per 4-byte DWORD — the same packing `_get_type_size()`
already accounts for when *sizing* a `BOOL[N]` array (`ceil(N/32)*4`), but this function was
never updated to match, and silently returned a raw packed byte value (e.g. `32`) instead of the
correct `0`/`1` bit for every element of every BOOL array tag. Fixed by reading the correct DWORD
(`offset + (i // 32) * 4`) and extracting bit `i % 32` for BOOL/BIT arrays specifically. Verified
against a real 256-element array tag: all 256 values now match Studio 5000's own export exactly.
Covered by a synthetic unit test (`test_read_tag_initial_value_bool_array_bit_packing`) since the
small fixture has no BOOL array tags.

**2. Scalar offset was simply wrong (0x19E instead of 0x1A2).** This was caught as a *direct
follow-on* to fix #1 above, and turned out to be much bigger: after fixing the array case,
`SecFlasher` (a scalar BOOL) still decoded as `1` when the real project value is `0` (confirmed
consistently across two real Studio 5000 exports taken hours apart from an offline, unchanging
project copy). Root-caused by comparing raw bytes for `SecFlasher` against `Always_Off` (a tag
that by convention must always be `0`) — both shared an *identical* 419-byte boilerplate
data-table record, with byte `0x19E == 1` for **both**, proving `0x19E` was never actually each
tag's own value at all, just incidental template/boilerplate data that happens to often be
nonzero. Systematically verified against the real project: comparing all 758 controller-scope
scalar BOOL tags and 812 scalar DINT tags against Studio 5000's own values (from a real
full-project L5X export), the old offset (`0x19E`) matched only 21.4% (BOOL) / 2.8% (DINT) of the
time, while the array offset (`0x1A2`) matched **100% for both** — there was never a real
scalar/array distinction; `0x1A2` is simply where the data-table's value region always starts.
This affected the decoded initial value of every scalar primitive tag project-wide (BOOL, DINT,
REAL, etc.), not something specific to one tag or type. Fixed by removing the scalar/array offset
distinction entirely — always read from `0x1A2`. Covered by
`test_read_tag_initial_value_scalar_uses_0x1a2_offset` (a decoy-vs-real value at each offset in a
synthetic blob) plus a correction to `test_scalar_primitive_tag_xml_shape`'s own expected value,
which was itself a casualty of this bug (never independently verified against real ground truth
for the small fixture, just whatever the wrong offset happened to produce).

## REAL/LREAL NaN and Infinity rendering (`_l5k_real_literal`/`_decorated_real_literal`)

Found while attempting a full whole-project `to_xml()` export of a large real project for the
first time (previously only individual routines/tags had been spot-checked) — it crashed
entirely with `ValueError: not enough values to unpack` in `_l5k_real_literal`. Root cause: a
handful of real REAL/REAL[] tags in that project (uninitialized, never written) decode to
NaN/Infinity, and Python formats these as bare `"nan"`/`"inf"` (no `"e"` to split on), which
`_l5k_real_literal` assumed would always be present. **This affected every non-finite REAL value
project-wide, and made whole-project export impossible for any project containing one** — not a
cosmetic issue.

Confirmed against that same project's own Studio 5000 L5X export (it has 6 such tags: one
`REAL[12]` array with `Infinity` in one element, several scalar `REAL` tags with `NaN`) that
Rockwell uses the classic MSVC CRT special-value convention, but the two output contexts
(`<Data Format="L5K">` vs `<Data Format="Decorated">`) render it differently, and a scalar
Decorated value renders differently again from an *array* Decorated value:

- **L5K** (`_l5k_real_literal`): the special-value label is left-padded with zeros into the same
  8-character mantissa slot a normal number occupies, then the usual `e+000` exponent is still
  appended: `"1.#QNAN000e+000"` for NaN, `"1.#INF0000e+000"` for +Infinity.
- **Decorated, scalar** (`_decorated_real_literal(..., in_array=False)`): the bare label with no
  padding/exponent — confirmed `"1.#QNAN"` for NaN; `"1.#INF"` for Infinity is inferred by direct
  symmetry (not independently observed in this project, no scalar Infinity tag existed to check).
- **Decorated, array element** (`_decorated_real_literal(..., in_array=True)`): a genuinely
  different, truncated value — `"1.$"` for the one case observed (+Infinity) — this is a real,
  reproducible quirk/bug in Studio 5000's *own* array-element exporter (verified byte-for-byte:
  `<Element Index="[11]" Value="1.$"/>` in the real L5X), not something we're free to "fix" to be
  more sensible. Applied to NaN too since no counter-evidence exists and the truncation looks like
  a generic "any `#`-prefixed label gets mangled in this code path" bug rather than one specific
  to Infinity.
- Sign-prefixed forms (`-1.#QNAN...`, `-1.#INF...`, `-1.$`) and the classic MSVC `-1.#IND`
  indeterminate-NaN special case were not observed in this project (all 6 tags were positive-signed)
  and are inferred by symmetry only — revisit if a real negative-signed non-finite value is ever
  found to disagree.

Also applied `_decorated_real_literal` to UDT member REAL/REAL[] fields (`_udt_scalar_to_xml`),
which previously used bare `f"{val}"` (Python's full-precision float repr, e.g.
`"1.2999999523162842"`) instead of the short `.6g`-style form every other REAL value in the
codebase uses — likely a latent, separate fidelity bug beyond just the NaN/Infinity crash, though
not independently verified against a real nested-UDT-with-REAL-member sample.

Regression tests: `test_l5k_real_literal_nan_and_infinity_do_not_crash`,
`test_decorated_real_literal_scalar_nan`, `test_decorated_real_literal_array_infinity_matches_real_quirk`.

## Rung patch write-back (`patch_rungs`/`patch_sbregion_dat`)

This path (`acd/zip/write_dat.py`) had **zero test coverage** until it was manually exercised
against a real, large project and found to have two real bugs (both now fixed, with regression
tests in `test/test_patch_rungs.py`):

1. **Compression.** `patch_sbregion_dat()` used to return *decompressed* `SbRegion.Dat` bytes.
   `build_acd_bytes()`/`save_acd()` never compresses anything — it writes whatever is in
   `_raw_files` verbatim — so the patched file alone ballooned ~12x in a real project (1.08MB →
   13.8MB decompressed) and was stored as a plain, non-gzip stream while every other internal
   `.Dat`/`.Idx` file stays gzip-compressed. `patch_sbregion_dat()` now re-compresses before
   returning. Rockwell's own encoder was reverse-engineered by trial: `gzip.compress(data,
   compresslevel=1, mtime=0)` reproduces the **entire DEFLATE payload + CRC32 + ISIZE trailer
   byte-for-byte** against a real project's original `SbRegion.Dat` — the only remaining
   difference is the header's XFL/OS bytes (offsets 8-9 of the gzip stream), which are purely
   informational per RFC 1952 and don't affect decompression; they're patched to Rockwell's
   values anyway (`XFL=0x00`, `OS=0x0b`/NTFS) for a fully byte-identical no-op round-trip.
2. **Hex-ref formatting.** `_restore_tag_refs()` re-encoded `@HEX_OBJECT_ID@` tag-reference
   placeholders with `:X` (uppercase, no zero-padding). The real convention, verified by sampling
   20,710 real `@...@` refs in one project's `SbRegion.Dat`, is **exactly 8 hex digits,
   zero-padded, lowercase** (`:08x`), 0 of them uppercase. Using `:X` produced a
   numerically-equivalent but textually different reference, so even a true no-op patch (rung
   rewritten to its own existing text) silently produced different bytes.

With both fixes, a no-op patch (rewrite a rung to its own current text) now reproduces the
**exact original ACD container, byte-for-byte** — verified against both the small test fixture
(`test_patch_rungs.py`) and a large real-world project manually. This is the strongest available
confidence check for this write path, since it proves the full decompress → re-encode →
recompress cycle is lossless and matches Rockwell's own encoding conventions closely enough to
be indistinguishable from the source, without needing an actual Studio 5000 install to verify.

**Still unverified: whether a real, non-no-op edit (i.e. actually different rung text) produces
a file real Studio 5000 accepts.** Two separate open questions remain, neither resolved yet:
- Without a registered `FileInfo.Dat` signing key (see `acd/integrity/`), any mutation leaves
  the checksum stale; whether Studio 5000 actually enforces/checks this on open (as opposed to
  only the SDK) is untested — **three purpose-built experiment ACDs now exist to answer this;
  see the next section**.
- Even with a valid key, nobody has confirmed a `save_acd()`-produced, mutated ACD actually
  opens correctly in real Studio 5000 — that would require an actual test against the real
  software, which hasn't been done as of this writing.

## ACD write-back: what a real Studio 5000 save/edit actually writes (three-way diff)

Reverse-engineered from three sibling saves of the same large real project (in
`...\PLC_Claude_Code\Bethel_Planer\source\`): `BPM_TrimmerSorter_20260707.ACD` (original),
`..._STUDIO_NOOP.ACD` (opened in Studio, saved unmodified), `..._STUDIO_EDITED.ACD` (opened,
one edit, saved). `Version.Log` (plain text, one `"...: Saved - V32.04"` line per save)
revealed the EDITED save actually happened *before* the NOOP save — both are independent
children of the original, so `noop→edit` isolates exactly one edit's footprint on an identical
save-normalization baseline. Compare **decompressed** contents (every internal `.Dat`/`.Idx`
is gzip-compressed in the container); many `.Dat` files have page-quantized sizes (multiples
of 65535) that stay constant while content changes.

**The identified edit** (recovered purely from the binary diff): rung `0x17c4b9bd` in routine
`Flasher` had `OTE(BitFlags[21])` appended, and — incidental leftover of the same editing
session — a new, unused controller-scope tag `BitsFlags` (note the extra "s": almost certainly
typed first, auto-created by Studio's inline new-tag flow, then corrected) was created under
`RxTagCollection`.

**Finding 1 — save-time compaction/GC exists but is NOT required on open.** A no-op resave
shrank `Comps.Dat` by ~581KB (19113→19097 records, dead `fd fd` records 151→142), dropped 372
stale `SbRegion.Dat` records, 54 `Nameless.Dat` records, etc. But the *original* (uncompacted,
dead-record-laden) file opens fine in Studio — that's where the NOOP/EDITED saves came from.
So a writer does **not** need to replicate compaction; it only needs to express its own delta
with consistent cross-file invariants.

**Finding 2 — the complete per-file footprint of the one rung edit (`noop→edit`)**:
- `SbRegion.Dat`: the rung's `Rung NT` record is **excised in place (bytes compacted out, not
  tombstoned) and the new version appended as the physically-last record**. Every other record
  byte-identical — including the rung's own 1065-byte `REGION AST` record (compiled form is
  NOT regenerated). Header: u32 at file offset 0 = (file length − 1), u32 at offset 8 =
  record-region length (both adjusted); `DatHeader` also has `no_records` at 0x14 and a
  second count at 0x18 (unchanged here: −1 removed +1 appended).
- `SbRegion.Idx`: ~10k tiny diffs — the B-tree entries store **absolute `.Dat` record offsets**
  which all rebase by the length delta after the excision point. Any length-changing `.Dat`
  edit MUST rebase its `.Idx` (our current `patch_rungs` does not — see experiments below).
- `Nameless.Dat`: the routine's compiled-artifact records are **deleted, not regenerated**
  (a 1740-byte compiled-body record and a 68-byte link record removed; a 56-byte list record
  keyed by the routine's object_id at body[8:12] rewritten shorter with its child references
  emptied). Net −2 records.
- `Comps.Dat`: 422 differing bytes in 13 regions, fully decoded:
  - the routine's own record: one byte at body[10] flips `0x03 → 0x00` (compile-state/"dirty"
    flag, matching the deleted compiled artifacts);
  - the controller's own record: an 8-byte FILETIME last-edit timestamp updated;
  - **new-object creation via free-slot resurrection**: a dead `fd fd` record (an old deleted
    tag `Test3dudt` — deleted comps records keep their full bytes, and *pointer* records get
    renamed to `$hex$` placeholder names like `$447f0b6a$`) is flipped to `fa fa` and
    overwritten with the new tag's record; same for its paired pointer record elsewhere;
  - a **free-list structure inside Comps.Dat** (same idea as the Comments.Dat free-list): a
    count field decremented (0x18→0x17) and the entry holding the resurrected slot's file
    offset — stored as a **3-byte big-endian** value inside a 10-byte entry — removed from the
    list (tail shifted up, last entry left duplicated as garbage);
  - `.Dat` header counts at file offsets 0x14/0x18: live-record count +1, free-record count −1;
  - two allocator/seed fields (one near the file header at ~0xc25 holding the most recently
    allocated object_id, one at ~0x4cce) updated.
  - Comps record body layout (relative to the 6-byte `fa fa`+u32len prefix): body[0:4] inner
    length, body[8:12] flags (body[10] = the dirty byte for routines), body[16:20] object_id,
    body[20:24] parent_id, body[24:] UTF-16LE name.
- `CanonicalSize.Dat`: a per-object table of `(0x0200 marker u32, canonical_size u32,
  object_id u32)` entries; the edited rung's size went `0x18 → 0x1c` (+4 for one added
  instruction).
- `RegnLink.Dat`: **header counter/timestamp only — zero record changes** (the rung kept its
  object_id and chain position); `RegnLink.Idx` byte-identical.
- `XRefs.Dat`: +3 records appended (header count at 0x14 `0xbbdf→0xbbe2`, count at 0x18 +1),
  one ~89-byte tail region rewritten with entries referencing the rung and routine ids —
  format still not reverse-engineered (`record_format` 132; `DbExtract` refuses it).
  `XRefs.Idx` grew by exactly one 0x3FFF page.
- Every `.Dat`/`.Idx` header also has a save-generation counter + unix-timestamp pair in the
  `[0x6c:0x74]` region that bumps on each save even when the file is otherwise untouched.
- `QuickInfo.XML`: the `CopyUID="..."` attribute value is regenerated per save.
  `OfflineChangelog.Dat`: a 4-byte counter. `Version.Log`: appends a `Saved - V<ver>` line.
  `FileInfo.Dat`: the 32-byte digest at [2:34] differs on every save (as expected).

**Finding 3 — experiment files for the FileInfo-enforcement question** (built with this
library from the NOOP baseline, in `...\Bethel_Planer\source\WriteBack_Tests\`; all three
verified to re-parse correctly with our own reader; none has a valid FileInfo digest):
- `EXP0_deadrecord_byte.ACD` — one byte inside a *dead* Comps record's leftover text
  (`Test3dudt`→`Xest3dudt`); semantically invisible. If Studio opens it → the FileInfo
  checksum is **not** enforced on open (nothing else can be blamed).
- `EXPA_comment_letter.ACD` — one letter changed in place, same length, in a live rung
  comment (`VAB_MainProgram/R02_Flash` rung 3: `Bit flash X/5`→`Bat flash X/5`). If it opens
  AND shows "Bat" → same-length in-place `Comments.Dat` edits are viable end-to-end.
- `EXPB_rung_append_ote.ACD` — the same rung edit Studio itself made, but via our
  `patch_rungs()` (in-place, length-changing), deliberately leaving `SbRegion.Idx` offsets,
  Nameless compiled artifacts, the Comps dirty flag, `CanonicalSize`, and `XRefs` all stale.
  If it opens and shows the new rung → Studio's loader is lenient about all of that; if it
  fails, add the bookkeeping pieces one at a time (start with `SbRegion.Idx` rebasing —
  the most likely hard requirement).

**RESULT — `FileInfo.Dat` IS enforced by Studio 5000 on open (definitive).** The user opened
`EXP0_deadrecord_byte.ACD` in real Studio 5000: it was **rejected** with *"File is not
recognized as a valid project file"* — a container-level rejection that fires before any
project-content parsing. This is the cleanest possible proof, because EXP0 is provably NOOP
with exactly ONE semantically-dead byte changed:
- A zero-edit passthrough (read the NOOP container's raw file blocks, rebuild via
  `build_acd_bytes`, no changes) reproduces the NOOP `.ACD` **byte-for-byte** — so the
  container writer is not the culprit.
- Recompressing an unchanged `Comps.Dat` with `gzip.compress(level=1, mtime=0)` + XFL/OS
  patch reproduces the original compressed stream **byte-for-byte** — so the recompression
  is not the culprit.
- EXP0's only change vs NOOP is one byte inside a dead `fd fd` record (invisible to parsing)
  and, consequently, a now-stale `FileInfo.Dat` digest. NOOP itself opens; EXP0 doesn't.
  The stale digest is the only remaining difference → `FileInfo.Dat` is enforced on open.

**Consequence: the entire raw-binary write path is blocked on recomputing `FileInfo.Dat`,
which needs the HMAC key.** EXPA/EXPB were not worth testing after this — they change *more*
than EXP0, so they can only also fail at the same gate; they become useful only once files
can be correctly re-signed. The key situation, corrected from earlier notes:
- `acd/integrity/fileinfo.py` implements the (hypothesised) construction:
  selector `02 00` = `HMAC-SHA-256(key, sha256(container − FileInfo.Dat))`, key = 32 bytes.
  This project's `FileInfo.Dat` is selector `02 00` (header bytes `02 00 …`), so it needs the
  32-byte key.
- **The key is a per-Studio-version constant, NOT a per-project brute-force target** (earlier
  task framing was wrong on this). Per our own module docs it is extractable from a legitimate
  Studio 5000 install. It is not shipped with this library and is not present anywhere in the
  repo, tests, or environment (`ACD_FILEINFO_KEY` unset).
- **The HMAC construction in `fileinfo.py` has never been validated against a real key** — the
  integrity tests only check self-consistency with dummy keys; the real end-to-end test is
  gated behind the unset `ACD_FILEINFO_KEY`. So even once a key is obtained, the algorithm
  itself is still an unconfirmed hypothesis. We hold three genuine Studio-signed containers
  (orig / noop / edit, all same project, all with *different* valid `FileInfo.Dat` digests):
  the instant a candidate 32-byte key is available, verify it against all three with
  `verify_fileinfo()` — a correct key must match all three, which simultaneously confirms both
  the key and the algorithm.

**Open paths from here** (none pursued yet, pending a decision):
1. Obtain the 32-byte key from the user's Studio 5000 install (DLL/static extraction on their
   machine — not installed on the dev machine). Biggest unlock: if the algorithm is right,
   `save_acd()` re-signs correctly and EXP0/EXPA become the next probes.
2. Native-import escape hatch (mirrors `export_routine()` → Studio "Import Routine"): sidesteps
   `FileInfo.Dat` entirely for the edits it covers. Likely the pragmatic path for actually
   getting tag/rung/comment edits into a project without solving the key.
Outcome of any Studio re-test after re-signing not yet recorded — update here when known.

## Testing gotchas

- `test/conftest.py` chdir's into `test/` for the whole session — needed because many tests
  reference `resources/CuteLogix.ACD` via `"../resources/..."` relative paths. If you add a new
  test file, you can rely on cwd already being `test/`.
- Some AB module DataType names contain `:` (e.g. `CHANNEL_DI_TIMESTAMP:O:0`), which is invalid
  in Windows paths — anything that turns a comp name into a filename/directory (see
  `DumpCompsRecords` in `elements.py`) needs to sanitize it first.
- The full suite (`pytest` from repo root) should show `77 passed, 2 skipped, 0 failed`. If you
  see `FileNotFoundError`s or `PermissionError`s across many unrelated test files, first check
  you're not missing the `conftest.py` chdir behavior or that a previous test crashed and left
  a locked SQLite file/build artifact behind.
