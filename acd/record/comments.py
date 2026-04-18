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
            self._cur.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", entry)

    @staticmethod
    def parse(dat_record: DatRecord) -> Optional[tuple]:
        if dat_record.identifier != 64250:
            return None
        try:
            r = FafaComents.from_bytes(dat_record.record.record_buffer)
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
            return (
                r.header.seq_number,
                r.header.sub_record_length,
                r.body.object_id,
                r.body.record_string,
                r.header.record_type,
                r.header.parent,
                tag_ref,
                rung_content,
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
