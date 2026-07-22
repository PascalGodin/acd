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

`ModuleBuilder` reads each I/O connection's Type (Input/Output/DiagnosticInput/MotionSync/
StandardDataDriven/...) from a real u16le CIP enum at raw offset 90, and its RPI (microseconds)
from a u32le immediately after it at offset 92 — not from the connection's name. The connection's
own name (e.g. `"Standard"`) gives **no reliable signal**: in a real project, most `"Standard"`
connections were `Type="Output"` while a couple were `Type="Input"`. If you ever need to
reverse-engineer a similar "guess from name" situation, the method that worked here: collect
every real `<Connection Name=... RPI=... Type=...>` from a project's own L5X export, match each
one to its raw ACD record (RPI is a convenient unique-ish key to match on, but not always unique
project-wide — scope the match to the owning Module too, since the same connection name/RPI pair
can recur across many different module instances), then scan every byte offset for one whose
value is constant within each `Type=` group and differs across groups — a real 1-byte/2-byte enum
will show up as a clean, zero-exception discriminator immediately.

`_CONNECTION_TYPE_BY_CODE` currently has 5/6/7/23/48; unrecognized codes log a `log.warning()` and
fall back to the old name heuristic rather than silently guessing — check the logs if you ever
suspect a module's connection Type is wrong. **Code 48 (`StandardDataDriven`) was added after a
user hit the warning on a real project** (module `MCC116_Output`, connection `OutputData`) — a
whole-project cross-check (every one of 205 real connections in that project, matched by
module+name+RPI between the ACD's raw bytes and the project's own L5X export) found all five
codes hold with zero exceptions, 134 of the 205 being code 48 alone. This case is a particularly
strong confirmation of the "don't trust the name" warning above: the exact same code 48 appears on
connections literally named both `"InputData"` and `"OutputData"` in this one project, meaning the
old name-based fallback silently guessed opposite answers ("Input" vs "Output") for two
functionally-identical connections depending only on which one happened to be in front of it —
neither guess was actually `StandardDataDriven`, so both were wrong, just not usually visible as
a hard error since callers mostly only care whether IO is input-like or output-like.

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

**A second, distinct "not a real line" case, found via a real false-positive routine diff**: some
`0x01000002` records — same record type as genuine source lines — carry sequence number
`0xFFFFFFFF` (u32 sentinel) instead of a real ordinal. These are a shadow/compiled copy of part of
the routine's logic (observed: the ladder-equivalent body backing a `for`-loop's semantics,
`ADD`/`CMP`/`MOVE`/`SIZE`/`SUB` instruction-call syntax, not valid ST), not source Studio ever
displays. `_st_routine_lines()` used to sort all lines by `(seq, text)` with no sentinel check, so
these all-`0xFFFFFFFF`-seq records tied on the primary key and fell back to sorting by their own
(still-unresolved) `@hexid@` text — which differs between any two saves of the *same* routine
simply because each save assigns different object ids to the same tags, producing a spurious,
save-dependent order for lines Studio never even shows. Root-caused by comparing the exact same
routine (`S01_Next_Board_Search`) across two real saves of one project that a user reported as
"identical" despite our tool reporting 4 differing lines — after excluding `seq==0xFFFFFFFF`
records, the remaining (real, numbered) lines were byte-for-byte identical between the two saves,
confirming both the fix and that the excluded records were never genuine source. Fixed by skipping
`seq == 0xFFFFFFFF` records entirely in `_st_routine_lines()`.

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

## Native-import escape hatches for write-back (routine L5X is the one active mechanism)

Because `FileInfo.Dat` is enforced on open (see "ACD write-back"), the sanctioned way to get an
edit into a project is to hand Studio 5000 a file it imports through its own UI — Studio then
does the binary write + re-sign. **`export_routine()` (partial L5X via "Import Routine") is the
one actively-developed, verified-end-to-end mechanism** — it now covers both rung edits (its
original purpose) and tag-level edits (description/value), the latter via the routine-carrier
trick below, per user direction. CSV "Import Tags" was explored as an alternative and is kept
below for reference, but **deprioritized**: the user does not want to rely on CSV. A standalone
single-tag partial-L5X exporter (via Studio's "Import Component") was also drafted early in this
investigation but removed before merging — its wrapper was never calibrated against a real
Studio single-tag export, and the routine-carrier approach superseded the need for it entirely.

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

**CONFIRMED WORKING END-TO-END, via a real tag-description edit imported into real Studio 5000.**
This is the first fully successful real-world round-trip of the routine-carrier write-back
mechanism: editing `LsRead_Start`'s description (a controller-scope tag, referenced in
`Continuous/LS_Read`) and importing the exported routine via Studio's real Import Routine
feature. Getting there took two rounds of real import failures, then a full ground-truth
comparison against the user's own native `LS_Read` export that closed out every remaining gap —
both rounds found real, general, previously-undiscovered bugs, not edge cases specific to one tag:

- **Round 1**: `Error: ... Failed to set the 'Data' property (Data type mismatch...)` on
  `Test_Bit_DINT`, plus a warning on `Luci_NOBRD`. See "Initial-value decoding offset bugs" below
  for the full root-cause and fix of both (a genuine one-element-array collapsing to a scalar, and
  TIMER/COUNTER-style built-in structs losing their BIT-overlay status members).
- **Round 2** (after fixing round 1): `Error creating 'Tag[@Name="Remote_TrimmerIO:0:I"]' (Invalid
  name.)`. Root cause: an Alias tag referenced by the routine (`LngthLmt_16ft`) has
  `AliasFor="Remote_TrimmerIO:0:I.Data.7"` — an I/O tag target. The existing alias-target
  base-name resolution correctly identified `Remote_TrimmerIO:0:I` as "referenced," but
  `export_routine()` then rendered that literal I/O `Tag` object as its own `<Tag>` element.
  Fixed by filtering `controller_tags`/`program_tags` through the existing `Tag._l5x_exclude`
  rule (I/O tags never appear as standalone `<Tag>` elements in a real full-project export
  either), which `export_routine()`'s own ad-hoc tag-list building had never applied.

**After round 2 succeeded, the user provided a real Studio 5000 "Export Routine" of `LS_Read`
itself** — ground truth for the exact same routine, letting every remaining discrepancy be found
by direct comparison rather than waiting for the next import attempt. A naive string diff falsely
flagged all 64 common tags as different (attribute order and `<Comments>` child order aren't
semantically significant but a plain text diff treats them as such); a proper XML-tree-based,
attribute-order/comment-order/L5K-whitespace-independent comparison found five more real,
previously-undiscovered bugs, all now fixed and reverified to an **exact match — zero differences
across every Tag/DataType/Module/AddOnInstructionDefinition/Routine**:

1. A UDT tag's `<Structure DataType="...">` used the internal all-uppercase lookup key directly
   instead of the real DataType's own declared casing (a project UDT named `Timing` rendered as
   `TIMING`) — `_udt_array_to_xml` already looked this up correctly; the scalar-UDT branch in
   `Tag.to_xml()` never did.
2. A top-level UDT-array tag's own `<Array>` element incorrectly carried a `Name="..."`
   attribute — real Studio never has one there (only nested `ArrayMember`s do), the same
   already-fixed convention for primitive arrays, never applied to `_udt_array_to_xml`.
3. A UDT member's own declared `Radix` (e.g. `"Binary"`) was ignored in favor of a generic
   per-type default, and `Radix="Binary"` members never got Rockwell's `"2#0000_..._0000"`
   grouped-binary-literal formatting at all.
4. `_referenced_tag_names()` wrongly matched a token immediately followed by `"("` as a tag name
   (that position is always an instruction/AOI/JSR mnemonic in RLL syntax) — a real tag literally
   named `AFI` collided with the `AFI()` (Always False Instruction) mnemonic used elsewhere in the
   same routine, pulling in an unrelated tag as context.
5. The same function wrongly matched a token immediately preceded by `"."` (Rockwell address
   syntax: `.` always introduces a MEMBER name, e.g. `Length_In` in
   `ToTrim[Timing.Length_Lug].Length_In`, never a fresh tag reference) — a real, unrelated tag
   named `Length_In` got the same treatment.
6. An Alias's own I/O-tag target needs its *owning Module(s)* referenced too (the rack
   `Remote_TrimmerIO` AND the module occupying its slot 0, `Trimmer_Inputs`) — resolved via the
   same rack/slot rule already verified for direct rung references, just never fed the
   alias-resolved I/O tag names before.

See "Initial-value decoding offset bugs" and "UDT L5K rendering" below for full detail on each.
This routine happens to exercise nearly every dependency class at once (tags, UDTs, TIMER/COUNTER
built-ins, aliases, I/O tags via both direct and alias-target reference, Modules via both direct
and rack/slot addressing), so this is a strong verification result — but it's still one routine;
treat "verified" as "verified for the patterns this routine exercises," not "every possible RLL
construct."

**Final result**: `LIVE_TEST_LsRead_Start_desc_v5.L5X` (same project/tag/routine, all six fixes
applied) imported into real Studio 5000 with the exact same behavior as importing Studio's own
native `LS_Read.L5X` export — no errors, only the expected/normal "tag exists in project only"
messages for I/O tags (see below), and the tag description overwrite applied successfully. The
routine-carrier mechanism is proven end-to-end for the tag-description-edit case.

**Second edit class also confirmed end-to-end: creating a brand-new tag from scratch** (not
editing an existing one). Test: a controller-scope `Tag` object constructed directly in Python
(never existing anywhere in the ACD, name `ACDTOOLS_NEW_TAG_TEST`, `DINT`, value 42, with a
description), appended to `project.controller.tags`, referenced via one new rung appended to
`LS_Read` (`XIC(Always_Off)MOV(42,ACDTOOLS_NEW_TAG_TEST);` — guarded by `Always_Off`, a tag
conventionally always 0, so the rung can never execute; it exists purely so
`_referenced_tag_names()` picks up the new tag as context). Exported via the same
`export_routine()` path and imported into real Studio 5000 successfully, confirmed by the user
("everything worked as expected") — Studio created the new tag and added the new (dead) rung with
no errors. Both core edit classes the routine-carrier mechanism needs to support (editing an
existing tag's fields, and introducing a brand-new tag) are now proven end-to-end against real
Studio 5000, using the exact same code path with no special-casing required for "new" vs
"existing" — Studio itself decides create-vs-overwrite based on whether the name already exists
in the project.

**Confirmed normal, not a gap**: Studio's own Import Routine comparison shows "tag exists in
project only" for `IO042:I` and `Remote_TrimmerIO:0:I` (I/O tags backed by `AB:` module-defined
datatypes) when importing our file — but the user independently confirmed Studio's own *native*
export of `LS_Read` produces the **identical** message when imported back. This isn't something
our exporter is missing; it's inherent to how Studio's own partial/context export mechanism
handles these tags — the `<Module Use="Reference">` stub (name only, no definition) is all that's
needed, since Studio regenerates the I/O tag itself from the *live project's own* already-existing
Module/connection configuration on import, rather than needing an explicit `<Tag>` or full
`<Module>` definition in the partial file. Confirms `Tag._l5x_exclude` correctly keeping these out
of the `<Tags>` section entirely (see the "I/O tag exclusion" fix above) matches real Studio
behavior, not just avoids an error.

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

12. **UDT/AOI/Module/called-Routine dependency closure — SOLVED, verified exact against a real
    Studio "Export Routine".** The user clarified the intent directly: replicate what Studio's own
    routine export does, "including UDT, AOI, MODULES, Etc" as transitive dependencies. A real
    export of `Motors/Main_Motors` (`BPM_TrimmerSorter_20260709.ACD`, obtained from the user) —
    whose rungs call `AOI_RPMtoFPM(TestFPM,VFD_P_INTBL2:I.OutputFreq)`, reference
    `Local:12:I.Data.0`, and `JSR(Infeed_LandingTable,0)` — exercised every open question in one
    file. Diffing our generated output against it (element vocabulary, `Use=` values, AND full
    top-level child order) came back an **exact match** except for one unrelated, separately-scoped
    gap (`<DefaultData>`, see below). Concretely:
    - `referenced_data_types` was single-level (a UDT containing another project UDT as a member
      wouldn't pull that inner UDT in) and `project.controller.aois` was never consulted at all —
      an AOI instruction call's instance tag has its AOI name resolvable through the tag's own
      `data_type` field (here `TestFPM.data_type == "AOI_RPMtoFPM"`) exactly like a UDT tag, but the
      AOI collection was simply never searched. `_resolve_type_closure()` (`acd/api.py`) now does a
      proper worklist-based transitive closure over both `project.controller.data_types` and
      `project.controller.aois` (following a UDT's own members, and an AOI's own parameters/local
      tags, for further nested dependencies).
    - `<AddOnInstructionDefinitions Use="Context">` (individual `<AddOnInstructionDefinition>`
      elements carry no `Use=`, matching the Tag/DataType convention) sits right after
      `</Modules>` and before `<Tags Use="Context">` — confirmed exact against the real export's
      full top-level child order: `DataTypes, Modules, AddOnInstructionDefinitions, Tags,
      Programs`.
    - **Module dependencies**, previously unhandled entirely (I/O tag names contain a `:` and
      aren't picked up by the plain-identifier `_referenced_tag_names` scan), are resolved by
      `_referenced_modules()` via a real Logix addressing-convention rule, verified exact: a
      2-part I/O reference (`ModuleName:Type...`, e.g. `VFD_P_INTBL2:I` — a directly-addressed
      Ethernet device) needs only that module; a 3-part reference
      (`ModuleName:SlotNumber:Type...`, e.g. `Local:12:I` — rack/chassis-slot addressing) needs
      BOTH the chassis module itself (`Local`) AND whichever module occupies that slot (found via
      `Module.parent_module == chassis_name and Module._slot == slot_number` — here `AC_IN_12`,
      slot 12 of `Local`). The real export's `<Modules Use="Context">` contained exactly
      `{AC_IN_12, Local, VFD_P_INTBL2}` for this rung, matching the rule precisely; critically, it
      did **not** include `Ethernet2` (`VFD_P_INTBL2`'s own `parent_module`) — a directly-addressed
      module's parent is not walked, only a slot-occupant's rack is. Each `<Module>` is an empty
      `Use="Reference"` stub (bare name, no definition content), a new `Use=` value distinct from
      `Context`/`Target` seen anywhere else in this wrapper. **Caveat**: verified against exactly
      one rack + one direct Ethernet device; bridged/remote racks (ControlNet, DeviceNet, a remote
      Ethernet chassis through an adapter) haven't been exercised.
    - **Routine dependencies**: a target routine calling another routine in the same program via
      `JSR` needs that routine included too — as an empty `<Routine Use="Reference" Name="...">`
      stub (no rung content), positioned *before* the real `<Routine Use="Target">` inside the same
      `<Routines Use="Context">` wrapper. `_referenced_called_routines()` resolves this via a
      `JSR\s*\(\s*(name)` scan against the *same program's* own routines (JSR can't cross program
      boundaries in native ladder logic). Verified exact against the real export
      (`Infeed_LandingTable` stub before `Main_Motors` target).
    - All of the above are purely additive/conditional (only emitted when actually referenced),
      confirmed to leave the earlier, already-verified no-AOI/no-Module/no-JSR case byte-for-byte
      unaffected.
    - **A genuine, general bug found and fixed along the way** (not AOI/Module-specific):
      `_decorated_real_literal()`'s `"%.6g"`-style formatting silently drops the decimal point for
      an exact whole-number float (`f"{1800.0:.6g}"` → `"1800"`, not `"1800.0"`) — this went
      undetected in every earlier verification sample because none happened to include a REAL
      value that reduces to a whole number. Confirmed against four real values on the AOI instance
      tag `TestFPM` (`MotorRPM=1800.0`, and three sheave/sprocket diameters at `6.0`/`12.0`/`14.0`,
      all rendered by real Studio with an explicit `.0`). Fixed by appending `.0` whenever the
      formatted string has neither a decimal point nor scientific notation. This affects every
      Decorated-format REAL/LREAL rendering project-wide (plain tags, UDT members, AOI members),
      not just AOI structures.

    **Separate, deeper, NOT-yet-solved gap found via the same real `TestFPM` comparison — AOI
    *instance value* decoding is measurably wrong, independent of the dependency-declaration fixes
    above**: comparing our rendered `TestFPM` tag (`DataType="AOI_RPMtoFPM"`) against the real
    export's byte-for-byte:
    - Two members are silently missing from both our `<Data Format="L5K">` and `<Structure>`
      output: `EnableIn`/`EnableOut` (both real BOOL members present in Studio's own output, not
      BIT-overlay pseudo-members). The underlying synthetic "DataType" that backs an AOI instance's
      value decode (found via `all_data_types_map[dt.name.upper()] = dt` in `ControllerBuilder`,
      which inserts *every* `RxDataTypeCollection` entry regardless of `cls`, not just `cls ==
      "User"` — meaning an AOI's own instance-data-shape record lives there under the AOI's name,
      separately from the AOI's own `AddOnInstructionDefinition`/Parameters) appears to mark these
      two members `hidden`, and `_udt_scalar_to_xml`/`_decode_single_udt_element`'s generic
      "skip if hidden" rule (correct for real UDT BIT-overlay members) incorrectly drops them here
      too. Whether that's a raw-byte misread of the hidden flag for this specific case, or a
      genuine semantic difference (AOI system-defined params need to never be skipped regardless
      of a hidden flag) is not yet determined.
    - The real `<Data Format="L5K">` literal has **17 comma-separated values**; ours has only 8
      (matching the 8 members we do emit). Real Decorated `<Structure>` only shows 10 named
      members (`EnableIn`/`EnableOut` + our 8) — still short of 17, meaning L5K encodes something
      beyond even the full named-Parameter list, quite possibly the AOI's own `LocalTags` (private
      storage) packed into the same flat blob, plus the leading value `3` in the real L5K array
      that doesn't map to any named Parameter or LocalTag at all (possibly an internal AOI
      execution-state field Studio never exposes as a named member).
    - `<Structure DataType="AOI_RPMtoFPM">` in real output preserves the AOI's own mixed-case name;
      ours renders `AOI_RPMTOFPM` (all-caps) — traceable to `display_name` falling back to the
      already-uppercased lookup key when the synthetic backing DataType's own stored `.name` isn't
      the properly-cased one.
    - `<DefaultData Format="L5K">`/`<DefaultData Format="Decorated">` (an AOI's own default value
      for a `Parameter`/`LocalTag`, e.g. `MotorRPM`'s default `0.0`) is never emitted at all —
      `Parameter`/`LocalTag` dataclasses don't even have an `_initial_value`-equivalent field yet,
      so this needs new binary reverse-engineering (where an AOI *definition's* own default values
      live in Comps.Dat, analogous to but distinct from `_read_tag_initial_value`/
      `_decode_udt_initial_value` for a tag *instance's* current value) before it can be
      implemented at all — not attempted this session.
    None of this blocks the dependency-declaration fixes above (which only need the AOI/Module/
    UDT/routine *names* to be correctly identified and included, not their values decoded
    correctly) — but any future work rendering an AOI-typed tag's own current value, or an AOI's
    own parameter/local-tag default values, should start here rather than assume the existing UDT
    value-decode pipeline already handles AOIs correctly.

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

**Same bug, second location, found much later via the tag-value-blob-offset investigation above.**
This fix was only ever applied to `_read_tag_initial_value` (a top-level *primitive tag*'s own
value). `_decode_single_udt_element` — which decodes a UDT's own members, including array-typed
ones — has a separate, parallel array-decode loop that never got the equivalent fix: a BOOL array
**member** inside a UDT (e.g. `Encoder`'s `Ons`, `BOOL[32]`) was still read one raw byte per
element (`elem_size = _get_type_size("BOOL", ...) = 1`), not from its shared packed DWORD. Found
via a real Studio 5000 "Tag Name Collision / Data Compare" dialog: `EncTrm.Ons[5]` decoded as `1`
instead of the real `0` — only **one** of 32 elements differed, since reading bit 0 of the wrong
byte coincidentally reproduces the correct packed bit for most positions, making this an easy bug
to miss without checking every element against real ground truth. Fixed the same way as above
(read the correct DWORD, extract bit `i % 32`), scoped to array members whose own `data_type` is
`BOOL`. Covered by `test_decode_single_udt_element_bool_array_member_bit_packing`. Verified against
the real project: `EncTrm.Ons` and `Trim_Decision.Ons` (both `BOOL[32]`) now decode to all zeros,
matching Studio's own "Existing Value" exactly.

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

**3. A genuine one-element array (`Dimensions="1"`) silently collapsed to a scalar**, causing a
real, reproducible Studio 5000 import rejection ("Data type mismatch") — found via the first
actual live end-to-end test of the routine-carrier write-back mechanism (see "Native-import
escape hatches" above): editing `LsRead_Start`'s description and importing the carrying routine
via Studio's real Import Routine failed, not on the intended edit, but on an unrelated context
tag (`Test_Bit_DINT`) swept in because the routine's own rung text also references it.
`_read_tag_initial_value`/`_decode_udt_initial_value` both collapsed to a bare scalar whenever
`n_elements == 1`, unable to distinguish "no dimensions declared at all" (a true scalar) from
"genuinely declared as a 1-element array" (`Dimensions="1"`, `n_elements` also 1) — both cases
hit the same `if n_elements == 1: return values[0]`. This produced an internally inconsistent
`<Tag>`: `Dimensions="1"` in the attributes (correctly derived from the raw record) alongside a
scalar `<DataValue>` in `<Data Format="Decorated">` (from the collapsed value) instead of the
`<Array><Element Index="[0]" .../></Array>` shape Studio expects for a declared array — exactly
the mismatch Studio's importer rejected. Fixed by threading an explicit `is_array` flag (derived
by the caller from `dimensions is not None`, not from `n_elements`) through both functions, only
collapsing to scalar when `not is_array`. Verified: `Test_Bit_DINT` (a real project tag with this
exact shape) now renders as `<Array Dimensions="1">...<Element Index="[0]".../></Array>`.

**4. Rockwell built-in structured types with BIT-overlay status members (TIMER, COUNTER, likely
CONTROL) were missing those members entirely from both `<Data Format="L5K">` and
`<Data Format="Decorated">` whenever the tag had a real decoded value** — the second bug the same
live import test surfaced: a `COUNTER`-typed tag (`Luci_NOBRD`) got a hard Studio import **error**
("Data does not have enough data type members"), not just a warning, because its `<Structure
DataType="COUNTER">` showed only `PRE`/`ACC`, missing `CU`/`CD`/`DN`/`OV`/`UN` entirely.
Root cause, found by comparing member metadata: TIMER/COUNTER's own hidden `Control` DINT member
(`hidden=True`) is where `EN`/`TT`/`DN` (or `CU`/`CD`/`DN`/`OV`/`UN`) actually live, as BIT-overlay
pseudo-members (`data_type=="BIT"`, `hidden=False`, `bit_number`+`target="Control"`) — but the
generic decode/render skip rule (`if member.hidden or member.data_type == "BIT": continue`),
correct for a UDT's own genuine bit-overlay members that shouldn't be independently serialized,
was ALSO unconditionally dropping BOTH the hidden backing value (needed for L5K) AND the BIT
pseudo-members themselves (needed for Decorated) for these built-in types. **This also falsified
last session's own "exact match" claim for the AOI/Module/JSR dependency-closure verification** —
that check only compared element vocabulary and top-level section order, never this deep into a
specific tag's own member content, so it did not catch that a TIMER tag in that very same
calibration file (`DelayedControlPowe`) was already missing `EN`/`TT`/`DN` the whole time; a
narrower "vocabulary + order" diff is not sufficient evidence for "byte-exact," a lesson worth
remembering for future verification passes. Fixed across four call sites with a decode/render
split, not a single shared skip rule: `_decode_single_udt_element` now decodes hidden non-BIT
members normally (first pass) and derives each BIT-overlay member's own value by extracting its
`bit_number` from the already-decoded `target` sibling (second pass) — Python's `>>` on a negative
int is sign-extending, so this works correctly for a negative packed `Control` value without special-
casing; `_l5k_udt_literal` now skips only BIT members (hidden members' raw value IS part of the L5K
literal); `_udt_scalar_to_xml` now skips only hidden members (BIT-overlay members DO get their own
`<DataValueMember DataType="BOOL">`); `_get_type_size` now skips only BIT members when computing a
struct's total byte size (a hidden member's own byte extent still counts). Verified against real
Studio ground truth for two different built-in types: `DelayedControlPowe` (TIMER) now renders L5K
`[-1607863227,3000,3000]` and Decorated `PRE/ACC/EN=1/TT=0/DN=1`, matching a real Studio export
exactly; `Luci_NOBRD` (COUNTER) now renders all 5 status bits (structurally verified, though no
independent real-Studio ground truth exists for this specific tag's own `Control` value).

**5. Arrays need offset 0x1A2 + 2, not 0x1A2 — a major, project-wide bug, found via a real Studio
5000 import error that turned out to be unrelated to what was actually wrong. SUPERSEDED — see the
"RESOLVED" note in "UDT total size must round up to a multiple of 4" below: the "array vs scalar"
framing here was itself wrong; the real, general mechanism is `_tag_value_blob_offset()`.** After
re-testing an
`export_routine()` export following the `Trim_Decision`/`LugWrk` fixes above, Studio reported `Only
ASCII characters are supported` on an unrelated tag (`LugTrm`) — chased down and fixed (see the
STRING/latin-1 section below) — but while re-verifying the *other* tags swept into that same export
as context, a completely different, far larger bug turned up: `Comm_From_VABView_Recipe_Status`
(a plain `DINT[40]` tag, no UDT involved at all) showed every decoded value as if multiplied by
65536 versus the real Studio value (e.g. existing `3` → our `196608`, existing `192` → our
`12582912`) in Studio's own **Tag Name Collision / Data Compare** dialog — the exact tell-tale
signature of a value's real bytes landing in the high 16 bits of a 4-byte read that started 2 bytes
too early (if the true low-order bytes are zero, as they always are for a small value, `value` read
2 bytes early becomes `value << 16` = `value * 65536`, with no other distortion). The 0x1A2 offset
established two findings up was **only ever verified against scalar tags** — arrays were never
independently checked. A project-wide sweep confirmed this is not a one-off: **273 of 347 primitive
array tags and 14 of 22 BOOL array tags** (SINT/INT/DINT/BOOL, every one checked against this same
project's real Studio 5000 L5X export) decoded wrong at 0x1A2, and **all of them** decoded correctly
at 0x1A2 + 2 — including a real `Dimensions="1"` tag (`Test_Bit_DINT`), confirming the split is keyed
on `is_array` (declared array, even a 1-element one), not `n_elements > 1`, mirroring the identical
scalar-vs-array distinction already established for the collapse-to-scalar behavior. Fixed by
splitting `offset = 0x1A2 + 2 if is_array else 0x1A2` in both `_read_tag_initial_value` (primitives)
and `_decode_udt_initial_value` (UDTs/struct arrays) — previously both hardcoded a single `0x1A2`
with an explicit comment claiming "for both scalar and array tags... no separate scalar offset,"
which this finding disproves.

**This also caught and reversed a wrong turn from the very same investigation just above (the
`Trim_Decision`/dead-member fix)**: `_get_type_size()` had been given a `+ dt._dead_member_bytes`
addition on the untested assumption that a deleted member's persisting footprint would affect an
*array* element's stride the same way it affects a scalar struct member's trailing siblings
(`_apply_dead_member_byte_corrections()`, which is unrelated and still correct). Verified wrong
against a real 200-element array of the exact UDT this was found on (`Lug`, via tag `LugTrm`): by
directly locating two known-consecutive elements' own leading field value in the raw data-table
blob (searching for the literal 4-byte little-endian encoding of "158" and "159"), the true
per-element stride is exactly 568 bytes — the *plain* `max(offset + size)` computation, with **no**
dead-byte addition (570 was wrong). Reverted `_get_type_size()` to never add `_dead_member_bytes`;
the scalar-sibling case remains correctly handled by the separate, already-verified
`_apply_dead_member_byte_corrections()` pass, which was never affected by this reversion.

**Verified end-to-end**: with both the array-offset split and the `_get_type_size()` reversion in
place, `Trim_Decision`/`Fence_Decision` still match real Studio ground truth exactly (unaffected,
since they're scalar), `LugTrm`'s array elements now show the correct incrementing sequence
(158, 159, 160, ...) at the correct stride, and the full project-wide sweep of every primitive/BOOL
array tag with real ground truth (369 tags) came back with **zero mismatches**, up from 273+14
wrong. This is the single highest-impact bug found in this investigation — it silently corrupted
the large majority of every project's array tag values, primitive or UDT, scalar-vs-array
distinction notwithstanding, and had gone undetected because the earlier verification pass (758
BOOL + 812 DINT tags) happened to only include scalars.

**Methodological lesson, worth restating a third time in this file**: a fix that resolves the
specific reported symptom (here: the `Trim_Decision` "Data type mismatch") is not evidence the
*surrounding* changes made along the way are correct — the `_get_type_size()` addition was never
actually required to fix the reported bug (`_apply_dead_member_byte_corrections()` alone was
sufficient) and was wrong for a case (arrays) nobody had checked yet. When touching a shared,
widely-called helper like `_get_type_size()`, verify the *new* behavior against a case that
specifically exercises the path being changed, not just the one bug report that prompted the change.

## BIT-overlay member Target resolution (`MemberBuilder.build`/`_resolve_bit_target`)

A UDT's BIT-type members (bit-overlay pseudo-members aliasing one bit of a sibling field, e.g.
TIMER's `EN`/`TT`/`DN` aliasing its hidden `Control` DINT — see the section above) need a `Target=`
attribute naming that sibling in the exported L5X; Studio 5000's schema requires it, and rejects
Import Routine with `Required property 'Target' was missing` if it's absent. This was originally
resolved via a small enumerated "pattern" on the member's raw `0x68` value (0/1/0x800), each
branch using a different resolution mechanism. **A downstream agent found this incomplete on a
real UDT** (`LugWrk` in a real project): its 4 BIT members (`ActvtnArea`/`AcqstnArea`/`TrtmntArea`/
`TrtmntAllwd`, overlaying hidden `ZZZZZZZZZZLugWrk9`) all had `val_68=0x9`, a value outside the
enum, which fell into the code's "not a BIT sub-element, leave as plain BOOL" catch-all — so
`export_routine()` emitted these as `<Member DataType="BOOL">` with no `Target=` at all, and
Studio's Import Routine rejected the file. The agent traced the raw bytes far enough to identify
`0x68=0x9` as the distinguishing value and confirm the existing "Pattern 1" mechanism (treating
`0x6c` as an offset60_to_name lookup key) failed for this case (`0x6c=596` matched no real member's
own `0x60`, including the true backing field's `0x60=640`), but didn't have real Studio ground
truth in hand to determine the *correct* fix.

**Investigation, with the user then providing a real Studio 5000 export of the exact UDT as ground
truth** (`LugWrk_DataType.L5X`, plus a whole-project L5X for broader verification) — the decisive
resource for getting this right rather than guessing:

1. Confirmed `LugWrk`'s 4 BIT members share their own `0x60` value (640) with the hidden backing
   field (`ZZZZZZZZZZLugWrk9`, also `0x60=640`) — this is the SAME condition the code's existing
   "Pattern 3" branch already checked (`offset60_to_name.get(own 0x60)`), just gated behind
   `val_68 == 1` specifically. Generalizing "Pattern 1"/"Pattern 3" into a single "not a BIT
   sub-element only if `0x68==0x800` and `0x6c==0xFFFFFFFF`; otherwise try `0x6c`-lookup then
   own-`0x60`-lookup" fixed `LugWrk` and, cross-checked against the same ground truth file's
   sibling `Lug` UDT (which the agent hadn't examined), ALSO fixed 8 more real BIT members there
   with yet more previously-unseen `val_68` values (0x2, 0x14) that had been silently misclassified
   as plain BOOL by the original code's catch-all `else` branch — not just left unresolved, actually
   wrong. Neither `LugWrk` nor `Lug` mentioned anywhere in this repo before this session.
2. **A separate, real bug found in the same pass**: `Member.to_xml()`'s generic attribute
   auto-serialization emitted `BitNumber="0"` on `Ons`, a plain `BOOL[32]` array member — because
   `member.bit_number` is set for every BOOL member internally (needed as a data-table decode hint
   by `_decode_single_udt_element`/`_decode_scalar_member`, unrelated to XML rendering) but the
   base `to_xml()` has no way to know that distinction. Real Studio export never emits `BitNumber=`
   for a non-BIT member. Fixed by having `Member.to_xml()` strip a spurious `BitNumber=` attribute
   whenever `data_type != "BIT"`, without touching the field's internal (still-needed) value.
3. **A deeper, more consequential bug found while cross-checking the *whole* project's L5X against
   ground truth** (99 `DataType`s, not just the two directly implicated): the "own `0x60` lookup"
   mechanism from step 1 is not reliable in general — `offset60_to_name` is a flat, UDT-wide map
   keyed purely by raw byte offset, and nothing prevents an unrelated, real (non-hidden) field from
   coincidentally sharing a BIT member's own `0x60` with a *different* field than its true backing
   one. Found concretely in `Bin_Sequence`: `Action_1`..`Action_16`'s own `0x60` all read `4`,
   which matches real field `Sling_Pos_1` (also `0x60=4`) — NOT either of the UDT's two genuine
   hidden backing fields (`ZZZZZZZZZZBin_Sequen1`/`ZZZZZZZZZZBin_Sequen10`, at `0x60=2`/`0x60=3`
   respectively). The lookup didn't fail — it returned a wrong-but-plausible name, which is worse
   than failing outright, and 3 more real UDTs in the same project turned up the identical
   collision (`Product_Definition`, `Sorts`, `VAB_Data_Sorter_To_Scanner`). The ONE mechanism that
   resolved every real case found correctly, including this collision — `LugWrk`, `Lug`, TIMER,
   COUNTER, and all four collision UDTs — is **declaration order**: a BIT-overlay member always
   immediately follows its own backing field, so the pre-existing `_fallback_target` (most-recent
   preceding hidden member, originally only used for one narrow `val_68==0` branch) is now tried
   FIRST, before either offset-based lookup. The offset-based lookups are kept only as a fallback
   for when no hidden member precedes at all (verified this is what makes TIMER/COUNTER's `EN`/
   `TT`/`DN` resolve — their shared `0x60=12` matches no plain-field entry, since `_fallback_target`
   already gives the right answer, "Control", before either lookup is even tried).
4. Extracted the whole decision into a small, independently unit-tested pure function,
   `_resolve_bit_target(target_key, val_60, offset60_to_name, fallback_target)` — this logic had
   zero test coverage before this session (surprising, given TIMER/COUNTER's own bit-overlay
   handling has been revisited multiple times per the section above) and is fragile enough
   (three real, wrong revisions in one investigation) to deserve permanent regression tests
   independent of any real ACD fixture.

**Verified**: every one of 362 BIT members across the whole real project resolves a Target after
the fix (0 unresolved, down from several); a full attribute-by-attribute comparison of all 99
`DataType`s against that project's own real Studio 5000 L5X export came back with **zero
mismatches** (previously 2 `DataType`s had entirely unresolved targets and, after the first-pass
fix, 4 different `DataType`s had wrong-but-resolved targets from the collision in step 3).

**Methodological note, worth repeating given how this session went**: the first-pass fix (step 1)
looked complete — it silenced the original bug report and matched ground truth for the two directly
implicated UDTs. It was only proven wrong by deliberately widening verification to the *whole*
project against a *whole-project* L5X export, not just the specific UDT named in the bug report.
Don't treat "fixes the reported case" as "correct in general" for this kind of byte-offset
heuristic — cross-check against everything available before considering it done.

## Nested-UDT decode recursion-depth double-increment (`_decode_single_udt_element`)

A real Studio 5000 import of an `export_routine()` output failed with `Failed to set the 'Data'
property (Data type mismatch...)` at the line of a tag's `<Data Format="L5K">` element
(`Trim_Decision`, `LugWrk`-typed). Traced to `_decode_single_udt_element`'s `depth` counter being
incremented **twice** per real struct-nesting level: once where it calls `_decode_scalar_member(...,
depth + 1, ...)`, and again inside `_decode_scalar_member`, which itself calls
`_decode_single_udt_element(..., depth + 1)` before descending. This silently halved the usable
nesting depth from the documented 3 levels (`_max_depth=3`) to effectively 1 — a real UDT only 2
real levels deep (`LugWrk` → `Lug` → `LugErrorCode`, via `Trim_Decision.BfrLug.ErrorCd`) had its
innermost member (`ErrorCd`) decode to `{}` well within the intended limit. An empty dict for a
struct-typed member renders as nothing at all in `<Data Format="Decorated">` (the whole
`<StructureMember>` is silently dropped since `_udt_scalar_to_xml` only appends it `if inner:`,
easy to miss entirely in a spot-check) but as a bare `"[]"` in the `L5K` literal's fixed-position
array — a shape Studio 5000 rejects on import, which is how this was actually caught (an ordinary
Decorated-only diff would have missed it, another argument for checking L5K too, not just
Decorated, per the AOI-instance-value gap noted elsewhere in this file).

Fixed by removing the redundant increment at the two call sites in `_decode_single_udt_element`
(now passes plain `depth`, not `depth + 1`, to `_decode_scalar_member` — which still owns the
single `depth + 1` when it actually recurses into a nested UDT). Verified: `ErrorCd` now decodes
all 36 of its own members instead of `{}`. Two synthetic unit tests
(`test_decode_single_udt_element_two_real_levels_of_struct_nesting`,
`test_decode_single_udt_element_still_truncates_beyond_max_depth`) lock in both the fix (2 real
levels of nesting must decode fully) and that the depth-limit safety net itself still works (4
real levels must still truncate the innermost to `{}`) — this had zero prior test coverage.

**A second, separate discrepancy found on the same tag while verifying the fix above — SOLVED**:
5 scalar members of `LugWrk` itself (`pntrTpStrt`/`pntrTpStp`/`pntrTpTrtmnt`/`pntrLug`/`pntrDrtn`,
declared directly after the nested `BfrLug` (`Lug`-typed) member) decoded values shifted by exactly
one `INT` (2 bytes) versus real Studio ground truth — confirmed by direct raw-byte inspection: the
true values (`24,25,0,183,0`) sit at byte offsets 570/572/574/576/578, but each of these members'
own *stored* `_byte_offset` (the raw ACD record's own `0x60` field) says 568/570/572/574/576 — 2
bytes short. Ruled out several explanations before finding the real one: `Lug`'s own 133 members
are individually self-consistent and 100% correct (the STRING member `Z5_Product_Name` at offset
340 correctly gaps 88 bytes to the next member, matching `_STRING_SIZE`; the struct's own last
member, `Trim_Decision`, a `DINT[10]`, is contiguous with its neighbors); `_get_type_size("LUG",
...)` and `Lug`'s own declared total-size attribute (a real, separate stored field, value 568)
both independently agree on 568; and — decisively — 568 is already aligned to 2, 4, *and* 8 bytes,
so a generic "round the struct size up to alignment" rule is mathematically a no-op here and
cannot explain needing 570 (ruling out a general alignment-padding theory the user separately
raised: Rockwell does pad individual members for natural alignment, e.g. three `SINT`s followed by
a `DINT` leaves a 1-byte gap — real and relevant to how *live* members get positioned, which we
already handle correctly by trusting each live member's own stored offset — but that's a different
mechanism from this specific gap).

**Root cause**: `Lug`'s member collection has a **deleted member** — a real child comps row
(`Z1_Nominal_Width`, `record_type=512` vs `256` for a live member) with **no matching
extended-record descriptor at all** (found by comparing the member-collection's child comps-row
count, 134, against the DataType's own extended-record-derived member count, 133 — the mismatch
itself is the detection signal). Deleting a UDT member removes its type-level descriptor
(`data_type`/`dimension`) entirely, but **not** its old byte range from any tag data table already
allocated before the deletion — so the type's own declared size (568) and every live sibling's own
stored offset are both computed from *currently-visible* members only, blind to the dead member's
physical footprint, while the real data table (frozen at allocation time) still reserves it. The
user confirmed (having authored the deletion) that `Z1_Nominal_Width` was originally `DataType=
"INT"` via an older Studio 5000 export of the same UDT from a sibling project
(`Lug_DataType_Snider.L5X`) — exactly the missing 2 bytes.

We cannot recover a dead member's original type from anything else available: its own comps row is
mostly boilerplate template data (nearly byte-identical to a live member's own row past a short
prefix that's absent/zeroed in the dead one — likely a type reference, which is exactly the thing
that's missing), and `CanonicalSize.Dat`-style per-object size tables weren't found to cover this
either. Fixed as a **documented best-effort default, not a general algorithm**: any orphaned
member-collection child (no extended-record descriptor) is assumed to cost 2 bytes (INT-sized —
the smallest non-BOOL primitive), logged via `log.warning()` so a wrong guess for a *different*
project's dead member is visible rather than silently corrupting values, stored on the owning
`DataType` as `_dead_member_bytes` (`DataTypeBuilder.build()`).

`pntrTpStrt` etc. are *scalar* (non-array) siblings of `BfrLug`, and a scalar struct-typed member
never consults `_get_type_size()` at all; its own (and every subsequent sibling's own) byte offset
comes directly from Rockwell's stored per-member value, equally blind to the dead member's
footprint. Fixed via `_apply_dead_member_byte_corrections()`, a post-processing pass run once every
`DataType` is built (so nested-type name references resolve, including forward references), which
walks each DataType's own members in declaration order and shifts every member *after* a scalar
struct-typed member whose nested type carries dead bytes, cumulatively (so multiple dead-byte-
carrying structs in the same chain compound correctly).

**A first attempt also added `dt._dead_member_bytes` inside `_get_type_size()` itself, reasoning
this would additionally fix an *array* of a dead-member-carrying struct type's element stride —
this was wrong, and reverted.** See "Initial-value decoding offset bugs" (finding 5) below for the
full story: verified against a real 200-element array of this exact UDT that the true per-element
stride is the plain `max(offset + size)` value with **no** dead-byte addition. `_get_type_size()`
must never add `_dead_member_bytes`; only `_apply_dead_member_byte_corrections()` needs it.

**Verified**: `Trim_Decision` and its sibling `Fence_Decision` (both `LugWrk`-typed) now match real
Studio 5000 ground truth **exactly** — 170/170 leaf `Decorated` values identical, and the `L5K`
literal byte-for-byte identical (736 chars, zero diff) — up from 5 wrong scalar values and a
truncated `L5K` shape. Re-ran the full 99-`DataType` whole-project comparison (see the BIT-target
section above) after this fix: still zero mismatches, confirming the correction pass doesn't
disturb any DataType lacking a dead member (the overwhelming majority — `_dead_member_bytes`
defaults to 0, making it a no-op unless a real orphan is detected). Unit tests
(`test_get_type_size_does_not_add_dead_member_bytes`,
`test_apply_dead_member_byte_corrections_shifts_subsequent_members`,
`test_apply_dead_member_byte_corrections_noop_when_no_dead_bytes`) lock in both the correction pass
and that `_get_type_size()` stays a no-op for dead bytes, independent of any real ACD fixture.

**Caveat for the next dead member found in a different project**: the "2 bytes, INT-sized" default
is confirmed correct for exactly one real case. If a future orphaned member turns out to need a
different size (DINT=4, LINT=8, etc.), the `log.warning()` this fix added is the signal to
investigate — check for an old export of the same UDT from before the deletion (as the user
provided here) rather than guessing.

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

## STRING-family decode must use latin-1, never utf-8 (`_decode_string_family_value`)

Found immediately after re-testing the `Trim_Decision` export fixes above against real Studio 5000:
a *different* real tag (`LugTrm`, a `Lug[200]` array) failed import with `Only ASCII characters are
supported` on its `<Data Format="L5K">` element. Root cause: `_decode_string_family_value` decoded a
STRING member's raw bytes with `raw.decode("utf-8", errors="replace")` — a Rockwell STRING is just a
raw `SINT[]` byte array with no guarantee of valid UTF-8 content, and for element 114 of that array
(uninitialized/garbage data — its own `LEN` field read as ~17.8 million, obvious nonsense, clamped to
the type's 82-byte capacity, meaning the "text" that follows was never real content either) the raw
bytes weren't valid UTF-8. `errors="replace"` silently inserted U+FFFD (the Unicode replacement
character) for every invalid sequence — itself a non-ASCII codepoint, and unlike control characters
(already `$XX`-hex-escaped by `_l5k_string_padded`, see above), nothing was escaping it, so it reached
the L5K literal raw and Studio rejected it.

Fixed by decoding as **latin-1** instead — a 1:1 byte↔codepoint mapping that can never fail (every
byte 0x00-0xFF maps to a valid codepoint), so every original byte value survives intact whether it's
meaningful accented/extended text (plausible in this project — French terminology in tag/product
names) or pure garbage. `_l5k_string_padded`'s existing `$XX`-escape logic (originally only for
control characters 0x00-0x1F/0x7F) was extended to also escape any byte `> 0x7E` (non-ASCII), so
every possible byte value the latin-1 decode can now produce is representable in an ASCII-only L5K
literal. **`_string_literal_cdata`/`Tag._sanitize_xml_text` (used for the `Decorated` CDATA content)
needed no change** — XML 1.0 legitimately allows Unicode text in CDATA (0x20–0xD7FF, 0xE000–0xFFFD),
so a latin-1-decoded accented character (or garbage byte) renders there as valid, unescaped XML,
matching what real Studio would show; only the `L5K` text-literal format has the ASCII-only
restriction.

Verified: the same real project's `LugTrm`/`LugALL`/etc. tags no longer produce any non-ASCII
character in their `L5K` output (swept every controller-scope tag), while `Decorated` output still
correctly contains the raw latin-1-decoded characters in CDATA (not stripped or escaped away) — and
`Trim_Decision`/`Fence_Decision` (fixed earlier in this same investigation) still match real Studio
ground truth exactly, confirming this change didn't regress anything for tags without STRING content.
Regression tests: `test_decode_string_family_value_uses_latin1_never_replacement_char`,
`test_l5k_string_padded_escapes_non_ascii_bytes`, `test_l5k_string_padded_still_escapes_control_chars`.

## UDT total size must round up to a multiple of 4 (`_get_type_size`), and alignment can absorb a
## pending dead-byte shift (`_apply_dead_member_byte_corrections`)

Found via a fresh Studio 5000 "Tag Name Collision / Data Compare" dialog on a re-exported routine
(same investigation as the fixes above): a plain `DINT[40]` tag (`Comm_From_VABView_Recipe_Status`)
showed every value multiplied by 65536 vs. the real Studio value, AND (after fixing that) a *scalar*
UDT tag (`EncTrm`, type `Encoder`) showed the exact same 65536x pattern on several plain scalar
members despite `Encoder` having zero orphaned members of its own. Two genuinely different bugs
were found chasing the `Encoder` case, both confirmed **directly against Studio 5000's own UDT
Properties dialog** (`Data Type Size` field — an authoritative value the user screenshotted for
`Lug`, `Encoder`, and `LugWrk`), after seven other hypotheses (TIMER/COUNTER reference, array-of-
struct members, `DataType`-level `built_in`/`module_defined`/`string_family` flags, tag-level
attributes, `record_format_version`/`cip_type`, object_id ordering) were tested and ruled out:

1. **`_get_type_size()` must round a UDT's computed size up to a multiple of 4, not just leave it
   as-is.** Rockwell always declares a UDT's total size as a multiple of 4, confirmed directly:
   `Encoder`'s members sum to 263 bytes (its own last member is one of three trailing 1-byte hidden
   `SINT` backing fields for BIT-flag groups), but Studio's own Properties dialog shows `Encoder`'s
   `Data Type Size` as **264** — later, the user explicitly confirmed this in general ("UDT can
   only have a multiple of 4 byte total size"), correcting an initial narrower guess of just
   "round to even" (264 happens to also be even, which is why the narrower guess wasn't immediately
   caught). `Lug` (568) and `Timing` (144) are already multiples of 4, which is why testing only
   those two earlier didn't surface this.
2. **A BOOL array member can absorb part or all of a pending dead-byte shift via its own 4-byte
   alignment, so `_apply_dead_member_byte_corrections()` must not apply the shift flatly.** Found by
   comparing `LugWrk`'s own computed size (650) against Studio's declared size for the *same* UDT
   (**648**) — a 2-byte *overcorrection*, in the opposite direction from the original dead-member
   bug. Root cause: `LugWrk`'s trailing `Ons` (`BOOL[32]`, 4-byte aligned since it's bit-packed into
   DINT-sized words — the same rule `_get_type_size()` already uses for BOOL-array *sizing*) had its
   own stored offset already correctly positioned by Rockwell's own alignment padding (which
   naturally absorbs a smaller gap left by the preceding dead-member correction); blindly adding the
   full pending +2 on top of an already-correctly-aligned offset overcorrected it. Fixed by having
   `_apply_dead_member_byte_corrections()` track each member's own true end as it walks a DataType's
   members, and for a BOOL array specifically, recompute its start by aligning up from the previous
   member's true end (`-(-prev_true_end // 4) * 4`) instead of adding the flat cumulative shift —
   the *effective* shift actually applied (which may be less than the pending amount) is what
   carries forward to subsequent members. Also fixed a related latent bug this exposed: a scalar
   struct-typed member's own contribution to the running "true end" tracker didn't include its
   nested type's own dead bytes, which would have mattered if a BOOL array followed such a member
   with nothing in between (not exercised by `LugWrk`'s own shape, but fixed since found).

**Verified**: `Lug` (568), `Encoder` (264), and `LugWrk` (648) computed sizes now all match Studio
5000's own declared "Data Type Size" for the same three real UDTs exactly. Re-ran the full
99-`DataType` whole-project comparison and the 369-tag array sweep (both established earlier in
this investigation): still zero mismatches for both, confirming these two fixes are a no-op for
every UDT that doesn't need them (the overwhelming majority — no dead members, no BOOL array
immediately following one). `Trim_Decision`/`Fence_Decision`'s `L5K` literal re-verified
byte-for-byte identical to real Studio ground truth after this change. New regression test:
`test_apply_dead_member_byte_corrections_bool_array_absorbs_shift_via_alignment`.

**RESOLVED (was "still open" above) — the whole "some tags need +2" mystery, definitively.** The
`0x1A2`/`0x1A2 + 2` split described throughout this section and "Initial-value decoding offset
bugs" below was **never actually about scalar-vs-array, or about which UDT type is involved** —
every one of those correlations (array-vs-scalar, `Lug`/`LugWrk`-vs-everything-else) was
coincidental to the specific projects tested. The real mechanism, found by finally parsing the
tag's `data_table_instance` comps record as the ordinary structured `RxGeneric` record it actually
is instead of guessing an absolute byte offset into it:

- That record's own header declares `count_record` attribute records, but `RxGeneric._read()`'s
  Kaitai-generated parsing loop (`for i in range(self.count_record - 1)`) always leaves the
  **last** one unparsed in the stream — deliberately or not, this last attribute record (always
  `attribute_id 0x66`) is never read into `extended_records` at all.
- That unparsed last attribute record **is the tag's own value blob**: its own 4-byte `len_value`
  field always exactly equals the tag's computed value size (verified across every scalar/array,
  primitive/UDT tag checked), and its value payload — starting 8 bytes (`attribute_id +
  len_value`) after wherever the 3 parsed `extended_records` leave off — is the real data.
- The "some tags need +2" appearance came entirely from this: the byte length consumed by the 3
  *parsed* attribute records (in particular attribute `0x1`, an opaque boilerplate blob) genuinely
  varies by a couple of bytes between records/projects — 286 bytes in one fresh Studio 5000 V32
  test project, 288 bytes in an older V38 production project — which is a real, computable
  difference in the record's own self-declared structure, not something dependent on whether the
  tag is a scalar, an array, or which UDT type it uses.

Fixed by adding `_tag_value_blob_offset(raw_rec)` (`elements.py`), which parses the record via
`RxGeneric.from_bytes()` and computes `82 + sum(8 + len(value) for er in extended_records) + 8` —
replacing the old fixed-constant/`is_array`-conditional guess entirely in both
`_read_tag_initial_value` and `_decode_udt_initial_value`.

**A second, compounding bug was found and fixed in the same investigation**: with the above fix
alone, a real, *populated* `Trim_Decision` tag (`LugWrk`-typed; the user provided a live Studio
5000 screenshot of its Monitor tab) still decoded 5 populated fields wrong
(`pntrTpStrt`/`pntrTpStp`/`pntrLug`/`Wrk[4]`) — because `_apply_dead_member_byte_corrections`
(see "Nested-UDT decode recursion-depth double-increment" below) was *also* adding a +2 shift to
every `LugWrk` member following `BfrLug` (`Lug`-typed, which has one deleted/orphaned member),
double-counting a correction that the fix above already fully accounts for. The earlier "verified
exact, 170/170 leaf values" claim for this exact tag was made against an **all-zero/unpopulated**
instance, which cannot distinguish a correct offset from one that's off by 2 — this is why a real,
populated instance was necessary to catch it, and a reminder that an "exact match" check is only
as strong as the ground truth data actually exercises the code path in question. Fixed by making
`_apply_dead_member_byte_corrections` a no-op — Rockwell's own stored per-member byte offsets
already account for everything correctly, with no adjustment needed for a nested type's dead
bytes. `_dead_member_bytes` is still computed and logged (`DataTypeBuilder.build()`) as a
diagnostic that a type has an orphaned member, but no longer feeds into any byte-offset math
anywhere.

Verified end-to-end against the real project: `EncTrm.PlssQty=256`, `Trim_Decision.pntrTpStrt=24`/
`pntrTpStp=25`/`pntrLug=183`/`Wrk=[0,0,0,0,32790,0,0,0,0,0]` (all matching the user's live Studio
5000 screenshot exactly), `LugTrm[0].No=158`/`Year=2026`, `Comm_From_VABView_Recipe_Status`'s first
10 values — and, separately, the fresh V32 test project's `TestDintArray`/`TestLug`/`ZZTest1` all
still decode correctly (proving the fix generalizes rather than just re-fitting the V38 project).
A full whole-project `to_xml()` export of the real project also completes without error. Full test
suite: 101 passed, 2 skipped (up from 97/2, after rewriting the tests that had encoded the old,
disproven fixed-offset assumptions to instead build a synthetic `RxGeneric`-shaped record via a new
`_build_dti_record()` test helper).

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

## Comparing I/O addresses across two projects (`find_io_addresses`/`diff_io_addresses`)

Added after a downstream LLM session, asked to find I/O address changes between two ACDs (two
saves of the same project, and separately a "mill" vs "VAB" variant), hand-rolled a regex that hit
a `re.error: unbalanced parenthesis`, then an `IndexError` from zipping two routines' rungs by
index once it worked — routines routinely have a different rung count between two otherwise-
similar projects/saves, so index-based comparison is fundamentally the wrong approach, not just a
bug to patch around.

`acd/api.py` now exposes three public functions for this instead of leaving every caller to
reinvent the tokenizer:
- `find_io_addresses(text) -> List[str]`: extracts every I/O-style address from one rung/ST-line
  of text (`"IO024:I.Data[0].13"`, `"Remote_GraderConsole:3:I.Pt13.Data"`,
  `"Local:10:I.Data.11"`, `"Sorter_VFD:I.DriveStatus_Active"`). A real I/O address always contains
  `":"` (reserved by Rockwell's own tag-naming rules for module addressing), so this never
  collides with a plain UDT member path like `"M304_Sorter_Lug_Chain.VFD.Running"` — verified
  against real examples pulled from an actual project-vs-project diff (see the regex `_IO_ADDRESS_RE`:
  base name, optional `:slot`, required `:Type`, then a repeating `.Member`/`.bit`/`[idx,...]` chain).
- `io_addresses_by_routine(project) -> Dict[(program_name, routine_name), List[str]]`: every
  routine's full set of I/O addresses (RLL rungs + ST lines), duplicates included, in source
  order. AOI logic routines are keyed as `("AOI:<name>", routine_name)` since they have no Program.
- `diff_io_addresses(project_a, project_b) -> Dict[(program_name, routine_name), {"removed":
  [...], "added": [...], "common": [...]}]`: routine-by-routine, set-based (not index-based) I/O
  address diff between two projects — only routines with an actual difference are included. A
  routine unique to one side still gets an entry (everything shows as fully added/removed).

Verified end-to-end against the real `BPM_TrimmerSorter_20260713.ACD` /
`BPM_TrimmerSorter_VAB_20260713.ACD` pair (`Bethel_Planer_20260713_Compare`): 64 routines reported
with real, sensible I/O address differences (e.g. `Advance`'s `Sorter_VFD:I.DriveStatus_Active`/
`Sorter_VFD:I.OutputFreq` present only in the mill project), with zero crashes despite routines
differing in rung count between the two files — the exact scenario that broke the ad hoc script.

**Follow-up gap, found immediately after shipping the above**: the user reported their downstream
LLM defaulted to `diff_io_addresses()` whenever asked for a *generic* "what changed between these
two files" comparison, not just I/O-specific requests — because it was, at the time, the only
`diff_*`-named function in the public API, so an LLM pattern-matching on "diff" had nothing more
appropriate to reach for. Added `diff_project()` (same file) as the actual general-purpose entry
point, and tightened `diff_io_addresses()`'s own docstring to explicitly disclaim general use
("do not reach for this function by default just because it has 'diff' in the name") — the lesson
being that a narrowly-scoped function with a generic-sounding name will get misused by an LLM
caller unless a correctly-scoped alternative exists *and* the narrow one's docstring actively
steers away from itself, not just describes what it does.

`diff_project(project_a, project_b) -> dict` covers, each only populated when something differs:
- `"routines"`: keyed like `io_addresses_by_routine()` (`(program_name, routine_name)`, AOI logic
  routines as `("AOI:<name>", routine_name)`). `"status"` is `"added"`/`"removed"`/`"changed"`; a
  `"changed"` entry's `"changes"` list comes from `difflib.SequenceMatcher(a=lines_a,
  b=lines_b).get_opcodes()` over the routine's rungs (RLL) or `_st_lines` (ST) — reusing the same
  alignment-based approach (not index-zipping) as `diff_io_addresses()`, for the same reason: two
  routines routinely have a different rung count even when "the same" logic-wise.
- `"tags"`: keyed `(program_name_or_"", tag_name)` (`""` = controller scope); compares
  `data_type`/`description`/`_initial_value` for tags present on both sides.
- `"data_types"`/`"modules"`/`"aois"`: presence-only (added/removed by name) — deliberately does
  NOT diff UDT member layout, module connection/RPI details, or AOI parameters; documented as a
  known scope limit in the function's own docstring rather than silently doing something partial.

**Second follow-up, found the very next time a downstream LLM actually used `diff_project()` on a
real large project pair**: it technically worked, but the "tags" section dumped every changed
tag's FULL old/new `_initial_value` inline — for a UDT array tag that's a list of dozens of
per-element dicts, so one real comparison (`BPM_TrimmerSorter_20260713.ACD` vs
`BPM_TrimmerSorter_VAB_20260713.ACD`, 1601 changed tags) produced an unreadable wall of raw numeric
noise that overflowed the LLM's context before it could even start summarizing. `_diff_tags()` now
runs each tag's `"value"` entry through `_summarize_value_diff()`: values under 200 chars of
`repr()` are still shown in full (`{"old": ..., "new": ...}`), but a large list is reduced to
`{"summary": "list[N] vs list[M]: K of N common elements differ", "differing_indices": [...]
(first 10)}` and a large dict similarly to `{"summary": ..., "differing_keys": [...] (first 10)}`
— callers can tell which shape they got by checking for a `"summary"` key vs `"old"`/`"new"` keys.
Verified against the same real project pair: total `repr()` size of the whole diff dropped from
"too large to read" to ~468KB (290 of 1018 changed-value tags actually needed summarizing; the
rest were small scalars shown in full) — the routines/tags sections can still legitimately be
large for two *genuinely very different* projects (this pair is a mill vs. a substantially
different VAB variant, not two saves of the same logic), so don't expect `diff_project()` output
to always be small; the fix targets the *per-value* blowup, not the *aggregate* size when the
underlying projects really do differ everywhere.

**Third follow-up**: despite both fixes above and the module docstring already recommending
`diff_project()`, a downstream LLM asked to look at one specific routine (`Motors/Main_Motors`)
across the same two real projects still wrote its own manual comparison — fetched both `Routine`
objects, then printed `.rungs` for each side by side by index. Three JSR rungs were removed near
the top of one project's copy, shifting every later rung's index by 3, which made the printed
lists look like the whole routine had changed even though the tail (`Infeed_LandingTable` onward)
was byte-identical. This wasn't a bug in `diff_project()`/`diff_io_addresses()` (both already
handle this correctly via `difflib`) — it was a *discoverability* gap: the LLM had two `Routine`
objects in hand and reached for `print()`/manual zip rather than any diff function, likely because
nothing in the public API matched that exact shape ("I already have two routines, just diff
these") as directly as `diff_project(project_a, project_b)` (which needs whole projects) did.

Extracted the per-routine alignment logic `_diff_routines()` already used into a new public
`diff_routine(routine_a, routine_b) -> {"status": "unchanged"/"changed", "changes": [...]}`, and
rewrote the top of `acd/__init__.py`'s module docstring to lead with an explicit "COMPARING TWO
PROJECTS/SAVES/ROUTINES — READ THIS BEFORE WRITING YOUR OWN COMPARISON CODE" section (previously
this guidance existed but was positioned after the Quick Start snippet, one paragraph among
several, with no equivalent function for the single-routine case) naming all three diff functions
by exact use case. Verified `diff_routine()` reproduces the real `Main_Motors` scenario exactly:
`{"status": "changed", "changes": [{"op": "delete", "old": [the 3 removed JSR rungs], "new": []}]}`
— nothing else reported, confirming the tail is correctly recognized as unchanged.

The recurring lesson across all three follow-ups: a correct implementation is not sufficient for
an LLM caller to actually use it — the function matching the caller's exact mental model ("I have
two routines" vs. "I have two projects") has to exist, and the guidance steering them to it has to
be positioned where it will actually be read (at the very top, restated at the point of need), not
just documented accurately somewhere in the file.

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
