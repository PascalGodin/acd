# Auto-split from the former acd/l5x/elements/builders.py -- see CLAUDE.md's structural-cleanup notes.
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlite3 import Cursor
from typing import Dict, List, Tuple, Union

from loguru import logger as log

from acd.generated.comps.rx_generic import RxGeneric

from .base import L5xElementBuilder
from .builders_common import external_access_enum, radix_enum
from .builders_tag import _aoi_tag_data_type, _aoi_tag_usage_flags
from .model import AOI, LocalTag, Parameter, Routine


@dataclass
class ParameterBuilder(L5xElementBuilder):
    """Build a Parameter from an AOI RxTagCollection child record."""

    def build(self) -> Parameter:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions (array size) at raw record offset 0x1A as u32; 0 means scalar.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts: Dict[int, bytes] = {
                er.attribute_id: bytes(er.value) for er in r.extended_records
            }
        except Exception:
            return Parameter(name, name, "Base", data_type, "Input", None, "false", "false", "Read/Write", None, dimensions)

        ext01 = exts.get(0x01, b"")
        flags = _aoi_tag_usage_flags(ext01)

        usage_bits = flags & 0x0C
        if usage_bits == 0x04:
            usage = "Input"
        elif usage_bits == 0x08:
            usage = "Output"
        else:
            usage = "InOut"

        required = "true" if (flags & 0x20) else "false"
        visible = "true" if (flags & 0x40) else "false"

        # ExternalAccess (u16 at ext01[0x21E])
        # MESSAGE-type InOut parameters don't carry Constant in L5X; all others do.
        if usage == "InOut":
            external_access = None
            constant: Union[str, None] = None if data_type == "MESSAGE" else "false"
        elif len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
            constant = None
        else:
            external_access = "Read/Write"
            constant = None

        # Radix (high nibble of ext01[0x20F]).  InOut params for complex/UDT types
        # omit Radix; InOut params for scalar/array types (BOOL, INT, DINT, REAL, etc.)
        # still carry Radix in the golden L5X, so include it whenever the radix index
        # is non-zero regardless of Usage.
        if not data_type or len(ext01) <= 0x20F:
            radix: Union[str, None] = None
        else:
            radix_idx = ext01[0x20F] >> 4
            radix = radix_enum(radix_idx) if radix_idx != 0 else None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific parameter description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND member_ref=? LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return Parameter(
            name,
            name,
            "Base",
            data_type,
            usage,
            radix,
            required,
            visible,
            external_access,
            constant,
            dimensions,
            description,
        )

@dataclass
class LocalTagBuilder(L5xElementBuilder):
    """Build a LocalTag from an AOI RxTagCollection child record."""

    def build(self) -> LocalTag:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions at raw record offset 0x1A.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts: Dict[int, bytes] = {
                er.attribute_id: bytes(er.value) for er in r.extended_records
            }
        except Exception:
            return LocalTag(name, name, data_type, dimensions, None, "Read/Write")

        ext01 = exts.get(0x01, b"")
        if len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
        else:
            external_access = "Read/Write"

        if len(ext01) > 0x20F:
            radix_idx = ext01[0x20F] >> 4
            radix: Union[str, None] = radix_enum(radix_idx) if radix_idx != 0 else None
        else:
            radix = None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific local tag description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND member_ref=? LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return LocalTag(name, name, data_type, dimensions, radix, external_access, description)

def routine_type_enum(idx: int) -> str:
    if idx == 0:
        return "TypeLess"
    if idx == 1:
        return "RLL"
    if idx == 2:
        return "FBD"
    if idx == 3:
        return "SFC"
    if idx == 4:
        return "ST"
    if idx == 5:
        return "External"
    if idx == 6:
        return "Encrypted"
    return "Typeless"

@dataclass
class RoutineBuilder(L5xElementBuilder):
    def build(self) -> Union[Routine, None]:
        """Build a Routine, or None if this comps record is a deleted/placeholder
        object rather than a real routine.

        A real project was found with several extra RxRoutineCollection children
        (under multiple different programs) that don't appear in that project's
        own Studio 5000 L5X export at all. Their record_type value varies
        inconsistently (515, 512, 0 -- not one consistent marker, unlike the
        clean record_type discriminator used for Program/Module), but every one
        of them shares the same real signal: either the record is too short/
        malformed for RxGeneric to parse at all, or it parses fine but resolves
        to routine_type_enum(0) == "TypeLess" -- a genuine CIP enum value
        Rockwell itself uses to mark a deleted/placeholder routine, not a
        parsing artifact. Returning None here (and filtering it out at both
        call sites) matches Studio 5000's own behavior of omitting these
        entirely rather than emitting a phantom <Routine Type=""/>.
        """
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return None

        record = results[0][3]
        name = results[0][0]
        routine_type = routine_type_enum(
            struct.unpack_from("<H", r.record_buffer, 0x30)[0]
        )
        if routine_type == "TypeLess":
            return None

        self._cur.execute(
            "SELECT rm.object_id, r.rung FROM region_map rm "
            "LEFT JOIN rungs r ON r.object_id = rm.object_id "
            "WHERE rm.parent_id=" + str(self._object_id) + " ORDER BY rm.unknown"
        )
        rows = [(row[0], row[1]) for row in self._cur.fetchall() if row[1] is not None]
        rung_ids = [row[0] for row in rows]
        rungs = [row[1] for row in rows]

        # Recover any rung whose Region Map entry has gone missing entirely (a
        # real, observed data-loss case on a real V38.02 project -- NOT a
        # parsing gap: the rung's own text decodes fine from SbRegion.Dat, and
        # RegnLink.Dat still carries an intact, correctly-typed
        # (routine, own_rung, next_rung) link record for it, even though
        # Region Map's own entry for that exact rung is simply gone). Verified
        # against two real cases: a rung unchanged since an earlier save (its
        # RegnLink.Dat record predates the save that dropped its Region Map
        # entry) and a rung created after that earlier save (whose Region Map
        # entry seemingly never got written at all) -- both fully recovered,
        # including correct position, from RegnLink.Dat alone. See CLAUDE.md
        # "Region Map entries can go missing independently of the format".
        self._cur.execute(
            "SELECT own_rung, next_rung FROM regnlink_chain WHERE routine_id=" + str(self._object_id)
        )
        chain_by_own: Dict[int, int] = {}
        next_to_own: Dict[int, int] = {}
        for own_rung, next_rung in self._cur.fetchall():
            chain_by_own[own_rung] = next_rung
            next_to_own[next_rung] = own_rung
        known_ids = set(rung_ids)
        missing = [oid for oid in chain_by_own if oid not in known_ids]
        recovered = []
        for oid in missing:
            self._cur.execute("SELECT rung FROM rungs WHERE object_id=?", (oid,))
            row = self._cur.fetchone()
            if row is None or row[0] is None:
                continue  # text itself is gone too -- nothing recoverable
            text = row[0]
            next_rung = chain_by_own.get(oid)
            pred_rung = next_to_own.get(oid)
            if next_rung in known_ids:
                idx = rung_ids.index(next_rung)
            elif pred_rung in known_ids:
                idx = rung_ids.index(pred_rung) + 1
            else:
                idx = len(rung_ids)  # no chain neighbor recovered either -- append
            rung_ids.insert(idx, oid)
            rungs.insert(idx, text)
            known_ids.add(oid)
            recovered.append(oid)
        if recovered:
            log.warning(
                f"Routine {self._object_id}: recovered {len(recovered)} rung(s) missing from "
                f"Region Map via RegnLink.Dat's own routine/rung link records: {recovered}"
            )

        # Resolve &hexid: placeholders (object ID references) to comp names.
        # The ACD binary stores tag references as &XXXXXXXX: where XXXXXXXX is the
        # object_id in hex. Batch-resolve all unique IDs to avoid per-rung queries.
        import re as _re
        all_hex = set(_re.findall(r'&([0-9a-f]{8}):', " ".join(r for r in rungs if r)))
        if all_hex:
            id_to_name: Dict[int, str] = {}
            for hex_id in all_hex:
                oid = int(hex_id, 16)
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=?", (oid,))
                row2 = self._cur.fetchone()
                if row2:
                    id_to_name[hex_id] = row2[0]
            if id_to_name:
                def _resolve(rung: str) -> str:
                    return _re.sub(
                        r'&([0-9a-f]{8}):',
                        lambda m: (id_to_name[m.group(1)] + ":") if m.group(1) in id_to_name else m.group(0),
                        rung,
                    )
                rungs = [_resolve(r) if r else r for r in rungs]

        # Fetch rung-level comments from the comments table.
        # Rung comments are stored under the routine's own comment parent key
        # (comment_id * 0x10000 + cip_type), as AsciiRecord (record_type=1) entries
        # where rung_content != 0 (distinguishes user rung comments from internal
        # metadata strings like FBDRoutineDescription which have rung_content=0).
        #
        # scope_id (2-byte discriminator at absolute byte offset 16 of the
        # routine's own raw record, same field used for tag comments) MUST also
        # be matched -- verified against a real project that many *unrelated*
        # routines share the exact same (comment_id, cip_type) parent key, so
        # without this filter a routine can silently pick up another routine's
        # rung comments entirely (e.g. "Flasher" showing a comment that was
        # actually written for a completely different tally/sorting routine).
        #
        # Which specific rung a comment belongs to is resolved via rung_content's
        # upper 16 bits, looked up against the `regnlink` table (see
        # populate_regnlink() in export_l5x.py for the full mechanism) -- NOT via
        # the comment's own object_id field, which was previously (wrongly)
        # assumed to be a 1-based rung index ("object_id - 1 = rung_index"). That
        # assumption only ever worked by coincidence for a routine with exactly
        # one rung comment (object_id is constant -- always 1 -- across every
        # rung comment in a routine, verified against a real routine with 26
        # rung comments, all object_id=1): with only one candidate there's no
        # wrong-slot collision to expose the bug. Verified byte-exact against a
        # real Studio 5000 "Export Routine" ground truth for a fresh routine's
        # comments; for a long-lived, heavily-edited real routine, resolution
        # can still drift for comments predating a later rung insertion/deletion
        # elsewhere in the routine (regnlink's per-rung fragment values appear to
        # be reassigned when the rung list structurally changes) -- see
        # "Rung comments" in CLAUDE.md for the full investigation. A comment
        # whose fragment can't be resolved to any of this routine's own rungs is
        # dropped rather than guessed at.
        rung_comments: Dict[int, str] = {}
        description: Union[str, None] = None
        try:
            own_scope_id = struct.unpack_from("<H", record, 16)[0] if len(record) >= 18 else 0
            comment_parent = (r.comment_id * 0x10000) + r.cip_type
            self._cur.execute(
                "SELECT record_string, rung_content FROM comments "
                "WHERE parent=? AND scope_id=? AND record_type=1",
                (comment_parent, own_scope_id),
            )
            desc_candidates = []
            rung_content_rows = []
            for rec_str, rung_content in self._cur.fetchall():
                if rung_content:
                    rung_content_rows.append((rung_content, rec_str))
                elif rec_str:
                    desc_candidates.append(rec_str)
            if desc_candidates:
                description = max(desc_candidates, key=len)

            if rung_content_rows:
                rung_id_to_index = {oid: i for i, oid in enumerate(rung_ids)}
                for rung_content, rec_str in rung_content_rows:
                    if not rec_str:
                        continue
                    fragment = rung_content >> 16
                    # Authoritative lookup: RegnLink.Idx maps the fragment
                    # directly to the rung's object_id (verified 582/582 exact
                    # against a real project's own Studio 5000 L5X export --
                    # see populate_regnlink() and CLAUDE.md "Rung comments").
                    # A fragment can match several Idx entries (stale entries
                    # from old index pages survive the file scan), so prefer
                    # the one naming a live rung of this routine.
                    rung_index = None
                    self._cur.execute(
                        "SELECT rung_object_id FROM regnlink_idx "
                        "WHERE routine_id=? AND fragment=?",
                        (self._object_id, fragment),
                    )
                    idx_uids = [row[0] for row in self._cur.fetchall()]
                    if idx_uids:
                        for uid in idx_uids:
                            rung_index = rung_id_to_index.get(uid)
                            if rung_index is not None:
                                break
                        # Idx entries exist but none names a live rung: the
                        # comment is genuinely stale -- drop it rather than
                        # guessing via the unreliable .Dat chain reading.
                    else:
                        # No Idx entry at all for this fragment (e.g. the file
                        # had no readable RegnLink.Idx): fall back to the .Dat
                        # chain reading -- only reliable for routines whose
                        # rungs were never reordered/relinked.
                        self._cur.execute(
                            "SELECT rung_object_id FROM regnlink WHERE routine_id=? AND fragment=?",
                            (self._object_id, fragment),
                        )
                        row = self._cur.fetchone()
                        if row is not None:
                            rung_index = rung_id_to_index.get(row[0])
                    if rung_index is not None and rung_index not in rung_comments:
                        rung_comments[rung_index] = rec_str
        except Exception:
            pass

        st_lines: List[str] = []
        if routine_type == "ST":
            st_lines = _st_routine_lines(self._cur, self._object_id)

        return Routine(
            name, name, routine_type, rungs, rung_ids, rung_comments, description, st_lines
        )

def _parse_fffeff(data: bytes, offset: int):
    """Parse one fffeff-encoded string at offset. Returns (str, new_offset).

    Handles the extended-length form: a length byte of 0xFF is followed by
    the real length as a u16 (used e.g. by long Structured Text lines)."""
    if offset + 4 > len(data) or not (data[offset] == 0xFF and data[offset+1] == 0xFE and data[offset+2] == 0xFF):
        return "", offset
    length = data[offset+3]
    offset += 4
    if length == 0xFF:
        if offset + 2 > len(data):
            return "", offset
        length = struct.unpack_from("<H", data, offset)[0]
        offset += 2
    s = data[offset:offset+length*2].decode("utf-16-le", errors="replace")
    return s, offset + length * 2


# Record type (u32 at offset 4) of a Structured Text source-line record in
# the nameless table.
_ST_LINE_RECORD_TYPE = 0x01000002

def _st_routine_lines(cur: Cursor, routine_object_id: int) -> List[str]:
    """Extract a Structured Text routine's source lines.

    ST bodies are not stored in SbRegion like ladder rungs; they live in
    Nameless.Dat, one record per source line. A line record carries:

      offset 0x04  u32  record type -- ``_ST_LINE_RECORD_TYPE``
      offset 0x14  u32  sequence number -- source order within the routine
      offset 0x18       fffeff-encoded UTF-16 line text

    Line records hang a few levels below the routine in the nameless
    parent tree (routine -> map -> region -> line), so walk it
    breadth-first from the routine's object id and collect every line
    record encountered, then sort by sequence number.

    Tag references appear as ``@hexid@`` placeholders (the referenced
    comps object id in hex); they are batch-resolved to component names,
    mirroring how rung text resolves ``&hexid:`` module references."""
    lines: List[Tuple[int, str]] = []
    frontier: List[int] = [routine_object_id]
    for _depth in range(6):
        if not frontier:
            break
        qmarks = ",".join("?" * len(frontier))
        cur.execute(
            f"SELECT object_id, record FROM nameless WHERE parent_id IN ({qmarks})",
            frontier,
        )
        rows = cur.fetchall()
        frontier = []
        for oid, rec in rows:
            frontier.append(oid)
            if len(rec) < 24:
                continue
            if struct.unpack_from("<I", rec, 4)[0] != _ST_LINE_RECORD_TYPE:
                continue
            seq = struct.unpack_from("<I", rec, 20)[0]
            if seq == 0xFFFFFFFF:
                # Sentinel "no sequence assigned" -- a shadow/compiled copy of
                # the routine's logic (e.g. a FOR-loop's ladder-equivalent
                # body), not real numbered source text. Sorting these in with
                # genuine lines makes their relative order fall back to a raw
                # (unresolved @hexid@) text comparison, which differs between
                # otherwise-identical saves purely because object ids differ
                # -- and Studio 5000 never displays them as source lines at
                # all. Confirmed via a real project: two saves of the same
                # routine, textually identical once these are excluded.
                continue
            text, _ = _parse_fffeff(rec, 24)
            lines.append((seq, text))
    if not lines:
        return []
    lines.sort()
    texts = [text for _seq, text in lines]

    # Batch-resolve @hexid@ tag references to comp names.
    all_hex = set(re.findall(r"@([0-9a-fA-F]{1,8})@", " ".join(texts)))
    if all_hex:
        id_to_name: Dict[str, str] = {}
        for hex_id in all_hex:
            cur.execute(
                "SELECT comp_name FROM comps WHERE object_id=?", (int(hex_id, 16),)
            )
            row = cur.fetchone()
            if row and row[0]:
                id_to_name[hex_id] = row[0]
        if id_to_name:
            def _resolve(line: str) -> str:
                return re.sub(
                    r"@([0-9a-fA-F]{1,8})@",
                    lambda m: id_to_name.get(m.group(1), m.group(0)),
                    line,
                )
            texts = [_resolve(t) for t in texts]
    return texts

def _lookup_object_description(cur: Cursor, r, record: bytes) -> Union[str, None]:
    """Look up a comps object's own whole-object Description via the comments
    table, using the same comment_id/cip_type "parent" key + scope_id
    resolution already used for tag and routine descriptions (see the
    "Comment / description resolution" notes and RoutineBuilder.build()).

    Used for Program and Controller, which -- like Routine -- can have their
    own <Description> rendered as a direct child of the element, found as a
    record_type=1 (AsciiRecord) comments entry under the object's own
    parent/scope_id key. Multiple candidate rows can exist (rung comments
    share this same shape for Routine, though Program/Controller have no
    rungs of their own); the longest candidate is taken, matching the same
    heuristic RoutineBuilder already uses.
    """
    try:
        own_scope_id = struct.unpack_from("<H", record, 16)[0] if len(record) >= 18 else 0
        comment_parent = (r.comment_id * 0x10000) + r.cip_type
        cur.execute(
            "SELECT record_string FROM comments WHERE parent=? AND scope_id=? AND record_type=1",
            (comment_parent, own_scope_id),
        )
        candidates = [row[0] for row in cur.fetchall() if row[0]]
        if candidates:
            return max(candidates, key=len)
    except Exception:
        pass
    return None

def _filetime_to_iso(ft: int) -> str:
    """Convert a Windows FILETIME (100-ns units since 1601-01-01) to ISO8601.

    Returns "" for an empty or out-of-range value. Some AOI nameless records
    carry a corrupt FILETIME (the variable-length field walk can drift and read
    8 garbage bytes), which would otherwise overflow ``datetime`` for years
    beyond 9999 and abort the whole export with ``OverflowError``.
    """
    if not ft:
        return ""
    try:
        dt = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
    except (OverflowError, OSError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

def _parse_aoi_nameless(data: bytes) -> dict:
    """Extract AOI metadata from its large nameless record."""
    result: dict = {}

    offset = 0x1A
    # Three empty fffeff strings
    for _ in range(3):
        _, offset = _parse_fffeff(data, offset)

    # 8 bytes (unknown - some kind of date, skip)
    offset += 8

    # 2-byte constant (0x0002 observed)
    offset += 2

    # CreatedBy
    result["created_by"], offset = _parse_fffeff(data, offset)

    # Software revision at creation time (skip - we want the current one later)
    _, offset = _parse_fffeff(data, offset)

    # 4 zero bytes
    offset += 4

    # Empty fffeff placeholder
    _, offset = _parse_fffeff(data, offset)

    # CreatedDate FILETIME (8 bytes, Windows FILETIME in 100-ns units)
    result["created_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, offset)[0])
    offset += 8

    # EditedBy
    result["edited_by"], offset = _parse_fffeff(data, offset)

    # SoftwareRevision (current)
    result["software_revision"], offset = _parse_fffeff(data, offset)

    # 4 bytes (01 00 00 00)
    offset += 4

    # RevisionExtension
    rev_ext, offset = _parse_fffeff(data, offset)
    result["revision_extension"] = rev_ext or None

    # EditedDate FILETIME (always last 8 bytes)
    result["edited_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, len(data) - 8)[0])

    return result

@dataclass
class AoiBuilder(L5xElementBuilder):
    def build(self) -> AOI:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        aoi_record = bytes(results[0][3])
        name = results[0][0]

        # --- Revision (major.minor) from ext[0x01] ---
        _r_aoi: Union[RxGeneric, None] = None
        try:
            r = RxGeneric.from_bytes(aoi_record)
            _r_aoi = r
            exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}
            e01 = exts.get(0x01, b"")
            rev_major = struct.unpack_from("<H", e01, 0x1A)[0] if len(e01) > 0x1B else 1
            rev_minor = struct.unpack_from("<H", e01, 0x1C)[0] if len(e01) > 0x1D else 0
        except Exception:
            rev_major, rev_minor = 1, 0
        revision = f"{rev_major}.{rev_minor}"

        # --- Vendor from comps record ---
        vlen = struct.unpack_from("<H", aoi_record, 0xA6)[0] if len(aoi_record) > 0xA8 else 0
        vendor: Union[str, None] = aoi_record[0xA8:0xA8+vlen].decode("utf-8", errors="replace") if vlen > 0 else None

        # --- Metadata from large nameless record ---
        self._cur.execute(
            "SELECT record FROM nameless WHERE parent_id=" + str(self._object_id)
            + " ORDER BY LENGTH(record) DESC LIMIT 1"
        )
        nameless_row = self._cur.fetchone()
        if nameless_row and len(bytes(nameless_row[0])) > 50:
            meta = _parse_aoi_nameless(bytes(nameless_row[0]))
        else:
            meta = {"created_by": "", "created_date": "", "edited_by": "", "edited_date": "",
                    "software_revision": "", "revision_extension": None}

        parameters: List[Parameter] = []
        local_tags: List[LocalTag] = []
        routines: List[Routine] = []

        # --- Extract Parameters and LocalTags from RxTagCollection ---
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        tag_coll_row = self._cur.fetchone()
        if tag_coll_row:
            tag_coll_oid = tag_coll_row[0]
            self._cur.execute(
                "SELECT object_id, record FROM comps WHERE parent_id="
                + str(tag_coll_oid)
                + " AND record_type != 512"
                + " ORDER BY seq_number"
            )
            for child_oid, child_rec in self._cur.fetchall():
                child_rec = bytes(child_rec)
                # Determine whether this is a parameter or a local tag by inspecting
                # ext01[0x20E]: bits 0x04 (Input) or 0x08 (Output) indicate a parameter.
                is_param = False
                try:
                    r_child = RxGeneric.from_bytes(child_rec)
                    exts_child: Dict[int, bytes] = {
                        er.attribute_id: bytes(er.value)
                        for er in r_child.extended_records
                    }
                    ext01 = exts_child.get(0x01, b"")
                    flags = _aoi_tag_usage_flags(ext01)
                    is_param = bool(flags & 0x0C)
                except Exception:
                    pass

                if is_param:
                    try:
                        parameters.append(ParameterBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass
                else:
                    try:
                        local_tags.append(LocalTagBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass

        # --- Extract Routines from RxRoutineCollection ---
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        routine_coll_row = self._cur.fetchone()
        if routine_coll_row:
            routine_coll_oid = routine_coll_row[0]
            self._cur.execute(
                "SELECT object_id FROM comps WHERE parent_id=" + str(routine_coll_oid)
            )
            for (child_oid,) in self._cur.fetchall():
                try:
                    routine = RoutineBuilder(self._cur, child_oid).build()
                except Exception:
                    routine = None
                if routine is not None:
                    routines.append(routine)

        # --- Description + RevisionNote ---
        aoi_description: Union[str, None] = None
        revision_note = ""
        if _r_aoi is not None:
            aoi_comment_parent = (_r_aoi.comment_id * 0x10000) + _r_aoi.cip_type
            # record_type=1 (AsciiRecord) is required: the AOI's own whole-object
            # comment "parent" key also carries other member_ref=0 entries that are
            # NOT the description -- e.g. record_type=13 (DI_EXT_HELP, extended
            # instruction help text) and record_type=19 (UDI_LAST_EDITED_BY revision
            # metadata). Without this filter, an unordered "LIMIT 1" could pick
            # whichever of these happened to come back first instead of the real
            # description -- found via a real project where two AOIs' own
            # Descriptions ("PowerFlex Drive Instruction for EtherNet/IP") were
            # missing from the export entirely because this query silently
            # grabbed one of the other member_ref=0 rows instead.
            self._cur.execute(
                "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 "
                "AND record_type=1 LIMIT 1",
                (aoi_comment_parent,),
            )
            desc_row = self._cur.fetchone()
            if desc_row and desc_row[0]:
                aoi_description = desc_row[0]
            try:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND tag_reference='__REVISION_NOTE__' LIMIT 1",
                    (aoi_comment_parent,),
                )
                rn_row = self._cur.fetchone()
                if rn_row:
                    revision_note = rn_row[0] or ""
            except Exception:
                pass

        return AOI(
            name, name, revision,
            meta["revision_extension"],
            vendor,
            "false", "false", "false",
            meta["created_date"], meta["created_by"],
            meta["edited_date"], meta["edited_by"],
            meta["software_revision"],
            parameters, local_tags, routines,
            aoi_description,
            revision_note,
        )
