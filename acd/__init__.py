"""Parses Rockwell `.ACD` project files (Studio 5000 / RSLogix 5000) directly
from their proprietary binary format -- no Studio 5000 install, no L5X
export, and no manual/raw parsing of the file needed or possible (it is a
zip-like container of several undocumented binary databases, not plain
text or a documented format).

THE API IS THE `db_*` FUNCTIONS BELOW. Every one takes `acd_path` directly,
does exactly one thing against a real, persistent SQLite file next to the
ACD, and returns -- there is no `load_acd()`/`project` object to hold onto
here, and no separate "save" step. An edit is durable the moment the call
returns, visible to every later `db_*` call against that same `acd_path`
(from this or a different process), until the DB is rebuilt from the real
`.ACD` (a new Studio 5000 save, detected automatically via the source
file's mtime, or an explicit `rebuild=True`).

    import acd
    acd.db_get_project_summary("MyController.ACD")   # names/counts overview
    acd.db_list_routines("MyController.ACD")           # routine names/types, no content
    acd.db_get_routine("MyController.ACD", "MyRoutine") # one routine's actual rungs
    acd.db_new_tag("MyController.ACD", "MyTag", "DINT", value=42)
    acd.db_insert_rung("MyController.ACD", "MyRoutine", 0, "XIC(Always_Off)OTE(MyTag);")
    acd.db_export_routine("MyController.ACD", "MyRoutine", "out.L5X")

WHY THIS SHAPE, NOT AN IN-MEMORY `project` OBJECT -- raw write-back to a
real `.ACD` is blocked (Studio 5000 enforces a `FileInfo.Dat` checksum on
open that this library cannot re-sign without a key it doesn't have), so
the only durable edit path is a partial L5X import via Studio's own native
"Import Routine"/"Import Data Type..." feature (`db_export_routine()`/
`db_export_datatype()` below). That means a typical workflow is several
separate script invocations against the same ACD (make an edit, export,
have the edit imported into Studio, maybe make another edit later) --
an in-memory `project` object from a hypothetical `load_acd()` call would
only live as long as one script's process, so a tag created in one script
would silently vanish by the next script's fresh load, with no error, just
a quietly incomplete export. Every `db_*` function instead reads and
writes a real file, so state survives across process boundaries the same
way editing an offline Studio 5000 project does. See CLAUDE.md's
"Persistent project DB" section for the full mechanism (locking, rebuild
triggers, what's and isn't covered) if you're modifying this library
itself rather than just calling it.

READS -- prefer these over asking for more than you need; each has a
"names/counts only" step and a separate "fetch this ONE thing in full"
step, so a call never returns more than actually asked for:
  - `db_get_project_summary(acd_path)` -- names/counts only (programs,
    tasks, data types, AOIs, modules, tag counts, routine count). Call
    this FIRST, before drilling into anything specific.
  - `db_list_routines(acd_path, program_name=None)` -- name/type/line-count
    for every routine, no rung/line content -- then `db_get_routine()` for
    one routine's actual logic.
  - `db_get_routine(acd_path, routine_name, program_name=None,
    aoi_name=None)` -- one routine's current rungs/comments (or ST lines)
    plus its description. A routine name is only unique WITHIN a program or
    AOI (many projects have a "Main" in several programs, and nearly every
    AOI names its own routine "Logic") -- raises `ValueError` if ambiguous
    and neither `program_name` nor `aoi_name` was given, rather than
    silently picking one. Its `"rung_comments"` is `Dict[int, str]` keyed by
    the INTEGER rung index (same index space as `"rungs"`) -- NOT
    stringified keys; `comments.get(str(i))` silently returns `None` for
    every rung instead of erroring, so use the int index directly. Do NOT
    call this in a loop over every routine to search for a reference --
    see "SCANNING EVERY ROUTINE" below.
  - `db_list_tags(acd_path, program_name=None)` -- name/data_type/
    dimensions/description for tags in one scope, WITHOUT the decoded
    value (can be large on its own for a UDT array tag) -- then
    `db_get_tag_value()` for one tag's value, only when actually needed.
  - `db_get_tag_value(acd_path, tag_name, program_name=None, offset=0,
    limit=50)` -- one tag's value, paginated if it's a large array.
  - `db_list_datatypes(acd_path)` -- name/family/cls/description/
    member_count for EVERY UDT, WITHOUT each one's own member list -- then
    `db_get_datatype(acd_path, name)` for one type's actual members
    (name/data_type/dimension/radix/hidden/target/bit_number/
    external_access/description, in declaration order). Both are
    SQL-direct, no full-project rehydration -- added after a real report
    that inspecting a single UDT's members (e.g. disambiguating several
    near-identical sibling array types) previously forced a full
    `db_to_controller()` decode every time, repeated 10+ times in one real
    session.
  - `db_list_aois(acd_path)` -- name/description/revision/parameter_count
    for EVERY AOI (real, pre-existing ones AND any created via
    `db_new_aoi()`) -- then `db_get_aoi(acd_path, name)` for one AOI's
    actual parameters + local tags. `db_get_aoi()` is cheap/SQL-direct for
    an AOI created via `db_new_aoi()` in this project DB, falling back to a
    full rehydration only for a real, pre-existing project AOI (same
    fast-path/fallback shape as `db_get_routine()`); `db_list_aois()`
    itself always pays the full-rehydration cost, same reasoning as
    `db_list_routines()` (a real AOI isn't tracked in the DB's own tables
    at all, see CLAUDE.md).
  - `db_tag_exists(acd_path, name, program_name=None)` -- pre-creation
    collision check in a given scope (controller by default).
  - `db_get_tag_comment(acd_path, name, path=None, program_name=None)` --
    the comment at `path` on tag `name`; `path=None`/`""` is the tag's own
    whole-tag description, otherwise the FULL tag-qualified address (tag
    name included, e.g. `"HTV_BStatus_Status[0].2"`, not just `"[0].2"`) --
    same convention as `db_set_tag_comment()`. Returns `None` if no comment
    is stored there (normal, not an error). A real project's per-bit
    comments often resolve ambiguity plain rung text/tag names alone don't
    (e.g. a bit commented `"Tray 1 Full"` changing how a branch should be
    read) -- there was previously a write path (`db_set_tag_comment()`) but
    no way to read one back at all.
  - `db_list_tag_comments(acd_path, name, program_name=None)` -- every
    comment on tag `name` at once, as `{path: text}` (whole-tag description
    under key `""`) -- the bulk counterpart to `db_get_tag_comment()`, for
    a routine that references a few dozen distinct bits without needing a
    separate round trip per address.
  - `db_find_tag_references(acd_path, name, regex=False, include_text=True)`
    -- every (program, routine, line_index, text) where a tag/member name is
    referenced, project-wide, in ONE call -- e.g. to check whether a name
    is already used before reusing it, or to find every place a tag/routine
    is referenced before converting/renaming it. `regex=True` matches a
    substring/family of names (e.g. `name=r"Str_Pad"` to match
    `Str_Pad_A`/`Str_Pad_B`/...) instead of the default whole-token match.
    Pass `include_text=False` for a broad exploratory "how many, where"
    pass (e.g. searching a common word across a whole project) -- returns
    (program, routine, line_index) 3-tuples instead of paying for full
    rung/ST text on every hit you're about to discard anyway; fetch the
    actual text afterward (`db_get_routine()`) only for the handful of
    hits you actually want to look at closer.
    **Prefer this over hand-looping `db_get_routine()`/`db_list_routines()`
    over every routine yourself** -- see "SCANNING EVERY ROUTINE" below.
  - `db_io_addresses_by_routine(acd_path)` -- every routine's I/O
    addresses, without hand-rolling a regex over rung text (easy to
    mis-tokenize -- `"Remote_Rack1:3:I.Pt13.Data"`, `"IO024:I.Data[0].13"`).

SCANNING EVERY ROUTINE FOR A REFERENCE -- READ THIS BEFORE LOOPING
`db_get_routine()`. A real report: looping the STATELESS `db_get_routine()`
over all ~180 routines in a project (grepping each result for a tag/routine
name) took 10+ minutes of actual CPU time, not just wall-clock -- each
stateless call opens its own connection AND (see below) used to pay for a
full project rehydration just to answer "what's in this one routine."
Two separate fixes, worth understanding both:
  1. **You almost never need to hand-loop at all.** `db_find_tag_references(
     acd_path, name, regex=True)` already answers "every place X is
     referenced, project-wide" in ONE call -- including a routine CALLING
     another routine by name (`JSR(RoutineName,...)` is just text a plain
     substring/regex search matches like anything else). This is the
     right tool for "what needs rewiring before I convert/rename this,"
     not a hand-rolled loop.
  2. **If you genuinely need per-routine access in a loop anyway** (e.g.
     editing many routines in sequence, not just searching), open ONE
     connection with `open_project_db(acd_path)` and call the instance
     methods (`db.list_routines()`, `db.get_routine(name, program_name=p)`)
     on it, closing once at the end -- never loop the stateless `db_*`
     wrappers, each of which opens/verifies/closes its own connection.
     `get_routine()` is ALSO now backed directly by SQL
     (`proj_routines`/`proj_rungs`/`proj_st_lines`) for the common case (a
     Program's routine, or an AOI created via `db_new_aoi()`), not a full
     `to_controller()` rehydration, specifically because of this report --
     a full-project routine-by-routine loop with a single connection is a
     cheap SQL query per routine now for that case, not a full project
     decode per routine (the original report's slowdown was BOTH factors
     compounding: opening a new connection AND a full rehydration, on every
     single iteration). It transparently falls back to the slower, old
     behavior only for a REAL, pre-existing AOI's own routine (not tracked
     in the fast path's tables at all) -- rare enough as a single lookup
     that this is a fine trade. `list_routines()` itself still always does
     one full `to_controller()` (unavoidable for now: it's the only way to
     see a real AOI's routines at all, and silently dropping those from a
     "list every routine" call would be worse than the one-time cost) --
     but that's ONE call, not the N-times loop that actually caused the
     reported slowdown.

EDITS -- durable the moment the call returns (see above), each raising
`KeyError` for an unknown name/scope:
  - `db_new_tag(acd_path, name, data_type, program_name=None,
    dimensions=None, description=None, value=None,
    external_access="Read/Write")`
  - `db_edit_tag(acd_path, name, program_name=None, description=None,
    value=None)` -- only the fields actually passed are changed.
  - `db_set_tag_comment(acd_path, name, path, text, program_name=None)`
    -- `path=""` is the tag's own whole-tag description; otherwise `path`
    is the FULL tag-qualified address, tag name included (e.g.
    `"MyTag.Member[4].5"`, not `"Member[4].5"`). `text=""` clears the
    comment at that path (it's filtered out at export time, never raises
    or renders an empty comment).
  - `db_set_tag_element_value(acd_path, tag_name, path, value,
    program_name=None)` -- set ONE leaf value inside a tag's (possibly
    array-of-struct) value, instead of hand-building the whole nested
    value just to change one field of one element. `path`:
    an optional leading `[N]` (required iff `tag_name` is a declared 1-D
    array; not yet supported: multi-dimensional or member-level array
    indices) followed by zero or more `.MemberName` segments, e.g.
    `"[3].PRE"` (`StartFaultTimer[3].PRE` for element 3 of an `[11]`-motor
    array), `"PRE"` (a scalar struct tag), `"[3]"` (a plain primitive
    array element). If the tag has no stored value yet, the WHOLE value is
    zero-filled first (Studio-consistent, via the same mechanism CLAUDE.md's
    "Mutating a UDT with live tag instances" section documents), then the
    one leaf is set -- no separate "seed a default first" step needed. Same
    zero-fill applies to any missing intermediate member found while
    navigating an existing value. Raises `KeyError` for an unknown tag or
    member, `ValueError` for an out-of-range/mismatched index or an
    unsupported `path` shape.
  - `db_new_datatype(acd_path, name, description=None)` -- create a new,
    empty UDT. Use `db_new_member()` afterward to populate it, the same
    way you already would for an existing UDT.
  - `db_new_member(acd_path, data_type_name, name, member_data_type,
    dimension=0, radix=None, description=None, index=None)` -- add a
    member to an EXISTING UDT, at `index` (default: appended).
    `member_data_type="BIT"` allocates a real bit position the way Studio
    5000 itself does (reusing a free bit in an existing hidden backing
    member, or creating a new one) -- previously committed with no error
    but with no `target`/`bit_number` at all, only failing a real Studio
    "Import Data Type..." on `Target` several steps later.
  - `db_new_aoi(acd_path, name, description=None)` -- create a new, empty
    Add-On Instruction. Use `db_new_aoi_parameter()` to add
    Input/Output/InOut parameters, `db_new_aoi_local_tag()` for private
    scratch storage, and `db_new_routine(..., aoi_name=name)` for its logic
    routine. **v1 scope limit**: only newly-created AOIs are addressable
    through this table at all -- a real project's own pre-existing AOIs are
    never edited through it (though `db_export_aoi()` can still export one
    of THOSE directly, unmodified or after you mutate it via
    `db_to_controller()`). `ExecutePrescan`/`ExecutePostscan`/
    `ExecuteEnableInFalse` and read-back beyond `db_get_project_summary()`'s
    own AOI name list are still explicitly out of scope for this v1 pass.
  - `db_new_aoi_parameter(acd_path, aoi_name, name, data_type,
    usage="Input", dimension=None, description=None, index=None,
    required=None, visible=None, external_access=None)` -- add a public
    parameter to an AOI created via `db_new_aoi()` (RAISES if `aoi_name` is
    a real project AOI this table doesn't know about -- same v1 scope limit
    as `db_new_aoi()` itself). `usage` is `"Input"`, `"Output"`, or
    `"InOut"`. `required`/`visible`/`external_access` default to
    `"true"`/`"true"`/usage-derived when omitted (`None`) -- pass explicit
    strings to override, e.g. to build the real `EnableIn`/`EnableOut`
    system-defined parameter pair every Studio-authored AOI carries
    (`Required="false"`, `Visible="false"`, `ExternalAccess="Read Only"`;
    `new_aoi_enable_parameters()` at the in-memory `acd.l5x.elements` layer
    builds this exact pair ready-made if you're constructing `Parameter`
    objects directly instead of going through `db_*`). **`dimension` is
    only valid for `usage="InOut"`** -- RAISES immediately otherwise
    (confirmed via a real Studio 5000 import rejection: `"Invalid array.
    Input or output parameter must be of supported elementary data type
    with no dimensions."`). **`data_type` for `usage="Input"`/`"Output"`
    must be an elementary/atomic type** -- `BOOL`/`SINT`/`INT`/`DINT`/
    `LINT`/unsigned variants/`REAL`/`LREAL` -- RAISES immediately for
    `STRING`, a UDT, or another AOI with that usage (confirmed via a real
    Studio 5000 import rejection: `"Error creating 'Parameter' (Input or
    output parameter must be of supported elementary data type.)"`). An
    `Input`/`Output` parameter is passed by value and may only be a single
    scalar elementary value; only `InOut` (passed by reference) may be an
    array OR a structured type (`STRING`/UDT/AOI).
  - `db_edit_aoi_parameter(acd_path, aoi_name, name, data_type=None, usage=None,
    dimension=None, description=None, required=None, visible=None,
    external_access=None)` -- update an existing AOI parameter's fields in
    place; only the fields actually passed (non-`None`) are changed. Re-runs
    the same validation `db_new_aoi_parameter()` itself does against the
    MERGED field set, so an edit can't sneak a parameter into a shape
    Studio 5000 would reject that creating one directly never could.
    CAVEAT: `dimension=None` means "leave unchanged," not "clear back to
    scalar" -- delete and recreate the parameter instead if you need that.
  - `db_delete_aoi_parameter(acd_path, aoi_name, name)` -- remove a
    parameter from an AOI created via `db_new_aoi()`. Same real-`.ACD`
    caveat as `db_delete_tag()` below (bookkeeping cleanup only, no
    "un-import" of an already-accepted Studio import) -- pairs with
    `db_edit_aoi_parameter()` above for "added with the wrong shape, no fix
    short of Studio's own AOI editor" (delete + recreate instead of leaving
    permanent clutter).
  - `db_new_aoi_local_tag(acd_path, aoi_name, name, data_type,
    dimension=None, description=None, index=None)` -- add a private/scratch
    LocalTag (internal AOI state that shouldn't be a public parameter) to
    an AOI created via `db_new_aoi()` (same v1 scope/RAISES rule as
    `db_new_aoi_parameter()`). No `Usage`/`Required`/`Visible` concept --
    unlike a `Parameter`, a LocalTag is never a public pin.
  - `db_new_routine(acd_path, routine_name, routine_type,
    program_name=None, description=None, aoi_name=None)` -- create a new,
    empty routine (`routine_type` `"RLL"` or `"ST"`) in an EXISTING program
    OR an AOI created via `db_new_aoi()`. EXACTLY ONE of
    `program_name`/`aoi_name` is required (unlike `db_new_tag()`, there is
    no controller-scope routine concept to default to for either). Use
    `db_insert_rung()`/`db_insert_st_line()` afterward to populate it.
    **When `aoi_name` is given, `routine_name` MUST be one of `"Logic"`/
    `"Prescan"`/`"Postscan"`/`"EnableInFalse"`** -- unlike a Program's
    routine, an AOI's own routine name is a fixed, Rockwell-reserved set
    (confirmed via a real Studio 5000 import rejection of a differently-
    named routine: `"Invalid name for Add-On Instruction routine."`).
    RAISES immediately if you pass anything else, rather than letting a
    creatively-named routine (e.g. named after the AOI itself, to dodge a
    collision with every other AOI's own `"Logic"` routine) fail much
    later at import time.
  - EVERY routine-content function below EXCEPT `db_export_routine`
    (`db_insert_rung`, `db_delete_rung`, `db_replace_rung_safe`,
    `db_set_rung_comment`, `db_insert_st_line`, `db_delete_st_line`,
    `db_replace_st_line_safe`, `db_delete_routine`, `db_get_routine`) also
    accepts `aoi_name=` alongside `program_name=` (exactly one of the two,
    or neither to search every program AND AOI and raise if ambiguous) --
    this is how you address an AOI's `"Logic"` routine, since nearly every
    AOI in a real project uses that exact name and `program_name=` doesn't
    resolve AOI scope at all. Was a real, confirmed gap: creating an
    AOI-scoped routine via `aoi_name=` worked, but nothing downstream could
    address it back, forcing a workaround of renaming the routine away from
    Rockwell's own convention -- fixed by adding `aoi_name=` everywhere
    `program_name=` was already accepted for a routine, not just at
    creation. `db_export_routine` accepts `aoi_name=` too, but ALWAYS
    raises if given -- see its own bullet below for why (there's no
    "Import Routine" mechanism for an AOI-owned routine at all; use
    `db_export_aoi()` instead).
  - `db_insert_rung(acd_path, routine_name, index, text, comment=None,
    program_name=None, aoi_name=None)` / `db_delete_rung(acd_path,
    routine_name, index, program_name=None, aoi_name=None)` -- RLL ONLY,
    raises `ValueError` if the routine's own type isn't `"RLL"` (see
    `db_insert_st_line`/`db_delete_st_line` below for ST). `text` is also
    checked for unbalanced brackets and a one-member `"[...]"` branch group
    (see `_validate_rll_rung_syntax()`) before being inserted; raises
    `ValueError` instead of silently accepting rung text a real Studio 5000
    import would reject.
  - `db_replace_rung_safe(acd_path, routine_name, index, expected_old,
    new_text, program_name=None, aoi_name=None)` -- RLL ONLY (same type
    guard as above; use `db_replace_st_line_safe` for ST). Edit an EXISTING
    rung's text, but only if it still matches `expected_old` (raises with a
    readable diff otherwise) -- guards against clobbering a rung someone
    hand-edited in Studio since your last read. This guard is about
    editing the WRONG rung, not rung grammar -- `new_text` gets the same
    RLL syntax check as `db_insert_rung()` above, run separately, after
    the match check.
  - `db_set_rung_comment(acd_path, routine_name, index, comment,
    program_name=None, aoi_name=None)` -- set or clear (`comment=None`) a
    rung's comment WITHOUT touching its text; use this instead of
    delete_rung+insert_rung just to rename a comment.
  - `db_insert_st_line(acd_path, routine_name, index, text,
    program_name=None, aoi_name=None)` / `db_delete_st_line(acd_path,
    routine_name, index, program_name=None, aoi_name=None)` /
    `db_replace_st_line_safe(acd_path, routine_name, index, expected_old,
    new_text, program_name=None, aoi_name=None)` -- ST ONLY (raises
    `ValueError` if the routine's own type isn't `"ST"`), same shapes as
    the RLL `*_rung*` functions above but for an ST routine's `"st_lines"`.
    Added because the RLL functions above used to accept being called on an
    ST routine with NO error -- silently writing into `"rungs"` (which
    `export_routine()` never reads for an ST routine) while the routine's
    real `"st_lines"` (what actually gets exported/imported) stayed
    untouched. That looked like a successful, committed edit with nothing
    marking it as wrong -- caught only because a downstream agent happened
    to diff `"st_lines"` before/after on a scratch copy before ever
    exporting for real. `db_insert_st_line`/etc. give ST routines their own
    real editing primitives instead of a silently-wrong RLL one; NO RLL
    syntax check applies to ST lines (ST syntax validation doesn't exist
    yet).
  - `db_delete_tag(acd_path, name, program_name=None)` /
    `db_delete_routine(acd_path, routine_name, program_name=None,
    aoi_name=None)` / `db_delete_member(acd_path, data_type_name,
    member_name)` -- remove dead code (e.g. a tag/routine/member left
    behind after a redesign) from this project's DB bookkeeping. Does NOT
    delete anything in the real `.ACD`/Studio project -- Studio's native
    Import Routine/Import Data Type mechanism has no delete semantics (it
    can only add/update entities present in the partial L5X, never remove
    ones that aren't mentioned), so removing something from the real
    project still needs a manual Studio action regardless. This only stops
    an abandoned entry from cluttering `db_list_tags()`/`db_list_routines()`/
    `db_get_project_summary()` forever with nothing marking it as dead.

MULTI-STEP EDITS THAT MUST SUCCEED OR FAIL TOGETHER -- each `db_*` call
above commits the instant it returns; a script doing several of them in a
row (e.g. add a UDT member, then create 3 tags, then edit 2 rungs) that
raises partway through leaves everything up to that point durably sitting
in the DB, with nothing marking it as an incomplete attempt -- unlike the
old in-memory workflow, where a crashed script left zero durable side
effects for free (nothing was ever written until a final export call
succeeded). Use `db_transaction(acd_path)` for anything that needs
all-or-nothing semantics:

    with db_transaction(acd_path) as db:
        db.new_member(dt_name, "Foo", "DINT")
        db.new_tag("Tag1", "DINT")
        db.insert_rung(routine_name, 0, "...")
    # all three committed together, or (if anything raised) none did

Inside the block, call methods on the yielded `db` (`db.new_tag(...)`), NOT
the top-level `db_*` functions (`db_new_tag(...)`) -- each of those opens
its own separate connection and would deadlock trying to acquire the same
project lock this transaction is already holding.

EXPORTING TO REAL STUDIO 5000 -- the only durable write path (see above).
`validate` defaults to `True` on ALL THREE functions below -- every check
they run is cheap relative to a full edit -> export -> Studio-import round
trip, and the failure modes they catch (a struct-typed name that doesn't
resolve, silently rendered as a bare zero instead of a real nested
structure; for `db_export_routine`/`db_export_program`, malformed RLL rung
syntax and an out-of-bounds literal array index -- see `db_insert_rung`
above) are silent/late, not loud errors up front, which is the wrong kind
of thing to leave opt-in. Pass `validate=False` explicitly if you're
confident it's unnecessary and want to skip the pass:
  - `db_export_routine(acd_path, routine_name, output_path,
    program_name=None, aoi_name=None, owner=None, validate=True)` -- a
    standalone partial L5X for Studio's "Import Routine" feature, covering
    both rung edits and any tag this routine's logic references. Validates
    every struct-typed name reachable from a referenced tag's own DataType
    tree, every rung's syntax via `_validate_rll_rung_syntax()` (RLL only),
    AND every literal-indexed array reference (`MyArray[5]`) against the
    referenced tag's own declared `Dimensions` -- a variable/expression
    index (`MyArray[i]`) is silently skipped, not flagged; found after two
    real reports of a sized-too-small array only a human happened to catch.
    `aoi_name` is accepted for signature
    symmetry with the routine-content functions above but ALWAYS raises --
    Studio has no "Import Routine" mechanism for a routine living inside an
    AddOnInstructionDefinition; use `db_export_aoi()` for an AOI's routine
    instead, which exports the whole AOI (including this routine's
    content).
  - `db_export_datatype(acd_path, data_type_name, output_path, owner=None,
    validate=True)` -- a standalone partial L5X for Studio's "Import Data
    Type..." feature, for creating/modifying a UDT (e.g. inserting a
    member via `db_new_member()` first). Validates every struct-typed
    member of `data_type_name` itself.
  - `db_export_aoi(acd_path, aoi_name, output_path, owner=None,
    validate=True)` -- a standalone partial L5X for Studio's "Import Add-On
    Instruction..." feature, for creating/modifying an AOI (`db_new_aoi()`/
    `db_new_aoi_parameter()`/`db_new_aoi_local_tag()`/
    `db_new_routine(..., aoi_name=...)` first). Validates every struct-typed
    parameter AND local tag of `aoi_name` itself, and (unconditionally, not
    gated by `validate=`) that every one of its routine names is one of
    Rockwell's own reserved set (`"Logic"`/`"Prescan"`/`"Postscan"`/
    `"EnableInFalse"` -- `db_new_routine(..., aoi_name=...)` already
    enforces this at creation time, this is defense-in-depth for a routine
    that reached `.routines` some other way). **CAUTION, more so than
    the other two**: this wrapper's shape has never been tried against a
    real Studio 5000 import at all (built by symmetry with the other two
    ALREADY-verified wrappers, not from its own real-import evidence) --
    test on a copy of your project first.
  - `db_export_program(acd_path, program_name, output_path, owner=None,
    validate=True)` -- a standalone partial L5X for Studio's distinct
    "Import Program..." feature (NOT "Import Routine"), exporting an ENTIRE
    program (every one of its own routines and program-scope tags, in
    full) in one file -- for when you've edited/added several routines in
    the same program and don't want a separate Import Routine per one.
    Unlike `db_export_routine()`, nothing is filtered by "referenced" for
    the program's own routines/tags -- they're all part of the Target.
    Wrapper shape WAS calibrated against a real Studio 5000 "Export
    Program" output (not guessed), but has the same **CAUTION** as
    `db_export_aoi()` -- never tried against a real Studio 5000 import
    itself yet, test on a copy first. KNOWN GAP: a Program that itself
    contains nested child/folder programs (`<ChildPrograms>` in a real
    export) isn't detected or represented -- see
    `acd.api.export_program()`'s own docstring for why a first hypothesis
    about how to detect this was checked against real data and disproven,
    not guessed further.

COMPARING TWO PROJECTS/SAVES/ROUTINES -- READ THIS BEFORE WRITING YOUR OWN
COMPARISON CODE. Do NOT fetch two routines via `db_get_routine()` and
zip/print their `"rungs"` lists side by side by index: a single rung
inserted, deleted, or reordered anywhere in one routine shifts every later
rung's index, so an otherwise identical tail will look completely
different in a naive index-paired comparison even though nothing there
actually changed. Use one of these instead -- all three already solve this
by aligning content with `difflib`, not by index:
  - `db_diff_project(acd_path_a, acd_path_b)` -- GENERIC "what changed
    between these two ACDs?" (routines, tags, data types, modules, AOIs).
    Use this by default for a broad comparison.
  - `db_diff_routine(acd_path_a, routine_name_a, acd_path_b,
    routine_name_b, program_name_a=None, program_name_b=None)` -- already
    know which two routines you want compared (by name, possibly the same
    project before/after an edit) and just want that one routine's diff,
    without a whole-project scan.
  - `db_diff_io_addresses(acd_path_a, acd_path_b)` -- ONLY when the
    request is specifically about I/O address wiring; it reports nothing
    about tag values or rung logic changes, so it is the wrong default for
    a broad comparison.

Two pure, no-DB-involved utilities, usable directly on text/lists you
already have in hand (e.g. from `db_get_routine()`'s own `"rungs"`):
  - `find_io_addresses(text)` -- every I/O-style address in one line of
    rung/ST text.
  - `diff_lines(old, new)` -- the same `difflib`-based alignment the diff
    functions above use internally, for when you already have two plain
    line lists rather than two routines.

ADVANCED / BATCH USE -- `open_project_db(acd_path)` returns a `ProjectDB`
with the same methods as the `db_*` functions above (minus the `db_`
prefix and the repeated `acd_path` argument) for a script doing MANY edits
in one session that wants to hold the DB open (and its lock held) across
all of them, rather than pay one open/close cycle per edit. Prefer the
`db_*` functions above for a single edit. `ProjectDB.to_controller()` (or
the `db_to_controller()` function) returns a normal `RSLogix5000Content`
object graph reflecting the DB's current state, for anything not covered
by a `db_*`/`ProjectDB` method -- see `RSLogix5000Content`/`Controller`/
`Program`/`Routine`/`Tag` in `acd.l5x.elements` for that object shape.
The lower-level, single-process, no-persistence functions this whole
module is built on (`load_acd`, `export_routine`, `get_routine`, ...) are
still there in `acd.api`/`acd.l5x.elements` if you have a specific reason
to need them, but are deliberately not re-exported here -- every `db_*`
call above already does the load/rehydrate/close cycle for you.
"""

from acd.api import diff_lines, find_io_addresses  # noqa: F401
from acd.l5x.project_db import (  # noqa: F401
    open_project_db,
    ProjectDB,
    db_transaction,
    db_new_tag,
    db_edit_tag,
    db_set_tag_comment,
    db_get_tag_comment,
    db_list_tag_comments,
    db_set_tag_element_value,
    db_new_datatype,
    db_new_member,
    db_new_aoi,
    db_new_aoi_parameter,
    db_edit_aoi_parameter,
    db_delete_aoi_parameter,
    db_new_aoi_local_tag,
    db_new_routine,
    db_insert_rung,
    db_delete_rung,
    db_replace_rung_safe,
    db_set_rung_comment,
    db_insert_st_line,
    db_delete_st_line,
    db_replace_st_line_safe,
    db_delete_tag,
    db_delete_routine,
    db_delete_member,
    db_export_routine,
    db_export_datatype,
    db_export_aoi,
    db_export_program,
    db_list_tags,
    db_list_routines,
    db_get_datatype,
    db_list_datatypes,
    db_get_aoi,
    db_list_aois,
    db_tag_exists,
    db_get_project_summary,
    db_to_controller,
    db_get_routine,
    db_get_tag_value,
    db_find_tag_references,
    db_io_addresses_by_routine,
    db_diff_project,
    db_diff_routine,
    db_diff_io_addresses,
)
