"""Parses Rockwell `.ACD` project files (Studio 5000 / RSLogix 5000) directly
from their proprietary binary format -- no Studio 5000 install, no L5X
export, and no manual/raw parsing of the file needed or possible (it is a
zip-like container of several undocumented binary databases, not plain
text or a documented format).

    from acd import load_acd
    project = load_acd("MyController.ACD")
    project.controller.name                    # controller name
    project.controller.tags                    # controller-scope tags
    for program in project.controller.programs:
        for routine in program.routines:        # NOT project.routines
            routine.rungs                        # list[str], plain ladder text

COMPARING TWO PROJECTS/SAVES/ROUTINES -- READ THIS BEFORE WRITING YOUR OWN
COMPARISON CODE. Do NOT manually zip/print two routines' `.rungs` (or
`._st_lines`) side by side by index and eyeball the difference: a single
rung inserted, deleted, or reordered anywhere in one routine shifts every
later rung's index, so an otherwise byte-identical tail will look completely
different in a naive index-paired printout even though nothing there
actually changed. Use one of these instead -- all three already solve this
by aligning content with `difflib`, not by index:
  - `diff_project(project_a, project_b)` -- GENERIC "what changed between
    these two ACDs?" (routines, tags, data types, modules, AOIs). Use this
    by default for a broad comparison.
  - `diff_routine(routine_a, routine_b)` -- already have two specific
    Routine objects (e.g. "the same" routine fetched from two projects) and
    just want that one routine's diff, without a whole-project scan.
  - `diff_lines(old, new)` -- already have two plain line lists (e.g.
    verifying your OWN in-memory edit to `.rungs`/`._st_lines` before
    calling `export_routine()`, not comparing two loaded Routine objects) --
    the same alignment primitive `diff_routine()` uses internally, exposed
    directly so you don't need two full Routine objects just to check what
    an edit actually did.
  - `diff_io_addresses(project_a, project_b)` -- ONLY when the request is
    specifically about I/O address wiring (e.g. "what I/O addresses
    changed?"); it reports nothing about tag values or rung logic changes,
    so it is the wrong default for a broad comparison.
All three also avoid a hand-rolled regex over rung text for I/O addresses
(`"Remote_GraderConsole:3:I.Pt13.Data"`, `"IO024:I.Data[0].13"` are easy to
mis-tokenize) -- see `find_io_addresses()`/`io_addresses_by_routine()` if you
need the raw address list rather than a diff.

See `acd.api` for the full public API (`load_acd`, `save_acd`,
`patch_rungs`, `export_routine`, `ConvertAcdToL5x`, ...) -- each function
and the returned object's own classes (`RSLogix5000Content`, `Controller`,
`Program`, `Routine`, `Tag`, ... in `acd.l5x.elements`) have docstrings
with concrete attribute paths; check those with `help()` before guessing
at the object shape. See this package's README.md for full usage examples,
and CLAUDE.md for internals/gotchas if modifying this library itself.

Writing changes back to a real `.ACD` that Studio 5000 will open: prefer
`export_routine()` (a partial L5X imported via Studio's own "Import
Routine" feature) for rungs/tags, or `export_datatype()` (imported via
Studio's own "Import Data Type..." feature) for creating/modifying a UDT
-- e.g. inserting a new member -- over `save_acd()`/`patch_rungs()`.
Studio enforces a `FileInfo.Dat` checksum on open that this library
cannot re-sign without a key it does not have, so `save_acd()` only
produces an openable file for a completely unmodified round-trip.

LAZY / SUMMARY-FIRST LOOKUPS -- for a caller operating under a context
budget (e.g. an MCP tool response), prefer these over walking
`project.controller...` yourself and returning everything you find. Each
has a "list names/counts only" step and a separate "fetch this ONE thing
in full" step, so a caller never gets more than it actually asked for:
  - `get_project_summary(project)` -- names/counts only (programs, tasks, data
    types, AOIs, modules, tag counts, routine count) for an initial "what's
    in this project" response, before drilling into anything specific.
  - `list_routines(project, program_name=None)` -- name/type/line-count for
    every routine, no rung/line content -- then `get_routine()` for one
    routine's actual logic.
  - `list_tags(project, program_name=None)` -- name/data_type/dimensions/
    description for tags in one scope, WITHOUT the decoded value (which can
    be large on its own for a UDT array tag) -- then `get_tag_value()` for
    one tag's value, only when actually needed.
  - `get_tag_value(project, tag_name, program_name=None, offset=0, limit=50)`
    -- one tag's value, paginated if it's a large array (returns
    total_elements/offset/returned alongside value) rather than dumping a
    multi-hundred-element array in one call.

EDITING RUNGS -- avoid hand-rolling these, they're easy to get subtly
wrong (see README.md for full examples):
  - `get_routine(project, routine_name, program_name=None)` -- instead of
    the nested program -> routine double-lookup. A routine name is only
    unique WITHIN a program (many projects have a "Main" in several
    programs), so this raises if the name is ambiguous and no
    `program_name` was given, rather than silently picking one.
  - `routine.insert_rung(index, text, comment=None)` /
    `routine.delete_rung(index)` -- inserting/deleting a rung means
    shifting every `_rung_comments` key at/after that index too; doing
    this by hand is a real, easy-to-mis-index footgun.
  - `replace_rung_safe(routine, index, expected_old, new_text)` -- edit an
    EXISTING rung's text, but only if it still matches what you last read
    (raises with a readable diff otherwise) -- guards against clobbering a
    rung someone hand-edited in Studio since your last read.
  - `find_tag_references(project, name)` -- every (program, routine,
    rung_index, text) where a tag/member name is referenced, project-wide
    -- e.g. to check whether a name is already used before reusing it.
  - `tag_exists(project, name, program_name=None)` -- pre-creation
    collision check in a given scope (controller by default).
  - `new_tag(name, data_type, dimensions=None, description=None, value=None)`
    -- construct a new controller-/program-scope `Tag` to append to
    `.tags`, instead of hand-rolling `Tag(_name=..., name=..., tag_type=
    "Base", ...)` positional construction from scratch every time.
  - `export_routine(..., validate=True)` -- before writing the file, verify
    every struct-typed name reachable from a referenced tag's own DataType
    tree actually resolves, raising with the specific tag/member/type
    instead of silently rendering a bare zero where a nested structure
    belongs (the class of bug that otherwise only surfaces as a Studio 5000
    import rejection, or worse, a silent wrong value). Off by default.

`load_acd()` is quiet by default (WARNING and above -- real data-quality
signals -- are always kept regardless); pass `verbose=True` to also see the
~15-20 lines of INFO/DEBUG progress output every load produces.
"""

from acd.api import (  # noqa: F401
    load_acd,
    save_acd,
    patch_rungs,
    export_routine,
    export_datatype,
    diff_lines,
    find_io_addresses,
    find_tag_references,
    get_routine,
    get_tag_value,
    io_addresses_by_routine,
    list_routines,
    list_tags,
    get_project_summary,
    diff_io_addresses,
    diff_project,
    diff_routine,
    replace_rung_safe,
    tag_exists,
)
from acd.l5x.elements import new_member, new_tag  # noqa: F401
