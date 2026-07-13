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

See `acd.api` for the full public API (`load_acd`, `save_acd`,
`patch_rungs`, `export_routine`, `ConvertAcdToL5x`, ...) -- each function
and the returned object's own classes (`RSLogix5000Content`, `Controller`,
`Program`, `Routine`, `Tag`, ... in `acd.l5x.elements`) have docstrings
with concrete attribute paths; check those with `help()` before guessing
at the object shape. See this package's README.md for full usage examples,
and CLAUDE.md for internals/gotchas if modifying this library itself.

Comparing two projects/saves -- for a GENERIC "what changed between these two
ACDs?" request (routines, tags, data types, modules, AOIs), use
`diff_project(project_a, project_b)`. Only reach for the narrower
`diff_io_addresses(project_a, project_b)` when the request is SPECIFICALLY
about I/O address wiring (e.g. "what I/O addresses changed?") -- it reports
nothing about tag values, rung logic changes, or anything else, so it is
the wrong default for a broad comparison. Both avoid a hand-rolled regex
over rung text (I/O addresses like "Remote_GraderConsole:3:I.Pt13.Data" or
"IO024:I.Data[0].13" are easy to mis-tokenize) and both compare routines by
aligning rung/ST-line content instead of zipping by index, which breaks
(IndexError) the moment two routines have a different rung count -- this
happens even between two saves of what is otherwise "the same" routine.

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
)
