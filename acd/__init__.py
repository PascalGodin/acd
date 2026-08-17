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
  - `db_get_routine(acd_path, routine_name, program_name=None)` -- one
    routine's current rungs/comments (or ST lines) plus its description.
    A routine name is only unique WITHIN a program (many projects have a
    "Main" in several programs) -- raises `ValueError` if ambiguous and no
    `program_name` was given, rather than silently picking one. Its
    `"rung_comments"` is `Dict[int, str]` keyed by the INTEGER rung index
    (same index space as `"rungs"`) -- NOT stringified keys;
    `comments.get(str(i))` silently returns `None` for every rung instead
    of erroring, so use the int index directly.
  - `db_list_tags(acd_path, program_name=None)` -- name/data_type/
    dimensions/description for tags in one scope, WITHOUT the decoded
    value (can be large on its own for a UDT array tag) -- then
    `db_get_tag_value()` for one tag's value, only when actually needed.
  - `db_get_tag_value(acd_path, tag_name, program_name=None, offset=0,
    limit=50)` -- one tag's value, paginated if it's a large array.
  - `db_tag_exists(acd_path, name, program_name=None)` -- pre-creation
    collision check in a given scope (controller by default).
  - `db_find_tag_references(acd_path, name, regex=False)` -- every
    (program, routine, line_index, text) where a tag/member name is
    referenced, project-wide -- e.g. to check whether a name is already
    used before reusing it.
  - `db_io_addresses_by_routine(acd_path)` -- every routine's I/O
    addresses, without hand-rolling a regex over rung text (easy to
    mis-tokenize -- `"Remote_Rack1:3:I.Pt13.Data"`, `"IO024:I.Data[0].13"`).

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
  - `db_new_member(acd_path, data_type_name, name, member_data_type,
    dimension=0, radix=None, description=None, index=None)` -- add a
    member to an EXISTING UDT, at `index` (default: appended).
    `member_data_type="BIT"` allocates a real bit position the way Studio
    5000 itself does (reusing a free bit in an existing hidden backing
    member, or creating a new one) -- previously committed with no error
    but with no `target`/`bit_number` at all, only failing a real Studio
    "Import Data Type..." on `Target` several steps later.
  - `db_new_routine(acd_path, routine_name, routine_type, program_name,
    description=None)` -- create a new, empty routine (`routine_type`
    `"RLL"` or `"ST"`) in an EXISTING program. Unlike `db_new_tag()`,
    `program_name` is REQUIRED (no controller-scope routine concept to
    default to). Use `db_insert_rung()`/`db_insert_st_line()` afterward to
    populate it.
  - `db_insert_rung(acd_path, routine_name, index, text, comment=None,
    program_name=None)` / `db_delete_rung(acd_path, routine_name, index,
    program_name=None)` -- RLL ONLY, raises `ValueError` if the routine's
    own type isn't `"RLL"` (see `db_insert_st_line`/`db_delete_st_line`
    below for ST). `text` is also checked for unbalanced brackets and a
    one-member `"[...]"` branch group (see `_validate_rll_rung_syntax()`)
    before being inserted; raises `ValueError` instead of silently
    accepting rung text a real Studio 5000 import would reject.
  - `db_replace_rung_safe(acd_path, routine_name, index, expected_old,
    new_text, program_name=None)` -- RLL ONLY (same type guard as above;
    use `db_replace_st_line_safe` for ST). Edit an EXISTING rung's text,
    but only if it still matches `expected_old` (raises with a readable
    diff otherwise) -- guards against clobbering a rung someone
    hand-edited in Studio since your last read. This guard is about
    editing the WRONG rung, not rung grammar -- `new_text` gets the same
    RLL syntax check as `db_insert_rung()` above, run separately, after
    the match check.
  - `db_set_rung_comment(acd_path, routine_name, index, comment,
    program_name=None)` -- set or clear (`comment=None`) a rung's comment
    WITHOUT touching its text; use this instead of delete_rung+insert_rung
    just to rename a comment.
  - `db_insert_st_line(acd_path, routine_name, index, text,
    program_name=None)` / `db_delete_st_line(acd_path, routine_name,
    index, program_name=None)` / `db_replace_st_line_safe(acd_path,
    routine_name, index, expected_old, new_text, program_name=None)` --
    ST ONLY (raises `ValueError` if the routine's own type isn't `"ST"`),
    same shapes as the RLL `*_rung*` functions above but for an ST
    routine's `"st_lines"`. Added because the RLL functions above used to
    accept being called on an ST routine with NO error -- silently writing
    into `"rungs"` (which `export_routine()` never reads for an ST
    routine) while the routine's real `"st_lines"` (what actually gets
    exported/imported) stayed untouched. That looked like a successful,
    committed edit with nothing marking it as wrong -- caught only because
    a downstream agent happened to diff `"st_lines"` before/after on a
    scratch copy before ever exporting for real. `db_insert_st_line`/etc.
    give ST routines their own real editing primitives instead of a
    silently-wrong RLL one; NO RLL syntax check applies to ST lines (ST
    syntax validation doesn't exist yet).
  - `db_delete_tag(acd_path, name, program_name=None)` /
    `db_delete_routine(acd_path, routine_name, program_name=None)` /
    `db_delete_member(acd_path, data_type_name, member_name)` -- remove
    dead code (e.g. a tag/routine/member left behind after a redesign)
    from this project's DB bookkeeping. Does NOT delete anything in the
    real `.ACD`/Studio project -- Studio's native Import Routine/Import
    Data Type mechanism has no delete semantics (it can only add/update
    entities present in the partial L5X, never remove ones that aren't
    mentioned), so removing something from the real project still needs a
    manual Studio action regardless. This only stops an abandoned entry
    from cluttering `db_list_tags()`/`db_list_routines()`/
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
`validate` defaults to `True` on BOTH functions below -- both checks it
runs are cheap relative to a full edit -> export -> Studio-import round
trip, and the failure modes they catch (a struct-typed name that doesn't
resolve, silently rendered as a bare zero instead of a real nested
structure; for `db_export_routine`, malformed RLL rung syntax -- see
`db_insert_rung` above) are silent/late, not loud errors up front, which
is the wrong kind of thing to leave opt-in. Pass `validate=False`
explicitly if you're confident it's unnecessary and want to skip the pass:
  - `db_export_routine(acd_path, routine_name, output_path,
    program_name=None, owner=None, validate=True)` -- a standalone
    partial L5X for Studio's "Import Routine" feature, covering both rung
    edits and any tag this routine's logic references. Validates every
    struct-typed name reachable from a referenced tag's own DataType tree,
    AND (for an RLL routine) every rung's syntax via
    `_validate_rll_rung_syntax()`.
  - `db_export_datatype(acd_path, data_type_name, output_path, owner=None,
    validate=True)` -- a standalone partial L5X for Studio's "Import Data
    Type..." feature, for creating/modifying a UDT (e.g. inserting a
    member via `db_new_member()` first). Validates every struct-typed
    member of `data_type_name` itself.

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
    db_new_member,
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
    db_list_tags,
    db_list_routines,
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
