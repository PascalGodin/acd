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

Writing changes back to a real `.ACD` that Studio 5000 will open: prefer
`export_routine()` (a partial L5X imported via Studio's own "Import
Routine" feature) over `save_acd()`/`patch_rungs()` -- Studio enforces a
`FileInfo.Dat` checksum on open that this library cannot re-sign without a
key it does not have, so `save_acd()` only produces an openable file for a
completely unmodified round-trip.
"""

from acd.api import load_acd, save_acd, patch_rungs, export_routine  # noqa: F401
