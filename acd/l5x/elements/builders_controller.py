# Auto-split from the former acd/l5x/elements/builders.py -- see CLAUDE.md's structural-cleanup notes.
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from typing import Dict, List, Union

from loguru import logger as log

from acd.generated.comps.rx_generic import RxGeneric

from .base import L5xElementBuilder
from .builders_common import _build_hex_oid_map
from .builders_datatype import DataTypeBuilder, _apply_dead_member_byte_corrections
from .builders_module import ModuleBuilder
from .builders_routine import AoiBuilder, RoutineBuilder, _lookup_object_description
from .builders_tag import TagBuilder
from .decode import _count_array_elements, _decode_udt_initial_value
from .model import (
    AOI,
    Controller,
    DataType,
    EventInfo,
    Module,
    Program,
    RSLogix5000Content,
    ScheduledProgram,
    Tag,
    Task,
)


def _resolve_hex_oid_chain(path: str, hex_oid_map: Dict[int, str]) -> Union[str, None]:
    """Resolve a chain of one or more `.!HEXOID` references, with NO
    literal array-index anywhere in the path (e.g. `".!02CA91B5.!0794E8EC"`)
    into a dotted member path (e.g. `".Pt15.Data"`), the same
    `hex_oid_map`-based lookup a regular (non-I/O) tag's own equivalent
    chain already resolves through (`TagBuilder.build()`'s `re.sub(r'!([0-9A-F]{8})',
    ...)`, see CLAUDE.md's "Hex-OID resolution" section -- a real,
    already-verified pattern for OTHER tags, e.g. `.!06DC4E61.!0751B500`
    resolving to `.Member1.Member2`).

    `TagBuilder.build()` deliberately skips that resolution for EVERY I/O
    tag (tag name containing `:`), leaving `.!HEXOID`-shaped paths for
    `ControllerBuilder` to resolve instead -- but `ControllerBuilder`'s own
    I/O-specific resolution (see `_resolve_io_tag_comments()` below) only
    ever handled a `.!HEXOID[N]`-shaped path (a literal array index
    physically present in the raw path text, where the hex OID itself is
    never even looked up -- only the bracketed digits matter). A path with
    NO bracket at all -- two (or more) chained `.!HEXOID` references and
    nothing else -- fell through completely unresolved, a real reported
    bug: `db_list_tag_comments()` returned the raw, opaque
    `".!02CA91B5.!0794E8EC"` as the key instead of a real, addressable
    path, for every I/O tag whose comments use this shape.

    This function is the missing piece: apply the SAME `hex_oid_map`
    lookup already built for regular tags to an I/O tag's own chain, on
    the theory (not independently verified at the byte level, but strongly
    suggested by the requested/expected shape matching exactly -- see the
    caller's own comment) that at least some I/O module-defined types DO
    have their own members recorded under `RxTypeMemberCollection` the
    same way a real UDT's do, even though `TagBuilder.build()`'s exclusion
    assumed otherwise for every I/O tag uniformly.

    Returns `None` -- caller leaves the path exactly as unresolved as
    before this fallback existed -- if the path doesn't match the "chain of
    `.!HEXOID` segments and nothing else" shape at all, or if ANY segment
    fails to resolve (a PARTIAL resolution, e.g. one real name and one
    still-opaque hex OID, would be more misleading than an obviously-still-
    unresolved path, not less).
    """
    segments = [s for s in path.split(".") if s]
    if not segments:
        return None
    names = []
    for seg in segments:
        m = re.fullmatch(r"!([0-9A-Fa-f]{8})", seg)
        if not m:
            return None
        oid = int(m.group(1), 16)
        name = hex_oid_map.get(oid)
        if not name:
            return None
        names.append(name)
    return "." + ".".join(names)


def _resolve_io_tag_comments(tags: List[Tag], data_types_map: Dict[str, "DataType"],
                              hex_oid_map: Dict[int, str]) -> None:
    """Resolve every I/O tag's (a tag whose name contains `:`) own comment
    paths from their raw, hex-OID-based storage form into full Studio 5000
    addresses (e.g. `"Remote_Moulder046:1:I.Pt15.Data"`), mutating each
    tag's `_comments` list in place. Extracted out of `ControllerBuilder.build()`
    as its own function so it's independently unit-testable against plain
    `Tag`/`DataType`/`Member` objects, without needing a full synthetic
    Comps.Dat/comments-table setup.

    `data_member`/`array_member` are found via the same narrow, explicitly-
    scoped name heuristic documented at the top of this file ("excludes
    members literally named Fault/Status when guessing which member of an
    I/O module's UDT is 'the data member'") -- the ONE other name-based
    heuristic in this codebase's whole parsing pipeline besides the
    connection-type fallback.
    """
    for tag in tags:
        if ":" not in tag.name:
            continue
        if not tag._comments:
            continue
        dt = data_types_map.get(tag.data_type.upper())
        data_member = None
        if dt:
            for m in dt.members:
                if m.bit_number is None and m.name.upper() not in ("FAULT", "STATUS"):
                    data_member = m
                    break
            if data_member is None:
                for m in dt.members:
                    if m.bit_number is None:
                        data_member = m
                        break
        resolved = []
        for path, text in tag._comments:
            if not path:
                resolved.append((path, text))
            elif path.startswith(".!"):
                inner = path[2:]
                if "[" in inner:
                    # A literal array index is physically present in the
                    # raw path text (type 16/17 bit-level I/O comments) --
                    # find the first array member (dimension > 0) since
                    # these references always target array elements like
                    # Data[N]. The hex OID itself is never even looked up
                    # for this shape -- only the bracketed digits matter.
                    array_member = None
                    if dt:
                        for m in dt.members:
                            if m.bit_number is None and m.dimension > 0:
                                array_member = m
                                break
                    _dm = array_member or data_member
                    if not _dm:
                        resolved.append((path, text))
                        continue
                    if "." in inner and inner.index("[") < inner.rindex("."):
                        hex_oid, bracket_part = inner.split("[", 1)
                        array_idx, bit_part = bracket_part.rsplit(".", 1)
                        array_idx = array_idx.rstrip("]")
                        resolved.append((f"{tag.name}.{_dm.name}[{array_idx}].{bit_part}", text))
                    else:
                        hex_oid, suffix = inner.split("[", 1)
                        suffix = suffix.rstrip("]")
                        resolved.append((f"{tag.name}.{_dm.name}[{suffix}]", text))
                else:
                    # No literal array index anywhere in the path -- try
                    # resolving it as a plain chain of member-name
                    # references instead (see _resolve_hex_oid_chain's own
                    # docstring for why this case exists and its
                    # real-world discovery). Deliberately does NOT need
                    # `_dm` at all -- unlike the bracket-shaped case above,
                    # the chain resolves directly to real member names via
                    # hex_oid_map, with no "guess which member is the data
                    # member" heuristic involved. Falls back to leaving the
                    # path exactly as unresolved as before if this doesn't
                    # pan out, with a warning so an unresolved I/O tag
                    # comment path is visible rather than silently opaque.
                    chain = _resolve_hex_oid_chain(path, hex_oid_map)
                    if chain:
                        resolved.append((f"{tag.name}{chain}", text))
                    else:
                        log.warning(
                            f"Tag {tag.name!r}: could not resolve I/O comment path "
                            f"{path!r} to a real address -- left unresolved"
                        )
                        resolved.append((path, text))
            elif path.startswith("!"):
                if not data_member:
                    resolved.append((path, text))
                    continue
                rest = path[1:]
                if "." in rest:
                    hex_oid, suffix = rest.split(".", 1)
                    if data_member.dimension > 0:
                        resolved.append((f"{tag.name}.{data_member.name}[{suffix}]", text))
                    else:
                        resolved.append((f"{tag.name}.{data_member.name}.{suffix}", text))
                elif "[" in rest:
                    hex_oid, suffix = rest.split("[", 1)
                    suffix = suffix.rstrip("]")
                    resolved.append((f"{tag.name}.{data_member.name}[{suffix}]", text))
                else:
                    resolved.append((path, text))
            elif path.endswith("]"):
                resolved.append((f"{tag.name}[{path}", text))
            elif path.isdigit():
                resolved.append((f"{tag.name}.{path}", text))
            else:
                resolved.append((path, text))
        tag._comments = resolved


@dataclass
class ProgramBuilder(L5xElementBuilder):
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    _redundancy_enabled: bool = field(default=False)
    _hex_oid_map: Union[Dict[int, str], None] = None

    def build(self) -> Program:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        prog_record = bytes(results[0][3])
        r = RxGeneric.from_bytes(prog_record)

        name = results[0][0]

        exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}

        # --- RxRoutineCollection children: needed both for MainRoutineName
        # resolution just below and for building the actual Routine objects
        # further down -- fetched once here and reused for both. ---
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        collection_results = self._cur.fetchall()

        collection_id: Union[int, None] = None
        routine_child_rows: list = []
        if collection_results:
            collection_id = collection_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
                + str(collection_id)
            )
            routine_child_rows = self._cur.fetchall()

        # --- MainRoutineName from ext[0x01]'s own trailing footer ---
        # NOT an object_id pointer -- the previous approach (ext[0x12D] read
        # as a raw routine object_id) never actually fired for any real
        # project checked: 0x12D was never present in any parsed extended
        # record. The real mechanism, found by diffing a real Program's raw
        # comps bytes between two saves of the same project differing by
        # exactly one change (reassigning the Program's own Main Routine
        # designation in Studio 5000 -- exactly 2 bytes changed, isolating
        # this field precisely): ext[0x01] (the same blob already used below
        # for the Disabled flag) has a small, fixed-size trailing footer
        # whose u16 at byte offset `len(ext01) - 8` is a "local reference
        # number" -- NOT the routine's own object_id -- that matches a u16
        # field at raw offset 16 of the designated main routine's OWN comps
        # record. Independently confirmed against a SECOND, unrelated real
        # program (same absolute footer position despite completely
        # different tag/routine content), and confirmed this per-routine
        # "local reference number" is unique across all 37 routines of the
        # real project's largest program (including one, `M100_Landing_Table`,
        # whose own record otherwise fails to fully parse -- see CLAUDE.md's
        # separate, still-open "routine silently dropped" investigation).
        ext01 = exts.get(0x01, b"")
        main_routine_name: Union[str, None] = None
        if len(ext01) >= 8:
            main_local_ref = struct.unpack_from("<H", ext01, len(ext01) - 8)[0]
            if main_local_ref:
                for child in routine_child_rows:
                    child_rec = bytes(child[3])
                    if (
                        len(child_rec) >= 18
                        and struct.unpack_from("<H", child_rec, 16)[0] == main_local_ref
                    ):
                        main_routine_name = child[0]
                        break

        # --- FaultRoutineName ---
        # NOT independently re-verified this session -- this still uses the
        # same kind of "ext[0x066] as a raw object_id pointer" approach that
        # turned out to never fire for MainRoutineName above, so it's likely
        # wrong too, just not yet confirmed either way (every real program
        # checked this session had FaultRoutineName unset, so there was no
        # positive example to diff against). Left as the old, unverified
        # code rather than guessing a fix with no real evidence -- revisit
        # with a real before/after pair (a Program's FaultRoutineName
        # explicitly set, then changed) the same way MainRoutineName was
        # solved above.
        fault_routine_name: Union[str, None] = None
        if 0x066 in exts and len(exts[0x066]) >= 4:
            fault_oid = struct.unpack_from("<I", exts[0x066], 0)[0]
            if fault_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(fault_oid))
                row = self._cur.fetchone()
                fault_routine_name = row[0] if row else None

        # --- Disabled flag from ext[0x01] at offset 0x24 ---
        # A u32 of 0xFFFFFFFF means the program is disabled; 0x00000000 means enabled.
        disabled_flag = (
            struct.unpack_from("<I", ext01, 0x24)[0] != 0
            if len(ext01) >= 0x28
            else False
        )
        disabled = "true" if disabled_flag else "false"

        routines = []
        if collection_id is not None:
            for child in routine_child_rows:
                routine = RoutineBuilder(self._cur, child[1], collection_id).build()
                if routine is not None:
                    routines.append(routine)

        # Get the Program Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one program tag collection")

        tags: List[Tag] = []
        if results:
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
                + str(results[0][1])
                + " AND record_type != 264"
            )
            # record_type=264 marks internal AOI default-value bookkeeping entries
            # (comp_name "__CONTAINER"/"__DEFVAL_XXXXXXXX") under RxTagCollection --
            # verified against a real project: all 12 record_type=264 entries in the
            # entire project are these, and none appear in that project's own Studio
            # 5000 L5X export as a <Tag>. Real tags use several other record_type
            # values (256 plain, 512 e.g. produced/consumed, 1280/1288 I/O -- already
            # excluded separately via Tag._l5x_exclude), so this must be a narrow
            # record_type exclusion, not a blanket "only 256" filter.
            results = self._cur.fetchall()
            for result in results:
                tag = TagBuilder(self._cur, result[1], self._hex_oid_map).build()
                tag._data_types_map = self._data_types_map
                # Decode UDT initial values now that data_types_map is available.
                # Multi-dimensional arrays are supported: _decode_udt_initial_value
                # iterates the flat (row-major) element count, and Tag.to_xml's
                # UDT-array branch / _udt_array_to_xml re-derive the per-dimension
                # [i,j,k] indices from that same flat ordering when rendering.
                if tag._initial_value is None and tag._data_table_instance:
                    n = _count_array_elements(tag.dimensions)
                    udt_val = _decode_udt_initial_value(
                        self._cur, tag._data_table_instance, tag.data_type, n,
                        self._data_types_map, is_array=tag.dimensions is not None,
                    )
                    if udt_val is not None:
                        tag._initial_value = udt_val
                tags.append(tag)

        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        # SynchronizeRedundancyDataAfterExecution: present only for redundant controllers.
        # The binary does not expose a per-program flag for this attribute — it is implicit
        # for all programs in a redundant controller project.
        sync_redundancy = "true" if self._redundancy_enabled else None

        # A Program can have its own whole-program Description, found the same way as a
        # Routine's (see RoutineBuilder.build()/_lookup_object_description) -- verified
        # against a real project ("_2_TONGLOADER" -> "Tong Loader Master Control").
        description = _lookup_object_description(self._cur, r, prog_record)

        return Program(name, name, "false", main_routine_name, fault_routine_name,
                       disabled, sync_redundancy, "false", tags, routines, description)


_TASK_TYPE_MAP = {1: "EVENT", 2: "PERIODIC", 4: "CONTINUOUS"}

@dataclass
class TaskBuilder(L5xElementBuilder):
    def build(self, comment_id_to_program: Dict[int, str]) -> Task:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name, record = row[0], row[1]

        # All task config fields live within ext[0x01], accessed via absolute BLOB offsets.
        # These offsets were reverse-engineered from a real sample project.
        rate_us = struct.unpack_from("<I", record, 0x106C)[0]
        type_val = struct.unpack_from("<H", record, 0x10F6)[0]
        priority = struct.unpack_from("<H", record, 0x10F8)[0]
        watchdog_us = struct.unpack_from("<I", record, 0x110A)[0]
        disable_update = record[0x112E]

        task_type = _TASK_TYPE_MAP.get(type_val, "PERIODIC")
        rate_str = str(rate_us // 1000) if task_type != "CONTINUOUS" else None

        # Scheduled programs: ext[0x01] value starts at BLOB offset 0x5A.
        # Format: u16 count followed by N u32 comment_ids. The count is
        # bounds-checked against the record: some firmware versions lay the
        # record out differently and a garbage count must not read past the
        # end of the buffer.
        prog_count = struct.unpack_from("<H", record, 0x5A)[0]
        scheduled_programs = []
        for i in range(prog_count):
            offset = 0x5A + 2 + i * 4
            if offset + 4 > len(record):
                break
            cid = struct.unpack_from("<I", record, offset)[0]
            prog_name = comment_id_to_program.get(cid)
            if prog_name:
                scheduled_programs.append(ScheduledProgram(prog_name, prog_name))

        event_info = None
        if task_type == "EVENT":
            event_info = EventInfo("EventInfo", "EVENT Instruction Only", "false")

        return Task(
            name,
            name,
            task_type,
            rate_str,
            str(priority),
            str(watchdog_us // 1000),
            "true" if disable_update else "false",
            "false",
            event_info,
            scheduled_programs,
        )

@dataclass
class ControllerBuilder(L5xElementBuilder):
    def build(self) -> Controller:
        # A real project can have OTHER root-level (parent_id=0) objects besides
        # the controller itself -- e.g. "Recycling Bin"/"DataSet Data" administrative
        # objects, seen in a real project, both with record_type=0 so they never
        # collide with this query. But a real, freshly re-saved project turned up a
        # second parent_id=0 row that DOES share the controller's own record_type
        # (256): object_id=1, comp_name='' (empty), no children, its raw record all
        # zero bytes except a handful of 0xFFFFFFFF sentinel values -- isolated
        # scratch/reserved data, not a second controller (a real controller always
        # has a real project name; Studio 5000 has no concept of an unnamed one).
        # Filtering to a non-empty comp_name excludes this without needing to
        # otherwise distinguish it structurally.
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type, record FROM comps "
            "WHERE parent_id=0 AND record_type=256 AND comp_name != ''"
        )
        results = self._cur.fetchall()
        if len(results) != 1:
            raise Exception("Does not contain exactly one root controller node")

        r = RxGeneric.from_bytes(results[0][4])
        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        # A Controller can have its own whole-project Description, found the same way
        # as a Routine's/Program's (see RoutineBuilder.build()/_lookup_object_description)
        # -- verified against a real project's own multi-line project description.
        controller_description = _lookup_object_description(self._cur, r, results[0][4])

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        def _decode_utf16(key):
            raw = extended_records.get(key)
            if raw is None or len(raw) < 2:
                return ""
            return bytes(raw[:-2]).decode("utf-16", errors="replace")

        sfc_execution_control = _decode_utf16(0x6F)
        sfc_restart_position = _decode_utf16(0x70)
        sfc_last_scan = _decode_utf16(0x71)

        # CommPath prefix (key 0x06A): may be in kaitai extended_records (usually empty),
        # or in the LastAttributeRecord appended after the counted records (value length
        # is stored as len_value where actual data = len_value - 4 bytes, UTF-16-LE).
        _comm_path_prefix: Union[str, None] = None
        if 0x06A in extended_records:
            _cp_raw = extended_records[0x06A]
            _cp_str = _cp_raw.decode("utf-16-le", errors="replace").rstrip("\x00")
            if _cp_str:
                _comm_path_prefix = _cp_str
        else:
            # LastAttributeRecord tail: located after the (count_record - 1) parsed records.
            # Header layout: parent_id(4) + unique_tag_id(4) + record_format_version(2) +
            #   cip_type(2) + comment_id(2) = 14 bytes, then main_record(60), then
            #   len_record(4) + count_record(4) = 82 bytes total before first AttributeRecord.
            _raw_record = bytes(results[0][4])
            _rec_offset = 82
            for _er in r.extended_records:
                _rec_offset += 4 + 4 + len(bytes(_er.value))
            _tail = _raw_record[_rec_offset:]
            if len(_tail) >= 8:
                _last_attr_id = struct.unpack_from("<I", _tail, 0)[0]
                _last_len_value = struct.unpack_from("<I", _tail, 4)[0]
                if _last_attr_id == 0x06A and _last_len_value >= 4:
                    _actual_len = _last_len_value - 4
                    if len(_tail) >= 8 + _actual_len and _actual_len > 0:
                        _cp_val = _tail[8: 8 + _actual_len]
                        _cp_str = _cp_val.decode("utf-16-le", errors="replace").rstrip("\x00")
                        if _cp_str:
                            _comm_path_prefix = _cp_str

        if 0x75 in extended_records:
            sn_raw = hex(struct.unpack("<I", extended_records[0x75])[0])[2:].zfill(8)
            project_sn = f"16#{sn_raw[:4]}_{sn_raw[4:]}"
        else:
            project_sn = "Unknown"

        raw_modified_date = struct.unpack("<Q", extended_records[0x66])[0] / 10000000
        last_modified_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_modified_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        raw_created_date = struct.unpack("<Q", extended_records[0x65])[0] / 10000000
        project_creation_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_created_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        # MajorRev and MinorRev: derived later from the root controller module (see below)

        # MajorFaultProgram from ext[0x068] OID → comp_name lookup
        major_fault_program: Union[str, None] = None
        if 0x068 in extended_records and len(extended_records[0x068]) >= 4:
            mfp_oid = struct.unpack_from("<I", extended_records[0x068])[0]
            if mfp_oid and mfp_oid != 0xFFFFFFFF:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(mfp_oid))
                mfp_row = self._cur.fetchone()
                major_fault_program = mfp_row[0] if mfp_row else None

        # RedundancyEnabled: controller extended record 0x001, byte offset 0x0E.
        # This byte is 0x01 for redundant controllers (e.g. 1756-L85E in redundancy mode)
        # and 0x00 for non-redundant controllers.
        _ctrl_ext001 = extended_records.get(0x001, b"")
        redundancy_enabled: bool = bool(_ctrl_ext001[0x0E]) if len(_ctrl_ext001) > 0x0E else False

        self._object_id = results[0][1]
        controller_name = results[0][0]

        # Get the data types
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxDataTypeCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller data type collection")

        _data_type_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_data_type_id)
        )
        results = self._cur.fetchall()

        data_types: List[DataType] = []
        # all_data_types_map includes ProductDefined types (excluded from L5X output but
        # needed for generating Decorated XML for tags that reference those types).
        all_data_types_map: Dict[str, DataType] = {}
        for result in results:
            _data_type_object_id = result[1]
            dt = DataTypeBuilder(self._cur, _data_type_object_id).build()
            all_data_types_map[dt.name.upper()] = dt
            if dt.cls == "User":
                data_types.append(dt)

        _apply_dead_member_byte_corrections(all_data_types_map)

        # data_types_map: case-insensitive name → DataType for all types (User + ProductDefined).
        # Used by Tag.to_xml() when generating Decorated XML.
        data_types_map: Dict[str, DataType] = all_data_types_map

        # Get the Controller Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller tag collection")
        _tag_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_tag_collection_object_id)
            + " AND record_type != 264"
        )
        # record_type=264 marks internal AOI default-value bookkeeping entries -- see
        # the identical filter (and full rationale) in ProgramBuilder's tag query above.
        results = self._cur.fetchall()

        # Built ONCE here and threaded through every TagBuilder/ProgramBuilder
        # call below -- it's an identical, project-wide map (see
        # _build_hex_oid_map's own docstring), not specific to any one tag.
        # Previously rebuilt from scratch inside TagBuilder.build() for every
        # single tag (thousands of times project-wide), re-running the same
        # full-project SQL join each time -- the dominant cost of load_acd()
        # for a real project (measured: ~82s of a 90s total load).
        hex_oid_map = _build_hex_oid_map(self._cur)

        tags: List[Tag] = []
        for result in results:
            _tag_object_id = result[1]
            tag = TagBuilder(self._cur, _tag_object_id, hex_oid_map).build()
            tag._data_types_map = data_types_map
            # Decode UDT initial values now that data_types_map is available.
            # Multi-dimensional arrays are supported: _decode_udt_initial_value
            # iterates the flat (row-major) element count, and Tag.to_xml's
            # UDT-array branch / _udt_array_to_xml re-derive the per-dimension
            # [i,j,k] indices from that same flat ordering when rendering.
            if tag._initial_value is None and tag._data_table_instance:
                n = _count_array_elements(tag.dimensions)
                udt_val = _decode_udt_initial_value(
                    self._cur, tag._data_table_instance, tag.data_type, n,
                    data_types_map, is_array=tag.dimensions is not None,
                )
                if udt_val is not None:
                    tag._initial_value = udt_val
            # Exclude internal placeholder/auto-generated tags:
            #   "$..."      -- hash-named objects (not a valid Rockwell tag-name start
            #                  character; real tags always start with a letter or "_").
            #   "__l0"/"__CLONE" -- known Rockwell-internal AOI clone/runtime tag prefixes
            #                  (same set Tag._l5x_exclude uses). A bare "__" prefix is a
            #                  legal user tag name (e.g. "__MyFlag") and must NOT be
            #                  excluded here, or a legitimately-named user tag would be
            #                  silently dropped from the object model entirely.
            if (
                tag.data_type
                and not tag.name.startswith("$")
                and not tag.name.startswith("__l0")
                and not tag.name.startswith("__CLONE")
            ):
                tags.append(tag)

        # Resolve comment paths to full Studio 5000 addresses for I/O tags
        _resolve_io_tag_comments(tags, data_types_map, hex_oid_map)

        # Get the Program Collection and get the programs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxProgramCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller program collection")

        _program_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_program_collection_object_id)
            + " AND record_type=256"
        )
        # record_type=256 matches every real Program seen in every local fixture and
        # the Task filter above; a real project was found with one extra program-name
        # comps record with record_type=512 under RxProgramCollection ("LuciGradeScan")
        # that does not appear in that project's own Studio 5000 L5X export at all --
        # excluded here the same way, rather than emitting a phantom <Program> element.
        results = self._cur.fetchall()
        programs: List[Program] = []
        for result in results:
            _program_object_id = result[1]
            programs.append(
                ProgramBuilder(
                    self._cur, _program_object_id, data_types_map, redundancy_enabled, hex_oid_map
                ).build()
            )

        # Build comment_id → program name map for task scheduled-program resolution.
        # comment_id is a u16 at BLOB offset 0x0C in each program's RxGeneric record.
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE parent_id=" + str(_program_collection_object_id)
        )
        comment_id_to_program: Dict[int, str] = {
            struct.unpack_from("<H", rec, 0x0C)[0]: pname
            for pname, rec in self._cur.fetchall()
        }

        # Get the Task Collection and build Tasks
        self._cur.execute(
            "SELECT comp_name, object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTaskCollection'"
        )
        task_coll_results = self._cur.fetchall()
        tasks: List[Task] = []
        if task_coll_results:
            _task_collection_object_id = task_coll_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id FROM comps WHERE parent_id="
                + str(_task_collection_object_id)
                + " AND record_type=256"
            )
            for task_result in self._cur.fetchall():
                try:
                    tasks.append(
                        TaskBuilder(self._cur, task_result[1]).build(comment_id_to_program)
                    )
                except Exception as e:
                    # Task config offsets are firmware-version dependent; a
                    # task that can't decode shouldn't abort the whole import.
                    log.warning(f"Skipping undecodable task '{task_result[0]}': {e!r}")

        # Get the AOI Collection and get the AOIs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxUDIDefinitionCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one AOI collection")
        _aoi_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_aoi_collection_object_id)
            + " AND record_type=256"
        )
        results = self._cur.fetchall()
        aois: List[AOI] = []
        for result in results:
            _aoi_object_id = result[1]
            aois.append(AoiBuilder(self._cur, _aoi_object_id).build())

        # Strip spurious "comments" that are really nested-AOI-instance InOut
        # parameter binding metadata, not user-authored descriptions.
        #
        # When a UDT member's DataType is itself an AOI (e.g. a "VFD" member of
        # type "MyAOI_PowerFlex"), Rockwell needs to record which of that
        # AOI's InOut parameters is used, and stores it in Comments.Dat using
        # the exact same (parent, scope_id) keyed record shape as a real
        # per-element comment: ref resolves to the whole member (e.g. ".VFD"),
        # text is literally the AOI's own InOut parameter name (e.g.
        # "Ethernet_Module"). Verified against a real project: this exact
        # shape recurs identically across every tag instance of several
        # different UDTs (all containing an AOI-typed member), always with
        # the same text for a given member/AOI pair regardless of the tag's
        # own identity -- and it never appears in the real L5X's per-tag
        # <Comments> block. A genuine user comment on a whole AOI-typed member
        # would not coincidentally equal one of that AOI's own parameter names
        # verbatim, so this is a safe, narrow filter.
        _aoi_param_names = {
            aoi.name.upper(): {p.name for p in aoi.parameters} for aoi in aois
        }
        if _aoi_param_names:
            def _strip_aoi_binding_comments(tag_list: List[Tag]) -> None:
                for _t in tag_list:
                    if not _t.data_type or not _t._comments:
                        continue
                    _dt = all_data_types_map.get(_t.data_type.upper())
                    if not _dt:
                        continue
                    _member_types = {m.name: m.data_type for m in _dt.members}
                    _kept = []
                    for _ref, _text in _t._comments:
                        if _ref.startswith(_t.name):
                            _suffix = _ref[len(_t.name):]
                            if _suffix.startswith(".") and "." not in _suffix[1:] and "[" not in _suffix:
                                _mname = _suffix[1:]
                                _mtype = _member_types.get(_mname)
                                if _mtype:
                                    _param_names = _aoi_param_names.get(_mtype.upper())
                                    if _param_names and _text in _param_names:
                                        continue
                        _kept.append((_ref, _text))
                    _t._comments = _kept

            _strip_aoi_binding_comments(tags)
            for _p in programs:
                _strip_aoi_binding_comments(_p.tags)

        # Get the Module (IO) Collection and build all Module elements.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxMapDeviceCollection'"
        )
        row = self._cur.fetchone()
        if row is None:
            modules: List[Module] = []
        else:
            coll_oid = row[0]
            self._cur.execute(
                "SELECT comp_name, object_id, record FROM comps WHERE parent_id="
                + str(coll_oid)
                + " AND record_type=256"
                + " ORDER BY seq_number"
            )
            # record_type=256 matches every real Module seen in every local fixture and
            # the Program/Task filter elsewhere in this method; a real project was found
            # with three extra module-name comps records with record_type=512 that do
            # not appear in that project's own Studio 5000 L5X export at all --
            # excluded here the same way, rather than emitting phantom <Module>
            # elements.
            mod_rows = self._cur.fetchall()

            # First pass: build modid→name map so child modules can resolve their parent name.
            from acd.generated.comps.rx_generic import RxGeneric as _RxG
            modid_to_name: Dict[int, str] = {}
            for db_name, mod_oid, mod_rec in mod_rows:
                display_name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name
                try:
                    r = _RxG.from_bytes(bytes(mod_rec))
                    if r.cip_type == 0x69:
                        exts = {er.attribute_id: bytes(er.value) for er in r.extended_records}
                        e1 = exts.get(0x001, b"")
                        if len(e1) >= 0x30:
                            modid = struct.unpack("<I", e1[0x2C:0x30])[0]
                            modid_to_name[modid] = display_name
                except Exception:
                    pass

            # Second pass: build Module objects.
            modules = []
            for _, mod_oid, _ in mod_rows:
                modules.append(
                    ModuleBuilder(self._cur, mod_oid, modid_to_name).build()
                )

            # Third pass: compute (parent_name, parent_port_id) → child count,
            # then inject the relevant sub-dict into each Module as _port_child_counts.
            child_counts: Dict[tuple, int] = {}
            for m in modules:
                k = (m.parent_module, m.parent_mod_port_id)
                child_counts[k] = child_counts.get(k, 0) + 1
            for m in modules:
                m._port_child_counts = {
                    port_id: child_counts.get((m.name, port_id), 0)
                    for port_id in range(1, 20)
                    if (m.name, port_id) in child_counts
                }

        # ProcessorType is the CatalogNumber of the root controller module (the one
        # whose parent is itself, i.e. MajorFault="true").
        processor_type = next(
            (m.catalog_number for m in modules if m.major_fault == "true" and m.catalog_number),
            None,
        )

        # MajorRev and MinorRev come from the firmware version of the Local (backplane
        # controller) module, stored in its ext[0x01] bytes [0x08] and [0x09].
        local_module = next(
            (m for m in modules if m.name == "Local"),
            next((m for m in modules if m.major_fault == "true"), None),
        )
        if local_module is not None:
            major_rev = str(local_module.major)
            minor_rev = str(local_module.minor)
        else:
            major_rev = "0"
            minor_rev = "0"

        # CommPath: combine the stored path prefix (ends with "\") with the controller
        # module's backplane slot number (e.g. "EthernetModule\192.168.1.10\Backplane\4").
        comm_path: Union[str, None] = None
        if _comm_path_prefix is not None:
            _ctrl_slot = next(
                (m._slot for m in modules if m.major_fault == "true"), None
            )
            if _ctrl_slot is not None:
                comm_path = _comm_path_prefix + str(_ctrl_slot)

        return Controller(
            controller_name,
            "Target",
            controller_name,
            processor_type,
            major_rev,
            minor_rev,
            major_fault_program,
            project_creation_date,
            last_modified_date,
            sfc_execution_control,
            sfc_restart_position,
            sfc_last_scan,
            comm_path,
            project_sn,
            "false",        # MatchProjectToController
            "false",        # CanUseRPIFromProducer
            "0",            # InhibitAutomaticFirmwareUpdate
            "EnabledWithAppend",  # PassThroughConfiguration
            "true",         # DownloadProjectDocumentationAndExtendedProperties
            "true",         # DownloadProjectCustomProperties
            "false",        # ReportMinorOverflow
            "false",        # AutoDiagsEnabled
            "false",        # WebServerEnabled
            data_types,
            modules,
            tags,
            programs,
            tasks,
            aois,
            redundancy_enabled,
            controller_description,
            data_types_map,
        )

@dataclass
class ProjectBuilder:
    quick_info_filename: PathLike

    def build(self) -> RSLogix5000Content:
        element = ET.parse(self.quick_info_filename)
        rslogix_content_element = element.find(".")
        if rslogix_content_element is not None:
            target_name = rslogix_content_element.attrib["Name"]

        schema_version_element = element.find("SchemaVersion")
        if schema_version_element is not None:
            schema_version_major = schema_version_element.attrib["Major"]
            schema_version_minor = schema_version_element.attrib["Minor"]
            schema_revision = f"{schema_version_major}.{schema_version_minor}"
        else:
            schema_revision = "1.0"

        # SWVersion reflects the Studio 5000 application version (e.g. "RSLogix 5000 v35.04"),
        # which is what RSLogix5000Content SoftwareRevision represents.  DeviceIdentity
        # MajorRevision/MinorRevision is the controller firmware version — a different value.
        sw_version_element = element.find("SWVersion")
        if sw_version_element is not None:
            sw_version_string = sw_version_element.attrib.get("String", "")
            # Extract the version number from the trailing "vXX.YY" portion.
            match = re.search(r"v(\d+\.\d+)$", sw_version_string.strip())
            if match:
                software_revision = match.group(1)
            else:
                # Unexpected format — fall back to DeviceIdentity firmware version.
                device_identity = element.find("DeviceIdentity")
                if device_identity is not None:
                    software_revision = (
                        f"{device_identity.attrib['MajorRevision']}"
                        f".{device_identity.attrib['MinorRevision']}"
                    )
                else:
                    software_revision = "33.01"
        else:
            # No SWVersion element — fall back to DeviceIdentity firmware version.
            device_identity = element.find("DeviceIdentity")
            if device_identity is not None:
                software_revision = (
                    f"{device_identity.attrib['MajorRevision']}"
                    f".{device_identity.attrib['MinorRevision']}"
                )
            else:
                software_revision = "33.01"

        target_type = "Controller"
        contains_context = "false"
        now = datetime.now()
        export_date = now.strftime("%a %b %d %H:%M:%S %Y")
        export_options = (
            "NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans"
        )
        return RSLogix5000Content(
            target_name,
            None,
            schema_revision,
            software_revision,
            target_name,
            target_type,
            contains_context,
            export_date,
            export_options,
        )

@dataclass
class DumpCompsRecords(L5xElementBuilder):
    base_directory: PathLike = Path("dump")

    def dump(self, parent_id: int = 0, log_file=None):
        self._cur.execute(
            f"SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id={parent_id}"
        )
        results = self._cur.fetchall()

        for result in results:
            object_id = result[1]
            name = result[0]
            record = result[4]
            # Some comp names carry characters that are invalid in Windows paths
            # (e.g. AB module DataType names like "CHANNEL_DI_TIMESTAMP:O:0").
            # Sanitize only for filesystem use; log_file output keeps the original name.
            safe_name = re.sub(r'[<>:"|?*]', "_", name)
            new_path = Path(os.path.join(self.base_directory, safe_name))
            if os.path.exists(os.path.join(new_path)):
                shutil.rmtree(os.path.join(new_path))
            if not os.path.exists(os.path.join(new_path)):
                os.makedirs(new_path)
            with open(Path(os.path.join(new_path, safe_name + ".dat")), "wb") as file:
                log_file.write(
                    f"Class - {struct.unpack_from('<H', result[4], 0xA)[0]} Instance {struct.unpack_from('<H', result[4], 0xC)[0]}- {str(new_path) + '/' + name}\n"
                )
                file.write(record)

            DumpCompsRecords(self._cur, object_id, new_path).dump(object_id, log_file)
