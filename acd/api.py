import os
import re
import tempfile
import shutil
import xml.dom.minidom
from abc import abstractmethod
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Dict

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
    DumpCompsRecords,
    RSLogix5000Content,
    Routine,
    _escape_xml_attr,
    _multiline_xml_text,
)


# Clean top-level API

def load_acd(path, temp_dir: str = None) -> RSLogix5000Content:
    """Load an ACD file into a Python object model.

    Args:
        path: Path to the .ACD file.
        temp_dir: Directory for SQLite and extracted files.  A temporary
            directory is created and cleaned up automatically if omitted.

    Returns:
        RSLogix5000Content with a fully populated controller object tree.
        The project also carries _raw_files / _file_order / _footer_unknown
        for use by save_acd().
    """
    cleanup = temp_dir is None
    if cleanup:
        temp_dir = tempfile.mkdtemp(prefix="acd_load_")
    try:
        exporter = ExportL5x(str(path), temp_dir)
        return exporter.project
    finally:
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
    """
    import re
    token_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    names = set()
    for text in rung_texts:
        if not text:
            continue
        names.update(token_re.findall(text))
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

    Args:
        project: The loaded project (from load_acd()) that owns `routine`
            -- used to find which Program contains it, resolve referenced
            tags/data types, and source SoftwareRevision/SchemaRevision.
        routine: The Routine object to export (e.g.
            project.controller.programs[0].routines[0]). Edit its `.rungs`
            (append new rung text, or edit existing entries) and/or
            `._rung_comments` (dict of {rung_index: comment_text}) before
            calling this to include those changes in the export.
        output_path: Destination .L5X file path.
        owner: Optional "Owner" attribute value (the registered Studio 5000
            license owner, e.g. "MyCompany, MyCompany" -- a real export
            included this attribute, but it's not clear whether Studio 5000
            requires it to import; omitted entirely if not supplied).

    Raises:
        ValueError: if `routine` isn't found in any program of `project`.

    Example:
        project = load_acd("MyController.ACD")
        routine = project.controller.programs[0].routines[0]
        routine.rungs.append("XIC(NewTag)OTE(AnotherTag);")
        routine._rung_comments[0] = "Explains what this rung does"
        export_routine(project, routine, "MyRoutine_export.L5X")
        # Then, in Studio 5000: right-click the Routines folder ->
        # Import Routine... -> select MyRoutine_export.L5X
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

    referenced_names = set(_referenced_tag_names(routine.rungs))

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
    # full definition -- confirmed against the real export.
    referenced_modules = _referenced_modules(routine.rungs, project)
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
    referenced_routines = _referenced_called_routines(routine.rungs, program)
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
