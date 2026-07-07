import re
import struct
from dataclasses import dataclass
from sqlite3 import Cursor
from typing import Optional

from acd.database.dbextract import DatRecord
from acd.generated.comments.fafa_coments import FafaComents


@dataclass
class CommentsRecord:
    _cur: Cursor
    dat_record: DatRecord

    def __post_init__(self):
        entry = CommentsRecord.parse(self.dat_record)
        if entry is not None:
            self._cur.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)

    @staticmethod
    def _parse_udi_body(body: bytes) -> Optional[tuple]:
        """Parse a UDI (type-12) fafa record body.

        UDI records store metadata like the AOI RevisionNote.  The body layout is:
          [0:8]   8 bytes unknown
          [8:12]  4 bytes some_id
          [12:16] 4 bytes flags
          [16:]   UTF-16LE null-terminated UDI-type string (e.g. "UDI_HISTORY")
                  followed by null padding, then a null-terminated ASCII text string.

        Returns (udi_type, text) or None if the structure is not recognized.
        """
        if len(body) < 20:
            return None
        try:
            # UDI type string starts at offset 16 (after 8 unknown + 4 id + 4 flags).
            utf16_start = 16
            pos = utf16_start
            code_units = []
            while pos + 1 < len(body):
                cu = struct.unpack_from("<H", body, pos)[0]
                if cu == 0:
                    break
                code_units.append(cu)
                pos += 2
            udi_type = "".join(chr(cu) for cu in code_units)
            # Skip null terminator and any subsequent null padding.
            pos += 2
            while pos < len(body) and body[pos] == 0:
                pos += 1
            # Read null-terminated ASCII text.
            text_end = body.find(b"\x00", pos)
            if text_end <= pos:
                return None
            text = body[pos:text_end].decode("utf-8", errors="replace")
            return (udi_type, text)
        except Exception:
            return None

    @staticmethod
    def parse(dat_record: DatRecord) -> Optional[tuple]:
        if dat_record.identifier != 64250:
            return None
        try:
            raw_full = bytes(dat_record.record.record_buffer)
            # scope_id: a 2-byte discriminator at absolute offset 16 in the raw record
            # (byte offset 2 within the record body, right after the 4-byte
            # record_length prefix + 10-byte header). Rockwell uses this internally
            # to disambiguate comments when multiple unrelated tags/objects share the
            # same (comment_id, cip_type) container key — the same value also appears
            # at offset 16 in the owning tag's own comps record. Without this, comment
            # rows for different tags sharing a container get scrambled together.
            scope_id = struct.unpack_from("<H", raw_full, 16)[0] if len(raw_full) >= 18 else 0
            r = FafaComents.from_bytes(dat_record.record.record_buffer)
            # Type-12 (0x0C) records carry UDI metadata such as the AOI RevisionNote.
            # The body is raw bytes; parse it to extract the text.
            if r.header.record_type == 12:
                parsed = CommentsRecord._parse_udi_body(bytes(r.body))
                if parsed is None:
                    return None
                udi_type, text = parsed
                # Only store UDI_HISTORY records (RevisionNote) for now.
                if udi_type != "UDI_HISTORY":
                    return None
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    1,              # object_id placeholder (not used for lookup)
                    text,
                    r.header.record_type,
                    r.header.parent,
                    "__REVISION_NOTE__",
                    0,              # rung_content
                    0,              # member_ref
                    scope_id,
                )
            if r.header.record_type in (16, 17):
                body = bytes(r.body)
                obj_id = struct.unpack_from("<I", body, 6)[0]
                rest = body[16:]
                tag_ref_end = None
                for i in range(0, len(rest), 2):
                    if i + 1 < len(rest) and rest[i] == 0 and rest[i + 1] == 0:
                        tag_ref_end = i
                        break
                if tag_ref_end is not None and tag_ref_end > 0:
                    tag_ref = rest[:tag_ref_end].decode("utf-16-le")
                    desc_start = tag_ref_end + 2
                    while desc_start < len(rest) and rest[desc_start] == 0:
                        desc_start += 1
                    desc_end = rest.find(b"\x00", desc_start)
                    record_string = rest[desc_start:desc_end].decode("ascii", errors="replace") if desc_end > desc_start else ""
                else:
                    tag_ref = ""
                    record_string = ""
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    obj_id,
                    record_string,
                    r.header.record_type,
                    r.header.parent,
                    tag_ref,
                    0,
                    0,
                    scope_id,
                )
            if r.header.record_type in (5, 6, 7, 8, 11, 15, 19, 21, 24, 29, 30, 37, 39):
                body = bytes(r.body)
                obj_id = struct.unpack_from("<I", body, 8)[0]
                tag_ref = ""
                record_string = ""
                # Parse UTF-16LE null-terminated tag_ref from body[16:]
                utf16_start = 16
                pos = utf16_start
                code_units = []
                while pos + 1 < len(body):
                    cu = struct.unpack_from("<H", body, pos)[0]
                    if cu == 0:
                        break
                    code_units.append(cu)
                    pos += 2
                if code_units:
                    tag_ref = "".join(chr(cu) for cu in code_units)
                    # Skip null terminator and any subsequent null padding.
                    pos += 2
                    while pos < len(body) and body[pos] == 0:
                        pos += 1
                    # Read null-terminated ASCII text.
                    text_end = body.find(b"\x00", pos)
                    if text_end > pos:
                        record_string = body[pos:text_end].decode("ascii", errors="replace")
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    obj_id,
                    record_string,
                    r.header.record_type,
                    r.header.parent,
                    tag_ref,
                    0,
                    0,
                    scope_id,
                )
            if r.header.record_type in (0x03, 0x04, 0x0D, 0x0E):
                tag_ref = r.body.tag_reference.value
            else:
                tag_ref = ""
            # For AsciiRecord (type 1 or 2), extract bytes [4:8] of unknown_1.
            # This value is non-zero for rung-level comments and zero for internal
            # metadata strings (FBDRoutineDescription, MainProgramLocalTagDescription, etc.).
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 8:
                rung_content = struct.unpack_from("<I", bytes(r.body.unknown_1), 4)[0]
            else:
                rung_content = 0
            # Extract bytes [0:4] of unknown_1 as member_ref.
            # For the object's own description (DataType, AOI, etc.) this is zero.
            # For sub-element descriptions (UDT members, AOI parameters/local tags)
            # this is non-zero, enabling callers to filter to just the object-level description.
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 4:
                member_ref = struct.unpack_from("<I", bytes(r.body.unknown_1), 0)[0]
            else:
                member_ref = 0
            return (
                r.header.seq_number,
                r.header.sub_record_length,
                r.body.object_id,
                r.body.record_string,
                r.header.record_type,
                r.header.parent,
                tag_ref,
                rung_content,
                member_ref,
                scope_id,
            )
        except Exception:
            return None

    def replace_tag_references(self, sb_rec):
        m = re.findall("@[A-Za-z0-9]*@", sb_rec)
        for tag in m:
            tag_no = tag[1:-1]
            tag_id = int(tag_no, 16)
            self._cur.execute(
                "SELECT object_id, comp_name FROM comps WHERE object_id=" + str(tag_id)
            )
            results = self._cur.fetchall()
            sb_rec = sb_rec.replace(tag, results[0][1])
        return sb_rec
