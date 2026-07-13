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
Routine" feature) over `save_acd()`/`patch_rungs()` -- Studio enforces a
`FileInfo.Dat` checksum on open that this library cannot re-sign without a
key it does not have, so `save_acd()` only produces an openable file for a
completely unmodified round-trip.
"""

from acd.api import (  # noqa: F401
    load_acd,
    save_acd,
    patch_rungs,
    export_routine,
    find_io_addresses,
    io_addresses_by_routine,
    diff_io_addresses,
    diff_project,
    diff_routine,
)
