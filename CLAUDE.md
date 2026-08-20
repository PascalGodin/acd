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

## Studio 5000 Help docs (local only, not in git)

If a `Help/` directory exists at the repo root (`Help/ENU/rs5000/...`), it's the real Studio 5000
help guide (Rockwell copyrighted content, ~325MB) — gitignored intentionally, never committed
(licensing + repo-bloat concerns, not a mistake). Check it when a question is about Logix/PLC
coding rules, instruction semantics, or L5X/UDT conventions themselves (as opposed to this
library's own binary-parsing internals, which is what the rest of this file covers) — e.g.
`Help/ENU/rs5000/common-content/comm-attributes/structured-text-syntax.html` for ST syntax rules,
or the `common-content/comm-attributes/` directory generally for tag/data-type/addressing
conventions. It won't be present in a fresh clone of this repo; don't assume it exists.

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
- `pip install .`/`pip install git+<url>` (non-editable) now work — previously `setup.py` had a
  custom `install` command that unconditionally tried to invoke the external Kaitai Struct
  compiler (`kaitai-struct-compiler.bat`/`ksc`, a separate Java-based tool, not a Python
  dependency) to regenerate `acd/generated/`, which hard-failed (`WinError 2`/`FileNotFoundError`)
  on any machine without that compiler on PATH — confirmed by actually testing `pip install .` in
  a throwaway venv, not assumed. `acd/generated/`'s output is already committed to git and never
  needed regenerating for a normal install; only `pip install -e .` (which goes through a
  different setuptools code path) ever worked, which is why this went unnoticed — every existing
  install (this repo's own dev setup, the downstream agent's) happened to use `-e`. Regenerating
  after changing a `.ksy` template is now a separate, explicit maintainer step: `python
  scripts/regenerate_kaitai.py` (see README's "Developing" section). Verified after the fix: a
  real non-editable `pip install .` in a fresh venv succeeds and `db_to_controller()` loads the
  real `CuteLogix.ACD` fixture correctly; the editable install path (`pip install -e .`) is
  unaffected. Found while investigating what a "lightweight, no-admin-rights" acd-tools setup for
  a non-technical user (delegating to an AI coding agent that runs `pip install` itself) would
  actually require — this was the real, confirmed blocker, not a hypothetical one.

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

**RESOLVED (after being wrong once first) — a hex-named connection (e.g. `"$0ce232bb$"`) with an
otherwise-unrecognized CIP connection-type code is a cached CIP MESSAGE connection, not physical
I/O, and is now skipped entirely rather than guessed.** Found on a real project's processor module
(`1756-L82E`, comp_name `Local`): one connection with RPI decoded as `0` and code `10`, not in
`_CONNECTION_TYPE_BY_CODE`, hitting the name-heuristic fallback and logging the "please report
this" warning. A whole-project sweep (246 modules, 201 real connections) found this was the *only*
hex-named connection AND the *only* unrecognized-code connection in the whole project.

**First attempt (wrong reasoning, reverted):** cross-referenced a real, exact-matching Studio 5000
L5X export of the same project, found the `Local` module has no `<Connections>` element there at
all, and concluded the connection should be excluded on that basis. That conclusion happened to be
directionally right but the REASONING was wrong and got called out directly: L5X being silent
about something is not evidence the ACD doesn't have real data for it (this project's own "Known
limitations" section already documents multiple cases where the ACD holds real data L5X never
surfaces — Module CIP identity records; 570 real Module/Connection-level comments with nowhere to
go in L5X). Reverted rather than ship a fix justified by an invalid inference.

**Second attempt (real investigation, with the user actively checking Studio 5000 directly) found
the actual mechanism.** The same real project has a routine literally named `SLC_504` containing
five `MSG` instructions (`SLC_Stacker_Write`/`Read`, `SLC_Planer_Write`/`Read`/`Read2`) reading and
writing an external SLC 5/04 (SLC-500-family) controller over **DH+**, routed through a real
`1756-DHRIO` bridge module (`RIO_Slot_3`) — confirmed directly via Studio's own Message
Configuration dialog: Communication Method = DH+, Path = `RIO_Slot_3`, and **"Cache Connections"
checked**. A cached `MSG` connection is a resource of the *requesting processor's own* message
cache (hence owned by `Local`, not the DHRIO bridge module it routes through), is inherently
message-triggered rather than cyclic (hence RPI=0), is auto-managed by Studio rather than
user-named (hence the hex placeholder), and — the key fact that makes the fix correct rather than
another guess — real Studio 5000 output confirms this whole *category* of connection is never
rendered as an L5X `<Connection>` element at all, because that schema models physical CIP I/O
connections specifically, not cached message connections. The 5-MSG-instructions-but-1-connection
count also checks out: all five `MSG` instructions share the exact same DH+ destination path
(same DHRIO module, same channel, same node), consistent with Rockwell caching one connection per
unique destination rather than one per `MSG` instruction.

Fixed in `ModuleBuilder.build()` by skipping any connection whose comp_name matches the `$hex$`
placeholder pattern (the same convention already used for a Module's own hex-encoded name a few
lines above) *before* the type-code lookup, rather than guessing an Input/Output `Type=` for
something that was never an I/O connection to begin with. This matters beyond just the warning
noise: `Module.to_xml()` DOES render `_connections` into real `<Connection>` XML output, so
leaving this in would have produced a spurious `<Connection Name="$0ce232bb$" .../>` on export
that real Studio 5000 would never emit. Covered by
`test_module_builder_skips_hex_named_connection` (`test/test_database.py`) — confirmed to fail
without the fix, and confirms a normally-named connection with a recognized code still comes
through correctly (not a blanket "ignore odd codes" fix).

**Caveat, stated plainly**: this is strong, multi-source converging evidence (Cache Connections
confirmed checked, DH+ method confirmed, destination-path/count math checks out, and the L5X-schema
absence now has a real, positive explanation instead of just being cited as silence) — but it was
never verified at the byte level (no field in the connection's own raw record was decoded to
directly read "this is a DH+ message cache entry"). The fix is scoped to the hex-name signal (not
to code `10` specifically) precisely because that's the part with an existing, independently-
established precedent in this codebase; don't assume code `10` universally/only means "DH+ message
cache" if it turns up on a normally-named connection in some other project — re-investigate rather
than assume.

**The other pre-existing warning on this same real project (`Comps: skipped N unparseable
record(s)`) was investigated, and an earlier claim here that the failing records were "dead" has
been retracted -- it was an unverified guess.** Both failures are on records with Kaitai
identifier `65021` (parsed via `FdfdComps`), which was assumed (by loose analogy with a
*different* part of this codebase -- Comments.Dat's own `fa fa`/`fd fd` deletion-marker
convention) to mean "deleted." Checked directly and found that's not true: of 680 real `65021`
records in this same project that parse successfully, `record_type` (the field actually used
elsewhere in this codebase to distinguish live from deleted) comes back `256` (live) 221 times,
`512` (deleted) only 56 times, `0` 380 times, and a long tail of values (`34471`, `65535`,
`32306`, ...) that don't look like a real small enum at all -- suggesting `CompsRecord.parse()`
may be reading `record_type` from the wrong offset for this record shape, not that these are
consistently anything in particular. There are also two OTHER valid record markers
(`0xFBBF`=64447, `0xFEFE`=65278) that `CompsRecord.parse()` doesn't even attempt to handle
(silently returns `None` for both) -- a broader gap than just these two exceptions. What's
actually known: `65021`'s own header is a different size (155 bytes) than `64250`'s (144 bytes)
and doesn't self-declare its own record length the way `64250` does, so it's a structurally
different record layout, not just a flag on the same one. Genuinely unresolved what it represents.
Left alone for now (the existing catch-and-warn behavior degrades gracefully either way), but
don't repeat the "verified dead, safe to ignore" claim without actually checking `record_type`
across a real sample first -- that's what caught this being wrong.

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

## Phantom UDT members: deleted-member child rows resurrected via a stale extended record

The "Phantom `<Program>`/`<Module>`/`<Tag>`/`<Routine>` elements" fix (see "Whole-project
element-count verification" above) filters out deleted-but-not-purged comps records for those four
object kinds via a distinct `record_type`/enum value — but this was never applied to **UDT
members**, and a real project turned up the exact same class of bug there too. Found via a user
report: `project.controller.data_types` for a real, freshly re-saved project (`Trimmer`, edited in
Studio 5000 to consolidate 16 scalar `DINT` members into a single `Saw_Pos[32]` array) returned 16
members instead of the expected 1.

Root cause, confirmed directly against the raw comps data: a member-collection child's own
`record_type` is `256` for a live member, `512` for a deleted one — the same convention already
established elsewhere in this file. Deleting a member correctly flips its own child row to `512`,
and correctly updates the type's own declared `member_count` (extended-record attribute `0x64`,
verified to read back as `1` for the real `Trimmer` case) — but does **not** reliably purge that
member's own extended-record descriptor from the *type's* own comps row. `DataTypeBuilder.build()`
never checked either signal: it matched every extended-record descriptor (attribute_id `>= 0x6E`)
against *any* same-named member-collection child regardless of that child's own `record_type`,
so a stale descriptor for a deleted member, paired with its equally-stale-but-still-present
`record_type=512` child row, silently resurrected it as if it were live.

Fixed in `DataTypeBuilder.build()` by filtering member-collection children to `record_type == 256`
*before* matching them against extended records — a stale descriptor for a name with no *live*
child now falls through to the same "no descriptor found" path already used for a fully-purged
deletion (folded into the same `_dead_member_bytes` diagnostic count/warning). Also added a
diagnostic (not authoritative — its exact counting convention, e.g. whether it includes hidden
BIT-backing members, isn't independently verified) cross-check comparing the final built member
count against the type's own declared `member_count`, which would have caught this immediately had
it existed already (it correctly said `1` while 16 members were about to be returned).

Verified: `Trimmer` now correctly returns exactly `[Saw_Pos (DINT, dimension=32)]`, matching the
live Studio 5000 project exactly. Covered by
`test_datatype_builder_excludes_deleted_member_with_stale_extended_record`
(`test/test_elements_helpers.py`) — a synthetic comps DB reproducing the exact shape (a live
member's extended record + child row, plus a stale extended record whose only matching child is
`record_type=512`) — confirmed this test fails without the fix, not just passes with it.

## `object_id` is not always unique — a genuine 3-way collision crashed the whole load

A different, real project (`BPM_TrimmerSorter_VAB_20260727.ACD`) failed to load entirely:
`IndexError: list index out of range` in `ControllerBuilder.build()`, at the line resolving
`RxDataTypeCollection` (the controller's own list of DataTypes) — `results` was empty, meaning no
child named `RxDataTypeCollection` was found under the controller at all, even though the string
provably exists (found via a raw grep of the decompressed `Comps.Dat` bytes) with a live (`fa fa`)
marker.

Root cause: `Comps.Dat` contained **three entirely unrelated objects sharing the exact same
`object_id`** (a real, heavily-edited production project's Comps.Dat, not a corrupted file) — the
genuine `RxDataTypeCollection` (parent = the controller, a tiny 78-byte record), and two unrelated
objects with different parents (`B_Manual_Solution`, 7410 bytes; `ZZZ_TEMPORARY_IMPORT_DATATYPE_
NAME_000`, 6874 bytes — the latter's name suggests a leftover artifact of some prior import/edit
operation). `ExportL5x.__post_init__`'s Comps.Dat dedup step (comment at the time: "a routine that
appears twice in Comps.Dat with different record_type values, keep the entry with the largest
record" — a real, correct fix for *that* scenario, see "Whole-project element-count verification")
keyed purely by `object_id`, picking whichever of the three colliding records was physically
largest — silently discarding the tiny-but-correct `RxDataTypeCollection` in favor of an unrelated
object, which then made the entire project unloadable.

Fixed by extracting the dedup loop into `_dedupe_comps_records()` (`acd/l5x/export_l5x.py`) and
keying it by **`(object_id, parent_id)`** instead of `object_id` alone — this still correctly
collapses the *original* truncated-vs-full same-object case (same parent both times, different
`record_type`) while keeping genuinely different objects that happen to share a raw `object_id`
apart, since they have different parents. Verified: the real project now loads successfully (102
DataTypes, 12 Programs, 3664 tags, full whole-project `to_xml()` export with no errors) and
`Trimmer` still correctly resolves to `[Saw_Pos, DINT, dim=32]` (unaffected by this fix). Covered
by `test_dedupe_comps_records_keeps_unrelated_objects_sharing_an_object_id` and
`test_dedupe_comps_records_still_collapses_truncated_duplicate_under_same_parent`
(`test/test_database.py`).

**Residual, theoretical risk not chased further**: `object_id` genuinely isn't unique in this
project's own `Comps.Dat`. Many other lookups throughout `elements.py` do `SELECT ... WHERE
object_id=?` expecting exactly one row (via `fetchone()`/`results[0]`) — if some *other* reference
elsewhere in the same project happens to point at a colliding `object_id` for a different purpose,
that lookup could resolve to the wrong one of the colliding rows. This risk already existed before
this fix (the "keep the largest" heuristic just silently resolved it one particular, wrong way);
this fix only guarantees the *dedup* step no longer discards a real object outright. No evidence of
this manifesting as an actual bug has been found (the whole-project export for this real project
completes and looks correct) — revisit only if a concrete case turns up, per this project's own
"don't guess a fix without real data" rule.

**UPDATE — this residual risk was confirmed as a real, concrete bug, not just theoretical.** See
"`RoutineBuilder` picked the wrong row on a colliding `object_id`" below: a real project had a
genuinely live routine share its `object_id` with an unrelated object, and `RoutineBuilder.build()`'s
own `object_id`-only re-query was exactly this class of exposure, silently dropping the real routine
entirely. Fixed there specifically (the one confirmed case); the other `object_id`-only lookups this
paragraph warns about are still unaudited — revisit each on its own if a concrete case turns up,
rather than assuming this one fix closes the risk everywhere it could theoretically appear.

## A second root-level object can share the controller's own `record_type`

Same project, next re-save from Studio 5000 (after further edits, same general "one real object
plus one inert impostor at the same structural level" shape as the `object_id` collision above, but
a different symptom): `ControllerBuilder.build()`'s very first query — "find the exactly-one
`parent_id=0 AND record_type=256` row, that's the controller" — matched **two** rows instead of
one, raising `Exception("Does not contain exactly one root controller node")` and making the whole
project unloadable again.

The second row: `object_id=1`, `comp_name=""` (empty), `record_type=256` (the same marker a real
controller has), **no children**, and a 154-byte raw record that's all zero bytes except a couple
of `0xFFFFFFFF` sentinel values at the tail — not tangled into the real project structure at all
(nothing references it, it references nothing), just inert. The project already has other
legitimate root-level (`parent_id=0`) administrative objects — "Recycling Bin", "DataSet Data" —
but those have `record_type=0`, so they never collided with this query; `object_id=1` is the first
one seen that happens to coincidentally share `record_type=256` too.

Fixed by adding `AND comp_name != ''` to the query — a real controller always has a real project
name (Studio 5000 has no concept of an unnamed one), so this excludes the empty-name impostor
without needing any deeper structural distinction. Verified: the real project loads again (101
DataTypes, 12 Programs, 3664 tags, full whole-project export with no errors). Covered by
`test_controller_builder_ignores_nameless_root_object` (`test/test_database.py`) — reuses the real
`CuteLogix.ACD` fixture's already-fully-populated comps table and injects one synthetic
empty-name/`record_type=256`/`parent_id=0` row directly, rather than trying to construct an entire
synthetic controller from scratch — confirmed this test fails without the fix.

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

**A real downstream report found the bare "skipped N unparseable record(s) of M" summary itself was
a problem, not just cosmetic.** A single record in a real project's `Comps.Dat` failed with a raw
`EndOfStreamError('requested 2 bytes, but only 0 bytes available')` — caught by the same bare
`except Exception` this section describes, logged only as a count. The record turned out to almost
certainly be a genuine, recently-authored routine's own definition: invisible to `db_list_routines()`,
`db_get_routine()` (a clean `KeyError`, indistinguishable from "this routine was never created"), and
even the legacy in-memory `load_acd()` loader — no error anywhere told the caller a real object had
gone missing, and the skipped-record count (a flat, unchanging "1") looked identical whether it was
one genuinely dropped real object or ordinary multi-record padding/noise, with nothing to tell the
two apart.

**Root-caused as far as possible without the real file, deliberately NOT hand-patched further.**
Both `FafaComps`/`FdfdComps` (`acd/generated/comps/`, dispatched by `CompsRecord.parse()` for
`identifier` `64250`/`65021`) read a FIXED-size header first (144/155 bytes — always safe, since it's
already-buffered), then a VARIABLE-length `record_buffer` sized from a length value baked into the
record's own payload (`FafaComps`) or passed in from the outer container's own `len_record`
(`FdfdComps`) — either one can legitimately disagree with how many bytes the record's raw payload
actually contains, and `FdfdComps` in particular is already flagged elsewhere in this file as a
"structurally different, not fully understood" record shape (see "Comps: skipped N unparseable
record(s)" below). Chasing the exact byte-level cause further without the real failing bytes in hand
would be exactly the "guess a fix without real data" mistake this file repeatedly warns against — the
Kaitai-generated parsers are also not something to hand-patch directly (see README's "Developing"
section on `.ksy` regeneration).

**Fix, scoped to what's safely knowable without the real file: rich, generic diagnostics.**
`_parse_records()` now tracks each failure's record INDEX and the REAL exception (not just a tally),
plus — read generically off the outer Kaitai `Record` wrapper shared by all four `.Dat` files, not
anything Comps-specific — its `identifier`/`len_record` when available. Every failure gets its own
`log.debug()` line (visible under `verbose=True`), and the summary `log.warning()` itself now inlines
the same detail directly (record index, identifier, len_record, real exception) whenever there are
`_MAX_INLINE_FAILURE_DETAILS` (5) or fewer failures — the common, most actionable case (one or two
genuinely dropped real objects) no longer requires separately re-enabling `verbose=True` just to learn
which record and why. A file with MORE failures than that (more likely genuine firmware-version format
noise, per this section's own opening paragraph) keeps the plain count in the WARNING and points at
`verbose=True` for the full per-record detail, rather than dumping a long, unhelpful list into every
default-quiet load.

Covered by `test_parse_records_warning_inlines_index_and_exception_for_a_single_failure`,
`test_parse_records_debug_detail_hidden_unless_verbose`,
`test_parse_records_many_failures_summary_omits_inline_detail`, and
`test_parse_records_per_record_detail_visible_under_verbose` (`test/test_database.py`) — all against a
fake `DbExtract`/`Record` (no real `.Dat` file needed to exercise the failure path itself). The
verbose-mode test found its own real test-isolation pitfall worth noting: `configure_logging(True)` is
intentionally a no-op (verbose just means "don't touch the sink"), which is fine in a real process but
left a PRIOR test's `configure_logging(False)` sink — bound to that test's own already-closed `capsys`
stream — still registered, raising "I/O operation on closed file" instead of ever reaching the
assertion. Not a library bug (a real process never swaps `sys.stderr` out from under a running sink the
way pytest's `capsys` does between tests) — fixed by having that one test manage its own loguru sink
directly rather than relying on `configure_logging(True)`'s deliberate no-op.

## Region Map format change (V38.02) — every routine's rungs/rung_ids came back empty

A downstream agent reported a total regression on a real project (`BPM_TrimmerSorter_VAB_...ACD`,
re-saved from Studio 5000 multiple times in one session): **every single routine, in every
program, returned `rungs == []` and `_rung_ids == []`** — not one routine anywhere had any rung
data — while everything else (tags, UDTs, modules) parsed normally. The same file had parsed
correctly earlier in that same session, against an earlier save; the only thing that changed was
a re-save, and `project.software_revision` on the failing file reads `38.02` (schema revision
1.0) — outside this library's previously-tested V20–35 range (see "Compatibility" in README.md).

Root cause, confirmed directly against the raw bytes: `populate_region_map()` (`export_l5x.py`)
locates the routine → rung mapping in a single per-project `"Region Map"` comps record, via a
hardcoded 78-byte header followed by a `region_length`-bounded dense array of 16-byte
`(parent_id, unknown, seq_no, object_id)` entries. On the V38.02 file, `region_length` (read from
its old fixed offset) came back as `8985` — not even a multiple of 16 — against a real payload of
`86151` bytes; `78 + region_length` (`9063`) landed nowhere near the record's real size
(`86229`). The old loop dutifully parsed 561 entries from this now-meaningless byte range anyway
(no exception, no warning — it's valid-looking binary, just misaligned garbage), and **none of
them matched any real routine/rung `object_id`** — hence a `region_map` table that looked
populated (`561` rows) but joined against precisely zero rungs for precisely every routine.

Confirmed by cross-referencing a specific routine's own `object_id` (`R07_Lift_Skids`, found via
`comps`) against the raw `"Region Map"` record bytes directly (byte-search for the object_id as a
raw `<I` LE value): it appears at 16 scattered offsets, none reachable by the old 78-byte-header
scan, but all 16-byte-aligned to the *same* residue (`offset % 16` constant across every hit) —
i.e. still a dense, gapless array of the same 4-field 16-byte entry shape, just relocated.

**FIRST FIX WAS WRONG — shipped, then caught by the same downstream agent on the very next
message.** The first attempt (committed, then reverted in the same investigation) started entries
at absolute offset 3 and read fields in order `(object_id, parent_id, unknown, seq_no)` —
concluding the field order itself had changed, not just the header size. This looked
well-verified at the time: searching for `R07_Lift_Skids`'s own `object_id` in the parent-id slot
found it every time, the `unknown`/seq field came back as a clean contiguous `0..15` run per
routine, and 5338 of 5340 real rung `object_id`s cross-validated against `SbRegion.Dat` — a
thorough-looking check that was nonetheless **entirely insufficient**, because it only asked "is
this object_id a real rung anywhere in the project?", never "is it the *correct* rung for *this*
routine?". The very next real-world use (opening the same project, reading `R07_Lift_Skids` and
`Continuous/LS_Read`, both routines the user had personally authored and knew precisely) showed
completely unrelated rung content attached to both — syntactically valid ladder logic (so it
passed the "is this a real rung" check), just for the wrong routine entirely, on every single
routine in the project.

**Root cause of the first fix's error, found by resolving real ground-truth rung TEXT (not just
object_id existence) to real `object_id`s**: the user supplied a whole-project Studio 5000 L5X
export of this exact save. For a handful of routines with unambiguous rung text (unique substring
match against the independently-decoded `rungs` table), the real `object_id` for e.g.
`R07_Lift_Skids` rung 0 (`MOVE(Lift_Skids.pntrTpStrt,...)`) was found to sit in the search hit's
**own** 4th field (`hit_offset + 12`), not in the *preceding* 16-byte slot's first field
(`hit_offset - 4`, what the first fix read). In other words: **the field order never changed at
all** — it's the exact same `(parent_id, unknown, seq_no, object_id)` order as every pre-V38
project — the first fix's entry-start offset was simply 4 bytes (one field) too early. Reading 4
bytes early means the "object_id" field silently comes from the END of the PRECEDING entry —
still a real, valid rung `object_id` (hence passing the naive existence check), just belonging to
whichever routine happens to own the entry immediately before this one in the table. This is why
*every* routine came back non-empty and structurally plausible, and *every* routine's content was
wrong: the true entry boundary is offset **7**, not 3 (`hit_offset - 4` vs the correct
`hit_offset`, since the parent_id field the search matched IS the entry's own first field, not its
second).

- **Layout is byte-for-byte identical in substance to every pre-V38 project**: the same 16-byte
  tuple `(parent_id, unknown, seq_no, object_id)`, same field order — the *only* thing that
  changed for V38.02 is the header size (7 bytes instead of 78) and the fact that there is no
  length field to bound the array at all; entries just run to the exact end of the record.
- Re-verified against the user's whole-project L5X ground truth, this time checking actual rung
  TEXT content (not just `object_id` existence) for every RLL routine in the project: **147 of 149
  routines matched byte-for-byte** (full rung sequence, in order) at this point in the
  investigation. The 2 remaining mismatches were each missing exactly **one** rung — see "Region
  Map entries can go missing independently of the format" below for the follow-up investigation
  that found (and recovered) the actual cause; both are now also exact matches (**149/149**).

Fixed by splitting entry extraction into two pure generator functions,
`_iter_region_map_entries_v_pre38()` (old behavior, byte-for-byte unchanged) and
`_iter_region_map_entries_v38()` (new layout: 7-byte header, dense to EOF, **same field order** as
pre-V38), with `populate_region_map()` picking one via the `78 + region_length == len(record)`
check above and logging a `log.warning()` on the fallback path so a *third*, still-different
future layout would be visible rather than silently misparsed the same way this one was. Full
existing test suite (all pre-V38 fixtures, which exercise only the old layout) passes unchanged.
Covered by `test_iter_region_map_entries_v_pre38_reads_dense_16_byte_entries`,
`test_iter_region_map_entries_v38_reads_dense_16_byte_entries_from_offset_7`, and
`test_populate_region_map_falls_back_to_v38_layout_when_header_length_is_stale`
(`test/test_database.py`) — synthetic records, since no V38.02 fixture is checked into this repo.

**The methodological lesson, stated plainly since it cost a full extra round-trip here**: a check
that only confirms "this looks like a real object of the right general kind" (any valid rung
`object_id`, a clean contiguous sequence field) is not evidence of *correct attribution* — it will
pass just as easily on data that's shifted by exactly one record. The only check that actually
catches an off-by-one-record bug is comparing real, specific, known TEXT CONTENT against ground
truth for multiple routines, not structural/statistical plausibility. This is the same lesson
already stated elsewhere in this file after other investigations; it applied again here just as
forcefully.

**Bonus finding from the same ground-truth sweep, unrelated to Region Map**: 15 of the 16
mismatches found *before* the offset-7 fix (all with matching rung *counts*, only content
differing) turned out to be a separate, pre-existing bug — `GSV(Module, ...)` instructions
resolving to `__Map:VAB_SERVER_Bridge`/`__Map:FencePositioner1`/etc. instead of the real
`VAB_SERVER_Bridge`/`FencePositioner1`. A `"__Map:"`-prefixed comps object is a distinct internal
shadow entry for some devices/modules (confirmed: different `object_id`, different parent, from
the real Module object of the same base name) that some raw `@HEX@` references in `SbRegion.Dat`
point at instead of the real object — real Studio 5000 never emits the `"__Map:"` prefix itself.
Fixed by stripping it once, at the single shared source (`name_lookup` construction in
`ExportL5x.__post_init__`) used by both rung-text resolution (`SbRegionRecord.parse`) and
tag-reference write-back (`_restore_tag_refs`), so both stay consistent. Verified: 0 of the 15
previously-affected routines show any remaining difference.

**Not chased further**: whether V36/V37 also use this new layout, or introduce a third one, is
unknown — only V35-and-earlier (old layout, many real projects) and this one real V38.02 project
(new layout) have actually been observed. If a future file trips neither invariant cleanly, that's
new territory, not a bug in this fallback.

## Region Map entries can go missing independently of the format — recovered via RegnLink.Dat

Follow-up to the offset-7 fix above: the 2 remaining routines with a missing rung turned out to be
a *different*, unrelated problem, not another Region Map parsing bug — and it's genuinely
recoverable, not a dead end.

**First, ruled out "V38.02 always drops entries"**: the user provided a *third* real save of the
same project, from earlier the same day (`_OLD/BPM_TrimmerSorter_VAB_20260810.ACD`, 11:56am,
restored from a backup after two later same-day saves at 14:50 and 16:17 both exhibited the bug).
This earlier file's own Region Map record independently satisfies the *pre-V38* invariant exactly
(`78 + region_length == len(record)`, no fallback triggered) — **and its own
`project.software_revision` also reads `38.02`**, identical to the two later, broken-layout saves.
This rules out firmware version as the layout trigger entirely (a real, hardening finding worth
keeping): whatever changed the on-disk Region Map layout happened during a specific save operation
sometime between 11:56am and 2:50pm that same day, not as a function of Studio's own version
number. Not chased further than that (would need the exact edit/save sequence from that window,
which wasn't available) — but this matters for how to read `software_revision` going forward: it
is NOT a reliable predictor of which Region Map layout a given file uses. `populate_region_map()`'s
own byte-level invariant check is the only trustworthy signal, which is exactly why the fix doesn't
key off firmware version at all.

**Then, tracing the two specific missing rungs through this earlier 11:56am file** (same project,
same routines, some content already different by the later saves) found two *different* root
causes for what looked like the same symptom:

- `Fence_Axis_2_Ctrl`'s missing rung (`GSV(Module,FencePositioner2,Mode,Module_Servo2_Mode);`) —
  **same exact `object_id`, same exact text**, genuinely present with a correct, live
  `region_map` row in the 11:56am file (`parent_id` = `Fence_Axis_2_Ctrl`'s own object_id,
  `unknown`/seq = 2, matching its real position). By the next save, that row is simply gone,
  even though nothing about the rung itself changed. This is real, reproducible **Region Map
  entry loss on an otherwise-untouched rung**, caused by whatever Studio operation rewrote the
  table's layout in that save window.
- `R07_Lift_Skids`'s missing rung (`...ONS(Lift_Skids.Ons[1])...`) has **no match at all** in the
  11:56am file's independently-decoded `rungs` table — it didn't exist yet. It was authored
  sometime after 11:56am, and its Region Map entry apparently was never written in the first
  place (or was dropped in the same event that dropped the other one — can't distinguish which
  from the data available).

**Recovery mechanism**: `RegnLink.Dat` (already used elsewhere for rung-*comment* attribution, see
"Rung comments" above) turned out to still hold fully intact, correctly-typed (`type=0x00020000`,
not the `0xFFFF0000` dead marker) link records for *both* rungs — independent of Region Map
entirely. Each 22-byte record already documented in `populate_regnlink()`'s own docstring as
`[0:4] owner_id, [4:8] own_id, [8:12] next_id, ...` directly gives `(routine_id, this_rung,
next_rung_in_sequence)` — a completely separate, still-correct source for exactly the ownership
question Region Map normally answers. Verified directly for both real cases (raw byte-scan of
`RegnLink.Dat` for the rung's own `object_id` in the `own_id` slot): `Fence_Axis_2_Ctrl`'s record
resolves `next_id` to `429377538`, confirmed via the independently-decoded `rungs` table to be the
*exact* real next rung's text (`XIC(Test_Axis.0)...`, ground-truth rung 3); `R07_Lift_Skids`'s
record resolves `next_id` to `1359329597`, confirmed to be its real next rung too
(`XIC(Lift_Skids.ActvtnArea)...`, ground-truth rung 3). Both chain records line up exactly with
ground truth, confirming this is genuinely recoverable data, not a coincidence.

**Fix**: `populate_regnlink()`'s existing single linear scan of `RegnLink.Dat` (already walking
every byte for the `regnlink`/`regnlink_idx` tables) now also captures `(routine_id, own_rung,
next_rung)` into a new `regnlink_chain` table, filtered by the same `type != 0xFFFF0000` liveness
check already used for `regnlink`. `RoutineBuilder.build()`, after building its rung list from
`region_map` as before, cross-checks `regnlink_chain` for its own `routine_id`: any `own_rung` not
already in the list (and whose text still exists in `rungs` — nothing recoverable otherwise) gets
spliced in, positioned by chain lookup (before its own `next_rung` if that's already in the list;
otherwise right after whichever other chain record's `next_rung` points at it; otherwise appended
at the end as a last resort, logged via `log.warning()` either way so a recovery is always visible,
never silent). This mirrors the resolution order already established for `RegnLink.Idx`-based
comment attribution ("prefer the entry that resolves to something real over guessing").

**Verified end-to-end**: re-ran the full 149-routine ground-truth sweep against the user's
whole-project L5X export — **149/149 exact matches**, both previously-incomplete routines now
recovering their missing rung in exactly the right position (confirmed by the `log.warning()`
firing for both: `Routine 2809983382: recovered 1 rung(s) ... [4294631627]` and `Routine
498307360: recovered 1 rung(s) ... [1520403580]`). Full existing test suite unaffected. Covered by
`test_routine_builder_recovers_rung_missing_from_region_map_via_regnlink_chain` (splices into the
correct middle position, via a real 14-rung routine in the `CuteLogix.ACD` fixture with one entry
deleted and its RegnLink.Dat chain data faked to match) and
`test_routine_builder_appends_recovered_rung_when_no_chain_neighbor_is_present` (the append-at-end
fallback, with both directions of the real chain data around that rung deliberately removed to
isolate it) — both in `test/test_database.py`.

**Caveat**: this recovers a rung whose Region Map link is gone but whose `RegnLink.Dat` link
survives. If *both* are gone (not observed in either real case here, but not provably impossible),
the rung is still lost to this library the same as before — `RegnLink.Dat` is a second chance, not
a guarantee. If a rung count still looks short after this fix, that's the remaining possibility
worth knowing about, not a sign this fix is incomplete.

## Lazy / summary-first lookups: `get_project_summary()`, `list_routines()`, `list_tags()`, `get_tag_value()`

Added in preparation for a possible future MCP server wrapping this library (not built yet, see
the "MCP server competitive landscape" auto-memory from that discussion if you're picking this
back up) -- the design goal discussed was that a persistent session (load once, keep the
`Controller` object graph alive across many tool calls) solves the *load-time* cost problem, but
tool call *responses* still need their own discipline: an MCP tool returning a whole project's
tags/routines/UDTs in one call would blow a caller's context budget regardless of how cheap the
underlying load was. These four functions (`acd/api.py`) give any caller (MCP wrapper or
otherwise) a summary-first / drill-down-on-demand shape to build on, instead of every future
caller re-deriving "walk `project.controller...` and decide what to include" from scratch:

- `get_project_summary(project)` -- names/counts only (program/task/data-type/AOI/module names,
  controller + per-program tag counts, total routine count). Meant as the very first call.
- `list_routines(project, program_name=None)` -- name/type/line-count per routine, no rung/ST
  content at all. Pairs with the already-existing `get_routine()` for one routine's actual logic.
- `list_tags(project, program_name=None)` -- name/data_type/dimensions/description per tag,
  deliberately WITHOUT `_initial_value` -- a UDT array tag's decoded value can be large enough on
  its own (see below) to matter even in a "just list what's here" call. Filters through the same
  `Tag._l5x_exclude` already used elsewhere (I/O tags, hex placeholders) since those were never
  "real" listable tags to begin with.
- `get_tag_value(project, tag_name, program_name=None, offset=0, limit=50)` -- the drill-down
  counterpart to `list_tags()`: one tag's actual value, but paginated if it's a top-level array
  (`total_elements`/`offset`/`returned` alongside a `value` slice) rather than ever returned in
  full. Scalar values (including a scalar struct's whole dict, e.g. a TIMER) are always returned
  in full -- pagination only applies to the outer list, mirroring the exact same "large container,
  not large scalar" distinction `diff_project()`'s own `_summarize_value_diff()` already draws for
  the same underlying reason (a real project's `To_VABView_Bins[50]`/`LugTrm[200]`-style tags are
  where this actually bites, not scalar tags).

All four are pure read-only additions over the existing object model -- no new binary parsing, no
interaction with the write/export path, verified against the real `CuteLogix.ACD` fixture
(including its real `Branching` `DINT[1000]` tag for the pagination case, `Map:Local` for the
I/O-tag-exclusion case) in `test/test_api.py`.

## Second convenience-API batch: `new_tag()`, `diff_lines()`, `validate=True`, log-level split

A second round of downstream-agent friction feedback (same style as the batch below, reflecting
on real friction points from a session rather than hypotheticals), addressed in one pass:

- **Log noise.** `DataType 'X': N deleted member(s) with no type descriptor found...` and
  `DataType 'X': N member-collection child row(s) marked deleted... correctly ignored` (see
  "Phantom UDT members" above) are self-healing confirmations, not problems — they fired at
  WARNING on every load of any project with a historically-deleted UDT member (common in real
  long-lived projects), forcing a downstream caller to `grep -v` them out by hand to find
  genuinely actionable signal (e.g. the connection-type-code fallback warning, which explicitly
  asks the user to report it). Downgraded both to `log.info()` — the WARNING-and-above quiet-mode
  filter now suppresses them for free, no new parameter needed. The `member_count` mismatch
  diagnostic ("possible remaining stale/phantom member data; investigate") stays at WARNING since
  it's explicitly framed as something to check, not a confirmed non-issue.
- **`load_acd()`/`ExportL5x`'s `verbose` default flipped from `True` to `False`**, per direct
  follow-up feedback in the same conversation ("I feel like verbose=false should be the default"):
  previously a caller had to know to pass `verbose=False` to get quiet output; now `load_acd(path)`
  alone is quiet (WARNING and above only) and `verbose=True` opts INTO the ~15-20 lines of
  INFO/DEBUG progress output. Matches this library's primary audience (AI agents operating under a
  token budget) defaulting to the token-cheap behavior rather than requiring every caller to
  discover and pass the flag themselves. `ExportL5x.verbose` (the field `load_acd()` itself
  forwards to) flipped the same way, so the lower-level entry point (`ImportProjectFromFile`, the
  `export_l5x.py` CLI) is quiet by default too, not just `load_acd()`. This is a real, if narrow,
  behavior change for any existing caller relying on the old loud-by-default behavior without
  passing `verbose=` explicitly — flagged here rather than treated as a pure no-risk addition like
  the rest of this batch.
- **`new_tag(name, data_type, dimensions=None, description=None, value=None,
  external_access="Read/Write")`** (`acd/l5x/elements.py`), mirroring `new_member()`: a downstream
  agent had hand-rolled `Tag(_name=..., name=..., tag_type="Base", ...)` positional construction
  "probably a dozen times" in one session — the same class of easy-to-misuse-by-hand risk
  `new_member()` was already hardened against. `radix` has no override parameter (unlike
  `new_member()`) since a tag's Radix is never an independent choice from its type; derived from
  `_PRIMITIVE_RADIX`, `None` for a UDT-typed tag (matching every ACD-decoded struct-typed `Tag`,
  which carries no Radix attribute — confirmed via `TagBuilder.build()`). Note (flagged by the
  same feedback, documented rather than "fixed" since both conventions are already correct and
  relied upon elsewhere): `Tag.dimensions` already uses `None` as its own scalar convention
  (unlike `Member.dimension`, which needs `0`, not `None` — see `new_member()`'s own
  `dimension=None` guard above) — `new_tag()` needs no equivalent guard because `Tag.dimensions`'s
  `None`-means-scalar convention was already correct on every ACD-decoded `Tag`.
- **`diff_lines(old, new)`** (`acd/api.py`): the `difflib.SequenceMatcher` alignment primitive
  `diff_routine()` already used internally, extracted and exposed directly for when a caller
  already has two plain line lists (verifying their OWN in-memory edit to `.rungs`/`._st_lines`
  before calling `export_routine()`) rather than two full `Routine` objects — a downstream agent
  had hand-rolled the same ~20-30 line `SequenceMatcher` boilerplate three separate times in one
  session for exactly this. `diff_routine()` refactored to call it rather than duplicate the logic.
- **`export_routine(..., validate=True)`** (opt-in, default `False`): recursively verifies every
  struct-typed name reachable from a referenced tag's own `DataType` tree resolves in
  `data_types_map` (a primitive, string-family type, built-in Logix struct, or a real project
  UDT/AOI) before any XML is written, raising `ValueError` naming the specific tag/member/type
  responsible. This is the eager version of the exact check that was missing when the
  `Tag._data_types_map` staleness bug (see "Mutating a UDT with live tag instances..." below) was
  silently producing a wrong-shaped `<Tag>` — an unresolved type never raised on its own before,
  it just fell into `_zero_value_for_member()`'s "harmless scalar zero" fallback, and the *only*
  way that was ever caught was a real Studio 5000 import rejecting the file. `validate=True` moves
  that same check to before-export time, at the cost of one extra pass over the referenced-type
  graph — off by default so no existing caller's behavior changes.

None of these four are re-verified against a live Studio 5000 import in this round (unlike
`insert_rung()`/`export_datatype()`/the region-map fixes above) — `new_tag()`/`diff_lines()` are
pure Python-side conveniences with no new XML shape, and `validate=True` only ever runs *before*
XML is written (it either raises or is a no-op on the exact same file `validate=False` already
produces), so there was nothing new to import-test. Covered by unit tests in
`test/test_api.py`/`test/test_elements_helpers.py` (`test_new_tag_*`, `test_diff_lines_*`,
`test_validate_tag_types_resolve_*`, `test_export_routine_validate_raises_on_unresolved_type`).

## Lookup/editing convenience API — `insert_rung()` verified end-to-end in real Studio 5000

Added `get_routine()`, `tag_exists()`, `find_tag_references()`, `replace_rung_safe()`, and
`Routine.insert_rung()`/`.delete_rung()` (`acd/api.py`, `acd/l5x/elements.py`) based on concrete
feedback from a downstream agent's session building 8+ new routines/tags against a live project —
four hand-rolled patterns (nested program→routine lookup, manual `_rung_comments` index shifting
on insert/delete, an unguarded rung overwrite, a repeated substring scan for existing tag/member
usage) kept recurring and had already caused two real index-arithmetic mistakes in one session.
`insert_rung()`/`delete_rung()` never touch `_rung_ids` positions beyond inserting/removing a
`None` placeholder to keep it the same length as `.rungs` — confirmed safe because `Routine.to_xml()`
(what `export_routine()` actually renders) only ever reads `.rungs`/`._rung_comments`, never
`_rung_ids` (that field exists solely for `patch_rungs()`, which can only edit an *existing* rung
by its real object_id, never insert a new one).

**Confirmed working end-to-end**: `insert_rung(5, new_text, comment=...)` on a real routine
(`MainProgram/MainRoutine`, `Rung_Comments_Test_Project.ACD`) — with a comment pre-attached to the
original rung 8, specifically to verify the comment-shifting arithmetic — exported via
`export_routine()`, imported into real Studio 5000 via native Import Routine with zero errors, the
result saved back to a new ACD by the user, then read back through this library's own `load_acd()`.
The read-back matched the pre-import prediction exactly: 12 rungs, the new rung at index 5 with its
own comment, the pre-existing comment now correctly on index 9 (shifted from 8), every other rung's
text and position otherwise untouched. One routine, one insertion point — not exercised yet: a
delete, multiple inserts in the same call, or inserting into a routine with `RegnLink.Dat`/rung-ID
peculiarities of its own.

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

## `export_routine()` for ST routines — dependency scan was RLL-only

`Routine.to_xml()` already rendered ST routine content correctly (`<STContent><Line .../>`, see
"Structured Text (ST) routine content" below) since that was built independently of
`export_routine()`. But `export_routine()`'s own dependency discovery (which controller-/program-
scope tags, UDTs, Modules, and called routines to pull in as `Use="Context"`) scanned only
`routine.rungs` — always empty for an ST routine, whose source lives in `._st_lines` instead — so
exporting an ST routine silently produced an empty `<Tags Use="Context">` with none of its real
tag/module/routine references included, even though the routine's own `<STContent>` rendered fine.
Fixed by routing every scan (`_referenced_tag_names`, `_referenced_modules`,
`_referenced_called_routines`) through `_routine_lines(routine)` — a small existing helper (already
used by `diff_routine()`) that returns `.rungs` for RLL or `._st_lines` for ST. No new ST-specific
scanning logic was needed: an ST routine's identifier syntax (member access via `.`, instruction/JSR
calls via `(`) is the same as RLL's for the purposes of these regex-based scans. `TargetSubType` in
the wrapper was already generic (`routine.type`), so `"ST"` was already correctly emitted once the
dependency scan was fixed. Verified against the real `ACDTestsNonRedundant.ACD` fixture's `STRoutine`
(source references controller-scope tags literally named `DINT`/`UDINT`/`ULINT`, this fixture's own
naming convention): all three now correctly appear as full `<Tag>` context elements in the export.
Covered by `test_export_routine_st_routine_pulls_in_referenced_tags` (`test/test_api.py`) — confirmed
this test fails without the fix, not just that it passes with it.

## `export_datatype()` — create/modify a UDT (now verified against real Studio 5000)

Added per user request (concrete example: insert a new member in the middle of the real `Lug`
UDT). Same "native-import escape hatch" architecture as `export_routine()`: exports a single
`DataType` (plus its transitive dependency closure via the already-existing `_resolve_type_closure()`)
as a standalone partial L5X, for Studio 5000's own **"Import Data Type..."** command (right-click
the Data Types folder) — sidestepping `save_acd()`/raw `Comps.Dat` writing entirely, the same way
`export_routine()` sidesteps it for rungs/tags.

- `new_member(name, data_type, dimension=0, radix=None, description=None)` (`acd/l5x/elements.py`)
  builds a plain (non-BIT, non-hidden) `Member` with a sensible default `Radix` (`_PRIMITIVE_RADIX`
  lookup, or `"NullType"` for a struct-typed member) — constructing `Member` directly is awkward
  (duplicate `_name`/`name` positional args, no radix default).
- To modify an existing UDT: mutate `dt.members` directly (`dt.members.insert(i, new_member(...))`
  inserts at a specific position — Studio recomputes the real byte layout on import, since
  `Member._byte_offset` is an internal decode-only field never emitted in XML at all). To create a
  brand-new UDT: build a `DataType` and append it to `project.controller.data_types` first, then
  export the same way — no special-casing needed, matching the already-proven "new vs existing tag"
  pattern (see "Native-import escape hatches" above).
- Wrapper shape (`<DataTypes Use="Context">` containing every dependency's full `<DataType>` element
  plus the one being edited/created with `Use="Target"` injected via the existing
  `_inject_use_attr()` helper, `TargetType="DataType"`, no `<Tags>`/`<Programs>`/`<Modules>`
  sections) was built by **direct symmetry** with `export_routine()`'s already-verified wrapper —
  it has **not** been confirmed against a real Studio 5000 "Export Data Type" output, nor against a
  real "Import Data Type..." attempt. Given how many real-import rounds it took to get
  `export_routine()`'s shape exactly right (see "Partial/context L5X exports" below — a crash, a
  missing `Use=` rule, several tag-rendering gaps, all only found via actual import attempts), expect
  this to need the same kind of iteration once tested against real Studio 5000.
- Verified structurally (without Studio 5000): generated XML is well-formed, the target `DataType`
  carries `Use="Target"` and nothing else does, and a member inserted at a given list index appears
  at the correct position in the rendered `<Members>` — covered by
  `test_export_datatype_inserts_member_at_requested_position` (`test/test_api.py`), using the
  `UDT_Test` fixture in `resources/ACDTestsWithAOI.ACD`.
- **Confirmed working end-to-end in real Studio 5000** (a downstream agent's session, real project,
  V38.02): a standalone `export_datatype()` call — new UDT member inserted into an existing type,
  no routine/tag involved — imported via "Import Data Type..." with zero errors. See the next
  section for a real, *adjacent* bug this same session found (and this library has since fixed):
  mutating a UDT's members and *then*, in the same session, exporting a routine referencing an
  existing tag of that type.

## Mutating a UDT with live tag instances, then exporting a routine in the same session (fixed)

Found via a real Studio 5000 import rejection in the same session that confirmed `export_datatype()`
itself works (previous section): add a new member to an existing `DataType` that already has live
tag instances (e.g. a `Bin` UDT with 50 instances via a `To_VABView_Bins[50]` controller tag), then
— **in the same session, no reload** — `export_routine()` a routine whose logic references one of
those existing instances (`To_VABView_Bins[i].NewMember...`). Studio rejected the import:

```
Error: Failed to set the 'Data' property (Data type mismatch - the object's value does not
match its data type.).
    RSLogix5000Content/Controller/Tags/Tag[@Name="To_VABView_Bins"]/Data
```

**Root cause**, confirmed by the downstream agent's own direct inspection of the exported XML
before reporting it: a tag's decoded value (`Tag._initial_value`, produced once by
`_decode_single_udt_element` from the ACD's raw stored bytes at `load_acd()` time, using
`DataType.members` *as it existed then*) is a plain Python dict/list snapshot — appending a new
`Member` to `DataType.members` afterward (the documented, correct way to mutate a UDT for
`export_datatype()`, see above) has no way to reach back and retroactively add the new member's key
to every already-decoded tag value of that type. The exported `<Tag>` element's `<DataType>`
declaration (rendered fresh from the current, mutated `DataType.members` at export time) then
disagrees with its own `<Data Format="L5K">`/`<Data Format="Decorated">` value blocks (rendered from
the stale decoded dict, one member short) — an internally inconsistent file Studio correctly refuses.

Confirmed narrowly scoped, not a general `export_routine()`/`export_datatype()` regression: a pure
`export_datatype()` call (no tag values involved) is unaffected; a brand-new tag created in the same
session is unaffected (no prior stored value to be stale); only "mutate an existing UDT with live
instances, then export something that carries one of those instances' *values*" triggers it.

**Fixed** by making the two value-rendering functions that walk `DataType.members` and look up each
member's value in the decoded dict (`_l5k_udt_literal`, `_udt_scalar_to_xml`, `acd/l5x/elements.py`)
zero-fill a member missing from that dict instead of silently skipping it — via a new
`_zero_value_for_member()` that synthesizes a Studio-consistent zero/default (0, 0.0, `{"LEN":0,
"DATA":""}` for a string-family type, or a recursively zero-filled dict for a nested struct,
matching each member's own dimension/type), **mirroring what Studio 5000 itself does natively**
when a UDT member is added to a type with existing instances via its own editor (per the downstream
agent's own stated expectation, matching real Studio behavior). Both `_udt_scalar_to_xml` (used for
both scalar UDT tags and, per-element, array-of-UDT tags like `To_VABView_Bins[50]`) and
`_l5k_udt_literal` (same recursion structure) share the one fix point — an array-of-struct tag needed
no separate handling. Also incidentally hardens the same two functions against a member that decodes
to a real Python `None` for an unrelated reason (`_decode_scalar_member` returns `None` for an
unrecognized member type) — previously silently dropped too, now zero-filled the same way.

Covered by `test_l5k_udt_literal_zero_fills_scalar_member_missing_from_decoded_value`,
`test_l5k_udt_literal_zero_fills_struct_member_missing_from_decoded_value` (the real reported shape:
the new member was itself a struct type), `test_udt_scalar_to_xml_zero_fills_member_missing_from_decoded_value`,
and three direct `_zero_value_for_member()` unit tests (scalar/array/nested-struct) —
`test/test_elements_helpers.py`.

**Follow-up crash found on the very next retry, before the fix above could even be re-exercised**:
`TypeError: '>' not supported between instances of 'NoneType' and 'int'` at `_zero_value_for_member`'s
own `if member.dimension > 0:` — not `Criteria_Qty` itself (array-typed, fine), but some other member
reached while zero-filling (most plausibly a scalar member of the newly-created `Bin_Criteria_Qty`
type, constructed with a `Member.dimension` of `None` rather than `0`). Root cause: `Member.dimension`
is documented/typed as `int` (0 = scalar) and every ACD-decoded `Member` (`MemberBuilder.build()`)
always sets a real int here via `struct.unpack_from` — but `new_member()`'s own signature
(`dimension: int = 0, radix=None, description=None`) makes `None` look like a valid "use the
default" sentinel by analogy with its other two params, when it silently isn't; a value only this
function itself was ever exercised against directly (`_zero_value_for_member` is new code, the first
real-world path to actually *read* `member.dimension` on a member the user authored with a mistake
that far upstream). Fixed in two places, not just the crash site: `member.dimension and
member.dimension > 0` (treats `None` the same as `0`, i.e. scalar) in `_zero_value_for_member`
itself, AND `new_member()` now raises `ValueError` immediately if called with an explicit
`dimension=None`, catching the actual mistake at its source rather than letting a bad value silently
propagate until some unrelated, far-later crash — the same "fail fast" preference already applied
elsewhere in this file. Covered by `test_zero_value_for_member_handles_none_dimension`
(`test/test_elements_helpers.py`) and `test_new_member_rejects_none_dimension` (`test/test_api.py`).

Still not yet re-verified against a real Studio import of the exact originally-failing case (the
user's own `Bin`/`Criteria_Qty`/`To_VABView_Bins` project) — structurally verified and unit-tested
twice over now, not yet confirmed by an actual third live-Studio retry.

**Third bug found on the SAME retry, once the `None`-dimension crash above was fixed on the caller's
own side (their script's mistake, not a library bug — see above) and the export finally completed
without error**: the new struct-typed member rendered, but as a bare `Value="0"` scalar instead of a
real nested structure — silently wrong, not a crash, which the reporting agent correctly flagged as
worse (nothing signals anything is broken; Studio wouldn't reject this file, it would just import a
broken value). Root cause, traced precisely by the reporting agent before handing it off:
`Tag._data_types_map` (a case-insensitive name → `DataType` map used by `Tag.to_xml()`/
`_udt_scalar_to_xml`/`_l5k_udt_literal`) is assigned once per tag, by reference, during the whole
`load_acd()` build (`ControllerBuilder.build()`/`ProgramBuilder.build()`) — but every already-built
`Tag` was found to share the literal SAME dict object (Python assigns dict references, not copies,
and the same `data_types_map` local variable is threaded through the whole builder call chain
unchanged). Appending a new `DataType` to `project.controller.data_types` (the documented, correct
way to register a new UDT — see `export_datatype()` above) updates a completely different
collection; nothing ever adds the new type to that shared map. `_zero_value_for_member()` (added in
the first fix above) then looks up the new struct type, gets `None` back from the stale map, and
silently takes its own "unknown type — a harmless scalar zero beats crashing" fallback — a fallback
whose own premise doesn't hold for a type that's real but simply missing from a stale map, not
genuinely unknown.

Fixed by exposing that same shared dict as `Controller._data_types_map` (`ControllerBuilder.build()`
now passes the same `data_types_map` object it already threads through every builder into the
`Controller` constructor too — zero behavior change, just a new reference to the same object) and
adding `_sync_data_types_map(project)` (`acd/api.py`), which registers any `project.controller.data_types`
entry missing from that shared map (`setdefault`, so it only ever adds — never overwrites an existing
entry, which already reflects any in-place member mutation correctly since it's the same object
either way). Both `export_routine()` and `export_datatype()` call this once, right after their own
initial validation, before any rendering — so a caller following the already-documented "just append
to `.data_types`" pattern needs no workflow change at all; the sync happens transparently. Because
every tag shares the ONE dict object, updating it through this ONE reference (`project.controller.
_data_types_map`) is instantly visible to every tag everywhere — no need to walk the whole project's
tags.

Verified directly: constructing a tag whose `_initial_value` was decoded against a 1-member type,
then appending a new struct-typed member (registering the struct type via `.data_types.append()`
only, exactly the documented pattern) and calling `Tag.to_xml()` WITHOUT the fix reproduces the
reported shape exactly (`<DataValueMember Name="NewStruct" DataType="Inner" Radix="Decimal"
Value="0"/>` — a bare scalar); calling `_sync_data_types_map()` first produces the correct
`<StructureMember Name="NewStruct" DataType="Inner">` with its real nested `<ArrayMember>` content.
Covered by `test_sync_data_types_map_propagates_new_type_to_existing_tags` (`test/test_api.py`),
which asserts both the new member's correct shape AND that the pre-existing member still renders
unaffected. No local fixture has a UDT-typed tag instance to exercise this end-to-end through
`export_routine()` itself, so this is verified at the `Tag.to_xml()`/`_sync_data_types_map()` level
directly, constructing the exact before/after scenario by hand rather than through a real ACD file.

**Three real bugs found in one investigation, each only surfacing once the previous one was fixed
and the retry got one step further** — first the crash-free-but-wrong "Data type mismatch" Studio
rejection, then a `None`-dimension crash in the first fix's own new code (a caller mistake, but one
the library's own `new_member()` signature invited), then this silent wrong-value bug one step
further still. Worth remembering next time a "fix" for a reported UDT/tag-value bug looks complete
after one retry: a retry that gets further than the last one is progress, not proof the whole chain
is now clean — this one needed three real rounds before actually converging, this project's
"structurally verified ≠ actually correct" lesson applying yet again.

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

**CORRECTION, found much later**: point 1 above was never actually fixed in code, despite this
section's own text (and the commit message that introduced it) explicitly saying "multiple of 4."
The real code was `return max_end + (max_end % 2)` — **round to even**, the exact narrower guess
this section says was corrected. Nobody caught it because the one real-world case used to verify it
(`Encoder`, 263 → 264) is ambiguous: 264 is both the next even number *and* the next multiple of 4
above 263, so it can't distinguish the two rules — `Lug`/`LugWrk` were already multiples of 4 going
in, so they couldn't distinguish it either. Surfaced by a real project (`FenceSkid`, members summing
to 13 bytes, used as an array-of-struct's own element type via `FenceGate.Skid[2]`): even-rounding
gave 14 instead of the correct 16, a 2-byte-too-small size that — because this function's return
value doubles as an **array element's stride** wherever the same struct type is used as an array —
silently shifted every field of `FenceGate[1]` and `FenceGate[2]` (every element beyond index 0) by
one member position, confirmed via a real Studio 5000 "Tag Name Collision" dialog. Fixed for real
this time: `return -(-max_end // 4) * 4`. New regression test that actually distinguishes the two
rules (13 bytes → 16, not 14):
`test_get_type_size_rounds_up_to_multiple_of_4_not_merely_even` — confirmed to fail under the old
`% 2` code. The methodological lesson repeated a third time in this file now: a fix "verified"
against a single real-world data point that happens to be ambiguous between the old and new
behavior is not verified at all; the previous 99-`DataType`/369-array whole-project sweeps
apparently never happened to include a struct whose size had a non-multiple-of-4 remainder *and*
was also used as an array element type, which is the exact combination needed to expose this.

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

## Persistent project DB (`acd/l5x/project_db.py`) — `db_*` functions / `open_project_db()`/`ProjectDB`

Added after a downstream agent's session hit a real correctness bug: since raw ACD write-back is
blocked (see "ACD write-back" below — no `FileInfo.Dat` signing key, so the only durable edit path
is a real Studio 5000 import of an `export_routine()`/`export_datatype()` output), every one-off
Python script in that session did a fresh `load_acd()` from the same unmodified `.ACD` on disk.
Tags created via `new_tag()` in one script existed only in that process's memory — the next
script's fresh load didn't have them, and `export_routine()`'s dependency scan silently omitted a
`<Tag>` for any name it couldn't find, producing a quietly incomplete export with no error. The
user's framing, arrived at through discussion, is the design target: this should feel like editing
Studio 5000's own **offline project state** — an edit (`new_tag`, add a UDT member, insert a rung,
set a comment) writes directly into the current project, no separate "pending edits"/journal/replay
concept — until it's explicitly rebuilt from the real `.ACD` (a new Studio save, detected via the
source file's mtime, or an explicit `rebuild=True`).

**Where it lives, concretely**: `ExportL5x` already builds a real SQLite file (`acd.db`) next to
the source `.ACD` on every load (see "Ingestion robustness" area of `export_l5x.py`) — not
`:memory:`, and (when constructed directly, not via `load_acd()`) not a throwaway temp dir either,
it defaults to a stable directory next to the ACD. `project_db.py` adds a second, higher-level set
of tables to that SAME file — normalized/decoded shape (`proj_data_types`, `proj_members`,
`proj_programs`, `proj_tags`, `proj_tag_comments`, `proj_routines`, `proj_rungs`, `proj_st_lines`,
`proj_meta`) sitting alongside the existing raw binary-decode tables (`comps`, `rungs`,
`region_map`, `comments`, `nameless`, `regnlink*`) that `ExportL5x` itself owns. **Every new table
is prefixed `proj_` deliberately** — the first working version used unprefixed names and its own
`rungs` table silently collided with (and `DROP TABLE`'d) `ExportL5x`'s own raw `rungs` table
(`object_id, rung, seq_number`), breaking `RoutineBuilder` with `no such column: r.rung` the moment
anything tried to read raw rung data again. Caught immediately by a real end-to-end smoke test, not
by code review — a reminder that adding tables to a database you don't fully own the schema of
needs an explicit namespace, not just "these names sound fine."

**Key architectural decision: rehydrate into the existing object graph, never rewrite rendering.**
`export_routine()`/`export_datatype()`/`Tag.to_xml()`/`_resolve_type_closure()` are the most
heavily real-Studio-import-verified code in this whole library (see the "Native-import escape
hatches" and "Partial/context L5X exports" sections above — many rounds of real import failures
fixed one at a time). Rewriting them to query SQL directly would re-risk all of that for no benefit.
Instead, `ProjectDB.to_controller()` rehydrates a **fresh, real `Controller`/`RSLogix5000Content`**
object graph from the `proj_*` tables (cheap — plain `SELECT`s into dataclasses, no binary
decoding) and hands it to the exact same, unmodified `export_routine()`/`export_datatype()`
functions. `.modules`/`.aois`/`.tasks` and every Controller-level scalar field are **not** part of
the new tables at all (nothing edits these yet — v1 scope is tags, UDT members, rungs, tag comments
only, matching exactly what caused the original bug report) — `to_controller()` re-derives them
fresh from the raw tables via the same `ControllerBuilder` a plain `load_acd()` uses, every call.
This is zero new decode logic for that part, at the cost of redoing that decode work (though never
re-parsing/re-unzipping the source `.ACD` itself) every time `to_controller()` runs — an accepted
v1 simplicity tradeoff, not a bug; a future pass could special-case skipping the tag/data_type
portion of that rebuild if the redundant decode cost ever actually matters in practice.

**A second real bug, also only caught via an end-to-end smoke test, not by review**: shifting
`rung_index` (or UDT member `seq`) values with a single `UPDATE ... SET x = x + 1 WHERE x >= ?`
against a table with a `UNIQUE(routine_id, rung_index)` index intermittently raised
`sqlite3.IntegrityError` — SQLite does not guarantee the row-processing order of a single UPDATE
statement, so shifting row A (index 5→6) can momentarily collide with row B (still at 6, not yet
processed) even though the *final* state after the whole statement would be perfectly valid; SQLite
has no deferred-UNIQUE-constraint mechanism (only foreign keys can be deferred). Fixed in
`insert_rung()`/`delete_rung()`/`new_member()` with a two-phase shift through a temporary *negative*
value (`x = -(x±1)`, then `x = -x` for every row `< 0`) — negative values can never collide with a
real (always `>= 0`) index/seq, so the intermediate state is always safe regardless of row order.

**Design points, for anyone extending this**:
- The `proj_programs` table has a reserved `id=0` row (name `""`) representing controller scope,
  rather than using `NULL` for `program_id` — SQLite's `UNIQUE` semantics treat every `NULL` as
  distinct from every other `NULL`, so two different controller-scope tags named identically would
  NOT have collided under a `(NULL, name)` unique index; a real sentinel row avoids this pitfall
  and keeps `tags.program_id`/`routines.program_id` genuine, always-non-NULL foreign keys.
- `Tag._initial_value` (an arbitrarily nested dict/list/scalar) is stored as a JSON text column
  (`json.dumps`/`json.loads`) — the natural fit given SQLite has no native nested-value column type.
- Deliberately NOT persisted (decode-only fields, irrelevant to rendering):
  `Member._byte_offset`, `DataType._dead_member_bytes`, `Tag._data_table_instance` (this last one IS
  captured as `proj_tags.source_object_id` for traceability, just never read back on rehydration).
  `Tag._data_types_map`/`Controller._data_types_map` (the shared-by-reference dict used everywhere
  in the existing decode/render code, see "Mutating a UDT with live tag instances" above) is never
  a stored column at all — rebuilt fresh in-memory on every `to_controller()` call, seeded from a
  COPY of the freshly-built `Controller._data_types_map` (to retain `ProductDefined`/module-defined
  types, needed for I/O tag rendering but never stored in `proj_data_types`) and then overlaid with
  every `proj_data_types` entry (which may include brand-new/edited User types) before being wired
  onto every rehydrated `Tag` by reference — the same "one shared dict object" pattern
  `ControllerBuilder.build()` already uses, just re-derived per rehydration instead of per load.
**Windows file-locking bug (found, then actually fixed — not just documented as a caveat)**: a
rebuild does `os.remove()` on the existing `acd.db` (inherited from `ExportL5x.__post_init__`'s own
always-wipe behavior). If another connection to that exact file was still open anywhere — even in
the SAME process — Windows raises `PermissionError` rather than allowing the delete (unlike POSIX,
which allows deleting an open file). First response to this was to document it as a known caveat
("don't do that"), relying on every caller remembering to `.close()` before anything else might
trigger a rebuild against the same path — directly contradicted by the user, who pushed back: this
should be the API's job, not caller discipline the agent has to get right every time. Correct
critique — the real problem was that `open_project_db()`/`ProjectDB` hands the caller a long-lived
connection to manage at all, which is exactly the kind of lifecycle detail a "just call the surface
function" API shouldn't require. Fixed with two changes together, not one:

1. **`_ProjectLock`** (`project_db.py`) — a cross-process mutex over one project, backed by an
   exclusively-created lock file (`project_dir/.lock`, via `os.open(..., O_CREAT | O_EXCL)`, atomic
   identically on Windows and POSIX). Acquired by `open_project_db()` before even checking
   staleness, held for the `ProjectDB`'s entire lifetime, released by `.close()` — including around
   `_rebuild_project_db()` itself. This is what actually closes the gap: a rebuild can never race a
   still-open connection, because nothing else can be mid-operation while the lock is held, and
   rebuild itself waits for the lock before touching the file. A lock file untouched for over
   `_LOCK_STALE_SECONDS` (120s — no legitimate operation here should ever take that long) is assumed
   abandoned by a crashed holder and stolen rather than waited out, via an mtime-age heuristic (not
   real PID liveness — simpler, no new dependency, and a false "still alive" read only costs an
   extra `_LOCK_TIMEOUT_SECONDS`-long wait, never wrong behavior).
2. **The `db_*` functions** (`db_new_tag`, `db_edit_tag`, `db_new_member`, `db_insert_rung`,
   `db_delete_rung`, `db_replace_rung_safe`, `db_export_routine`, `db_export_datatype`,
   `db_list_tags`, `db_list_routines`, `db_tag_exists`, `db_get_project_summary`,
   `db_to_controller`) — stateless, one-call-does-everything wrappers (open → do the one thing →
   close, via a shared `_run()` helper) that are now the *documented default surface*, matching this
   repo's existing `acd.api` convention of flat functions over stateful handles. There's no
   connection object for a caller to see, so there's nothing to forget to close.
   `open_project_db()`/`ProjectDB` are still there (also lock-protected) for a script batching many
   edits in one session that wants to hold the lock across all of them rather than pay one
   acquire/release cycle per edit — documented as the secondary, advanced option, not removed.

Covered by `test/test_project_db.py`: rebuild/materialization matches a plain `load_acd()`'s counts,
no-rebuild-on-unchanged-mtime via a monkeypatched call spy, rebuild-on-changed-mtime discarding
prior edits, every edit method (both the `ProjectDB` and `db_*` forms) persisting across a
close/reopen cycle including program-scope isolation and duplicate-name rejection, rung
insert/delete/replace-safe index arithmetic, end-to-end `export_routine()`/`export_datatype()`
producing XML that contains a DB-created tag/member, and `_ProjectLock` specifically (a second
acquirer times out while the first holds it, a waiting acquirer succeeds once the holder releases —
verified with a real background thread, not simulated — and a stale lock is stolen quickly rather
than waited out for the full timeout) — all against the real `CuteLogix.ACD` fixture.

**A third real bug, found only through repeated test runs, not a single pass**: the lock's own
acquire loop only caught `FileExistsError` on the `os.open(..., O_CREAT|O_EXCL)` create — but a
real background-thread test (`test_project_lock_waiter_succeeds_once_released`) failed intermittently
with `PermissionError` instead. This is a genuine Windows NTFS timing quirk, not a hypothetical:
immediately re-creating a filename right after another thread/process deletes it can transiently
raise `PermissionError` rather than either succeeding or reporting `FileExistsError` (deletion isn't
always atomic from a following create's perspective). `acquire()` now catches `(FileExistsError,
PermissionError)` identically — both mean "can't acquire right now," handled by the exact same
retry/stale-check/timeout logic either way. Re-ran the lock tests 8 times in a row after the fix
(a single clean pass proves nothing for a race condition) before trusting it.

### API surface: `db_*` only, everything else deliberately unexported from `acd`

First cut of this feature re-exported the ENTIRE existing in-memory API (`load_acd`, `get_routine`,
`list_tags`, `export_routine`, `new_tag`, ...) from `acd/__init__.py` alongside the new `db_*`
functions — i.e. both surfaces sitting side by side at the top level. The user pushed back directly:
their fear was that an agent would "silently decide to use the legacy ones instead of using the top
level ones" — a real risk specifically because this library's own stated audience is AI agents,
which will happily reach for whichever similarly-named function is sitting right there rather than
reliably reading a docstring recommendation first. A recommendation in prose doesn't stop an agent
from calling `acd.load_acd()` if it's importable and looks like it does the job.

Fixed by actually removing the legacy functions from `acd/__init__.py`'s top-level namespace, not
just documenting a preference — `acd.load_acd`/`acd.get_routine`/`acd.list_tags`/`acd.export_routine`/
`acd.new_tag`/etc. no longer exist as `acd.X` at all. They still work exactly as before via
`acd.api.X`/`acd.l5x.elements.X` (every `db_*` function is itself built on top of them internally,
and any advanced caller can still reach them deliberately) — but `import acd; dir(acd)` /
`help(acd)` now shows only the `db_*` functions, `open_project_db`/`ProjectDB`, and the two pure
utilities with no project/DB dependency at all (`find_io_addresses`, `diff_lines`). Verified this
was a safe removal, not just a hoped-for one: grepped the whole existing test suite first — every
test file already imports from `acd.api`/`acd.l5x.elements` submodule paths directly, not the
top-level `acd` package, so nothing broke.

This also exposed real, pre-existing gaps in `db_*` coverage that had to be filled before the
restriction made sense: there was no `db_*` way to read a specific routine's actual rung content
(only counts via `db_list_routines`), read a tag's value, find tag references, list I/O addresses,
or diff two projects/routines — an agent needing any of those would have had no choice but to reach
for `db_to_controller()` and then call the legacy in-memory functions on the result anyway, defeating
the whole point of restricting the surface. Added `db_get_routine()` (returns a plain dict — name/
type/description/rungs/rung_comments/st_lines — not a `Routine` object, keeping the DB surface
self-contained), `db_get_tag_value()`, `db_find_tag_references()`, `db_io_addresses_by_routine()`,
and three two-project comparison functions (`db_diff_project()`, `db_diff_routine()`,
`db_diff_io_addresses()` — these don't fit as `ProjectDB` instance methods since an instance is
scoped to one project; each opens/rehydrates/closes both sides independently, through the same
`_ProjectLock` every other `db_*` call goes through).

A genuine bug found and fixed while adding tests for these: `_first_routine(open_project_db(...))`
(a test helper, not library code) passed a throwaway `ProjectDB` straight into a helper that never
closes it — leaking that project's lock file for the rest of the test and hanging any later
`db_*`/`open_project_db()` call against the same path until the lock's own timeout. The exact same
"nothing forces a caller to close this" class of bug the whole `_ProjectLock`/`db_*` redesign above
was built to eliminate from the *library's* surface — reappeared immediately in hand-written test
code the moment a raw `open_project_db()` call got used carelessly, which is itself a small,
concrete demonstration of why the user's original instinct (make forgetting-to-close structurally
impossible, not just documented) was the right one.

### First real-usage feedback round (two real bugs, one confirmed-as-designed)

Asked the downstream agent that had actually been using `db_*` against a real project for direct
feedback rather than waiting for it to surface as a bug report. Got three concrete items, two real:

1. **A rebuild triggered by the source `.ACD`'s mtime changing silently discards any edit made
   since the last rebuild that was never exported** — and for this real project specifically, the
   source file gets re-synced from Studio mid-session more than once in a single day. An edit made
   and not immediately exported can vanish with nothing but an `log.info()` line, indistinguishable
   from the normal "old project, fresh start" case. Fixed by adding `proj_meta.dirty` (flipped to 1
   by every edit method in the same commit as the edit itself, defaulting to 0 on a fresh
   `_materialize()`) — `open_project_db()` now checks it before any rebuild (mtime-triggered OR
   explicit `rebuild=True`) and logs a `log.warning()` naming the risk plainly if it would discard
   real edits, instead of the same `log.info()` a routine "nothing to lose" rebuild gets. Deliberately
   NOT a hard block/raise — the whole point of the wipe-and-rebuild model is that it's always
   allowed, this only adds visibility into when it's throwing something away.
2. **`db_*` functions didn't inherit `load_acd()`'s quiet-by-default logging** — confirmed directly:
   `ExportL5x.__post_init__` is the only place the quiet/verbose reconfiguration ran, but
   `ProjectDB.to_controller()` calls `ControllerBuilder` directly against an already-open connection,
   which never goes through `ExportL5x` at all on `open_project_db()`'s "reuse existing DB, no
   rebuild needed" path — meaning a process that never happens to trigger a rebuild never reconfigures
   loguru's sink, so `ControllerBuilder`'s own `log.info()` calls (e.g. the deleted-member diagnostics
   documented elsewhere in this file) print unfiltered regardless of `verbose=False`. Fixed by
   extracting the reconfiguration into `configure_logging(verbose)` (`export_l5x.py`, used by both
   `ExportL5x.__post_init__` and `open_project_db()`, called unconditionally at the very top of the
   latter, before any rebuild-or-reuse decision is even made).
3. **A new `acd.db` file — and its own subfolder, named after the ACD stem — appears directly in the
   project's own working directory** (e.g. `source/BPM_TrimmerSorter_VAB_20260813/acd.db`), not a
   temp dir. Confirmed as-designed, not a bug: this is the literal persistence mechanism the whole
   feature exists to provide (see "Persistent project DB" above) — `ExportL5x` already had this exact
   default-location behavior for any direct call without an explicit `temp_dir`, `db_*`/
   `open_project_db()` are just the first thing to actually exercise it routinely. No code change;
   documented here so the answer doesn't need re-deriving next time it comes up. If a downstream
   project's own working directory is itself version-controlled, that project's `.gitignore` (not
   this repo's) is the right place to exclude these — not something for acd-tools itself to manage.

Covered by three new tests in `test/test_project_db.py`
(`test_open_project_db_warns_when_rebuild_discards_dirty_edits`,
`test_open_project_db_does_not_warn_when_rebuild_has_nothing_to_discard`,
`test_open_project_db_configures_quiet_logging_even_without_rebuild`) — the warning tests assert
against real captured `stderr` (via pytest's `capsys`) rather than a separately-added loguru sink,
since `configure_logging()`'s own `log.remove()` call (run at the very top of `open_project_db()`)
would silently wipe any sink a test added beforehand; the logging test spies on `configure_logging`
itself rather than depending on the small `CuteLogix.ACD` fixture happening to trigger a specific
internal `log.info()` call it may not have any real reason to emit (no deleted UDT members in that
fixture) regardless of whether the fix is present.

### Second real-usage feedback: partial multi-step edits are now durable, not atomic-by-accident

A follow-up report from the same downstream agent, articulating a real architectural shift in
failure-mode risk that neither the original design discussion nor the first feedback round had
addressed. With the OLD in-memory workflow (`load_acd()` + edit + `export_routine()`), a script that
raised partway through (collision asserts, typos, the `dimension=None` mistake documented elsewhere
in this file, ...) left zero durable side effects — not because anything was designed to guarantee
that, but purely as a side effect of nothing ever persisting between separate process invocations in
the first place. A failed script was, for free, a clean slate to just fix and rerun.

With `db_*`, each call commits independently the instant it returns. A script doing several edits in
a row — add a UDT member, create 3 tags, edit 2 rungs — that raises on step 4 has already durably
committed steps 1-3, with nothing in the DB marking that as an incomplete attempt. The practical
mitigation the agent had already been relying on (check `db_get_project_summary`/`db_list_tags` at
the start of a retry to spot and clean up a stray partial attempt) works, but is easy to forget, and
puts the burden on every caller to reimplement the same defensive check.

Added real transaction support rather than leaving this as caller discipline, matching this whole
subsystem's own established principle (see the `_ProjectLock`/`db_*` redesign above: don't rely on a
caller remembering something, make the failure mode structurally impossible instead):

- **`ProjectDB.transaction()`** (a context manager): every edit method's own `self._conn.commit()`
  becomes conditional (`if not self._in_transaction: self._conn.commit()`) — inside a `with
  db.transaction():` block, nothing commits until the block exits cleanly; `self._conn.rollback()`
  runs instead if anything inside it raises, undoing every edit made so far in the block, not just
  the one that failed. Cannot be nested (raises `RuntimeError`) — this is one flat transaction, not
  SAVEPOINT-based partial rollback, which wasn't asked for and would add real complexity for no
  reported need.
- **`db_transaction(acd_path)`** (a `@contextlib.contextmanager` function): the `db_*`-surface
  equivalent — opens a `ProjectDB`, wraps the whole `with` block in `.transaction()`, closes on exit
  either way. Exported at the top level (`acd/__init__.py`) and documented in its module docstring
  alongside the other `db_*` functions, since this fills a real gap in the *recommended* workflow,
  not just the advanced `ProjectDB` one.
- **A real footgun, called out explicitly in both docstrings**: calling a stateless `db_*` function
  (`db_new_tag`, ...) from INSIDE a `db_transaction()`/`.transaction()` block doesn't raise or silently
  misbehave — it hangs. Each `db_*` call opens its own separate connection and tries to acquire the
  SAME project lock the enclosing transaction is already holding, blocking until that inner call's own
  `_ProjectLock` timeout. Must use the yielded `db` object's own methods (`db.new_tag(...)`) inside the
  block, never `acd.db_new_tag(...)`.
- Reads (`to_controller()`, `list_tags()`, ...) called from inside an open transaction correctly see
  its own uncommitted writes — a single SQLite connection always sees its own in-flight changes, no
  special handling needed; verified directly
  (`test_transaction_sees_its_own_uncommitted_writes`).
- Cross-process isolation was already covered by the existing `_ProjectLock` (held for the whole
  `ProjectDB`/`db_transaction()` lifetime, transaction or not) — a transaction in progress on one
  connection already fully blocks any other process's `open_project_db()`/`db_*` call for its
  duration, so adding rollback semantics didn't need any new concurrency-control work on top of what
  already existed.

Verified with the exact reported scenario, not just the individual primitives: a transaction adding
a UDT member, two tags, and a rung, deliberately failing on a simulated "step 5," confirming NONE of
the first four steps are visible afterward (`test_transaction_partial_multi_step_edit_rolls_back_completely`)
— plus commit-together, rollback-together, nesting-raises, and mid-transaction-visibility as separate
tests. Full suite unaffected by this change outside the new tests (every edit method's *end result*
for a caller not using `.transaction()` is identical to before — commit still happens, just
conditionally rather than unconditionally).

### Third real-usage feedback: no way to delete anything, and `validate=False` was the wrong default

Two more items from the same downstream agent, both grounded in a real thing that happened this
session, not hypotheticals:

1. **No `db_delete_tag`/`db_delete_routine`/`db_delete_member`.** Hit directly: a routine
   (`S06_Bin_Criteria_Check` in the real project) got redesigned away — its logic moved inline into
   another routine's RLL — leaving it and six now-unused tags as orphaned dead code, with no way to
   remove them from the persistent DB short of a manual Studio deletion. The agent correctly split
   this into two separate halves, and only one of them is actually buildable right now:
   - **Deleting something in the real `.ACD`/Studio project** — almost certainly needs a manual
     Studio action regardless of what this library does. Studio's native "Import Routine"/"Import
     Data Type..." mechanism (the only durable write path this library has, see "ACD write-back"
     above) can only add/update entities present in the partial L5X; it has no delete semantics for
     something simply left out. No attempt was made to invent an L5X-based delete trick to work
     around this — this project's own repeated "don't guess a fix without real data" lesson applies
     directly here, and there's no real Studio behavior to test such a trick against without actual
     access to try it.
   - **Removing the entry from the persistent DB's own bookkeeping** — squarely buildable, and
     valuable on its own even without the first half: without it, an abandoned tag/routine/member
     keeps showing up in `db_list_tags()`/`db_list_routines()`/`db_get_project_summary()` forever,
     with nothing distinguishing "still relevant" from "dead, forgot to clean up." Added
     `delete_tag()`/`delete_routine()`/`delete_member()` on `ProjectDB` (each a straightforward
     `DELETE` from the relevant `proj_*` table plus its child rows — `proj_tag_comments` for a tag,
     `proj_rungs`/`proj_st_lines` for a routine — marking `proj_meta.dirty=1` the same way every
     other edit method does) and the matching `db_delete_tag`/`db_delete_routine`/`db_delete_member`
     stateless wrappers. Both docstrings state the real-`.ACD` limitation explicitly, right up front,
     rather than letting a caller assume "delete" means "gone from Studio too."
2. **`validate=False` as the default on `export_routine()`/`export_datatype()` was the wrong
   asymmetry for the `db_*`/`ProjectDB` layer specifically.** The check is one extra graph-walk pass,
   and it catches exactly the bug class (declared-type-vs-rendered-value mismatch, silently rendered
   as a bare zero instead of raising) that took two separate rounds of live-Studio-rejection
   debugging to fully run down earlier this same session (see "Initial-value decoding offset bugs"
   and the AOI-instance-value gaps elsewhere in this file for the general pattern) — a real, not
   hypothetical, cost of leaving it off by default. Flipped `ProjectDB.export_routine()`/
   `db_export_routine()` and `ProjectDB.export_datatype()`/`db_export_datatype()` to `validate=True`
   by default, with an explicit `validate=False` opt-out — **scoped to this layer only**, not the
   underlying `acd.api.export_routine()`/`export_datatype()`, whose own defaults stay `False`
   unchanged (no reason to risk an unrelated behavior change to callers of the lower-level functions
   this session never touched).

   This exposed a real, pre-existing gap: `export_datatype()` had **no `validate` parameter at all**
   — only `export_routine()` did. `_validate_tag_types_resolve()`'s own recursive type-graph walker
   was written specifically to start from a *Tag's* own value tree, not a bare `DataType`'s member
   declarations, so it couldn't be reused as-is. Extracted the shared recursive step into
   `_validate_type_graph_resolves(dt_name, context, data_types_map, seen)` (`rendering.py`) —
   behavior-preserving for the existing `_validate_tag_types_resolve()` caller, verified by running
   the full suite after the refactor, not just the new tests — and added
   `_validate_data_type_resolves(data_type, data_types_map)` on top of it, which walks a `DataType`'s
   own `.members` instead of a tag's `data_type`. `export_datatype()` (`acd/api.py`) gained a
   `validate: bool = False` parameter (default `False` at THIS layer, matching `export_routine()`'s
   own base default — the flip to `True` only happens at the `db_*`/`ProjectDB` layer above it) that
   calls the new function before rendering.

Covered by new tests in `test/test_project_db.py`: delete methods (removal confirmed, missing-name
`KeyError`, program-scope isolation for `delete_tag`) and `validate=True`-by-default on
`db_export_datatype()` (a deliberately-bad member type raises by default, `validate=False` explicitly
opts back out and succeeds). Full suite re-run after the `rendering.py`/`api.py` refactor with zero
regressions, confirming `_validate_tag_types_resolve()`'s own existing behavior/tests are unaffected
by extracting its shared walker.

### Fourth real-usage feedback: undocumented conventions, not code bugs — all three self-inflicted by a caller having to test empirically to learn something the docstring should have said

Same downstream agent, asked directly "any friction using the new tool" rather than waiting for
something to break. All three items were things that *worked exactly as coded* but cost the agent a
real debugging/testing round to discover, because the convention wasn't stated anywhere it would be
read before calling the function — the same class of gap already called out in "The recurring lesson"
under "Persistent project DB" above, just for docstrings instead of API surface shape:

1. **`db_get_routine()["rung_comments"]` is `Dict[int, str]`, not string-keyed.** The agent
   reasonably guessed `comments.get(str(i))` (a common JSON-API convention) and got `None` for
   every single rung — no exception, quietly empty data; only caught because a separate
   `json.dumps()` of the same dict happened to show the values were really there. This is exactly
   the "returns success with wrong/empty data" failure mode this whole file's methodology section
   keeps warning about, just triggered by documentation instead of a decode bug. Fixed by stating
   the key type and the wrong-guess failure mode explicitly in both `ProjectDB.get_routine()`'s and
   the module-level `acd/__init__.py` docstring's description of `db_get_routine()` — not just the
   type, the *consequence* of guessing wrong, since "it's an int" alone doesn't warn against the
   specific silent-`None` trap a string-keying guess falls into.
2. **No way to rename a rung's comment without touching its text.** `replace_rung_safe()` takes
   `new_text` but no comment param; the only workaround was `delete_rung()` + `insert_rung()` with
   the same text retyped by hand — which also throws away `replace_rung_safe()`'s optimistic-
   concurrency guard (the agent had to re-type the expected-old text from memory instead of a real
   compare-and-swap). Added `ProjectDB.set_rung_comment(routine_name, index, comment,
   program_name=None)` / `db_set_rung_comment(...)` (`comment=None` clears it) — a direct
   `UPDATE proj_rungs SET comment=? WHERE ...`, no text touched, no shift arithmetic needed since
   the rung doesn't move.
3. **`db_set_tag_comment()`'s `path` convention had no example**, and empty-string behavior was
   unverified without a scratch-copy test. The one-liner said `path=""` is the whole-tag
   description and otherwise "same convention as `Tag._comments`" — but discovering `path` must be
   the FULL tag-qualified address (`"MyTag.Member[4].5"`, tag name included, not just
   `"Member[4].5"`) required reading a real `_comments` dump first. **This also turned out to be a real,
   separate latent bug, not just missing docs**: the PRE-EXISTING test for this function
   (`test_set_tag_comment_element_path`) itself passed a bare suffix (`"[0]"`, no tag name prefix)
   and only asserted the entry landed in `tag._comments` — never that it actually rendered. Checking
   `_build_comments_xml`'s own filter (`if not ref.startswith(tag_name): continue`,
   `rendering.py`) confirms a path missing the tag-name prefix is stored without error but silently
   dropped at export time — the exact same "succeeds with quietly wrong/missing data" shape as item
   1, just embodied in this repo's own test suite rather than caught by it. Fixed the test to use
   the correct full-path convention (and additionally assert on the rendered XML, not just the raw
   list), added `test_set_tag_comment_without_tag_name_prefix_is_silently_dropped_at_render` to lock
   in the footgun itself as a named regression rather than leave it implicit, and confirmed
   `text=""` clears a comment (filtered by the same `not text` check in `_build_comments_xml`, not a
   distinct code path) with `test_set_tag_comment_empty_text_clears_it`. Docstrings for
   `ProjectDB.set_tag_comment()` and the `acd/__init__.py` module-level description now state the
   full-path requirement with a concrete example and the `text=""`-clears behavior directly, rather
   than pointing at `Tag._comments`'s own convention and requiring a caller to go find it.

Nothing here was "the tool did the wrong thing" per the agent's own framing — confirming the pattern
already established by the third feedback round (see "Confirmed normal, not a gap" elsewhere in this
file): real friction can be 100% a documentation/discoverability gap, and the fix is still worth
making with the same rigor as a code fix, including a regression test where an existing test's own
blind spot (item 3) let a real "silently drops data" behavior go unverified for one function since
before this round.

Covered by new tests in `test/test_project_db.py`: `test_set_rung_comment_changes_comment_without_touching_text`,
`test_set_rung_comment_none_clears_it`, `test_set_rung_comment_raises_on_missing_rung`,
`test_db_set_rung_comment_stateless_wrapper`, `test_db_get_routine_rung_comments_keyed_by_int_not_str`,
`test_set_tag_comment_without_tag_name_prefix_is_silently_dropped_at_render`,
`test_set_tag_comment_empty_text_clears_it` — full suite re-run clean (220 passed, 2 skipped, up from
213 passed before this round).

### RLL rung-text syntax lint (`_validate_rll_rung_syntax`) — a real error only Studio's own import parser caught

A downstream agent hit a real (self-caused, not this library's fault) editing mistake: removing one
of two parallel branches from a `"[...]"` group in a real rung but leaving the brackets around what
became a single branch (`"[MOVE(...) FOR(...) ]"`). In this ASCII RLL dialect `"[...]"` means
"parallel branches" and needs >= 2 comma-separated members — a single remaining branch should have
no brackets at all. Nothing in the `db_*` surface caught it: `db_replace_rung_safe()`'s guard only
checks you're editing the rung you *think* you are (an optimistic-concurrency check against the OLD
text), not the grammar of the NEW text; `db_export_routine(validate=True)`'s existing check
(`_validate_tag_types_resolve()`) walks struct-typed tag names for resolvability, a data-shape
check with nothing to do with RLL syntax. The only thing that actually caught the error was Studio
5000's own import parser, after a full edit -> export -> import round trip.

Added `_validate_rll_rung_syntax(text)` (`acd/l5x/elements/base.py`, exported through
`acd.l5x.elements`) — deliberately narrow, NOT a real ladder-logic grammar checker (an actual
grammar checker is a much bigger undertaking the agent explicitly declined to prescribe: "I don't
know how far it's worth going toward a real RLL grammar checker; that's their call, not mine to
prescribe"). Just the two cheapest, lowest-false-positive checks available without one:
- Unbalanced `(`/`)`/`[`/`]`.
- A branch `"[...]"` group with fewer than 2 top-level (not nested) comma-separated members —
  exactly the reported bug.

**The one real design subtlety**: a `[` can mean two different things in this dialect — a parallel-
branch group (`"XIC(A)[OTE(B),OTE(C)]"`) or an array-index operand (`"MyArray[5]"`,
`"MyArray[2,2,1]"` — multi-dimensional indices are genuinely comma-separated too, see the
comment-resolution section above). Applying the branch-multiplicity check to BOTH would misclassify
every single-element array index as an invalid one-branch group. Distinguished by the character
immediately preceding the `[` (no whitespace-skipping — array-index brackets are always written
adjacent to their operand): an identifier char, `_`, or `.` means "array index" (skip the
branch-multiplicity check entirely); anything else (`(`, `)`, `,`, `;`, another `[`/`]`, whitespace,
or start-of-string) means "branch group." Verified this correctly handles nested branches too
(`"[[XIC(A),XIC(B)],XIC(C)]"` — the inner `[` is preceded by `[`, correctly still a branch, not an
index) via `TestValidateRllRungSyntax` in `test/test_api.py`.

Single-quoted string literals (Rockwell's own `"$'"`-escaped convention, see `_l5k_string_padded`)
are skipped whole rather than parsed as syntax — a STRING tag's literal value can legitimately
contain brackets/commas/parens (e.g. `MOV('a[1,2](x',MyStringTag.DATA[0])`) that have nothing to do
with RLL grammar and must never trip this check.

**Wired into every place raw rung text enters the system**, per the agent's own suggested angle
("even a narrow lint ... checked at db_insert_rung/db_replace_rung_safe/db_export_routine time,
would've caught this immediately"):
- `Routine.insert_rung()` (`elements/model.py`) — the in-memory list-splice version.
- `replace_rung_safe()` (`acd/api.py`) — runs AFTER the expected-old-text match check, so a mismatch
  is always reported as a mismatch, never masked by `new_text` also happening to be malformed.
- `ProjectDB.insert_rung()` / `ProjectDB.replace_rung_safe()` (`project_db.py`, and therefore
  `db_insert_rung()`/`db_replace_rung_safe()`) — added `ProjectDB._routine_type()` (a small
  `SELECT type FROM proj_routines WHERE id=?` helper) since these SQL-backed methods didn't
  previously need to know a routine's type at all.
- `export_routine(..., validate=True)` — extended the EXISTING `validate` flag (previously only
  `_validate_tag_types_resolve()`) to also sweep every rung of an RLL routine, independent of
  whether the rung entered `.rungs` via `insert_rung()` or some other path (e.g. rehydrated from a
  DB written before this check existed, or `.rungs.append()`'d directly in a script) — this is the
  defense-in-depth layer: `insert_rung()`'s own guard only protects rungs that go through it.
  `ProjectDB.export_routine()`/`db_export_routine()` already default `validate=True` (see the third
  feedback round above), so this protection is on by default at the `db_*` layer with no caller
  change needed.

All four call sites are guarded by `routine.type == "RLL"` (or the SQL equivalent via
`_routine_type()`) — an ST routine's `._st_lines` has completely different syntax and was never in
scope for this check.

Covered by `TestValidateRllRungSyntax` in `test/test_api.py` (the pure function: valid two/nested
branch groups, array-index exclusion incl. multi-dim, single- and zero-member branch rejection,
unbalanced brackets/parens, string-literal skipping incl. escaped quotes, unterminated string
literal, empty text is a no-op) plus integration tests at each of the four call sites above
(`test_replace_rung_safe_rejects_malformed_rll_syntax`,
`test_routine_insert_rung_rejects_malformed_rll_syntax`,
`test_export_routine_validate_rejects_malformed_rll_syntax` — via `.rungs.append()`, NOT
`insert_rung()`, specifically to prove the `export_routine()` sweep doesn't just rely on
`insert_rung()`'s own guard — and the `project_db.py` equivalents in `test/test_project_db.py`,
including `test_replace_rung_safe_mismatch_takes_priority_over_syntax_error` locking in the
ordering guarantee).

### RLL edit primitives silently accepted being called on an ST routine — added a type guard, plus real ST-line editing primitives

A downstream agent hit a real, worse-than-a-missing-feature bug: `insert_rung`/`delete_rung`/
`replace_rung_safe` are RLL-only in both intent and implementation (they read/write `.rungs`/
`proj_rungs`), but calling any of them against an ST routine didn't error — it silently wrote into
a `rungs`-shaped slot the routine's own export never reads, while `._st_lines`/`proj_st_lines`
(what `export_routine()` actually renders as `<STContent>`, and what a real Studio import would
apply) stayed completely untouched. No exception, transaction commits clean, `get_routine()`
afterward shows BOTH: the new content sitting in `"rungs"`, the original unchanged content still in
`"st_lines"`, on a routine whose own `"type"` field plainly says `"ST"`. The agent only caught this
because it happened to diff `st_lines` before/after on a scratch copy before ever exporting for
real — without that check, it would have handed over an L5X that looked fine but contained none of
the intended changes. Two asks, both done:

**Minimum fix — a clear guard, not silent acceptance.** `Routine.insert_rung()`/`delete_rung()`
(`elements/model.py`), `replace_rung_safe()` (`acd/api.py`), and `ProjectDB.insert_rung()`/
`delete_rung()`/`replace_rung_safe()` (`project_db.py`, and therefore their `db_*` wrappers) all now
raise `ValueError` up front — naming the routine, its actual type, and which function to use
instead — if the routine's own type isn't `"RLL"`. `ProjectDB` needed a new `_routine_type(routine_id)`
helper (`SELECT type FROM proj_routines WHERE id=?`) since these SQL-backed methods previously had
no reason to look up a routine's type at all.

**Real fix — proper ST-line editing primitives, mirroring the RLL surface shape exactly**, per the
agent's own priority order: `Routine.insert_st_line()`/`delete_st_line()` (model.py),
`replace_st_line_safe()` (`acd/api.py`), and `ProjectDB.insert_st_line()`/`delete_st_line()`/
`replace_st_line_safe()` → `db_insert_st_line()`/`db_delete_st_line()`/`db_replace_st_line_safe()`
(`project_db.py`). Same negative-intermediate-value shift technique as the rung versions for the SQL
layer (`proj_st_lines` already had a `UNIQUE(routine_id, line_index)` index with the identical
mid-`UPDATE` collision risk — see "Persistent project DB" above). Each of these is ALSO guarded the
other direction (raises if called on a routine whose type isn't `"ST"`), so a caller gets a clear
error regardless of which primitive/routine-type mismatch they hit. No RLL syntax check applies to
ST lines — `_validate_rll_rung_syntax()` is meaningless for ST source and ST syntax validation
doesn't exist yet (documented as a real, not accidental, scope limit in both the code and the
`acd/__init__.py` module docstring).

`acd/__init__.py`'s module docstring now marks `db_insert_rung`/`db_delete_rung`/
`db_replace_rung_safe` "RLL ONLY" explicitly and documents the new `db_insert_st_line`/
`db_delete_st_line`/`db_replace_st_line_safe` trio right next to them, including the real failure
this fixes (quoted plainly: "That looked like a successful, committed edit with nothing marking it
as wrong").

Needed a second test fixture: `CuteLogix.ACD` (the fixture `test_project_db.py`'s existing
`acd_copy` uses) has zero ST routines (confirmed directly — every routine in it is RLL), so a new
`st_acd_copy` fixture copies `ACDTestsNonRedundant.ACD` (already used elsewhere for ST content
tests, see "Structured Text (ST) routine content" above) instead. Covered by type-guard tests at
every one of the six call sites (three RLL functions called on an ST routine, three ST functions
called on an RLL routine) plus normal insert/delete/replace-safe behavior tests for the new ST
primitives, in both `test/test_api.py` and `test/test_project_db.py` — including
`test_insert_rung_raises_on_st_routine`, which explicitly re-reads `st_lines`/`rungs` afterward to
confirm the guard didn't just raise but also left BOTH untouched, not only stopped short of the
original silent-corruption case.

### `new_member(name, "BIT")` silently produced an unimportable member — added real bit allocation

A downstream agent hit a real reported bug, same shape as several others in this file: `db_new_member
(dt_name, name, "BIT", ...)` committed with no error, but the new member never got a `bit_number` or
a backing hidden field assigned — `target: None`, `bit_number: None` — while every pre-existing BIT
member on the same UDT had both (Rockwell packs BIT-overlay members 8-per-byte into a hidden SINT
backing field, already documented in "BIT-overlay member Target resolution" above). Studio 5000's own
"Import Data Type..." then failed on `Target`, since the exported XML had nothing valid to point the
new member at. Nothing signalled this at creation time; it only surfaced three steps later as a real
Studio import rejection, after the agent compared the new member against its siblings to notice the
missing allocation. The agent explicitly declined to hand-patch the bit allocation itself ("that's
exactly the kind of thing that risks getting Rockwell's packing convention subtly wrong") and asked
for real allocation: reuse a free bit in an existing hidden backing member if one has room, or create
a new backing member if none does.

**Root cause, two layers deep — the SQL layer would have silently discarded a correct allocation even
if the Python-level constructor already produced one.** `new_member()` (`elements/model.py`) was
already documented as deliberately leaving `target`/`bit_number` both `None` for `data_type="BIT"` --
"a BIT-overlay pseudo-member needs a hidden backing field... which only applies to members actually
read back from a real ACD record, not one authored fresh in Python." That was true but the DOCUMENTED
gap was itself the bug: no exception, no warning, a plausible-looking `Member` object that happily
committed and exported. Separately, and worse, `ProjectDB.new_member()`'s own SQL `INSERT` hardcoded
`hidden=0, target=NULL, bit_number=NULL` literally in the statement regardless of what the in-memory
`Member` object actually held — so even a caller that worked around the first gap by hand-constructing
a correct `Member` would have had the allocation silently thrown away again at THIS layer, with the
identical symptom (commits cleanly, only a real Studio import catches it).

**Fix, both layers:**
- `new_member()` now RAISES `ValueError` for `data_type.upper() == "BIT"`, naming the correct
  replacement — fail fast at the point of the mistake, matching this file's own established
  convention (e.g. the `dimension=None` guard elsewhere in this same function), rather than silently
  producing a member that looks fine and isn't.
- New `new_bit_member(data_type, name, description=None) -> Member` (`elements/model.py`) allocates a
  real bit position the way Studio itself does: scans `data_type.members` for an existing HIDDEN
  member that already backs >= 1 other real BIT member (i.e. some other member's own `target` already
  points at it) with room left (capacity = that member's own primitive size in bits — 8 for the
  standard SINT backing field, computed generically via `_PRIM` in case a project ever uses a wider
  one), and reuses the lowest free bit. If none has room, appends a brand-new hidden SINT member to
  `data_type.members` (mutated directly, same pattern as `new_member()`'s own documented "insert the
  result yourself" convention) named `"Z"*10 + data_type.name[:10] + <next unused sequence number>` —
  mirroring the exact naming convention already observed and documented on real UDTs elsewhere in this
  file (`ZZZZZZZZZZLugWrk9`, `ZZZZZZZZZZBin_Sequen1`/`...10`) as closely as possible. Deliberately
  never repurposes a hidden member that ISN'T already backing a real BIT member — there's no way to
  tell from the object model alone whether an arbitrary hidden field is genuinely free bit storage or
  something else entirely, so only a member with a verifiable BIT-backing role is ever treated as
  reusable. Also raises `ValueError` on a case-insensitive duplicate member name, which `new_member()`
  itself still doesn't check (out of scope for this fix — this function has the DataType in scope to
  check cheaply, `new_member()` never has).
- `ProjectDB.new_member()` (`project_db.py`) rewritten to actually use this: for `member_data_type=
  "BIT"`, builds a lightweight in-memory `Member` list from `proj_members` (a new `_load_member_view()`
  helper — not a full `to_controller()` rehydration, just enough for the allocator to see this UDT's
  own existing members), wraps it in a throwaway `DataType`, calls `new_bit_member()`, and — if a new
  backing member was created — inserts that as its own new `proj_members` row (appended at the current
  end) BEFORE the requested member's own insert-at-`index` logic runs. The final `INSERT` for the
  requested member itself no longer hardcodes `hidden=0, target=NULL, bit_number=NULL` — it now uses
  the real `member.target`/`member.bit_number` (still hardcoding `hidden=0`, correctly, since the
  member being inserted here is never itself the hidden one).

**A real bug found and fixed while verifying this end-to-end, not just via unit tests**: the first
implementation compared `len(dt_view.members) > len(existing_members)` to detect whether
`new_bit_member()` had appended a new backing field — but `DataType(..., existing_members)` does NOT
copy the list; `dt_view.members` and `existing_members` are the SAME object, so that comparison was
always comparing a list's length to itself (always `False`). This silently meant the newly-created
backing member's own row was NEVER inserted into `proj_members` at all — confirmed directly: a second
`db_new_member(..., "BIT")` call on a fresh UDT (no existing BIT members) reused `bit_number=0` again
instead of `1`, because the backing member the first call claimed to create didn't actually exist in
the database to be found on reload. Caught by an actual end-to-end sanity script (open a real DB,
create two BIT members, rehydrate, inspect), not by the unit tests for `new_bit_member()` itself
(which construct their `DataType`/`Member` objects directly in Python and never touch this SQL
bridging code at all) — another instance of this file's own repeated lesson that a fix "verified" only
at the pure-function level isn't verified at the layer a real caller actually uses. Fixed by capturing
`count_before = len(existing_members)` as a plain int BEFORE calling `new_bit_member()`, then comparing
against `len(dt_view.members)` (same object, but now compared against a frozen count) afterward.

**Verified end-to-end** (script, not just pytest): `db.new_member()` called once creates a real hidden
`SINT` backing member (`hidden=True`) plus the BIT member correctly targeting bit 0 of it; a second
call on the same UDT reuses the same backing field at bit 1; nine successive calls correctly fill bits
0–7 of the first backing field then create and use a second one at bit 0; `export_datatype()` on the
resulting UDT renders the expected `<Member ... DataType="BIT" ... Target="..." BitNumber="..." />`
alongside its own `Hidden="true"` backing `<Member>`. Not independently verified against a REAL Studio
5000 import for this specific fix (unlike several other entries in this file) — the shape mirrors
`export_datatype()`'s already-verified rendering path exactly (see "`export_datatype()` — create/modify
a UDT" above), and the CREATE-a-new-backing-member naming convention is explicitly documented as a
best-effort mirror of Rockwell's own observed convention, not a confirmed requirement (Studio
recomputes a UDT's real physical layout/object IDs from the imported XML regardless — the backing
member's name is the only real judgment call left, per `Member._byte_offset` never being emitted at
all, already established elsewhere in this file).

Covered by pure-function tests in `test/test_api.py` (`test_new_member_rejects_bit_type`,
`test_new_bit_member_creates_backing_field_when_none_exists`,
`test_new_bit_member_reuses_free_bit_in_existing_backing_field`,
`test_new_bit_member_creates_new_backing_field_once_full`,
`test_new_bit_member_ignores_hidden_member_not_already_backing_a_bit`,
`test_new_bit_member_rejects_duplicate_name`) and DB-layer integration tests in
`test/test_project_db.py` (`test_new_member_bit_type_allocates_backing_field`,
`test_new_member_bit_type_reuses_free_bit_across_separate_calls`,
`test_new_member_bit_type_creates_second_backing_field_once_full`,
`test_db_new_member_bit_type_persists_through_export_datatype` — the one that would have caught the
`len(dt_view.members) > len(existing_members)` bug above had it existed first, a reminder to write the
integration test before declaring a fix like this done, not just the unit test).

### A real .ACD can contain two DataTypes with the same name — a full blocker, not scoped to one edit

A downstream agent reported a total blocker, more severe than any prior report: EVERY `db_*` call
against one real project crashed, including plain reads, because `open_project_db()` auto-rebuilds
the persistent DB whenever the source `.ACD`'s mtime changes, and the rebuild itself was what
crashed — so there was no way to even inspect the project through this layer, let alone edit it.

**Root cause, confirmed by the agent via the legacy `load_acd()` loader (which bypasses the
persistent-DB layer entirely and has no uniqueness assumption at all — it's just a plain Python
list)**: the real `.ACD` genuinely contains TWO distinct DataType records that both decode to the
exact same name, `ZZZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000` — a known Rockwell-internal placeholder
name for an in-flight/incomplete UDT import, both empty (0 members), almost certainly left behind by
a run of back-to-back partial-L5X imports (exactly the `export_datatype()`/Studio "Import Data
Type..." workflow this whole subsystem exists to support). Not corrupted data — Rockwell's own format
tolerates this; `proj_data_types.name` has a GLOBAL `UNIQUE INDEX ... COLLATE NOCASE` (see `_SCHEMA`
above), an assumption that simply doesn't hold for this project. The agent explicitly flagged the
broader question worth checking: whether tags/routines/AOIs have the same exposure, "since this
project clearly can produce same-named entries that Rockwell's own format tolerates."

**Fix, two parts:**
1. **The immediate unblock**: `_materialize()` now filters OUT any DataType whose name starts with
   `ZZZZZ_TEMPORARY_IMPORT_DATATYPE_NAME` (`_TEMPORARY_IMPORT_DATATYPE_PREFIX`, matched by prefix, not
   an exact string, in case Rockwell appends a different counter suffix for a different in-flight
   import than the `_000` observed in this one real case) BEFORE it ever reaches the `INSERT` —
   exactly the same "not a real object a caller would ever want to reference" reasoning already
   applied throughout this codebase to other Comps-level artifacts (hex-named cached-MSG connections,
   `"__Map:"`-prefixed shadow entries, phantom Program/Module/Tag/Routine records — see this file's
   own extensive history above). A skipped count is logged via `log.info()` for visibility. This
   directly and completely resolves the reported crash: after filtering, zero placeholder entries
   reach the `INSERT` at all, regardless of how many duplicates exist.
2. **The broader exposure the agent asked about, addressed as diagnosability rather than a blind
   guess**: `AOIs` are not exposed to this specific bug at all — they're never persisted through
   `proj_*` tables in the first place (v1 scope explicitly excludes them, always re-derived fresh via
   `ControllerBuilder` on every `to_controller()` call, see "Persistent project DB" above). `proj_tags`
   and `proj_routines` ARE scoped uniqueness (`(program_id, name)` / `(program_id, name)`, not global
   like DataTypes), so a real collision there needs two tags/routines sharing BOTH the same scope AND
   the same name — a narrower, unconfirmed scenario with no known placeholder-name convention to
   filter by (guessing one without real data would violate this project's own repeatedly-stated
   "don't guess a fix without real data" rule). Instead of leaving those two tables exposed to the
   exact same class of opaque crash, `_materialize()`'s DataType/tag/routine `INSERT`s are now each
   individually wrapped to catch `sqlite3.IntegrityError` and re-raise with a clear message naming the
   specific object and its scope, instead of SQLite's own bare "UNIQUE constraint failed" — so if a
   real tag/routine collision (or a DataType duplicate that isn't the known placeholder pattern) is
   ever hit in the future, it fails fast with an actionable diagnosis instead of reproducing this same
   "every db_* call blocked, no idea why" severity from scratch.

**Verified**: a synthetic reproduction (loading the small fixture ACD, then appending two DataType
objects both named `ZZZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000` directly to
`project.controller.data_types` before calling `_materialize()`) confirms both cases — the known
placeholder pair is silently skipped with no crash and zero rows persisted for it, while an otherwise
identical but NON-placeholder-named duplicate still raises, now with the offending name and table
named directly in the error message instead of SQLite's own opaque text. The same clear-error pattern
verified for a synthetic controller-scope tag name collision and a synthetic same-program routine
name collision (both narrower/less-likely scenarios per the scoping above, but now diagnosable rather
than silently unguarded either way).

Covered by `test_materialize_skips_duplicate_temporary_import_placeholder_datatypes`,
`test_materialize_raises_clear_error_on_genuine_datatype_name_collision`,
`test_materialize_raises_clear_error_on_controller_tag_name_collision`, and
`test_materialize_raises_clear_error_on_routine_name_collision` (`test/test_project_db.py`) — all
constructed via direct `_materialize()` calls against a synthetic in-memory SQLite connection (not
`open_project_db()`), since reproducing this class of bug needs to inject a genuinely duplicate-named
object into the object graph BEFORE materialization, not something reachable through any normal edit
API (every `db_*` edit method already enforces unique names going forward — this is specifically about
what the SOURCE `.ACD` can already legally contain on the very first rebuild).

## `RoutineBuilder` picked the wrong row on a colliding `object_id` — a real routine silently vanished

Follow-up investigation to the Comps.Dat diagnostics fix above, on the exact same real project and
the exact same reported symptom (a specific, recently-authored routine invisible to every read path,
with Studio 5000 itself confirming the routine genuinely exists). The improved diagnostics correctly
identified the truncated-record failure (see above) — but with real file access in hand this time
(the user provided the actual project directory), the truncated record's own `object_id`/`parent_id`
were cross-checked against the rest of the successfully-parsed `comps` table and matched NOTHING —
no children, no matching parent, no `RegnLink.Dat`/`SbRegion.Dat` references anywhere. **That record
was a dead end, not the missing routine.** The missing routine's own comps record, once searched for
directly by name, was found to parse completely cleanly on its own — proving the two problems were
unrelated despite the matching symptom, a genuinely important negative result worth stating plainly:
confirming a hypothesis "fits" isn't the same as confirming it explains the actual mechanism.

**Real root cause, found by tracing the routine's own object_id all the way through the builder
pipeline**: `object_id` genuinely collided with an unrelated object elsewhere in the same
`Comps.Dat` — a real, now-CONFIRMED instance of the risk flagged (but left theoretical) in "A genuine
3-way collision crashed the whole load" above. The real, live routine (correctly parented under its
Program's own `RxRoutineCollection`, a completely ordinary `record_type=256` RLL routine) shared its
numeric `object_id` with an unrelated object under a DIFFERENT parent (a garbage-looking single-
control-character name, a large opaque record, `record_type=0`, no children, no references anywhere
else in the project — almost certainly some inert internal Comps.Dat artifact, not a meaningful
object of any kind).

`RoutineBuilder.build()`'s own first step re-queries `comps` by `object_id` ALONE (`SELECT ... WHERE
object_id=?`, discarding the parent_id its own caller already knew from a more specific query) and
blindly takes `results[0]` — with two rows now matching, SQLite returned the UNRELATED row first.
That row's own (garbage) bytes got fed to `RxGeneric` in the real routine's place; `RxGeneric` either
raises or resolves to a nonsense `routine_type`, and — via the *existing*, already-correct
"`routine_type_enum(0) == 'TypeLess'` means deleted/placeholder, return `None`" filter documented in
this same function (see its own docstring) — the real, live routine was silently treated as if it
didn't exist. No exception, no warning: `ProgramBuilder`'s own routine-collecting loop just quietly
appended nothing for it, indistinguishable from "this routine was correctly filtered as a genuine
placeholder."

**Fix**: `RoutineBuilder` gained an optional `_parent_id` field (defaults to `None`, preserving the
old object_id-only lookup for any caller that doesn't have a parent_id in hand); when set, `build()`'s
own re-query filters on `(object_id, parent_id)` instead of `object_id` alone. Both real call sites
(`ProgramBuilder.build()`'s routine-collection loop and `AoiBuilder.build()`'s AOI-logic-routine
loop) already independently know the correct `RxRoutineCollection` object_id from their OWN query
that found the child in the first place — they were simply discarding it before now. Both updated to
pass it through.

**Verified against the real project**: the previously-invisible routine now appears correctly — 16
routines in its Program instead of 15, real RLL content (43 rungs, sensible ladder logic referencing
real project tags), `db_get_routine()`/`db_list_routines()` both now find it. Also confirmed the fix
doesn't accidentally paper over the ALREADY-correct `TypeLess` filtering: a synthetic reproduction
(`test_routine_builder_disambiguates_colliding_object_id_by_parent`, `test/test_database.py`, using
a real routine from the small `CuteLogix.ACD` fixture plus an injected colliding row under a
different parent) confirms the collision is genuinely ambiguous at the `object_id`-only level (2 rows
match), that the parent-scoped lookup finds the real routine correctly despite it, AND that querying
with the COLLIDING object's own (wrong) parent_id correctly returns `None` rather than something
bogus — proving `_parent_id` is a real disambiguation, not a no-op.

**Not chased further**: this fixes the one confirmed case (`RoutineBuilder`). Per the updated
"Residual, theoretical risk" note above, the same `object_id`-only-lookup pattern exists in other
builders too — left alone until a concrete case turns up in one of them specifically, rather than
speculatively rewriting every such lookup on the strength of this one confirmed instance.

## `new_routine()`/`db_new_routine()` — creating a brand-new routine was a gap in the `db_*` API

A downstream agent flagged a real gap, found while trying to add a new routine to an existing
program: `db_insert_rung()`/`db_insert_st_line()`/`db_replace_rung_safe()`/
`db_replace_st_line_safe()` all require the routine to already exist (they shift rows within an
already-existing sequence) — nothing created the routine record itself. `db_delete_routine()`
existed on the deletion side with no creation-side counterpart at all.

Added `new_routine(name, routine_type, description=None) -> Routine` (`elements/model.py`, mirroring
`new_tag()`/`new_member()`'s shape: a plain constructor for a caller to append to `Program.routines`
themselves) and `ProjectDB.new_routine()`/`db_new_routine()` (`project_db.py`, the SQL-backed
equivalent — `INSERT INTO proj_routines`). Both validate `routine_type` is exactly `"RLL"` or `"ST"`
(case-sensitive, matching every ACD-decoded Routine's own `.type`), raising `ValueError` for anything
else (FBD/SFC content isn't supported by this library at all, see "Known limitations") rather than
silently accepting a routine type nothing downstream could ever populate or export. The new routine
starts empty (`.rungs`/`._st_lines` both `[]`) — `insert_rung()`/`insert_st_line()` (already
type-guarded per "RLL rung edit primitives silently accepted being called on ST routines" above)
populate it afterward, no separate wiring needed.

**One deliberate deviation from the request's own suggested shape**: the agent's proposed signature
had `program_name=None` (mirroring `db_new_tag()`, where `None` means controller scope). A routine
has no controller-scope equivalent at all in Rockwell's own object model — it always belongs to
exactly one Program (or, for an AOI's own logic routines, an AOI — not reachable via `db_*` at all,
same v1 scope limit as everything else AOI-related in this subsystem). `program_name` is REQUIRED on
both `ProjectDB.new_routine()`/`db_new_routine()` (raises `ValueError` naming why, including if
`None` is passed explicitly, not just omitted) rather than silently accepting a `None` that has no
sensible meaning to default to.

Covered by `test_new_routine_rll`/`test_new_routine_st`/`test_new_routine_rejects_invalid_type`/
`test_new_routine_result_can_be_populated_with_insert_rung`/
`test_new_routine_result_can_be_populated_with_insert_st_line` (`test/test_api.py`, the pure
constructor) and `test_new_routine_rll_can_be_populated_and_read_back`/
`test_new_routine_st_can_be_populated_and_read_back`/`test_new_routine_requires_program_name`/
`test_new_routine_rejects_invalid_type`/`test_new_routine_missing_program_raises_key_error`/
`test_new_routine_duplicate_name_raises`/`test_db_new_routine_stateless_wrapper_and_export`
(`test/test_project_db.py`, the SQL-backed layer — the last one exercises the full
create-routine → insert-rung → `export_routine()` pipeline end-to-end, confirming the new routine's
name and rung content both actually reach the rendered L5X, not just the DB).

## `new_datatype()`/`db_new_datatype()` — the same creation-side gap, for UDTs this time

Same class of gap as `new_routine()`/`db_new_routine()` above, flagged by the same downstream agent
immediately after that one landed: `db_new_member()` requires the UDT to already exist (`KeyError`
otherwise), and there was no `db_*` way to originate a brand-new one at all — only
`db_export_datatype()` existed, for exporting an already-existing type. Not a blocker this time (the
agent used standalone tags instead of a bundled parameter struct as a clean workaround, a pattern
already used elsewhere in the same project) but flagged as real, since the next case might not have
as clean a workaround available.

Added `new_datatype(name, description=None) -> DataType` (`elements/model.py`, mirroring
`new_tag()`/`new_member()`/`new_routine()`'s shape) and `ProjectDB.new_datatype()`/
`db_new_datatype()` (`project_db.py`, the SQL-backed equivalent — `INSERT INTO proj_data_types`).
`family`/`cls` are always `"NoFamily"`/`"User"` — the conventional values for a plain, user-created
type (matching `DataTypeBuilder.build()`'s own fallback for a real ACD-decoded type, and every
hand-constructed `DataType` already used elsewhere in this codebase's own tests) — with no override
parameter, since `"StringFamily"`/`"ProductDefined"`/`"IO"` only ever apply to a string-family type
or a module-defined/built-in type, never something a caller constructs by hand. Starts with
`.members` empty; `new_member()`/`new_bit_member()` (or `db_new_member()`) populate it afterward the
same way they already do for an existing UDT — no new wiring needed on that side, confirmed via a
`new_datatype()` → `new_member()`/`new_bit_member()` round trip covering both a plain member and a
BIT member (allocating a real backing field, per "`new_member(name, "BIT")` silently produced an
unimportable member" above) directly on the freshly-created type.

Covered by `test_new_datatype_is_empty_user_type`/
`test_new_datatype_result_can_be_populated_with_new_member` (`test/test_api.py`, the pure
constructor) and `test_new_datatype_creates_empty_udt`/
`test_new_datatype_can_be_populated_with_new_member`/`test_new_datatype_duplicate_name_raises`/
`test_db_new_datatype_stateless_wrapper_and_export` (`test/test_project_db.py` — the last one
exercises the full create-datatype → new_member → `export_datatype()` pipeline end-to-end, same
"confirm it actually reaches the rendered L5X, not just the DB" discipline as `new_routine()`'s own
equivalent test above).

## AOI creation support — `new_aoi()`/`new_aoi_parameter()`/`export_aoi()`, `db_new_aoi()`/`db_new_aoi_parameter()`/`db_export_aoi()`

A downstream agent flagged the same class of gap as `new_datatype()`/`db_new_datatype()` above, one
level up: `db_new_member()` needs an existing UDT, `db_new_routine()` needs an existing Program (or
now, an AOI) — but there was no way to originate an AOI itself at all. Explicitly trimmed down and
flagged LOW PRIORITY by the agent, with LocalTags, `ExecutePrescan`/`ExecutePostscan`/
`ExecuteEnableInFalse`, and general read-back all explicitly scoped OUT as non-blocking — the real
ask was narrow: create an AOI, add parameters, add its logic routine, export it.

**This is a materially bigger lift than `new_routine()`/`new_datatype()` were, for one specific
reason: those two extended an ALREADY-VERIFIED export mechanism (`export_routine()`/
`export_datatype()`) to a new creation path.** There was no `export_aoi()` at any layer before this
— it had to be built from scratch, by direct symmetry with the two already-verified wrappers rather
than from its own real-import evidence. That distinction is carried through everywhere below,
including in the code's own docstrings — this is the least-verified corner of the whole `db_*`
surface, more so than `export_datatype()` was when it first shipped (see its own history above).

**New pure constructors** (`elements/model.py`, mirroring `new_tag()`/`new_member()`/`new_routine()`'s
shape): `new_aoi(name, description=None) -> AOI` (fixed, non-configurable defaults for everything out
of scope — `revision="1.0"`, `revision_extension`/`vendor` both `None`, all three execute flags
`"false"`, `created_by`/`edited_by` both `""`, `created_date`/`edited_date` the current time,
`software_revision` a generic `"33.00"` placeholder not derived from any real project since this is a
pure, context-free constructor with no project in scope) and `new_aoi_parameter(name, data_type,
usage="Input", dimension=None, description=None) -> Parameter` (`usage` must be `"Input"`/`"Output"`/
`"InOut"`; `radix`/`external_access`/`constant` derived from `data_type`/`usage` per the real
convention already documented on the `Parameter` dataclass itself — `external_access=None`/
`constant="false"` for InOut, `external_access="Read/Write"`/`constant=None` otherwise; `required`/
`visible` both default `"true"`).

**New `export_aoi(project, aoi, output_path, owner=None, validate=False)`** (`acd/api.py`) — the
missing wrapper. Built by reusing `_resolve_type_closure()` (already handles an AOI's own parameters/
local_tags as dependency-resolution roots, per its own docstring) and the same `Use="Target"`-
injection pattern already verified for `<Routine>`/`<DataType>`, wrapped in
`<AddOnInstructionDefinitions Use="Context">` — the SAME wrapper/placement already independently
verified for an AOI as a `export_routine()` CONTEXT dependency. The combination — an AOI as the
TARGET itself, fully rendered inline with its own Parameters/Routines — has never been tried against
a real Studio 5000 import. New `_validate_aoi_parameters_resolve()` (`rendering.py`, third sibling to
`_validate_tag_types_resolve()`/`_validate_data_type_resolves()`, sharing the same
`_validate_type_graph_resolves()` walker) backs `validate=True`, checking `.parameters` only (not
`.local_tags` — nothing in this feature can create one, so nothing this check would catch there was
ever caused through this library's own API).

**`ProjectDB`/`db_*` layer** (`project_db.py`) — the part with a real, deliberate architectural
choice worth explaining:

- **Schema**: new `proj_aois`/`proj_aoi_parameters` tables. `proj_routines` gained a nullable
  `aoi_id` column alongside the now-also-nullable `program_id`, with a `CHECK ((program_id IS NULL)
  != (aoi_id IS NULL))` enforcing exactly one owner. Uniqueness is TWO separate PARTIAL indexes
  (`... WHERE program_id IS NOT NULL` / `... WHERE aoi_id IS NOT NULL`), not one combined
  `UNIQUE(program_id, aoi_id, name)` — a combined index would never actually catch a same-AOI
  name collision, since SQLite treats every NULL as distinct from every other NULL and the
  always-NULL `program_id` column (for an AOI-owned routine) would make every row look unique
  regardless of `aoi_id`/name matching. This is the exact same pitfall `proj_programs`' own `id=0`
  sentinel row (for controller-scope tags) was already built to avoid — documented in this module's
  own "Design points" — just solved differently here (a partial index instead of a sentinel row,
  since there's no natural "AOI scope" analog to controller scope to reserve one for).
- **`proj_aois` holds ONLY brand-new AOIs, NEVER a real project's own pre-existing ones** — a
  deliberate scope boundary, not an oversight, and the one place this feature diverges from every
  other `proj_*` table's "materializes the whole real project" convention. Reason: a real AOI's own
  LocalTags aren't persisted anywhere in this schema at all (out of scope per the original request).
  If `_materialize()` walked `project.controller.aois` into `proj_aois` the way it already does for
  every DataType/Tag/Routine/Program, rehydrating a real AOI through this table would silently DROP
  its real LocalTags on every single `to_controller()` call — corrupting real project data for
  something nobody asked to edit. Verified this risk is real and the fix actually prevents it: `
  test_new_aoi_never_touches_real_pre_existing_aoi` (`test/test_project_db.py`, against the
  `ACDTestsWithAOI.ACD` fixture's own real AOI) confirms a real AOI's `LocalTags` count is IDENTICAL
  before and after creating an unrelated new AOI via `db_new_aoi()`.
  - Consequence: `to_controller()` now does `controller.aois = controller.aois + self._load_aois()`
    — APPENDS `proj_aois` content onto the real, freshly-`ControllerBuilder`-decoded list, never
    replaces it (unlike `.data_types`/`.tags`/`.programs`, which ARE fully replaced from `proj_*`).
  - Consequence: `db_new_aoi_parameter()`/`db_new_routine(..., aoi_name=...)` only ever resolve
    against `proj_aois` — they raise `KeyError` for a real project's own pre-existing AOI name, even
    though `db_export_aoi()` can still export one of those directly (untouched, or after mutating it
    via `db_to_controller()` — that path was never broken, since it doesn't go through `proj_aois`
    at all).
- **`_routine_id()`'s existing "search everywhere" fallback (`program_name=None`) now also searches
  AOI-owned routines** via an added `LEFT JOIN proj_aois`, rather than giving every routine method
  (`insert_rung`, `delete_rung`, `replace_rung_safe`, `set_rung_comment`, `insert_st_line`,
  `delete_st_line`, `replace_st_line_safe`, `delete_routine`, `export_routine`, `get_routine`) its
  own new `aoi_name=` parameter — all of them transparently gained AOI-routine support for free
  through this one shared helper. Trade-off, stated plainly: there is still no `aoi_name=`
  disambiguation parameter on any of them (unlike `program_name=`) — if a routine name collides
  between two AOIs, or an AOI and a program, the error names every scope the match was found in, but
  the caller's only recourse is renaming one of them. Judged an acceptable gap for a routine name
  that's usually distinctive in practice, not worth a signature change across eight methods for a
  v1, explicitly-low-priority feature.
- `new_routine()`/`db_new_routine()` signatures changed: `program_name` is no longer a required
  positional with no default — it's now `Union[str, None] = None`, and EXACTLY ONE of
  `program_name`/`aoi_name` must be given (raises `ValueError` naming both if neither or both are).
  Backward compatible for existing positional callers (`db_new_routine(path, name, type,
  "MyProgram")` still works unchanged) — only a caller passing `program_name=None` explicitly (same
  as omitting it) sees a reworded error message, since "no scope given at all" is now indistinguishable
  from "you meant to pass one and typo'd `None`".

Covered by `test_new_aoi_is_empty`/`test_new_aoi_parameter_input_defaults`/
`test_new_aoi_parameter_inout_omits_external_access`/`test_new_aoi_parameter_udt_type_omits_radix`/
`test_new_aoi_parameter_dimension`/`test_new_aoi_parameter_rejects_invalid_usage`/
`test_new_aoi_can_be_populated_with_parameters_and_routine`/
`test_export_aoi_produces_well_formed_target_xml`/`test_export_aoi_raises_if_not_in_project`/
`test_export_aoi_validate_rejects_unresolved_parameter_type` (`test/test_api.py`, the pure layer) and
`test_new_aoi_creates_empty_aoi`/`test_new_aoi_can_be_populated_with_parameters_and_routine`/
`test_new_aoi_parameter_missing_aoi_raises_key_error`/`test_new_aoi_duplicate_name_raises`/
`test_new_routine_requires_exactly_one_of_program_or_aoi`/
`test_new_aoi_never_touches_real_pre_existing_aoi`/`test_db_new_aoi_stateless_wrappers_and_export`
(`test/test_project_db.py`, the SQL-backed layer, the last one again exercising the full
create → populate → export pipeline end-to-end against the rendered L5X, not just the DB).

### Real-ground-truth verification round — a genuine Rockwell AOI export, plus a real project that has it

The user supplied a real, Rockwell-authored AOI L5X export (`AOI_SNTP_QUERY`, a fairly complex sample
instruction with 20 parameters, 13 local tags, 2 routines, and its own DataType/AOI dependencies) —
AND mentioned the exact same AOI exists, unmodified, in a real project already used elsewhere in this
file's history. This allowed BOTH a structural comparison of the wrapper shape (the export file) AND
a load-the-real-ACD-and-re-export-through-this-library comparison against that same ground truth
(`created_date`/`edited_date` matched to the millisecond between the two, confirming genuinely the
same, unedited AOI) — the strongest verification available without an actual Studio 5000 import.

**Confirmed correct, no changes needed**: `Use="Target"` placement and attribute order on
`<AddOnInstructionDefinition>`, the `<Description>`/`<RevisionNote>`/`<Parameters>` child order, the
top-level `<DataTypes>` → `<AddOnInstructionDefinitions>` section order, and the `ExportOptions=`
string all matched exactly.

**Two real gaps found and fixed**, both low-risk and mechanically verified against the real file:
1. `new_aoi()`'s `created_date`/`edited_date` used `"%a %b %d %H:%M:%S %Y"` (copied from
   `export_date`'s own unrelated convention) — Rockwell's real format is ISO-8601 with milliseconds
   and a `Z` suffix (`"2014-04-02T15:31:19.017Z"`), confirmed via `AoiBuilder`'s own EXISTING decode
   (which already reads this correctly from real ACDs) matching the ground-truth file's own dates
   exactly. Fixed to generate the same format.
2. Every real `<AddOnInstructionDefinition>` (the target AND every context dependency) carries its
   own `<Dependencies>` child listing `<Dependency Type="DataType"/"AddOnInstructionDefinition"
   Name="..."/>` entries, positioned right before the closing tag — `export_aoi()`'s wrapper never
   rendered this at all. Added `_inject_aoi_dependencies_xml()` (`acd/api.py`, same
   "post-process the rendered XML string" pattern as `_inject_use_attr()`), wired in for the TARGET
   AOI only (a context AOI's own `<Dependencies>` isn't rendered — `AOI.to_xml()` has no data for it
   without externally-computed closure info, and this hasn't been tested either way). Best-effort,
   not exact: lists the FULL resolved dependency closure, not precisely limited to this one AOI's own
   DIRECT dependencies the way Rockwell's own export is (`_resolve_type_closure()` doesn't track
   per-node direct edges at that granularity) — over-including a few transitively-reachable names is
   judged safer than omitting a real one.
3. Also added `TargetRevision`/`TargetLastEdited` to the wrapper's own `<RSLogix5000Content>`
   attributes (redundant with the AOI element's own `Revision`/`EditedDate`, but present at the
   wrapper level too in the real file) — a small, low-risk addition alongside the other two.

**A separate, real, CONFIRMED bug found in the EXISTING decode path (`AoiBuilder.build()`), unrelated
to this session's AOI-creation feature — left un-fixed, deliberately, pending more data.**
`execute_prescan`/`execute_postscan`/`execute_enable_in_false` are hardcoded to `"false", "false",
"false"` in `AoiBuilder.build()`'s own `return AOI(...)` call — never actually read from the real ACD
binary at all. The real `AOI_SNTP_QUERY` has `ExecutePrescan="true"` (a real, load-bearing setting,
not incidental) — confirmed genuinely the same AOI via the matching millisecond-precision
`created_date`/`edited_date` described above, ruling out "it's just a different/edited copy" as an
explanation. **Not fixed in this pass**: finding the real byte offset/bit for these three flags would
need reverse-engineering against MULTIPLE real samples with different True/False combinations (the
same rigor this file's own "Connection Type / RPI" and "BIT-overlay member Target resolution"
investigations required) — only ONE real data point is available so far (Prescan=true,
Postscan=false, EnableInFalse=false), nowhere near enough to safely triangulate an offset without
risking a wrong guess that looks plausible but isn't (the exact mistake this file has been burned by
more than once — see its own repeated "don't guess a fix without real data" lesson). Revisit with
more real AOI samples if this becomes a real blocker.

**Known, NOT-yet-addressed gaps this same comparison surfaced, still open** (both now confirmed real,
not speculative, but genuinely out of scope for this pass):
- ~~A real Studio-authored AOI always carries `EnableIn`/`EnableOut` system-defined parameters...~~
  — **fixed, see "EnableIn/EnableOut and AOI LocalTag creation" below.**
- An AOI's own logic can reference a GSV/SSV system object (`AOI_SNTP_QUERY` does this via
  `GSV(WallClockTime,,DateTime,...)`/`SSV(WallClockTime,,DateTime,...)`, reading/setting the
  controller's own wall-clock time) — a real dependency class Rockwell's own export handles (a bare
  `<WallClockTime Use="Reference">` element, sibling to `<AddOnInstructionDefinitions>`, not wrapped
  in a `<Modules Use="Context">` container the way a real hardware Module reference is) that this
  wrapper's dependency resolution has no concept of at all — only `.parameters`/`.local_tags` data
  types are walked, never a routine's own instruction text for system-object references.

Covered by `test_new_aoi_dates_use_real_iso8601_format`,
`test_export_aoi_wrapper_includes_target_revision_and_last_edited`,
`test_export_aoi_includes_dependencies_block_for_target_with_udt_parameter`, and
`test_export_aoi_no_dependencies_block_when_nothing_referenced` (`test/test_api.py`).

### EnableIn/EnableOut and AOI LocalTag creation — the two gaps flagged after the first real AOI build

A downstream agent's first real build against the brand-new `db_new_aoi()`/`db_new_aoi_parameter()`/
`db_export_aoi()` support (confirmed working via a smoke test first — create/parameter/routine/rung/
export all succeeded on a scratch copy) hit both of the "known, not-yet-addressed" gaps flagged in the
section above the moment it needed an AOI with real internal logic, not just the smoke-test shape:

1. **No way to produce a correct `EnableIn`/`EnableOut` pair.** `new_aoi_parameter()`/
   `db_new_aoi_parameter()` always hardcoded `Required="true"`, `Visible="true"`, and (for
   Input/Output) `ExternalAccess="Read/Write"` — the real convention for these two specifically is
   `Required="false"`, `Visible="false"`, `ExternalAccess="Read Only"`, with no way to get there
   short of hand-constructing a `Parameter` and bypassing the constructor entirely.
2. **No `db_*`/constructor path to add an AOI LocalTag at all.** `new_aoi()`'s own docstring said so
   explicitly — real internal scratch state (the agent's case: 1-2 reconstructed/clamped working
   values per scan with no business being a public Input/Output pin) had no way in except appending a
   hand-built `LocalTag(...)` object directly.

**Fix, both requested shapes delivered rather than picking one over the other** (the agent's own
write-up offered either "overrides on `new_aoi_parameter()`" or "a dedicated call," and both are cheap
enough to add together):

- `new_aoi_parameter()`/`db_new_aoi_parameter()` gained optional `required`/`visible`/
  `external_access` override parameters (`None` = original default behavior, unchanged for every
  existing caller) — the general-purpose fix, usable for `EnableIn`/`EnableOut` or any other
  parameter needing non-default attributes.
- `new_aoi_enable_parameters() -> (Parameter, Parameter)` (`acd/l5x/elements/model.py`) — a
  ready-made, correctly-attributed `EnableIn`/`EnableOut` pair built on top of the overrides above, so
  a caller doesn't have to get the three attributes right by hand for the one case that needs them on
  every single AOI. No `db_*`-layer equivalent constructor was added (nothing to add — a caller uses
  `db_new_aoi_parameter(..., required="false", visible="false", external_access="Read Only")` twice,
  or builds the pair via the in-memory function and inserts both through the ordinary
  `project.controller.aois`/`export_aoi()` path if working at that layer instead).
- `new_aoi_local_tag(name, data_type, dimension=None, description=None) -> LocalTag`
  (`acd/l5x/elements/model.py`, mirroring `new_aoi_parameter()`'s shape minus the
  Usage/Required/Visible concerns a LocalTag doesn't have) and `ProjectDB.new_aoi_local_tag()`/
  `db_new_aoi_local_tag()` (`acd/l5x/project_db.py`) — a real SQL-backed creation path, not just the
  in-memory constructor. New `proj_aoi_local_tags` table (`aoi_id`, `seq`, `name`, `data_type_name`,
  `dimensions`, `radix`, `external_access`, `description`), same shape and same negative-intermediate
  seq-shift technique as `proj_aoi_parameters`/`proj_members`. `_load_aois()` now populates
  `AOI.local_tags` from this table instead of always passing `[]` — the one other place that needed
  updating, since `to_controller()`'s own append-not-replace convention for `.aois` (see the AOI
  creation section above) required no changes at all.
- `_validate_aoi_parameters_resolve()` (`rendering.py`) now ALSO walks `.local_tags`, not just
  `.parameters` — its own docstring previously said local tags were skipped specifically because
  nothing could create one through this library's API, so there was nothing the check could ever catch
  there; now that `new_aoi_local_tag()`/`db_new_aoi_local_tag()` exist, that reasoning no longer holds,
  and a local tag typed as an unresolvable struct name is exactly the same silent-bare-zero failure
  mode this whole `validate=True` mechanism exists to catch early (see "Second convenience-API batch"
  above for the original incident this pattern was built to prevent).

**Still not fixed, deliberately, same reasoning as before**: `ExecutePrescan`/`ExecutePostscan`/
`ExecuteEnableInFalse` decode (confirmed wrong via the real `AOI_SNTP_QUERY` instance, see above) is
unrelated to either of these two gaps and still needs more real samples before a byte-offset fix is
safe to attempt.

Covered by `test_new_aoi_parameter_overrides_required_visible_external_access`,
`test_new_aoi_parameter_overrides_default_to_original_behavior_when_omitted`,
`test_new_aoi_enable_parameters`, `test_new_aoi_local_tag_defaults`, `test_new_aoi_local_tag_dimension`,
`test_new_aoi_local_tag_udt_type_omits_radix`, `test_export_aoi_validate_rejects_unresolved_local_tag_type`,
and `test_export_aoi_renders_enable_parameters_and_local_tags` (`test/test_api.py`); `test_new_aoi_parameter_required_visible_external_access_overrides`,
`test_new_aoi_local_tag_creates_and_persists`, `test_new_aoi_local_tag_missing_aoi_raises_key_error`,
`test_new_aoi_local_tag_duplicate_name_raises`, `test_new_aoi_never_touches_real_pre_existing_aoi_local_tags`
(companion to the pre-existing `test_new_aoi_never_touches_real_pre_existing_aoi`, confirming the
append-not-replace guarantee still holds now that a brand-new AOI can carry its own LocalTags too), and
`test_db_new_aoi_local_tag_stateless_wrapper_and_export` (`test/test_project_db.py`) — full suite
re-run clean (335 passed, 2 skipped, up from 321 before this round).

**Still unverified, same caveat as the rest of this AOI feature**: none of this has been tried against
a real Studio 5000 import yet — the attribute values (`Required="false"`/`Visible="false"`/
`ExternalAccess="Read Only"` for the enable pair) are confirmed correct only by structural comparison
against the real `AOI_SNTP_QUERY` export, not by an actual successful import of a file built with them.

**UPDATE — the EnableIn/EnableOut and LocalTag fix above WAS verified against a real Studio 5000
import.** The same downstream agent built a real AOI (rung-count and internal logic elided here per
this file's own "no real project references" convention) using both features together, verified the
read-back content matched exactly, and exported it. This is the first real-import confirmation for
ANY part of the AOI-creation feature since it was first added.

### AOI routines are NOT free-named, and AOI-scoped routine content had no way to be addressed back

Two related, real problems surfaced from that same first real AOI build — one a genuine `db_*` API
gap, the other a previously-unknown Rockwell constraint this library had never encoded anywhere:

**1. `aoi_name=` was accepted by `db_new_routine()` but nothing downstream could resolve it.**
`db_new_routine(acd_path, name, type, aoi_name=aoi_name)` could create a routine unambiguously scoped
to an AOI, but `db_insert_st_line`/`db_insert_rung`/`db_get_routine`/`db_replace_st_line_safe`/
`db_replace_rung_safe`/`db_delete_st_line`/`db_delete_rung`/`db_set_rung_comment` only ever accepted
`program_name=` to disambiguate — which doesn't resolve AOI scope at all (`KeyError: "No program
named '...'"`). Passing neither raised `ValueError: ... is ambiguous ... pass program_name= to
disambiguate`, but `program_name=` was exactly the thing that didn't work. Since nearly every real
AOI in a typical project names its main routine `"Logic"` (see point 2 below — this isn't a coincidence,
it's close to the only legal name), creating ANY new AOI with a `"Logic"` routine became instantly
ambiguous and uneditable the moment a second AOI existed in the project.

Fixed by adding `aoi_name=` to every one of those eight `ProjectDB` methods (and their `db_*`
wrappers), mirroring `program_name=`'s own shape exactly — `_routine_id()` (the shared scope-resolution
helper) now accepts either (raising `ValueError` if both are given), resolving against `proj_routines
.aoi_id` via `_aoi_id()` the same way `program_name` resolves via `_program_id()`. `get_routine()`/
`export_routine()` (the two methods that go through the in-memory `acd.api.get_routine()` rather than
raw SQL) translate `aoi_name` into that function's own PRE-EXISTING `program_name=f"AOI:{aoi_name}"`
convention internally (`_get_routine_scope()`) — that convention already existed for
`io_addresses_by_routine()`/`find_tag_references()`, just never plumbed through to these two.

**2. A real Studio 5000 import rejection revealed AOI routine names are a fixed, reserved set —
not a free-form choice.** Before the `aoi_name=` fix above landed, the practical workaround was
renaming the routine away from the `"Logic"` collision (e.g. to something derived from the AOI's own
name) — which felt like the wrong kind of workaround (documented as exactly that at the time), and
turned out to be provably wrong, not just inelegant: importing the resulting L5X into real Studio 5000
failed outright:
```
Error: Line 36: Error creating 'Routine' (Invalid name for Add-On Instruction routine.).
    RSLogix5000Content/Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition[@Name="..."]/Routines/Routine[@Name="..._Logic"]
```
Confirmed independently against the real, Rockwell-authored `AOI_SNTP_QUERY` ground-truth export
(already used elsewhere in this file's AOI verification work): `grep`-ing every `<Routine Name=...
Type=...>` in that file returns ONLY `"Logic"` (three times, across the target and its own context
AOI dependencies) and `"Prescan"` (once) — never a custom name, anywhere. Unlike a Program's routine
(any name is fine), a routine living directly under an `AddOnInstructionDefinition` may ONLY be named
`"Logic"`/`"Prescan"`/`"Postscan"`/`"EnableInFalse"` — the same four names as the AOI's own
`ExecutePrescan`/`ExecutePostscan`/`ExecuteEnableInFalse` flags plus the mandatory main routine.
`"Postscan"`/`"EnableInFalse"` are included by direct symmetry with `"Prescan"`, not independently
observed (neither flag was `"true"` in the one real sample available) — revisit if a real sample using
either ever turns up different behavior.

Fixed with defense at three layers, not just one:
- `ProjectDB.new_routine()` raises `ValueError` immediately if `aoi_name` is given with a
  `routine_name` outside `{"Logic", "Prescan", "Postscan", "EnableInFalse"}`
  (`_AOI_RESERVED_ROUTINE_NAMES`, `acd/api.py`) — catches the mistake at creation time, before any
  content is even written, rather than only at export/import time.
- `export_aoi()` (`acd/api.py`) checks the SAME constraint unconditionally (not gated behind
  `validate=`) against every routine in `aoi.routines` — this is the defense-in-depth layer for the
  in-memory `new_routine()`/`model.py` path, which has no way to know at construction time which
  collection its result is about to be appended to and so can't enforce this itself (its own docstring
  now says so explicitly, pointing forward to `export_aoi()`/`ProjectDB.new_routine(...,
  aoi_name=...)`).
- This is deliberately NOT gated behind `validate=True/False` the way the type-resolution checks are
  — a wrong AOI routine name is a certainty to fail Studio's import, not a "might be a problem"
  data-shape risk, so it isn't optional.

**3. A related design mistake caught before shipping, not after**: the first draft of the `aoi_name=`
fix added the parameter to `ProjectDB.export_routine()`/`db_export_routine()` too, by analogy with the
other seven methods. This is wrong and was caught by the test suite itself (a real `ValueError: routine
not found in any program of this project` from the underlying, Program-only `acd.api.export_routine()`)
before it ever reached a user: Studio 5000 has no "Import Routine" mechanism for a routine living
inside an `AddOnInstructionDefinition` at all — the ONLY way to get an AOI's routine content into Studio
is `export_aoi()`, which exports the whole AOI (parameters, local tags, AND every routine) via "Import
Add-On Instruction...". `export_routine()`/`db_export_routine()` still accept `aoi_name=` for signature
symmetry with the other seven methods (so a caller who reflexively tries it gets a clear, actionable
error naming `export_aoi()` instead of either silently doing the wrong thing or getting a confusing
"not found in any program" message with no indication of why).

Covered by `test_new_routine_rejects_invalid_aoi_routine_name`,
`test_new_routine_accepts_reserved_aoi_routine_names`,
`test_routine_content_functions_use_aoi_name_to_disambiguate_logic_routines` (the literal reported
repro: two AOIs, both with a `"Logic"` routine, disambiguated via `aoi_name=` instead of renaming),
`test_db_routine_content_functions_accept_aoi_name`,
`test_export_routine_raises_clear_error_for_aoi_owned_routine` (`test/test_project_db.py`); and
`test_export_aoi_rejects_invalid_routine_name`, `test_export_aoi_accepts_reserved_routine_name`
(`test/test_api.py`) — full suite re-run clean (342 passed, 2 skipped, up from 335 before this round).

### `to_controller()` returned STALE Parameters/LocalTags for an AOI whose routine content was fresh

The very next real report, from the very same real AOI (`Lug_Advance`), after the `aoi_name=` fix
above unblocked the naming/routine-disambiguation problem: `db_get_routine(acd_path, "Logic",
aoi_name="Lug_Advance")` and `db_list_routines()` both correctly showed the CURRENT routine body (a
74-line ST rewrite, several `db_new_aoi("Lug_Advance", ...)` recreate-cycles later), but
`db_to_controller(acd_path).controller.aois` — and therefore `db_export_aoi()`, which uses it
internally — returned a completely different, OLDER `Parameters`/`LocalTags` shape for that same AOI
(a scalar `AdvTP` with `Required="true"`, `LocalTags` including members that don't exist in the
current design at all). Net effect: `db_export_aoi()` was producing an internally-inconsistent L5X —
`<Parameters>` describing `AdvTP` as a scalar while the (correctly fresh) `<Routine>` ST text wrote to
it as an array (`AdvTP[i] := ...`) — something a real Studio import would either reject outright or,
worse, accept with a silently wrong definition. One detail in the report turned out to be the key
clue rather than a red herring: `aoi.local_tags` also included a hex-placeholder-looking name
(`$...$`) that was never created by any `db_new_aoi_local_tag()` call.

**Root cause, confirmed directly**: the reporting agent had, by this point in the same session,
actually completed a real Studio 5000 import of an earlier `Lug_Advance.L5X` (see the `aoi_name=`
section above — that's what surfaced the `"Invalid name"` rejection in the first place; a later retry,
after renaming the routine to `"Logic"`, evidently succeeded) and the project had been re-saved from
Studio. On the NEXT `open_project_db()` call, the changed mtime triggered `rebuild=True` — which,
correctly per `_load_aois()`'s own documented v1 scope, means `ControllerBuilder`'s fresh decode of
the real ACD NOW includes a real, Studio-authored `Lug_Advance` AOI (the hex-placeholder LocalTag is
exactly the kind of real, ACD-decoded artifact this codebase has documented elsewhere — see the
`"__Map:"`/hex-named-connection sections above — never something a synthetic `proj_aois` row would
ever contain). Meanwhile the user kept iterating on `Lug_Advance` through `db_new_aoi_parameter()`/
`db_new_aoi_local_tag()`, which (correctly, per `new_aoi()`'s own v1 scope: `db_new_aoi()` never
checks against real project AOI names) created a SEPARATE, still-fresh `proj_aois` row under the
identical name. `to_controller()` then did `controller.aois = controller.aois + self._load_aois()` —
literally concatenating both same-named objects into one list, real-decoded first, `proj_aois`-sourced
second. `ProjectDB.export_aoi()`'s own lookup (`next(a for a in ... aois if a.name.upper() ==
aoi_name.upper())`) — and the reporting agent's own diagnostic script's `[a for a in ctrl.aois if
a.name == "Lug_Advance"][0]` — both pick the FIRST match, i.e. the stale, real one, never reaching the
fresh one appended right after it.

**Fixed** in `to_controller()` (`acd/l5x/project_db.py`): before appending, any real,
`ControllerBuilder`-decoded AOI whose name (case-insensitively) collides with one loaded via
`_load_aois()` is excluded from the real portion first — the `proj_aois`-sourced object always wins a
name collision, on the reasoning that it's unconditionally the more current state of that name for
anyone actively editing it through `db_*` (the same reasoning already applied everywhere else in this
subsystem: `proj_*` tables ARE the live, editable project state). This is a targeted collision rule,
not a reversal of the "never materialize a real AOI into `proj_aois`" v1 design decision — a real AOI
with NO name collision is completely unaffected, still sourced fresh from the ACD with its real
LocalTags intact, exactly as before.

The hex-placeholder LocalTag needed no separate fix: `LocalTag._l5x_exclude` (checked generically by
every list-section XML serializer, `acd/l5x/elements/base.py`) already excludes any name whose first
character isn't alphabetic/`_` — a `$hex$`-style name was already being filtered out of `<LocalTags>`
XML rendering before this fix; it was only ever a diagnostic clue (proof the picked object was the
real, ACD-decoded one), never itself a rendering bug.

Covered by `test_new_aoi_wins_name_collision_against_real_pre_existing_aoi` (constructs the exact
collision using the real `AddOnInstruction` AOI in the `ACDTestsWithAOI.ACD` fixture, confirms exactly
ONE object survives per name and it's the fresh one) and
`test_db_export_aoi_uses_fresh_parameters_on_name_collision` (end-to-end through `db_export_aoi()`,
confirming the rendered L5X reflects the fresh parameter, not the real fixture AOI's own) —
`test/test_project_db.py`.

### AOI `Input`/`Output` parameters can never be arrays — only `InOut` can

The next real Studio 5000 import attempt on the same real AOI (`Lug_Advance`, now past the
stale-parameter fix above) failed on a different, previously-unencoded Rockwell constraint:
```
Error: Line 22: Invalid array. Input or output parameter must be of supported elementary data
type with no dimensions.
    RSLogix5000Content/Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition[@Name="Lug_Advance"]/Parameters/Parameter[@Name="StaticTP"]
```
An AOI parameter declared `Usage="Input"`/`"Output"` (passed by value/copied) may ONLY be a scalar
elementary type — Rockwell rejects an array outright, unlike `Usage="InOut"` (passed by reference),
which may be an array. Neither `new_aoi_parameter()` nor `db_new_aoi_parameter()` (nor `export_aoi()`
itself) had ever checked this — an Input/Output parameter with a `dimension` simply rendered as-is,
looking structurally fine right up until a real Studio import.

Fixed at three layers, mirroring the reserved-routine-name fix above exactly:
- `new_aoi_parameter()` (`acd/l5x/elements/model.py`) raises `ValueError` immediately if `dimension`
  is given with any `usage` other than `"InOut"` — catches the mistake at construction time. This
  required fixing an existing test (`test_new_aoi_parameter_dimension`) that had been exercising
  exactly this now-invalid combination (`usage="Output", dimension=10`) since it was first written —
  nobody had checked it against real Rockwell rules before this report.
- `ProjectDB.new_aoi_parameter()`/`db_new_aoi_parameter()` inherit the same guard for free, since both
  call the same underlying constructor.
- `export_aoi()` (`acd/api.py`) checks every parameter in `aoi.parameters` unconditionally (not gated
  by `validate=`, same reasoning as the routine-name check: this is a certainty to fail import, not a
  "might be a problem" risk) — defense-in-depth for a caller who hand-constructs a `Parameter` object
  directly rather than going through `new_aoi_parameter()`.

Covered by `test_new_aoi_parameter_rejects_array_input`, `test_new_aoi_parameter_rejects_array_output`,
`test_new_aoi_parameter_scalar_input_output_still_allowed`,
`test_export_aoi_rejects_array_input_output_parameter` (the defense-in-depth layer, via a
hand-constructed `Parameter`), and `test_export_aoi_accepts_array_inout_parameter` (confirms `InOut`
arrays are still allowed, not accidentally banned entirely) — `test/test_api.py`.

## `ProjectDB.get_routine()` used to pay for a FULL project rehydration just to answer "what's in this one routine" — a real 10-minute regression on a routine-by-routine scan

A real report, unrelated to the AOI work above but hitting the exact "full-project scan" pattern this
codebase's own docs (see "Lazy / summary-first lookups" above) explicitly promise is cheap: before
converting a routine to an AOI, the agent wanted to find every reference to it and to the controller-
scoped tags it uses — "same pattern as" prior conversions. Two attempts:
1. Looped `acd.db_get_routine(acd_path, name, program_name=prog)` (the STATELESS wrapper) once per
   routine over all ~180 routines in the project, grepping each result. Killed after 10+ minutes —
   `tasklist` showed 10:52 of actual CPU time already burned, not idle/blocked.
2. Same loop, but through ONE `open_project_db()` connection and the instance methods
   (`db.list_routines()`/`db.get_routine(...)`) instead of the stateless wrappers, closing once at the
   end. ~8-9x less CPU time at a comparable wall-clock point (1:16 vs 10+ minutes) — confirming
   per-call connection open/lock/verify overhead was A real cost — but STILL running past 120 seconds,
   meaning reusing one connection wasn't the whole story.

**Root cause of the remaining cost, found by reading `ProjectDB.get_routine()`'s own implementation**:
it built a FULL `to_controller()` rehydration — `ControllerBuilder.build()` decoding every tag's
initial value, every UDT, every Module/AOI/Task, the whole controller graph — just to pick ONE
routine's rungs/ST-lines/comments out of it and discard everything else. This is the exact "known v1
cost/simplicity tradeoff" this class's own docstring already named (see "Persistent project DB" above)
— always true, but never actually measured against a 180-routine real project until this report made
it concrete: 180 full rehydrations for a single scan.

**Two real fixes, not one, because they compound differently**:
1. **`ProjectDB.get_routine()`** (`acd/l5x/project_db.py`) now reads `proj_routines`/`proj_rungs`/
   `proj_st_lines` DIRECTLY via SQL for the common case — a Program's routine, or an AOI created via
   `new_aoi()`/`db_new_aoi()` in THIS project DB — no `to_controller()` call at all. `_routine_id()`
   (already shared by every routine-content method, see the earlier `aoi_name=` section) does the
   name→id resolution; a small, targeted set of `SELECT`s pulls exactly the rows this one routine
   needs. Falls back to the old, full-rehydration behavior ONLY when nothing is found in
   `proj_routines` at all — which happens for exactly one case: **a REAL, pre-existing AOI's own
   routine, which `_materialize()` never puts in `proj_routines` in the first place** (it only ever
   walks `ctrl.programs`, never `ctrl.aois` — the same "`proj_aois` never holds a real AOI" design
   decision from the earlier AOI-collision fix, just discovered to have this second consequence too).
   That fallback is a single, rare lookup (one named routine, not a 180-iteration loop), so paying the
   old slow-path cost there is an acceptable, deliberate trade — not something worth chasing further.
2. **`ProjectDB.list_routines()`** was ALSO tried as a SQL-direct rewrite first, and reverted — this
   is the one part of this fix that needed a second pass to get right. A SQL-only `list_routines()`
   would silently OMIT every real, pre-existing AOI's own routines from its output (same
   `_materialize()` gap as above), which is a much more dangerous failure mode for a "list every
   routine" call than for a single named lookup: a caller auditing "everything that references X"
   would get an incomplete, silently-wrong list with no error, exactly the kind of "succeeds with
   quietly wrong data" bug this codebase's own methodology section warns hardest against. Caught by a
   dedicated test (`test_get_routine_accepts_aoi_prefixed_program_name_from_list_routines`) failing
   with `KeyError` when a real AOI's own routine, listed via the (at-the-time SQL-only) `list_routines()`,
   couldn't be found by the also-SQL-only `get_routine()` — the mismatch is what surfaced the gap.
   `list_routines()` stays on `to_controller()` — the ONE-time full-decode cost of a single call was
   never actually the reported problem (the report's own numbers show `db_list_routines()` was called
   ONCE, not looped; the 180x cost was entirely `get_routine()`).
3. A related, standalone improvement made while fixing this: `_routine_id()` now also accepts a
   `program_name` starting with `"AOI:"` (e.g. `"AOI:MyAOI"`) as shorthand for `aoi_name="MyAOI"` —
   the exact convention `list_routines()`'s own `"program"` field already uses for an AOI-owned routine
   (matching `acd.api._all_routines()`'s keying). Without this, a caller iterating `list_routines()`'s
   own output and feeding each entry straight into `get_routine(entry["routine"],
   program_name=entry["program"])` would get a confusing `KeyError` for every AOI-owned entry (no
   program is ever literally named `"AOI:MyAOI"`) — this closes that gap so the natural
   list-then-fetch loop works uniformly regardless of whether a routine belongs to a Program or an AOI.

**Also, a discoverability fix**: the reporting agent's own third question was whether this should be
called out more explicitly, since reading the docstrings first hadn't surfaced the anti-pattern. Added
a new "SCANNING EVERY ROUTINE FOR A REFERENCE" section to `acd/__init__.py`'s module docstring
(positioned right after the READS bullets, where `db_get_routine`/`db_find_tag_references` are
introduced) naming the real numbers from this report and pointing at the actual best tool for the
underlying need first: `db_find_tag_references(acd_path, name, regex=True)` already answers "every
place X is referenced, project-wide" — including a routine CALLING another by name, since `JSR(X,...)`
is just text a substring/regex search matches like anything else — in ONE call, with no loop needed at
all. Looping `get_routine()`/`db_get_routine()` is for when you need per-routine access itself (e.g.
editing many routines in sequence), not for a pure "who references X" search.

Covered by `test_get_routine_does_not_rehydrate_full_controller` (spies on `ControllerBuilder.build`,
confirms it's never called for a Program routine), `test_list_routines_still_includes_real_aoi_routines`
(confirms the deliberately-NOT-SQL-only `list_routines()` still sees a real AOI's routines),
`test_get_routine_accepts_aoi_prefixed_program_name_from_list_routines` (the fallback path, confirmed
identical whether reached via the `"AOI:"` prefix shorthand or the explicit `aoi_name=` parameter), and
`test_list_routines_and_get_routine_line_counts_agree` (cross-checks the two independent SQL paths
report the same line counts for every routine) — `test/test_project_db.py`.

## `export_routine()` couldn't resolve an instance tag typed as a brand-new (not-yet-real) AOI

A real report, immediately following the `Lug_Advance`/`Value_To_Str` AOI work above: converting a
6-line ST subroutine into a reusable AOI, then rewiring its 4 call sites into an existing routine
(`R11_Printer`) in one `db_transaction()`, verified correct via routine-content readback -- but
`db_export_routine()` for `R11_Printer` failed validation outright, even though `db_export_aoi()` for
the same new AOI, and creating the instance tag itself via `db_new_tag()`, both already worked. The
report itself correctly diagnosed the shape of the bug from the error message alone: `"Tag
'MyInstance': type 'Value_To_Str' does not resolve to ... most likely a stale/incomplete
data_types_map (see _sync_data_types_map()) rather than a genuinely unknown type"` -- and confirmed it
was specific to "AOI exists only in `proj_aois`, not yet real" by checking that a routine referencing
an instance of `Lug_Advance` (a REAL, already-imported AOI from earlier in the same session) exported
fine through the exact same code path.

**Root cause, confirmed exactly as the report's own diagnosis suggested**: `_sync_data_types_map()`
(`acd/api.py`) only ever walked `project.controller.data_types`, registering each into the shared
`data_types_map` every tag/validator consults -- it never touched `project.controller.aois` at all. A
REAL AOI's own name resolves anyway, but for a completely different reason that has nothing to do with
this function: `ControllerBuilder.build()` (at `load_acd()`/`to_controller()` time) already seeds a
real, Comps.Dat-derived SYNTHETIC `DataType` for every real AOI's own instance-data shape into that
same map (see the "AOI instance-value decoding" gap documented elsewhere in this file for how
imperfect that synthetic shape actually is) -- a mechanism that only ever runs for AOIs that exist in
the real ACD at load time. A `db_new_aoi()`-created AOI has no such backing entry at all, so its name
was never resolvable, regardless of how many times `_sync_data_types_map()` ran.

**Fixed** by extending `_sync_data_types_map()` to also register each `project.controller.aois` entry
that has no existing map entry (`setdefault`-equivalent, so a REAL AOI's own richer, Comps.Dat-derived
synthetic shape is never overwritten -- only a genuinely new, not-yet-real AOI gets this fallback) via
a new `_synthetic_aoi_data_type(aoi)`: a best-effort `DataType`-shaped stand-in built from the AOI's
own `.parameters` + `.local_tags`, each converted to a plain `Member` via the already-existing
`new_member()`. Verified this doesn't leak into `_resolve_type_closure()`'s own dependency-closure
computation (which builds its own separate `data_types_by_name`/`aois_by_name` dicts directly from
`project.controller.data_types`/`.aois`, never from `data_types_map` at all) -- the AOI still correctly
resolves as an AOI dependency (`<AddOnInstructionDefinitions Use="Context">`), not a spurious duplicate
DataType context entry.

**Explicitly NOT claiming real fidelity** -- the docstring says so directly: this is a reasonable
default (every parameter/local tag rendered zero-filled, in declared order) for the one narrow purpose
of letting validation pass and the instance tag's own `<Data>` block render SOMETHING real instead of
silently nothing (the previous, safe-but-unhelpful behavior: `_struct_members_xml()` already gracefully
returns `None` for an unresolved type rather than crashing, which is why this was "only" a validation
failure and a silently-empty `<Data>` block, never a corrupt file) -- not a claim that a not-yet-real
AOI's rendered instance value matches what a real Studio-created instance would eventually show.

**A real, separate gap found and worked around while writing the regression test for this, NOT part of
the same fix**: a bare model-layer `new_tag()` call (as opposed to `db_new_tag()`, which goes through
`ProjectDB._load_tags()`) never wires the new `Tag`'s own `_data_types_map` attribute to the project's
shared map at all -- `new_tag()` is a deliberately pure, project-context-free constructor (matching
`new_member()`/`new_datatype()`/`new_routine()`'s own established shape), so it has no project reference
to wire against. This is why the FIRST version of this fix's own end-to-end test failed on a rendering
assertion (an empty `<Data>` block) even though `export_routine(validate=True)` itself already
succeeded -- validation reads `project.controller._data_types_map` directly, but XML *rendering* reads
each tag's OWN `._data_types_map` reference, a genuinely separate value unless something wires them
together. This exact caveat was already known and already demonstrated correctly by an existing test
(`test_sync_data_types_map_propagates_new_type_to_existing_tags`, which manually sets
`tag._data_types_map = project.controller._data_types_map` for precisely this reason) -- not a new bug,
just a pre-existing trap this investigation walked into and is now flagging explicitly rather than
leaving implicit. `db_new_tag()`/`ProjectDB` callers (the real, documented, recommended surface) are
unaffected -- `_load_tags()` already wires this correctly for every tag it builds.

Covered by `test_sync_data_types_map_registers_new_aoi_instance_type`,
`test_sync_data_types_map_does_not_overwrite_real_aoi_synthetic_type`,
`test_export_routine_validate_resolves_new_aoi_instance_type` (`test/test_api.py`, the latter using the
manual `_data_types_map` wiring per the caveat above, checked against real rendered XML: both the
`<AddOnInstructionDefinition>` context block and a real `<Structure DataType="Value_To_Str">` for the
instance tag, not just that validation passes) and `test_db_export_routine_resolves_instance_of_not_yet_real_aoi`
(`test/test_project_db.py` -- the literal reported repro, through the real `db_*` surface end-to-end,
where `db_new_tag()` wires `_data_types_map` automatically with no manual step needed).

## AOI Input/Output parameters must be an elementary (atomic) data type, not just non-array

The very next real Studio 5000 import attempt on the same `Value_To_Str` AOI (after the fix above
unblocked exporting `R11_Printer`) hit a second, related, previously-unencoded Rockwell constraint:
```
Error: Line 14: Error creating 'Parameter' (Input or output parameter must be of supported
elementary data type.).
    RSLogix5000Content/Controller/AddOnInstructionDefinitions/AddOnInstructionDefinition[@Name="Value_To_Str"]/Parameters/Parameter[@Name="PadChar"]
```
`PadChar` was declared `DataType="STRING"`, `Usage="Input"` — a SCALAR (no dimensions), so the
already-fixed "array only on InOut" check (see the AOI creation support section above) didn't catch
it; the actual `Value_To_Str.L5X` export also has a second STRING-typed, non-`InOut` parameter
(`Result`, `Usage="Output"`) that Studio never even reached, since it stops at the first error per
import attempt — both would need the same fix.

**This unifies with, rather than replaces, the array-only-on-InOut rule already documented above**:
Rockwell's real constraint is broader than "no arrays" — an `Input`/`Output` AOI parameter (passed by
value/copied) may ONLY be one of Rockwell's own "elementary" (atomic) types: `BOOL`, `SINT`/`INT`/
`DINT`/`LINT` (+ unsigned `U*` variants), `REAL`/`LREAL`. `STRING`, any project UDT, and another AOI are
all "structured" types in Rockwell's own terminology (same category as `TIMER`/`COUNTER`), and are
therefore just as invalid on `Input`/`Output` as an array is — only `InOut` (passed by reference) may
use ANY of: an array, `STRING`, a UDT, or another AOI.

**Fixed the same three-layer way as the array constraint**, generalizing rather than duplicating:
- `new_aoi_parameter()` (`acd/l5x/elements/model.py`) gained a companion check alongside the existing
  dimension check: for `usage != "InOut"`, `data_type.upper()` must be in the new
  `_AOI_ELEMENTARY_PARAM_TYPES` constant (`_PRIMITIVE_RADIX`'s own key set + `"BOOL"`, since `BOOL` has
  no `Radix` attribute of its own in Rockwell's schema and so isn't in that dict) — raises `ValueError`
  immediately otherwise, naming the real Studio error text.
- `ProjectDB.new_aoi_parameter()`/`db_new_aoi_parameter()` inherit the guard for free (same shared
  constructor).
- `export_aoi()` (`acd/api.py`) checks every `Input`/`Output` parameter's `data_type` unconditionally
  (not gated by `validate=`, same reasoning as every other structural AOI check in this file) — the
  array check and the new elementary-type check are now one combined loop (`if p.usage == "InOut":
  continue` up front, then both checks in sequence), rather than two separate loops.

**Existing tests needed real fixes, not just additions** — several pre-existing tests had been
constructing STRING/UDT-typed `Input` parameters (the default `usage`) purely incidentally, never
checked against this real constraint before now: `test_new_aoi_parameter_udt_type_omits_radix`,
`test_export_aoi_includes_dependencies_block_for_target_with_udt_parameter`, and
`test_export_aoi_validate_rejects_unresolved_parameter_type` all switched to `usage="InOut"` so they
keep testing what they were actually meant to test (radix omission, dependency closure, unresolved-type
detection) rather than tripping the new, unrelated elementary-type guard first. The two
`Value_To_Str`-instance-type regression tests from the section above (which use a STRING `Result`
parameter) were similarly updated to `usage="InOut"` — matching what the REAL AOI will also need once
both `PadChar` and `Result` are fixed in Studio.

Covered by `test_new_aoi_parameter_rejects_string_input`, `test_new_aoi_parameter_rejects_string_output`,
`test_new_aoi_parameter_rejects_udt_input`, `test_new_aoi_parameter_string_inout_still_allowed`,
`test_new_aoi_parameter_all_elementary_types_allowed_for_input_output`,
`test_export_aoi_rejects_string_input_output_parameter` (defense-in-depth, via a hand-constructed
`Parameter`), and `test_export_aoi_accepts_string_inout_parameter` (`test/test_api.py`).

## `_synthetic_aoi_data_type()` must exclude InOut parameters — they have no instance storage at all

A direct, immediate consequence of the elementary-type fix above, found on the very next real Studio
5000 import attempt (of `R11_Printer.L5X`, after `PadChar`/`Result` were correctly changed to `InOut`
in the still-not-yet-real `Value_To_Str` AOI to fix that constraint):
```
Error: Line 644: Failed to set the 'Data' property (Data type mismatch - the object's value does
not match its data type.).
    RSLogix5000Content/Controller/Programs/Program[@Name="VAB_Trim_And_Sort"]/Tags/Tag[@Name="Printer_Thick_V2S"]/Data
```
`Printer_Thick_V2S` is one of the routine's own context instance tags of `Value_To_Str` (a
controller-scope tag typed as the AOI, per the "static config on the instance tag" design described in
the R11_Printer rewire). Root cause: `_synthetic_aoi_data_type()` (see the AOI-instance-type-resolution
fix above) built its member list from `aoi.parameters` UNCONDITIONALLY — it never excluded `InOut`
parameters, so once `Result` (and by the same logic, `PadChar`) became `InOut`, the synthetic instance
shape still included them as regular members, giving the instance tag's own rendered `<Data>` block
MORE fields than Rockwell's own real instance data shape has room for.

**The real Rockwell fact this was missing**: an AOI's `InOut` parameter is passed BY REFERENCE (an
alias to whatever tag is supplied at the call site) — Rockwell never allocates any real storage for it
inside the instance tag's own data structure at all. Only `Input`/`Output` parameters (copied in/out,
real backing storage) and `LocalTags` (private internal storage) are actually part of an instance's own
persistent data shape. This is consistent with — and retroactively explains — a detail from the
earlier, real `AOI_RPMtoFPM` instance-value investigation elsewhere in this file: the real Decorated
`<Structure>` there showed exactly `EnableIn`/`EnableOut` + the AOI's own Input/Output parameters, never
any `InOut` ones, though at the time that was noted only as an observation, not traced to this specific
mechanism.

**Fixed**: `_synthetic_aoi_data_type()` now filters `p.usage == "InOut"` out when building its member
list (LocalTags are unaffected -- they never had this problem, since a LocalTag has no `Usage` concept
at all). Left completely untouched, on purpose: `_resolve_type_closure()`'s own walk of `aoi.parameters`
(finding UDT/AOI dependency TYPES to include as export context) and `_validate_aoi_parameters_resolve()`
(checking every parameter's type resolves) — both correctly still need to see EVERY parameter including
`InOut` ones, since those are about the parameter's own TYPE being available as context/resolvable, a
completely different question from whether the parameter has instance STORAGE.

**A real, second-order lesson worth restating**: this is the third real Studio import round in a row
for the very same `Value_To_Str` AOI (reserved routine names → array-only-on-InOut → elementary-type →
this), each fix landing cleanly but each one only revealed by the NEXT real import attempt after the
previous fix was already applied. None of these were guessable in advance from documentation alone;
each came from an actual Studio 5000 rejection on an actual real project. This matches this file's own
long-standing "verified after N rounds, not fixed in one" pattern for `export_routine()`/
`export_datatype()`'s own history — `export_aoi()`/AOI-parameter creation is now accumulating the same
kind of real-import mileage, just later, since it's the newer feature.

Covered by `test_sync_data_types_map_excludes_inout_parameters_from_instance_shape` (direct unit test:
an `InOut` STRING parameter must not appear in the synthetic type's own members, while `Input`/`Output`
DINT parameters do) and updated assertions in
`test_export_routine_validate_resolves_new_aoi_instance_type`/
`test_db_export_routine_resolves_instance_of_not_yet_real_aoi` (confirming `"Result"` — correctly still
present in the AOI's own `<Parameters>` definition — is specifically absent from the INSTANCE tag's own
`<Structure>...</Structure>` block) — `test/test_api.py`/`test/test_project_db.py`.

## A not-yet-real AOI's instance tag renders NO `<Data>` at all — stop guessing Rockwell's real internal layout

Direct follow-up to the fix immediately above, on the exact same `Value_To_Str` AOI, one real Studio
5000 import further. The user did exactly the documented two-step workflow correctly: imported
`Value_To_Str.L5X` (`Use="Target"`) on its own first — Studio created the real AOI with no errors —
then tried importing `R11_Printer.L5X` (which references an instance tag typed as `Value_To_Str`, the
AOI included only as `Use="Context"`). That failed:
```
Error: Line 644: Failed to set the 'Data' property (Data type mismatch - the object's value does
not match its data type.).
    RSLogix5000Content/Controller/Programs/Program[@Name="VAB_Trim_And_Sort"]/Tags/Tag[@Name="Printer_Thick_V2S"]/Data
```
Root cause, confirmed by inspecting the actual rejected file and the project's own `acd.db` directly
(not guessed): `R11_Printer.L5X` had been generated *before* the AOI import, from a project state
where `Value_To_Str` was still only a `proj_aois` row, not a real ACD object — so
`Printer_Thick_V2S`'s `<Data>` content came entirely from `_synthetic_aoi_data_type()`'s best-effort
guess (`EnableIn`/`EnableOut` + declared `Input`/`Output` parameters + `LocalTags`, zero-filled). By
the time that file was actually handed to Studio, `Value_To_Str` already existed for real (from the
just-completed first import) with Studio's own, independently-computed internal instance layout —
and our guess didn't match it. This is the SAME already-documented, unresolved gap as the real
`AOI_RPMtoFPM`/`TestFPM` investigation elsewhere in this file (a real AOI instance's true `L5K` value
count didn't match its named-member count at all — 17 values for 10 named members, with an
unexplained leading value never traced to anything) — just now biting a *synthetic*, not-yet-real
AOI's instance too, for the identical underlying reason: Rockwell's real internal AOI instance layout
has structure beyond what's derivable from a declared Parameter/LocalTag list alone, and nobody has
ever fully reverse-engineered it.

**The user's own follow-up question was the right one to ask, and changed the fix**: "shouldn't the
DB be able to know it's there? The whole point of the DB is to do additions without creating the
whole thing again at export." That's correct, and exposed that my first answer (recommending a
mandatory two-step Studio workflow) was an unverified assumption, not a real constraint — this
codebase already has a *proven* precedent that a `Use="Context"` block CAN create a brand-new object
in a single Import Routine pass (see "Native-import escape hatches" above: a controller-scope tag
that never existed anywhere was created successfully by Studio from `<Tags Use="Context">` content
alone, no prior separate import). There's no reason `<AddOnInstructionDefinitions Use="Context">`
should behave differently for the AOI *definition* itself — and nothing in this investigation
contradicts that; the AOI creation half of a combined import was never what failed. The one thing
that's genuinely hard is asserting a *correct instance value* for a struct type whose real internal
layout we can't fully predict — that's a narrower problem than "AOI creation needs two Studio
imports," and one that can be sidestepped rather than solved.

**Fix**: rather than continue refining a value-shape guess with no way to verify it, a tag typed as a
still-not-real AOI (i.e. its `DataType` resolves in `_data_types_map` only via
`_synthetic_aoi_data_type()`, marked with a new `DataType._is_synthetic_aoi_instance = True` flag) now
renders **no `<Data>` element at all** — `Tag.to_xml()` (`acd/l5x/elements/model.py`) checks this flag
and skips both the known-value and the zero-fill-fallback rendering branches entirely. This mirrors an
existing, already-verified precedent in the very same function: an Alias tag also renders zero
`<Data>` elements (it has no value of its own), and a `Module` dependency stub already renders as a
bare, definition-free reference rather than asserting content Studio can supply itself. Studio
self-initializes the tag's value when it creates the AOI and the tag together — sidestepping the need
to guess Rockwell's real internal layout at all, rather than trying (and risking another wrong guess)
to match it. Once the AOI genuinely becomes real, `_sync_data_types_map()`'s own `setdefault` means
`_synthetic_aoi_data_type()` is never invoked for that name again — a later export of the same tag
naturally switches to the real, `ControllerBuilder`-decoded instance shape, with no extra code needed
for that transition.

**Consequence for a caller**: any static config set on a not-yet-real AOI's instance tag (e.g. this
project's `Width`/`PadChar` on `Printer_Thick_V2S`) is silently NOT carried through the first,
AOI-creating export — it needs to be applied as a small follow-up tag edit once the AOI is confirmed
real in Studio (a normal `db_edit_tag()`, which at that point goes through the real decode path and
should work reliably, the same as every other already-working tag-value edit documented in this
file). This is a real, deliberate trade-off, not an oversight: asserting a value we can't verify risks
exactly the failure this section describes; asserting no value at all trades a small extra step for
correctness.

**Not yet verified against real Studio 5000** — the previous fix in this AOI chain also looked correct
after unit verification and still needed a real import to catch the next issue; this one hasn't had
that real round-trip yet. Worth having the user agent retry the ORIGINAL `Value_To_Str`/`R11_Printer`
case once the project's `.ACD` is resaved and the DB rebuilt (which, per the mechanism above, will now
use the REAL decoded AOI shape rather than this fix's code path at all, since the AOI is already real
there) — but any NEW not-yet-real-AOI-plus-referencing-routine case going forward should exercise this
fix's actual code path and is worth confirming end-to-end.

Covered by `test_tag_to_xml_omits_data_for_synthetic_aoi_instance_type` (direct unit test: a tag typed
as a not-yet-real AOI, even with an explicit value set, renders zero `<Data>` elements),
`test_tag_to_xml_renders_data_normally_for_a_real_udt_type` (sanity counterpart: an ordinary struct tag
is unaffected), and updated assertions in `test_export_routine_validate_resolves_new_aoi_instance_type`/
`test_db_export_routine_resolves_instance_of_not_yet_real_aoi` (confirming no `<Data` appears anywhere
inside the instance tag's own element, while the AOI's own `<Parameters>` definition is unaffected) —
`test/test_api.py`/`test/test_project_db.py`.

## `export_program()`/`db_export_program()` — exporting a whole Program (all its routines) in one file

Added after a direct user question ("how hard would it be to export a whole program with multiple
routines?") followed immediately by the user supplying real, decisive ground truth: a genuine Studio
5000 "Export Program" output (`Motors_Program.L5X`, a real 52-routine production program from the same
Bethel_Planer project used throughout this file's history). Unlike `export_routine()` (which took
several real-import rounds of trial and error to get right — see "Partial/context L5X exports" above),
having the real wrapper shape in hand up front meant this could be calibrated correctly from the start
rather than guessed.

**Confirmed real Studio 5000 feature, not assumed**: "Import Program..." is a distinct, native command
from "Import Routine..." — verified via the local Studio 5000 help docs
(`Help/ENU/rs5000/help/rs5000/import-and-export/import-program-and-equipment-phase-considerations.html`),
not just inferred from the file's own `TargetType="Program"` attribute.

**Wrapper shape, confirmed against the real `Motors_Program.L5X`**:
- `TargetType="Program"` with **no `TargetSubType`** at all (unlike Routine (`RLL`/`ST`) or AOI targets,
  a Program has no sub-type) and **no leading `<!--description-->` XML comment** (unlike
  `export_routine()`'s own routine-description comment — a Program apparently gets no equivalent).
- The Program itself is the Target: `<Programs Use="Context"><Program Use="Target" ...>` — and, critically,
  **everything inside the target `<Program>` renders with NO individual `Use=` at all** — not on `<Tags>`/
  `<Routines>`, not on any single `<Tag>`/`<Routine>` inside them. This differs from `export_routine()`'s
  own wrapper, where the routine's *owning* Program is only `Use="Context"` and just the one target
  routine gets `Use="Target"` — here the whole Program **is** the target, so every routine/tag under it
  is part of the target too, exactly the same way `export_aoi()`'s own Parameters/LocalTags/Routines
  render with no individual `Use=` once the AOI itself carries `Use="Target"`.
- `Program.to_xml()` (already used unmodified for full-project export) turned out to render this exact
  shape already — `<Tags>`/`<Routines>` wrapped in no `Use=`, individual `<Tag>`/`<Routine>` elements the
  same way — so `export_program()` reuses it directly via the same `_inject_use_attr(program.to_xml(),
  "Program", "Target")` pattern already used for Routine/DataType/AOI targets, rather than needing new
  per-element rendering code.
- No `<Routine Use="Reference">` stub is ever needed the way `export_routine()` emits one for a JSR-called
  routine outside the exported set — a JSR call can never cross a Program boundary in native ladder logic
  (already established elsewhere in this file, `_referenced_called_routines()`'s own docstring), so every
  possible JSR target within the exported Program is already part of the Target itself.
- `<Modules Use="Context">`/`<AddOnInstructionDefinitions Use="Context">`/`<DataTypes Use="Context">`/
  controller-scope `<Tags Use="Context">` are the exact same shape `export_routine()` already produces —
  just fed the UNION of every routine's own referenced names (via the same `_referenced_tag_names()`/
  `_referenced_modules()`/`_resolve_type_closure()` helpers, unmodified, simply called with the
  concatenation of every routine's `_routine_lines()` instead of one routine's) rather than one routine's.
  Verified this union actually matters, not just theoretically: the small fixture's own `Branching`
  program has two routines (`B001_Main` referencing `Branching`, `B002_Timers` referencing
  `b_Timer`/`FaultCounter`/`FirstOutFault`, called from `B001_Main` via `JSR(B002_Timers,0)`) — all four
  controller tags appear as context, and no `Routine Use="Reference"` stub is emitted for the JSR target
  since it's already part of the Program's own routine list.
- Every one of the Program's own tags renders unconditionally (not filtered to "referenced," unlike
  `export_routine()`'s program-scope tag handling) since the whole Program is the Target — but the
  alias-target-resolution and program-tag-shadows-controller-tag rules from `export_routine()` still
  apply the same way, just checked against the FULL `program.tags` list instead of a referenced subset.

**A real, deliberately UN-solved gap, found and correctly abandoned rather than guessed**: the real
`Motors_Program.L5X` also has a `<ChildPrograms><ChildProgram Name="Motor_Sequence"/></ChildPrograms>`
element (Rockwell "Program Folder" nesting — a Program containing other Programs). A first hypothesis —
that this is a plain `parent_id` relationship in `Comps.Dat`, the same mechanism used everywhere else in
this codebase for object hierarchy — was checked directly against the real source ACD (the exact same
project) and **disproven**: no comps row named `Motor_Sequence` has `Motors`' own `object_id` as its
`parent_id`; in fact no comps row named exactly `Motor_Sequence` exists in the current project at all (a
*different*, unrelated top-level program `VAB_Motor_Sequence` does — the real export was very likely
taken at a moment shortly before a rename, in an actively-edited project). Rather than guess a mechanism
real data just disproved, `export_program()` does not attempt to detect or emit `<ChildPrograms>` at
all — documented as a known, explicit gap (in both the function's own docstring and `acd/__init__.py`'s
module docstring) rather than a silent wrong guess, the same treatment already given to FBD/SFC routine
content elsewhere in this file.

**NOT YET verified against a real Studio 5000 import** — only checked structurally against the real
`Motors_Program.L5X`'s shape and via unit tests against the small fixture. Every other `export_*()`
function in this file needed at least one real import round to find something the structural comparison
alone couldn't — expect the same here.

**A real, avoidable mistake made while building this, worth keeping as its own lesson**: the first smoke
test for `export_program()` was run directly against the user's LIVE production project (via
`db_to_controller()` on the real, in-use `.ACD`) instead of the small test fixture — it triggered a DB
rebuild that discarded one or more `db_*` edits made since the last rebuild that had never been exported
(the persistent DB's own `dirty` flag, see "Persistent project DB" above, correctly fired its warning).
The user confirmed real in-progress work was lost, though tolerated it ("it's fine, we'll do it again").
**The lesson, not really new but re-learned expensively**: this codebase's own established discipline of
testing against `resources/CuteLogix.ACD` (or an explicit scratch copy) instead of a real, actively-edited
project applies to informal smoke tests too, not just the checked-in test suite — a `db_to_controller()`/
`open_project_db()` call is not read-only against the project's own working state the way `load_acd()` is,
precisely because of the whole point of the persistent-DB feature (see "Persistent project DB" above:
"an edit ... writes directly into the current project"). The DIRTY-flag rebuild warning existing at all is
exactly the safety net that caught this — it did its job — but the right move is to never trigger it
against real project state for a throwaway experiment in the first place.

Covered by `test_export_program_produces_well_formed_target_xml`,
`test_export_program_includes_every_routine_with_no_individual_use_attr`,
`test_export_program_unions_tag_dependencies_across_every_routine`,
`test_export_program_raises_if_not_in_project`,
`test_export_program_validate_rejects_malformed_rll_syntax`,
`test_export_program_validate_false_does_not_check_rll_syntax` (`test/test_api.py`, against the real
`Branching` program in the small fixture) and `test_export_program_stateless_wrapper_includes_every_routine`,
`test_export_program_raises_on_missing_program`, `test_export_program_instance_method`
(`test/test_project_db.py`).

## CRITICAL: AOI parameter order was scrambled on export -- `seq_number` is not a reliable order key

Found via a real, severe downstream bug report on `db_export_program()`'s very first real use, but the
root cause turned out to live in `AoiBuilder.build()` (decode-time, not `db_export_program()`-specific)
-- affecting `export_routine()`/`export_aoi()`/`export_program()` identically, any time any of them pulls
in a pre-existing project AOI as a referenced Context dependency.

**Why this is critical, not cosmetic**: Studio 5000 calls an AOI instruction POSITIONALLY in RLL, not by
parameter name. A reordered `<Parameters>` redefinition silently rebinds every existing call site's
arguments to the wrong pins the moment it's accepted -- e.g. a value meant for `RunFwd` landing on
`Alarm`'s pin instead. Studio's own import warning for this case (`"Add-on instruction '...' already
exists ... Differences exist between the instruction definitions"`) does not call out that the
difference is a reordering, and the ladder logic itself displays completely normally after import --
this would not be caught by a visual review of the rungs, only by directly comparing the AOI's own
Parameters tab order before/after, which is exactly how the downstream user caught it (side-by-side
against the real, un-imported definition, on a copy project -- never risked against production).

**Root cause, confirmed directly against the real project**: `AoiBuilder.build()`'s query for an AOI's
own Parameters/LocalTags (`RxTagCollection` children) used `ORDER BY seq_number` as its only ordering.
Decoding the real, reported AOI (`VAB_PowerFlex_753`, 17 parameters) directly -- via a plain, read-only
`ExportL5x()` call against a throwaway scratch `_temp_dir` (never touching the project's own persistent
`acd.db`, learned the hard way earlier this same session, see "A real, avoidable mistake" under
`export_program()` above) -- found `seq_number` was IDENTICAL (a constant value) for 16 of its 19
`RxTagCollection` children. SQLite's tie-break order for `ORDER BY` on a column with no discriminating
value is implementation-defined (effectively arbitrary/insertion-order-ish here), which is exactly why
the resulting `.parameters` list came out scrambled -- confirmed by reproducing the EXACT same wrong
order the user reported, byte-for-byte, via this same decode path (independent of `db_export_program()`
or the persistent-DB layer entirely).

**Finding the real order key**: with the real AOI's own confirmed-correct Parameters-tab order in hand
(the user compared it directly in Studio 5000), every raw byte offset of each Parameter's own comps
record was brute-force scanned for one whose sort order reproduces that real order exactly. Multiple
offsets matched (all monotonic re-encodings of the same underlying bytes), but the cleanest and most
defensible: `member_ref` -- the SAME 4-byte field at raw record offset 14 that `ParameterBuilder`/
`LocalTagBuilder` ALREADY decode (for comment/description lookup, a completely different, already-
verified purpose) -- turned out to ALSO already fully and correctly encode the real order, monotonically,
with zero gaps. No new byte offset or new decode logic was needed -- reusing this already-established
field was chosen over a newly-discovered single-byte offset (which also happened to match) specifically
because `member_ref` already has independently-verified real semantic meaning in this codebase, rather
than trusting an unexplained byte position that "just happens" to match on one example.

**Fix**: `AoiBuilder.build()` now collects `(member_ref, object_id, is_param, original_position)` for
every `RxTagCollection` child, then does a single stable sort by `(member_ref, original_position)` --
the second tuple element only ever matters as a deterministic tiebreaker for the case two children
somehow share the same `member_ref` (never observed, but safer than leaving that case to chance) -- and
walks the SORTED list to build `parameters`/`local_tags`, instead of trusting the SQL `ORDER BY
seq_number` result order directly. `seq_number` is still used in the SQL query as that same deterministic
tiebreaker input's own initial ordering, not removed outright.

**Verified**: re-decoding the real `VAB_PowerFlex_753` AOI (same read-only, scratch-`_temp_dir` method)
after the fix reproduces the user's own reported CORRECT order exactly, all 17 parameters, zero
differences. Also confirmed via `export_routine()`/`export_aoi()`'s underlying mechanism: since all three
(`export_routine()`, `export_aoi()`, `export_program()`) render an AOI's `<Parameters>` via the same
`AOI.to_xml()` off the same decoded `.parameters` list, and none of them do any reordering of their own
(`_resolve_type_closure()` -- shared by all three -- only ever READS `.parameters` to find dependency
names, never mutates or reorders the list, confirmed by inspection), this one fix in the shared decode
layer covers all three export paths identically. The user's own request to "double check
`db_export_aoi()`/`db_export_routine()` for the same bug" is answered by this: yes, they had it too,
same root cause, same fix.

**Caveat, stated plainly since only one real AOI has been checked**: `member_ref` is presumed to be a
per-child, Rockwell-assigned identifier that happens to track intended Parameter/LocalTag order. It has
NOT been independently confirmed whether Rockwell reassigns it if a user later manually drag-and-drop
reorders parameters in Studio's own AOI Definition Editor (as opposed to just tracking original creation
order) -- if it does, this fix is fully correct; if it doesn't, a real AOI that was manually reordered
AFTER initial authoring could theoretically still export in its original creation order rather than its
current display order. No evidence either way has been found yet; revisit if a future real AOI with a
KNOWN, confirmed manual reorder disagrees with this fix's output.

Covered by `test_aoi_builder_orders_parameters_by_member_ref_not_seq_number` and
`test_aoi_builder_orders_local_tags_by_member_ref_too` (`test/test_elements_helpers.py`) -- a synthetic
comps DB reproducing the exact real shape (three RxTagCollection children, all sharing one `seq_number`,
inserted in the REVERSE of their intended `member_ref`-sorted order) -- confirmed both tests FAIL without
the fix (reproducing the same raw-insertion-order scramble) before confirming they pass with it, not just
added as passing tests after the fact.

## `Program.main_routine_name` was always `None` -- `ext[0x12D]` never actually fires; the real field is a footer-based local reference

Found via a real, high-severity downstream report: `MainRoutineName` was completely missing from `db_export_program()`'s output for a real program, and a copy-project Studio 5000 import confirmed the Main Routine designation genuinely got dropped on import. Checked directly (`db_to_controller(acd_path).controller.programs`): every one of 13 real programs in the project returned `main_routine_name = None`.

**The pre-existing code was never actually correct**: `ProgramBuilder.build()` read a raw routine `object_id` from `ext[0x12D]` on the Program's own comps record. Checked directly against a real project: `0x12D` was NEVER present in any Program's parsed extended records at all (only `0x1` ever parses; `RxGeneric`'s own "always leaves the last declared attribute unparsed" quirk, documented elsewhere in this file, meant a second attribute existed but decoded to `0x67` with a value pointing at an unrelated `$hex$`-placeholder object, not `0x12D` and not the main routine). This code had apparently never been verified against real ground truth.

**Investigation dead ends, ruled out with real evidence before finding the real mechanism** (each one exhaustively checked, not just assumed): no raw 4-byte object_id pointer to ANY child routine anywhere in the Program's own 4304-byte comps record (brute-force scanned every offset); nothing in the `RxRoutineCollection` child object's own record (a minimal 78-byte stub, too short to carry anything); no `nameless`-table entry for the Program object; no discriminating byte anywhere in a routine's own extended-record data when diffing the real `Main` routine against 3 confirmed non-main routines in the same program; and a "just guess the routine literally named `Main`" fallback was separately disproven outright -- 6 of the project's 13 real programs have no routine named `Main` at all (their real main routines are named things like `PowerUp`, `Trimmer_LS`, `Main_Motors`).

**The real mechanism, found via a live before/after diff**: the user reassigned one program's (`VAB_Motors`) Main Routine designation in Studio 5000 itself, from `Main` to a different, non-`Main`-named routine (`Mxxx_MCC_Routine`), saved, and supplied both the before and after `.ACD` plus a fresh native "Export Program" of each. Diffing the Program's own raw comps record between the two saves -- an isolated, single-change edit, the same "noop vs. edit" technique already used successfully elsewhere in this file -- found **exactly 2 bytes differed**. Those 2 bytes sit inside `ext[0x01]` (the same blob already used for the `Disabled` flag) at a fixed position relative to the END of the blob: `len(ext01) - 8`, a u16 value. This value is **not the routine's own `object_id`** -- it's a small "local reference number" that separately, ALSO appears at raw offset 16 of the designated routine's own comps record. Resolving `MainRoutineName` means reading this u16 from the Program's footer, then scanning the Program's own `RxRoutineCollection` children for the one whose own record has this same u16 at offset 16.

**Verified, not just theorized**:
- The isolated 2-byte diff decoded to `47563` (old, `Main`) → `7800` (new, `Mxxx_MCC_Routine`) -- and both values were independently found at raw offset 16 of `Main`'s and `Mxxx_MCC_Routine`'s own comps records respectively, in the SAME (unchanged) original project.
- A **second, unrelated real program** (`VAB_MainProgram`, completely different tag/routine content) has its own `Main` routine's local-ref value at the exact same footer position (`len(ext01) - 8`), confirming this isn't a `VAB_Motors`-specific coincidence.
- The offset-16 local-ref field was checked for uniqueness across all 37 routines of the real project's largest program (`VAB_Motors`) -- zero collisions, including the one routine (`M100_Landing_Table`) whose own record otherwise fails to fully parse (see the next section).
- After the fix, **all 13 real programs** resolve to a real, correct routine name -- including all 6 that have no routine literally named `Main` (`StartUp`→`PowerUp`, `LS_Histogram`→`Trimmer_LS`, `Sort_LS_Histogram`→`Sorter_LS`, `Paddle_Fence_Control`→`Paddle_Fence`, `_2_TONGLOADER`→`_00_MAIN`, `Motors`→`Main_Motors`) -- decisive confirmation this is a real, general mechanism, not a name-matching coincidence.

**`FaultRoutineName` was NOT fixed** -- left as the old, likely-equally-broken `ext[0x066]`-as-object_id code, since every real program checked this session had it unset (all zeros), giving no positive before/after example to diff against the way `MainRoutineName` was solved. Documented in the code as an open, unverified gap rather than guessed at by analogy.

Covered by `test_program_builder_resolves_main_routine_name_via_local_ref` (`test/test_database.py`) -- a synthetic comps DB with a Program record (footer-encoded local ref) and two routine children, one matching and one not -- confirmed to fail (`main_routine_name` comes back `None`) without the fix before confirming it passes with it.

## `M100_Landing_Table` under `VAB_Motors` — investigated as a possible dropped-routine bug, RETRACTED

Found as a side effect of the `MainRoutineName` investigation above: a comps row named `M100_Landing_Table`
under `VAB_Motors`' `RxRoutineCollection` has `record_type=0` (its 36 live siblings are `256`/`259`) and is
notably shorter (334 bytes vs. 379 for every sibling); `RxGeneric.from_bytes()` raises on it entirely, and
`RoutineBuilder.build()`'s existing `except Exception: return None` drops it from `program.routines`.

**Initially misdiagnosed as silent data loss of a live routine** — reasoning: a routine with this same name
and real rung content appears in the project's own native Studio 5000 "Export Program" output
(`Motors_Program.L5X`). That reasoning was wrong, corrected directly by the user: `M100_Landing_Table` is
NOT actually in the project (confirmed checking Studio's own Controller Organizer for `VAB_Motors`) — the
real, live routine seen in `Motors_Program.L5X` belongs to the *different*, unrelated `Motors` program (part
of the same `Motors` → `VAB_Motors` reorganization already documented elsewhere in this session), not to
`VAB_Motors`. The `record_type=0` comps row under `VAB_Motors` is stale debris left over from that
migration — a genuinely deleted/orphaned object, not a routine anyone is missing.

**So the exclusion is currently CORRECT, but by accident, not by a deliberate, understood rule** — a parse
exception happens to get caught by the same generic `except Exception: return None` that also (correctly,
deliberately) handles genuinely deleted routines elsewhere, per that function's own docstring. This is worth
remembering as a real, if currently harmless, fragility: nothing today actually confirms `record_type=0` +
unparseable *always* means deleted, only that it did in this one case. Revisit if a future report finds a
genuinely live routine silently missing with this same signature — don't assume this write-up already ruled
that out in general, only this specific instance.

**Methodological note**: this was reasoned from circumstantial evidence (same name, real content, in a
*different* program's export) rather than confirmed directly against the actual program in question — the
same class of mistake this file has warned against repeatedly elsewhere (don't trust an inference over
direct confirmation). Caught quickly here because the user could check Studio directly; flagged in this
file's own history as a reminder rather than quietly deleted, per this project's existing convention of
retracting wrong conclusions in place rather than erasing them.

## Testing gotchas

- `test/conftest.py` chdir's into `test/` for the whole session — needed because many tests
  reference `resources/CuteLogix.ACD` via `"../resources/..."` relative paths. If you add a new
  test file, you can rely on cwd already being `test/`.
- Some AB module DataType names contain `:` (e.g. `CHANNEL_DI_TIMESTAMP:O:0`), which is invalid
  in Windows paths — anything that turns a comp name into a filename/directory (see
  `DumpCompsRecords` in `elements.py`) needs to sanitize it first.
- The full suite (`pytest` from repo root) should show all tests passing with 2 skipped, 0 failed
  (220 passed as of this writing — the exact count grows over time, don't treat a higher number as
  a problem). If you see `FileNotFoundError`s or `PermissionError`s across many unrelated test
  files, first check you're not missing the `conftest.py` chdir behavior or that a previous test
  crashed and left a locked SQLite file/build artifact behind.
