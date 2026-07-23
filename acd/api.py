import difflib
import os
import re
import tempfile
import shutil
import xml.dom.minidom
from abc import abstractmethod
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Dict, List, Tuple, Union

from acd.integrity import (
    FILEINFO_LENGTH,
    compute_fileinfo,
    detect_fileinfo_selector,
    expected_key_length_for_selector,
    find_fileinfo_offset,
    get_fileinfo_key,
)
from acd.l5x.export_l5x import ExportL5x
from acd.zip.unzip import Unzip
from acd.zip.write_acd import build_acd_bytes, write_acd
from acd.zip.write_dat import patch_sbregion_dat

from acd.database.acd_database import AcdDatabase
from acd.l5x.elements import (
    DataType,
    DumpCompsRecords,
    Member,
    RSLogix5000Content,
    Routine,
    _escape_xml_attr,
    _multiline_xml_text,
    new_member,
)


# Clean top-level API

def load_acd(path, temp_dir: str = None) -> RSLogix5000Content:
    """Load an ACD file into a Python object model.

    Args:
        path: Path to the .ACD file.
        temp_dir: Directory for SQLite and extracted files. If omitted
            (the normal case), a directory is created under the OS temp
            location (`tempfile.mkdtemp()` -- e.g. under
            "%LOCALAPPDATA%/Temp/" on Windows, NOT next to `path`) and
            deleted again before this function returns. Pass an
            explicit `temp_dir` if you want the extracted `.Dat`/`.Idx`
            files and SQLite DB left on disk afterward for inspection --
            they are NOT cleaned up when you supply this yourself.

            This differs from the lower-level `ExportL5x` class (which
            `load_acd()` wraps): with no `temp_dir` given, `ExportL5x`
            defaults to a folder next to the source file itself
            (`<dir>/<stem>/`, e.g. `MyController.ACD` ->
            `MyController/`) and leaves it in place -- the two entry
            points intentionally behave differently (`load_acd()` favors a
            clean one-shot load; `ExportL5x` favors leaving artifacts
            around for a debugging/exploration session).

    Returns:
        RSLogix5000Content with a fully populated controller object tree.
        The project also carries _raw_files / _file_order / _footer_unknown
        for use by save_acd().

        There is no top-level `.routines`/`.tags` shortcut on the returned
        object -- routines and program-scope tags are nested under
        programs, controller-scope tags/UDTs/AOIs/modules are directly on
        `.controller`:

            project = load_acd("MyController.ACD")
            project.controller.name                       # controller name
            project.controller.tags                        # controller-scope tags
            for program in project.controller.programs:
                for routine in program.routines:            # NOT project.routines
                    routine.rungs                            # list[str], plain ladder text

        See RSLogix5000Content/Controller/Program/Routine/Tag's own
        docstrings (acd/l5x/elements.py) for the full shape, or README.md
        for more complete usage examples.

        Editing this object graph does NOT let you write changes back via
        save_acd() for anything except rung text (patch_rungs()) -- and
        even that requires a FileInfo.Dat signing key Studio 5000 enforces
        on open, which this library does not have. For tag/routine edits
        that need to actually open in real Studio 5000, use export_routine()
        instead (see its own docstring, and CLAUDE.md's "Native-import
        escape hatches" section) -- verified end-to-end in real Studio 5000
        for both editing an existing tag and creating a brand-new one.
    """
    cleanup = temp_dir is None
    if cleanup:
        temp_dir = tempfile.mkdtemp(prefix="acd_load_")
    exporter = None
    try:
        exporter = ExportL5x(str(path), temp_dir)
        return exporter.project
    finally:
        # The SQLite connection must be closed BEFORE rmtree, or the open
        # acd.db file keeps the temp directory locked on Windows -- rmtree
        # then silently fails (ignore_errors=True swallows the
        # PermissionError) and the "auto-deleted" directory is left behind
        # on disk forever, not just until some later cleanup. Found via a
        # real user report ("does the temp folder stay active until
        # reboot?") after directly confirming the directory survived a
        # completed load_acd() call. Safe to close here: .project/
        # .controller are lazily built and cached on first access (see
        # ExportL5x.project/.controller), so the object graph returned
        # above is already fully materialized in plain Python objects with
        # no further dependency on the connection.
        if exporter is not None:
            exporter.close()
        if cleanup:
            shutil.rmtree(temp_dir, ignore_errors=True)


def save_acd(project: RSLogix5000Content, output_path) -> None:
    """Write a project object model back to an ACD file.

    The project must have been loaded via load_acd() or ExportL5x so that
    it carries _raw_files, _file_order, and _footer_unknown.

    If the project has had `acd.integrity.set_fileinfo_key(project, key)`
    called on it, the container's `FileInfo.Dat` is recomputed here so
    that the Logix Designer SDK accepts the file. The algorithm is
    auto-selected by key length (32 bytes = modern Studio, 126 bytes =
    older Studio); the key length must match the source ACD's
    FileInfo.Dat selector or a ValueError is raised.

    Without a registered key, the container is written as-is (suitable
    for byte-equal round-trips, but the SDK will reject any output
    where covered streams were modified).

    Args:
        project: Project loaded by load_acd().
        output_path: Destination .ACD file path.

    Raises:
        ValueError: if a key is registered whose length doesn't match
            the source ACD's FileInfo.Dat selector.
    """
    container = build_acd_bytes(
        files=project._raw_files,
        file_order=project._file_order,
        footer_unknown=project._footer_unknown,
    )

    key = get_fileinfo_key(project)
    if key is not None:
        fi_offset = find_fileinfo_offset(container)
        source_selector = detect_fileinfo_selector(container, fi_offset)
        expected_key_len = expected_key_length_for_selector(source_selector)
        if len(key) != expected_key_len:
            raise ValueError(
                f"ACD's FileInfo.Dat uses selector {source_selector:#06x} "
                f"requiring a {expected_key_len}-byte key; the registered "
                f"key is {len(key)} bytes. Register the correct key for "
                f"this ACD's Studio version."
            )
        new_fi = compute_fileinfo(container, fi_offset, key=key)
        container = (
            container[:fi_offset]
            + new_fi
            + container[fi_offset + FILEINFO_LENGTH:]
        )

    Path(output_path).write_bytes(container)


def patch_rungs(project: RSLogix5000Content, changes: dict) -> None:
    """Patch rung text in a loaded project's SbRegion.Dat in-place.

    Call this before save_acd() to modify ladder rung logic.

    Args:
        project: Project loaded by load_acd().
        changes: Mapping of {rung_object_id: new_rung_text}.

            rung_object_id — the integer object_id for the rung.  Available
            as routine._rung_ids[i] for the i-th rung in a Routine.

            new_rung_text — the new rung text with plain tag names (not
            @HEX@ placeholders).  Tag names are resolved back to object_id
            placeholders automatically using project._id_to_name.

    Example:
        project = load_acd("project.ACD")
        routine = project.controller.programs[0].routines[0]
        changes = {routine._rung_ids[0]: "XIC(MyTag)OTE(OutputTag);"}
        patch_rungs(project, changes)
        save_acd(project, "modified.ACD")
    """
    project._raw_files["SbRegion.Dat"] = patch_sbregion_dat(
        project._raw_files["SbRegion.Dat"],
        changes,
        project._id_to_name,
    )


_IO_ADDRESS_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*"                          # module/tag base name
    r"(?::\d+)?"                                          # optional :slot (rack/chassis addressing)
    r":[A-Za-z]\w*"                                       # :Type (I, O, C, ...)
    r"(?:\.[A-Za-z_]\w*|\.\d+|\[\d+(?:,\d+)*\])*"         # .Member / .bit / [index,...] chain
)


def find_io_addresses(text: str) -> List[str]:
    """Extract every I/O-style tag address referenced in one rung/ST-line of
    text, e.g. "IO024:I.Data[0].13", "Remote_GraderConsole:3:I.Pt13.Data",
    "Local:10:I.Data.11", "Sorter_VFD:I.DriveStatus_Active" -- in
    left-to-right order, duplicates included.

    A real I/O address always contains a ":" (reserved by Rockwell's own
    tag-naming rules for module addressing), so this never matches a plain
    UDT member path like "M304_Sorter_Lug_Chain.VFD.Running" -- no
    false-positive collisions with ordinary tag/member references.

    Use this instead of writing a new regex for this address shape --
    hand-rolled versions are easy to get subtly wrong (unbalanced brackets,
    dropping the trailing bit/array suffix, multi-dimensional indices).
    """
    if not text:
        return []
    return _IO_ADDRESS_RE.findall(text)


def io_addresses_by_routine(project: RSLogix5000Content) -> Dict[Tuple[str, str], List[str]]:
    """Map every routine in the project to the full, ordered list of I/O
    addresses (see find_io_addresses()) referenced anywhere in its rungs
    (RLL) or lines (ST) -- duplicates included, in source order.

    Keyed by (program_name, routine_name); AOI logic routines are keyed by
    (f"AOI:{aoi.name}", routine_name) since they don't belong to a Program.

    This is a rung/line-count-independent "signature" of a routine's I/O
    wiring -- use it (or diff_io_addresses(), below) instead of zipping two
    routines' rungs by index to compare I/O references: that breaks with an
    IndexError as soon as the two routines have a different rung count,
    which is common even between two saves of what is otherwise "the same"
    routine.
    """
    def _addrs(routine: Routine) -> List[str]:
        addrs: List[str] = []
        for text in routine.rungs or []:
            addrs.extend(find_io_addresses(text))
        for text in routine._st_lines or []:
            addrs.extend(find_io_addresses(text))
        return addrs

    result: Dict[Tuple[str, str], List[str]] = {}
    for program in project.controller.programs:
        for routine in program.routines:
            result[(program.name, routine.name)] = _addrs(routine)
    for aoi in project.controller.aois:
        for routine in aoi.routines:
            result[(f"AOI:{aoi.name}", routine.name)] = _addrs(routine)
    return result


def diff_io_addresses(
    project_a: RSLogix5000Content, project_b: RSLogix5000Content
) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
    """Compare ONLY I/O address usage between two projects -- for a general
    "what changed between these two projects" comparison (routines, tags,
    data types, modules, AOIs), use diff_project() instead. Do not reach for
    this function by default just because it has "diff" in the name; it
    reports nothing about non-I/O tag values, rung logic, descriptions, etc.

    Compares two projects (e.g. two saves/versions of the same controller)
    routine by routine, without assuming the two routines' rungs/lines line
    up by index -- the naive "zip rung i of A with rung i of B" approach
    breaks (IndexError) the moment a routine gained or lost a rung between
    the two saves, even when the actual I/O wiring is unchanged.

    Returns one entry per (program_name, routine_name) key (see
    io_addresses_by_routine()) that differs between the two projects --
    routines with identical I/O address usage are omitted entirely. Each
    entry is {"removed": [...], "added": [...], "common": [...]}: "removed"
    are addresses used in project_a but not project_b, "added" the reverse,
    both order-insensitive with duplicates collapsed. A routine present in
    only one project still gets an entry, with every one of its addresses
    reported as fully added or fully removed.
    """
    a = io_addresses_by_routine(project_a)
    b = io_addresses_by_routine(project_b)
    diff: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    for key in sorted(set(a) | set(b)):
        set_a, set_b = set(a.get(key, [])), set(b.get(key, []))
        if set_a == set_b:
            continue
        diff[key] = {
            "removed": sorted(set_a - set_b),
            "added": sorted(set_b - set_a),
            "common": sorted(set_a & set_b),
        }
    return diff


def _routine_lines(routine: Routine) -> List[str]:
    return routine.rungs if routine.type == "RLL" else routine._st_lines


def _all_routines(project: RSLogix5000Content) -> Dict[Tuple[str, str], Routine]:
    result: Dict[Tuple[str, str], Routine] = {}
    for program in project.controller.programs:
        for routine in program.routines:
            result[(program.name, routine.name)] = routine
    for aoi in project.controller.aois:
        for routine in aoi.routines:
            result[(f"AOI:{aoi.name}", routine.name)] = routine
    return result


def diff_routine(routine_a: Routine, routine_b: Routine) -> dict:
    """Compare two Routine objects directly -- e.g. "the same" routine
    fetched from two different projects/saves, when you already have both
    Routine objects in hand and don't need diff_project()'s whole-project
    scan.

    Do NOT manually zip/print routine_a.rungs and routine_b.rungs side by
    side (or by ST-line index) and eyeball the difference: two rungs at the
    same index are NOT necessarily "the same rung" -- a single rung deleted
    or inserted anywhere in one routine shifts every later rung's index,
    making an otherwise-identical tail look completely different in a naive
    side-by-side printout even though nothing actually changed there. This
    function aligns the two routines' rungs (RLL) or lines (ST) with
    difflib.SequenceMatcher instead (the same approach diff_project()'s
    "routines" section uses internally), so an inserted/deleted/edited rung
    shows up as exactly that -- one small op -- with everything else
    correctly recognized as unchanged regardless of where it shifted to.

    Returns {"status": "unchanged"/"changed", "changes": [...]}: "changes"
    is a list of {"op": "replace"/"delete"/"insert", "old": [...], "new":
    [...]} blocks, empty (status "unchanged") if the two routines' rungs/
    lines are identical.
    """
    lines_a, lines_b = _routine_lines(routine_a) or [], _routine_lines(routine_b) or []
    if lines_a == lines_b:
        return {"status": "unchanged", "changes": []}
    changes = []
    matcher = difflib.SequenceMatcher(a=lines_a, b=lines_b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({"op": tag, "old": lines_a[i1:i2], "new": lines_b[j1:j2]})
    return {"status": "changed", "changes": changes}


def _diff_routines(project_a: RSLogix5000Content, project_b: RSLogix5000Content) -> dict:
    routines_a = _all_routines(project_a)
    routines_b = _all_routines(project_b)
    diff = {}
    for key in sorted(set(routines_a) | set(routines_b)):
        ra, rb = routines_a.get(key), routines_b.get(key)
        if ra is None:
            diff[key] = {"status": "added", "lines": _routine_lines(rb) or []}
            continue
        if rb is None:
            diff[key] = {"status": "removed", "lines": _routine_lines(ra) or []}
            continue
        result = diff_routine(ra, rb)
        if result["status"] == "unchanged":
            continue
        diff[key] = result
    return diff


_MAX_VALUE_REPR = 200  # chars before a tag value diff switches to a summary


def _summarize_value_diff(old, new) -> dict:
    """Compact old/new representation for a tag's "value" diff.

    Small/scalar values are reported in full ({"old": ..., "new": ...}).
    Large containers -- typically a UDT array tag's decoded initial value,
    a list of per-element dicts -- are summarized instead of embedded in
    full: dumping every element's full old/new value produces an unreadable,
    multi-megabyte wall of output on a real project (observed: 1601 changed
    tags in one real comparison, many holding full UDT-array values), which
    defeats the purpose of a diff meant to be read directly.
    """
    old_repr, new_repr = repr(old), repr(new)
    if len(old_repr) <= _MAX_VALUE_REPR and len(new_repr) <= _MAX_VALUE_REPR:
        return {"old": old, "new": new}
    if isinstance(old, list) and isinstance(new, list):
        n = min(len(old), len(new))
        differing = [i for i in range(n) if old[i] != new[i]]
        return {
            "summary": f"list[{len(old)}] vs list[{len(new)}]: "
            f"{len(differing)} of {n} common elements differ",
            "differing_indices": differing[:10],
        }
    if isinstance(old, dict) and isinstance(new, dict):
        keys = set(old) | set(new)
        differing = sorted(k for k in keys if old.get(k) != new.get(k))
        return {
            "summary": f"dict with {len(keys)} keys: {len(differing)} differ",
            "differing_keys": differing[:10],
        }
    return {"summary": f"value too large to display ({len(old_repr)} / {len(new_repr)} chars)"}


def _all_tags(project: RSLogix5000Content) -> dict:
    result = {}
    for tag in project.controller.tags:
        result[("", tag.name)] = tag
    for program in project.controller.programs:
        for tag in program.tags:
            result[(program.name, tag.name)] = tag
    return result


def _diff_tags(project_a: RSLogix5000Content, project_b: RSLogix5000Content) -> dict:
    tags_a, tags_b = _all_tags(project_a), _all_tags(project_b)
    diff = {}
    for key in sorted(set(tags_a) | set(tags_b)):
        ta, tb = tags_a.get(key), tags_b.get(key)
        if ta is None or tb is None:
            diff[key] = {"status": "added" if ta is None else "removed"}
            continue
        changed = {}
        if ta.data_type != tb.data_type:
            changed["data_type"] = {"old": ta.data_type, "new": tb.data_type}
        if ta.description != tb.description:
            changed["description"] = {"old": ta.description, "new": tb.description}
        if ta._initial_value != tb._initial_value:
            changed["value"] = _summarize_value_diff(ta._initial_value, tb._initial_value)
        if changed:
            diff[key] = {"status": "changed", "changed": changed}
    return diff


def _diff_names(objs_a, objs_b) -> Union[dict, None]:
    names_a = {o.name for o in objs_a}
    names_b = {o.name for o in objs_b}
    if names_a == names_b:
        return None
    return {"added": sorted(names_b - names_a), "removed": sorted(names_a - names_b)}


def diff_project(project_a: RSLogix5000Content, project_b: RSLogix5000Content) -> dict:
    """General-purpose structural diff between two projects (e.g. two saves,
    or two related variants of the same controller) -- routines, tags, data
    types, modules, and AOIs. Use this for a broad/generic "what changed"
    comparison; use diff_io_addresses() only when specifically asked about
    I/O address wiring, or diff_routine() if you already have two specific
    Routine objects in hand and don't need a whole-project scan.

    Returns a dict with up to five keys, each populated only where something
    actually differs (an empty dict means no differences found at all):

      "routines": {(program_name, routine_name): {...}, ...}
          "status": "added"/"removed"/"changed".
          "added"/"removed" entries include the routine's full rungs/ST
          lines under "lines". "changed" entries include "changes": a list
          of {"op": "replace"/"delete"/"insert", "old": [...], "new": [...]}
          blocks from aligning the two routines' rung/line lists with
          difflib.SequenceMatcher -- NOT a naive index-by-index zip, so a
          routine with a different rung count between the two projects
          (routine) diffs correctly instead of raising IndexError. AOI
          logic routines are keyed as ("AOI:<name>", routine_name).

      "tags": {(program_name_or_"", tag_name): {...}, ...}
          "" as the first key element means controller-scope. "status":
          "added"/"removed"/"changed", where "changed" includes a
          "changed" dict naming which of data_type/description/value
          differ. data_type/description are always {"old": ..., "new": ...}
          in full; "value" is too for a small/scalar value, but a large
          container (typically a UDT array tag's decoded value -- a list of
          per-element dicts) is instead summarized as {"summary": "...",
          "differing_indices"/"differing_keys": [...] (first 10)} --
          dumping every element's full old/new value in full for every
          changed tag is not readable and can produce a multi-megabyte
          result on a real project (observed: 1601 changed tags in one real
          comparison). Check for a "summary" key vs "old"/"new" keys to tell
          which shape a given "value" entry is.

      "data_types" / "modules" / "aois": {"added": [...], "removed": [...]}
          Presence-only (by name) -- this does NOT diff UDT member
          layout, module connection/RPI details, or AOI parameters; for
          that, compare the relevant objects directly (e.g.
          project.controller.data_types) or render both to L5X and diff
          the XML.
    """
    diff = {}
    routines = _diff_routines(project_a, project_b)
    if routines:
        diff["routines"] = routines
    tags = _diff_tags(project_a, project_b)
    if tags:
        diff["tags"] = tags
    data_types = _diff_names(project_a.controller.data_types, project_b.controller.data_types)
    if data_types:
        diff["data_types"] = data_types
    modules = _diff_names(project_a.controller.modules, project_b.controller.modules)
    if modules:
        diff["modules"] = modules
    aois = _diff_names(project_a.controller.aois, project_b.controller.aois)
    if aois:
        diff["aois"] = aois
    return diff


def _inject_use_attr(xml_str: str, element_name: str, use_value: str) -> str:
    """Insert Use="value" as the first attribute of <element_name ...>.

    e.g. _inject_use_attr('<Tag Name="Foo">...', 'Tag', 'Context')
      -> '<Tag Use="Context" Name="Foo">...'
    """
    marker = f"<{element_name} "
    idx = xml_str.index(marker)
    insert_at = idx + len(marker)
    return xml_str[:insert_at] + f'Use="{use_value}" ' + xml_str[insert_at:]


_TAG_TOKEN_RE = None  # compiled lazily to avoid importing re at module load if unused


def _referenced_tag_names(rung_texts) -> set:
    """Extract candidate tag-name tokens referenced in rung text.

    A simple identifier scan, not a real ladder-logic parser: it grabs every
    identifier-like token (letters/digits/underscore) and lets the caller
    intersect against the project's actual known tag names -- instruction
    mnemonics (XIC, OTE, TON, ...) are harmless false positives here since
    they simply won't match any real tag name afterward.

    A token immediately followed by "(" (optionally with whitespace) is
    excluded: in RLL syntax this position is always an instruction, AOI, or
    JSR mnemonic being invoked, never a bare tag operand (a real tag
    reference is either standalone or followed by "[...]"/"."). Found via a
    real project where a genuine tag happened to share its name with the
    AFI (Always False Instruction) mnemonic -- "AFI()" in the rung text
    wrongly matched that unrelated real tag by name, pulling it in as
    context even though the rung has nothing to do with it. This exclusion
    is safe for AOI instance calls too (e.g. "AOI_RPMtoFPM(TestFPM,...)"):
    the AOI's own mnemonic name isn't a tag and was never meant to be
    matched here -- it's resolved separately via the instance tag's own
    data_type field (see _resolve_type_closure()).

    A token immediately preceded by "." is also excluded: in Rockwell
    address syntax "." always introduces a MEMBER name (e.g. "Length_In" in
    "ToTrim[Timing.Length_Lug].Length_In"), never a fresh, independent tag
    reference -- a real tag reference never itself follows a literal ".".
    Found via the same real project: an unrelated real tag happened to be
    named "Length_In", coincidentally matching the member-access suffix
    ".Length_In" in several rungs and getting wrongly pulled in as context.
    """
    import re
    token_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    paren_next_re = re.compile(r"\s*\(")
    names = set()
    for text in rung_texts:
        if not text:
            continue
        for m in token_re.finditer(text):
            if paren_next_re.match(text, m.end()):
                continue
            if m.start() > 0 and text[m.start() - 1] == ".":
                continue
            names.add(m.group())
    return names


def _referenced_modules(rung_texts, project: RSLogix5000Content) -> list:
    """Resolve which Module dependencies a routine's rung text needs, the
    same way Studio's own "Export Routine" does (confirmed against a real
    export of Motors/Main_Motors in BPM_TrimmerSorter_20260709.ACD, whose
    rung text references "Local:12:I.Data.0"/"Local:12:I.Data.1" and
    "VFD_P_INTBL2:I.OutputFreq" -- Studio's own <Modules Use="Context">
    section included exactly {"AC_IN_12", "Local", "VFD_P_INTBL2"}).

    Standard Logix I/O tag addressing has two shapes: "ModuleName:Type..."
    (a directly-addressed module, e.g. an Ethernet-connected drive) needs
    only that module; "ModuleName:SlotNumber:Type..." (rack/chassis-slot
    addressing, e.g. a local-chassis input card) needs BOTH the chassis
    module itself (here "Local") AND whichever module actually occupies
    that slot (found via Module.parent_module == chassis name and
    Module._slot == the slot number -- here that's "AC_IN_12", slot 12 of
    "Local") -- confirmed exactly against the real export above (it
    included "AC_IN_12" and "Local" for the 3-part reference, but did NOT
    include "Ethernet2", the parent of the 2-part-addressed
    "VFD_P_INTBL2" -- so a directly-addressed module's own parent is NOT
    walked, only a slot occupant's rack).

    Caveat: verified against exactly one real project's one rack (a local
    chassis) plus one direct Ethernet device -- topologies with bridged/
    remote racks (ControlNet, DeviceNet, remote Ethernet chassis reached
    through an adapter) haven't been exercised and may need this extended.
    """
    import re
    io_ref_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*):(\d+:)?([A-Za-z]\w*)")
    modules_by_name = {m.name: m for m in project.controller.modules}
    found: Dict[str, object] = {}
    for text in rung_texts:
        if not text:
            continue
        for m in io_ref_re.finditer(text):
            base, slot_part, _typ = m.group(1), m.group(2), m.group(3)
            base_module = modules_by_name.get(base)
            if base_module is None:
                continue
            found[base] = base_module
            if slot_part:
                slot_num = int(slot_part.rstrip(":"))
                occupant = next(
                    (mod for mod in project.controller.modules
                     if mod.parent_module == base and mod._slot == slot_num),
                    None,
                )
                if occupant is not None:
                    found[occupant.name] = occupant
    return list(found.values())


def _referenced_called_routines(rung_texts, program) -> list:
    """Resolve which OTHER routines in the same program a routine's rung
    text calls via JSR (subroutine calls can't cross program boundaries in
    native ladder logic), so they can be included as empty
    <Routine Use="Reference"> stubs the way Studio's own "Export Routine"
    does -- confirmed against a real export: Motors/Main_Motors calls
    "JSR(Infeed_LandingTable,0);", and the real export included an empty
    <Routine Use="Reference" Name="Infeed_LandingTable"></Routine> alongside
    the actual <Routine Use="Target" Name="Main_Motors">.
    """
    import re
    jsr_re = re.compile(r"\bJSR\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)")
    routines_by_name = {r.name: r for r in (program.routines or [])}
    found: Dict[str, object] = {}
    for text in rung_texts:
        if not text:
            continue
        for m in jsr_re.finditer(text):
            name = m.group(1)
            r = routines_by_name.get(name)
            if r is not None:
                found[name] = r
    return list(found.values())


def _resolve_type_closure(initial_type_names: set, project: RSLogix5000Content):
    """Recursively resolve a set of DataType/AOI names to every DataType and
    AOI they transitively depend on, so a partial export is self-consistent
    the same way Studio's own "Export Routine"/"Export Component" is meant to
    be (per the user: it "only includes what is referenced ... including
    UDT, AOI, Modules, etc").

    Expands the closure through: a UDT's own members' data types (so a UDT
    containing another project UDT pulls that one in too -- e.g. a member
    whose data_type is itself a UDT name), and an AOI's own parameters' and
    local tags' data types (so an AOI using a project UDT, or nesting another
    AOI as a parameter type, pulls those in too). Order names never seen
    before are processed in a simple worklist so the recursion terminates
    even with cyclic-looking (but not actually cyclic, Logix disallows UDT
    self-reference) dependency graphs.

    Returns (data_types, aois) -- each a list in the project's own
    declaration order, filtered to the resolved closure.

    NOTE: only the "which UDTs/AOIs get included" computation is exercised
    against real data (mirrors the already-verified single-level UDT
    resolution, generalized into a transitive closure and extended to also
    search project.controller.aois, which the single-level version never
    did). The AOI wrapper's XML *placement* in export_routine() is still
    unverified -- see the comment at its one call site.
    """
    resolved_types: set = set()
    resolved_aois: set = set()
    worklist = set(initial_type_names)
    data_types_by_name = {dt.name.upper(): dt for dt in project.controller.data_types}
    aois_by_name = {a.name.upper(): a for a in (project.controller.aois or [])}

    while worklist:
        name = worklist.pop()
        if name in resolved_types or name in resolved_aois:
            continue
        dt = data_types_by_name.get(name)
        if dt is not None:
            resolved_types.add(name)
            for member in dt.members:
                if member.data_type:
                    worklist.add(member.data_type.upper())
            continue
        aoi = aois_by_name.get(name)
        if aoi is not None:
            resolved_aois.add(name)
            for p in aoi.parameters:
                if p.data_type:
                    worklist.add(p.data_type.upper())
            for lt in aoi.local_tags:
                if lt.data_type:
                    worklist.add(lt.data_type.upper())

    data_types = [dt for dt in project.controller.data_types if dt.name.upper() in resolved_types]
    aois = [a for a in (project.controller.aois or []) if a.name.upper() in resolved_aois]
    return data_types, aois


def export_routine(project: RSLogix5000Content, routine: Routine, output_path, owner: str = None) -> None:
    """Export a single routine as a standalone, partial L5X file.

    Unlike ConvertAcdToL5x (which serialises the whole project), this is
    meant to be imported into an *existing* Studio 5000 project via its
    native "Import Routine" feature -- Studio 5000 itself then handles all
    the internal consistency (cross-reference index, object database,
    re-signing) that a raw ACD binary write would otherwise require. This
    sidesteps the save_acd()/patch_rungs() limitations entirely for the
    common case of editing/adding rungs (including rung comments) in an
    already-existing routine.

    Verified against a real Studio 5000 "Export Routine" output (a 2-rung
    routine referencing a controller-scope tag and two program-scope tags):
    the wrapper needs, in order: an empty <DataTypes Use="Context"> section
    (present even when no custom UDTs are referenced), a <Tags Use="Context">
    section under <Controller> with the full <Tag> definition (current
    value, description, comments -- reusing Tag.to_xml()) of every
    controller-scope tag the routine's rung text references, a
    <Programs Use="Context"> wrapper (the plain <Programs> this used to
    emit was wrong -- missing Use="Context"), a per-program
    <Tags Use="Context"> section the same way for program-scope tags, and
    a <Routines Use="Context"> wrapper around <Routine Use="Target" ...>
    (both Use= attributes were previously missing entirely). ExportOptions
    for a partial/context export also differs from a full project export --
    it includes extra "References Context Dependencies" tokens.

    Referenced tags/data types are found with a simple identifier scan of
    the rung text intersected against the project's actual known tag names
    (see _referenced_tag_names) -- not a full ladder-logic parser, but
    correct for plain tag-name operands (which is what matters for
    instruction operands). Custom UDTs used by any referenced tag are
    included in <DataTypes Use="Context"> automatically, expanded to a full
    transitive closure (a UDT containing another project UDT, or an AOI
    parameter/local tag typed with a project UDT or a nested AOI, pulls in
    that dependency too -- see _resolve_type_closure()). AOI dependencies
    (a routine calling an AOI instruction, whose instance tag's data_type is
    the AOI's own name) are resolved the same way and emitted under
    <AddOnInstructionDefinitions Use="Context">, placed right after
    <Modules Use="Context"> and before <Tags Use="Context"> -- this whole
    section's placement/shape IS now verified against a real Studio export
    (Motors/Main_Motors, which calls AOI_RPMtoFPM -- see CLAUDE.md "Native-
    import escape hatches"). Module dependencies (a routine referencing an
    I/O tag, e.g. "Local:12:I.Data.0" or a direct-addressed
    "VFD_P_INTBL2:I.OutputFreq") are resolved via _referenced_modules() and
    emitted as empty <Module Use="Reference" Name="..."> stubs under
    <Modules Use="Context"> -- also verified against that same real export.
    Routine dependencies (the target routine calling another routine in the
    same program via JSR) are resolved via _referenced_called_routines() and
    emitted as an empty <Routine Use="Reference" Name="..."> stub alongside
    the real <Routine Use="Target">-- also verified against that same
    export (Main_Motors calls JSR(Infeed_LandingTable,0)).

    ST routines are supported the same way: `Routine.to_xml()` already
    renders an ST routine's own content as <STContent><Line .../></STContent>
    (see CLAUDE.md's "Structured Text (ST) routine content" section) rather
    than <RLLContent>, and every dependency scan above (tags, modules,
    called routines) uses `_routine_lines(routine)` -- a small helper
    (shared with `diff_routine()`) that returns `.rungs` for an RLL routine
    or `._st_lines` for an ST one, since `.rungs` is simply empty for the
    latter. An ST routine's identifier syntax (member access via ".",
    instruction/JSR calls via "(") is the same as RLL's for the purposes of
    these regex-based scans, so no ST-specific scanning logic was needed --
    only routing to the right source list. ST routines have no rung-comment
    concept (`_rung_comments` only applies to RLL).

    Args:
        project: The loaded project (from load_acd()) that owns `routine`
            -- used to find which Program contains it, resolve referenced
            tags/data types, and source SoftwareRevision/SchemaRevision.
        routine: The Routine object to export (e.g.
            project.controller.programs[0].routines[0]). For an RLL
            routine, edit `.rungs` (append new rung text, or edit existing
            entries) and/or `._rung_comments` (dict of {rung_index:
            comment_text}) before calling this. For an ST routine
            (`routine.type == "ST"`), edit `._st_lines` (list[str], one
            source line each) instead -- `.rungs`/`._rung_comments` don't
            apply to ST.
        output_path: Destination .L5X file path.
        owner: Optional "Owner" attribute value (the registered Studio 5000
            license owner, e.g. "MyCompany, MyCompany" -- a real export
            included this attribute, but it's not clear whether Studio 5000
            requires it to import; omitted entirely if not supplied).

    Raises:
        ValueError: if `routine` isn't found in any program of `project`.

    Example (RLL):
        project = load_acd("MyController.ACD")
        routine = project.controller.programs[0].routines[0]
        routine.rungs.append("XIC(NewTag)OTE(AnotherTag);")
        routine._rung_comments[0] = "Explains what this rung does"
        export_routine(project, routine, "MyRoutine_export.L5X")
        # Then, in Studio 5000: right-click the Routines folder ->
        # Import Routine... -> select MyRoutine_export.L5X

    Example (ST):
        st_routine = next(r for r in project.controller.programs[0].routines if r.type == "ST")
        st_routine._st_lines.append("NewTag := AnotherTag + 1;")
        export_routine(project, st_routine, "MySTRoutine_export.L5X")
    """
    import datetime

    program = next(
        (p for p in project.controller.programs if any(r is routine for r in p.routines)),
        None,
    )
    if program is None:
        raise ValueError(
            "routine not found in any program of this project -- pass the "
            "same Routine object obtained from project.controller.programs[i].routines[j]"
        )

    controller_name = project.controller.name
    export_date = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
    export_options = (
        "References NoRawData L5KData DecoratedData Context Dependencies "
        "ForceProtectedEncoding AllProjDocTrans"
    )

    # ST routines keep their source in ._st_lines, not .rungs (which is empty
    # for them) -- _routine_lines() (already used by diff_routine()) picks
    # whichever one is actually populated, so every dependency scan below
    # (tags, modules, called routines) works the same way for either type.
    routine_lines = _routine_lines(routine)
    referenced_names = set(_referenced_tag_names(routine_lines))

    # An Alias tag's target must also be included -- Studio 5000's own
    # export does this too (e.g. a routine using alias "Sort_Enc_Calibrated"
    # also includes its AliasFor target "HTV_ECal_SortPos" as its own <Tag>,
    # even though the target's own name never literally appears in the rung
    # text). Resolved iteratively since a target could itself be an alias
    # (rare, but handled for robustness); target names are stripped of any
    # trailing member/bit-index suffix (e.g. "Tag.Member" -> "Tag") to get
    # the base tag name.
    def _base_name(ref: str) -> str:
        return re.split(r"[.\[]", ref, 1)[0]

    while True:
        program_tags = [t for t in program.tags if t.name in referenced_names]
        controller_tags_all = [t for t in project.controller.tags if t.name in referenced_names]
        new_names = set()
        for t in program_tags + controller_tags_all:
            if t.tag_type == "Alias" and t.target:
                base = _base_name(t.target)
                if base not in referenced_names:
                    new_names.add(base)
        if not new_names:
            break
        referenced_names |= new_names

    # An Alias's own target can resolve to an I/O tag (e.g. LngthLmt_16ft ->
    # "Remote_TrimmerIO:0:I.Data.7") -- the base-name resolution above
    # correctly identifies "Remote_TrimmerIO:0:I" as a referenced name, but
    # that literal I/O Tag object must NOT be emitted as its own <Tag>
    # element: Tag._l5x_exclude already encodes exactly this rule for a
    # normal full-project export (I/O tags never appear as standalone
    # <Tag> elements there either), but export_routine() builds its own
    # ad-hoc tag lists rather than going through that generic list-section
    # serialization, so the exclusion was never applied here. Confirmed via
    # a real Studio 5000 import rejecting exactly this: "Error creating
    # 'Tag[@Name="Remote_TrimmerIO:0:I"]' (Invalid name.)".
    program_tags = [t for t in program_tags if not t._l5x_exclude]

    # The I/O tag's *owning Module(s)* (per the rack/slot rule in
    # _referenced_modules()) ARE what Studio references instead -- verified
    # against a real Studio export of LS_Read: LngthLmt_16ft's alias target
    # above needs both "Remote_TrimmerIO" (the rack) and "Trimmer_Inputs"
    # (the module occupying slot 0 of that rack), even though neither name
    # appears literally in the routine's own rung text -- only in the
    # alias's target string. Collected here (any referenced_names entry
    # that looks like an I/O tag reference) and fed to _referenced_modules()
    # below alongside the routine's own rungs.
    alias_io_targets = [name for name in referenced_names if ":" in name]

    # Standard Logix scoping: a program-scope tag shadows a same-named
    # controller-scope tag for bare-name operand resolution within that
    # program. Verified against a real project: a routine's OTE(Flash)
    # resolves to the *program*-scope BOOL tag "Flash", not an unrelated
    # controller-scope tag also named "Flash" (a custom UDT) -- so a
    # controller-scope tag must be excluded here if a program-scope tag of
    # the same name is also referenced, or the export includes a completely
    # wrong, irrelevant tag.
    program_tag_names = {t.name for t in program_tags}
    controller_tags = [
        t for t in project.controller.tags
        if t.name in referenced_names and t.name not in program_tag_names
        and not t._l5x_exclude
    ]

    referenced_type_names = {t.data_type.upper() for t in controller_tags + program_tags if t.data_type}
    referenced_data_types, referenced_aois = _resolve_type_closure(referenced_type_names, project)

    # NOTE: individual <Tag>/<DataType> elements never carry a Use= attribute
    # themselves -- only the wrapping container elements (<Tags Use="Context">,
    # <DataTypes Use="Context">, <Programs Use="Context">, <Routines
    # Use="Context">, <Program Use="Context">, <Controller Use="Context">) and
    # the routine actually being targeted (<Routine Use="Target">) do.
    # Verified against a real Studio 5000 "Export Routine" output of this
    # exact routine+edit: previously this incorrectly added Use="Context" to
    # every <Tag> element, which was the actual trigger for a real Logix
    # Designer crash (0x80004003 "Invalid pointer") on import -- confirmed by
    # importing Studio 5000's own (Use=-free) export of the identical edit
    # successfully, then diffing it attribute-by-attribute against ours.
    data_types_xml = "".join(dt.to_xml() for dt in referenced_data_types)

    # Modules: verified against the real Motors/Main_Motors export -- see
    # _referenced_modules() for the rack/slot-vs-direct-addressing rule.
    # Each <Module> is an empty Use="Reference" stub (just a name), not a
    # full definition -- confirmed against the real export. Also scans
    # alias_io_targets (see above) so an alias's I/O target pulls in its
    # owning Module(s) too.
    referenced_modules = _referenced_modules(list(routine_lines) + alias_io_targets, project)
    modules_xml = "".join(
        f'<Module Use="Reference" Name="{_escape_xml_attr(m.name)}">\n</Module>\n'
        for m in referenced_modules
    )
    modules_section = (
        f'<Modules Use="Context">\n{modules_xml}</Modules>\n' if referenced_modules else ""
    )

    # AOIs: placement (right after </Modules>, before <Tags>) and the
    # Use="Context" wrapper are now verified against that same real export.
    aois_xml = "".join(aoi.to_xml() for aoi in referenced_aois)
    aois_section = (
        f'<AddOnInstructionDefinitions Use="Context">\n{aois_xml}\n</AddOnInstructionDefinitions>\n'
        if referenced_aois else ""
    )

    # Routines called via JSR within the same program: empty Use="Reference"
    # stubs alongside the real Use="Target" routine -- verified against that
    # same real export (Main_Motors calls JSR(Infeed_LandingTable,0)).
    referenced_routines = _referenced_called_routines(routine_lines, program)
    called_routines_xml = "".join(
        f'<Routine Use="Reference" Name="{_escape_xml_attr(r.name)}">\n</Routine>\n'
        for r in referenced_routines
    )

    controller_tags_xml = "".join(t.to_xml() for t in controller_tags)
    program_tags_xml = "".join(t.to_xml() for t in program_tags)
    routine_xml = _inject_use_attr(routine.to_xml(), "Routine", "Target")

    owner_attr = f' Owner="{_escape_xml_attr(owner)}"' if owner else ""

    # A real "Export Routine" output includes an XML comment right after the
    # declaration, mirroring the routine's own <Description> (e.g. a routine
    # named "Lug_Skip" with description "Shift the Data on the Grading Chain
    # and Start a Skip if needed" gets that exact text as a leading
    # "<!--...-->" comment). "--" is illegal inside an XML comment body, so
    # it's split apart if present to keep the file well-formed.
    leading_comment = ""
    if routine._description:
        # Normalize line endings to bare "\n" first -- Path.write_text()'s
        # default text-mode newline translation on Windows blindly replaces
        # every "\n" with "\r\n", including the "\n" half of an existing
        # "\r\n" pair, which doubles up into "\r\r\n" (rendered as a blank
        # line) if the raw \r\n from the ACD is left in as-is.
        safe_comment = _multiline_xml_text(routine._description).replace("--", "- -")
        leading_comment = f'<!--{safe_comment}-->\n'

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'{leading_comment}'
        f'<RSLogix5000Content SchemaRevision="{project.schema_revision}" '
        f'SoftwareRevision="{project.software_revision}" '
        f'TargetName="{_escape_xml_attr(routine.name)}" '
        f'TargetType="Routine" TargetSubType="{_escape_xml_attr(routine.type)}"'
        f'{owner_attr} ContainsContext="true" ExportDate="{export_date}" '
        f'ExportOptions="{export_options}">\n'
        f'<Controller Use="Context" Name="{_escape_xml_attr(controller_name)}">\n'
        f'<DataTypes Use="Context">\n{data_types_xml}\n</DataTypes>\n'
        f'{modules_section}'
        f'{aois_section}'
        f'<Tags Use="Context">\n{controller_tags_xml}\n</Tags>\n'
        f'<Programs Use="Context">\n'
        f'<Program Use="Context" Name="{_escape_xml_attr(program.name)}">\n'
        f'<Tags Use="Context">\n{program_tags_xml}\n</Tags>\n'
        f'<Routines Use="Context">\n'
        f'{called_routines_xml}'
        f'{routine_xml}\n'
        f'</Routines>\n'
        f'</Program>\n'
        f'</Programs>\n'
        f'</Controller>\n'
        f'</RSLogix5000Content>\n'
    )

    Path(output_path).write_text(xml, encoding="utf-8")


def export_datatype(project: RSLogix5000Content, data_type: DataType, output_path, owner: str = None) -> None:
    """Export a single DataType (UDT) as a standalone, partial L5X file, for
    Studio 5000's native "Import Data Type..." command (right-click the
    Data Types folder) -- the same "native-import escape hatch" strategy
    `export_routine()` uses for rungs/tags, sidestepping the raw-binary
    `save_acd()`/`patch_rungs()` limitations (`FileInfo.Dat`'s checksum is
    enforced on open and this library cannot re-sign it) for the common
    case of creating or modifying a UDT.

    To **modify** an existing UDT: get it from `project.controller.data_types`,
    mutate its `.members` list in place (e.g. `dt.members.insert(i,
    new_member("Foo", "DINT"))` to add a field at a specific position, or
    edit/remove entries directly), then export it. To **create** a brand-new
    UDT: construct a `DataType` and append it to `project.controller.data_types`
    first, then export it the same way -- no special-casing needed for
    "new" vs "existing", matching the same pattern already proven for tags
    via the routine-carrier mechanism (see CLAUDE.md).

    Member `_byte_offset` is irrelevant here and never emitted in the XML at
    all (`Member.to_xml()` skips every underscore-prefixed field) -- Studio
    5000 recomputes each member's real physical offset from the member
    *order* you provide, the same way it would if you dragged a new row
    into the UDT editor's grid.

    Any other DataType this one depends on (a member typed as another
    project UDT, transitively) is automatically included as additional
    context via `_resolve_type_closure()` -- the same dependency-resolution
    logic already verified for `export_routine()`'s own `<DataTypes
    Use="Context">` section.

    CAUTION -- unverified wrapper shape: unlike `export_routine()` (whose
    exact XML shape was calibrated against multiple real Studio 5000
    "Export Routine" outputs and real import trial-and-error), this
    function's wrapper is a best-effort guess built by direct symmetry with
    that already-verified case -- it has NOT yet been confirmed against a
    real Studio 5000 "Export Data Type" output or a real "Import Data
    Type..." attempt. Test on a COPY of your project first, and expect this
    may need adjustment based on what Studio's importer actually accepts or
    rejects (see CLAUDE.md's "Partial/context L5X exports" section for how
    many real-import rounds it took to get export_routine()'s shape right).

    Args:
        project: The loaded project (from load_acd()) that owns `data_type`
            (or that you've just appended a brand-new `data_type` to).
        data_type: The DataType to export -- must already be an element of
            `project.controller.data_types`.
        output_path: Destination .L5X file path.
        owner: Optional "Owner" attribute value, as in export_routine().

    Raises:
        ValueError: if `data_type` isn't in `project.controller.data_types`.

    Example:
        project = load_acd("MyController.ACD")
        lug = next(dt for dt in project.controller.data_types if dt.name == "Lug")
        member = new_member("NewField", "DINT", description="Added via acd-tools")
        insert_at = next(i for i, m in enumerate(lug.members) if m.name == "Z1_Board_Length") + 1
        lug.members.insert(insert_at, member)
        export_datatype(project, lug, "Lug_modified.L5X")
        # Studio 5000: right-click Data Types -> Import Data Type... -> select the file
    """
    import datetime

    if not any(dt is data_type for dt in project.controller.data_types):
        raise ValueError(
            "data_type not found in project.controller.data_types -- append "
            "it there first (for a brand-new UDT) or pass the same DataType "
            "object obtained from project.controller.data_types"
        )

    controller_name = project.controller.name
    export_date = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
    export_options = (
        "References NoRawData L5KData DecoratedData Context Dependencies "
        "ForceProtectedEncoding AllProjDocTrans"
    )

    referenced_data_types, _referenced_aois = _resolve_type_closure(
        {data_type.name.upper()}, project
    )

    # Individual <DataType> elements never carry a Use= attribute themselves
    # except the one actually being targeted -- same rule already verified
    # for export_routine()'s <Tag>/<Routine> elements (see its own docstring
    # for the real Studio 5000 crash this rule was found from).
    data_types_xml = "".join(
        _inject_use_attr(dt.to_xml(), "DataType", "Target")
        if dt is data_type else dt.to_xml()
        for dt in referenced_data_types
    )

    owner_attr = f' Owner="{_escape_xml_attr(owner)}"' if owner else ""

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<RSLogix5000Content SchemaRevision="{project.schema_revision}" '
        f'SoftwareRevision="{project.software_revision}" '
        f'TargetName="{_escape_xml_attr(data_type.name)}" '
        f'TargetType="DataType"'
        f'{owner_attr} ContainsContext="true" ExportDate="{export_date}" '
        f'ExportOptions="{export_options}">\n'
        f'<Controller Use="Context" Name="{_escape_xml_attr(controller_name)}">\n'
        f'<DataTypes Use="Context">\n{data_types_xml}\n</DataTypes>\n'
        f'</Controller>\n'
        f'</RSLogix5000Content>\n'
    )

    Path(output_path).write_text(xml, encoding="utf-8")


# Returned Project Structures


# Import Export Interfaces
class ImportProject:
    """ "Interface to import an PLC project"""

    @abstractmethod
    def import_project(self) -> RSLogix5000Content:
        # Import Project Interface
        pass


class ExportProject:
    """ "Interface to export an PLC project"""

    @abstractmethod
    def export_project(self, project: RSLogix5000Content):
        # Export Project Interface
        pass


# Concreate examples of importing and exporting projects
@dataclass
class ImportProjectFromFile(ImportProject):
    """Import a Controller from an ACD stored on file"""

    filename: PathLike

    def import_project(self) -> RSLogix5000Content:
        # Import Project Interface
        export = ExportL5x(self.filename)
        try:
            return export.project
        finally:
            export.close()


@dataclass
class ExportProjectToFile(ExportProject):
    """Export a Controller to an ACD file"""

    filename: PathLike

    def export_project(self, project: RSLogix5000Content):
        # Concreate example of exporting a Project Object to an ACD file
        raise NotImplementedError


# Extracting/Compressing files from an ACD file Interfaces
class Extract:
    """Base class for all extract functions"""

    @abstractmethod
    def extract(self):
        # Interface for extracting database files
        pass


class Compress:
    """Base class for all compress functions"""

    @abstractmethod
    def compress(self):
        # Interface for extracting database files
        pass


# Concreate examples of extracting and compressing ACD files
@dataclass
class ExtractAcdDatabase(Extract):
    """Extract database files from a Logix ACD file"""

    filename: PathLike
    output_directory: PathLike

    def extract(self):
        # Implement the extraction of an ACD file
        unzip = Unzip(self.filename)
        unzip.write_files(self.output_directory)


@dataclass
class CompressAcdDatabase(Extract):
    """Compress database files to a Logix ACD file"""

    filename: PathLike
    output_directory: PathLike

    def compress(self):
        # Implement the compressing of an ACD file
        raise NotImplementedError


@dataclass
class ExtractAcdDatabaseRecordsToFiles(ExportProject):
    """Export all ACD databases to a raw database record tree"""

    filename: PathLike
    output_directory: PathLike

    def extract(self):
        # Implement the extraction of an ACD file
        database = AcdDatabase(self.filename, self.output_directory)
        database.extract_to_file()


@dataclass
class DumpCompsRecordsToFile(ExportProject):
    """
    Dump the Comps database to a folder. Each individual record can then be navigated and viewed.

    :param str filename: Filename of ACD file
    :param str output_directory: Location to store the records
    """

    filename: PathLike
    output_directory: PathLike

    def extract(self):
        export = ExportL5x(self.filename)
        with open(
            os.path.join(self.output_directory, "output.log"),
            "w",
        ) as log_file:
            DumpCompsRecords(export._cur, 0).dump(log_file=log_file)


@dataclass
class ConvertAcdToL5x(Extract):
    """Convert an ACD file to an L5X XML file.

    Parses the ACD binary databases (Comps.Dat, SbRegion.Dat, Comments.Dat)
    and serialises the in-memory project model to an L5X-compatible XML file
    that can be imported back into Studio 5000 Logix Designer.

    The output captures controller tags, programs, routines (ladder rungs),
    data types (UDTs), add-on instructions (AOIs), and hardware modules.

    :param PathLike acd_filename: Path to the source .ACD file.
    :param PathLike l5x_filename: Path for the output .L5X file.
    :param bool pretty_print: Pretty-print the XML output (default True).
    """

    acd_filename: PathLike
    l5x_filename: PathLike
    pretty_print: bool = True

    def extract(self):
        project = ImportProjectFromFile(self.acd_filename).import_project()
        raw_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + project.to_xml()
        if self.pretty_print:
            try:
                dom = xml.dom.minidom.parseString(raw_xml.encode("utf-8"))
                output = dom.toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")
                # minidom adds its own XML declaration; strip the duplicate header
                lines = output.splitlines()
                if lines and lines[0].startswith("<?xml"):
                    lines[0] = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                output = "\n".join(lines)
            except Exception:
                output = raw_xml
        else:
            output = raw_xml
        with open(self.l5x_filename, "w", encoding="utf-8") as f:
            f.write(output)
