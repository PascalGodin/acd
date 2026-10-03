"""XRefs.Dat -- the project's cross-reference index (what references what).

Not readable through the generic DbExtract/Kaitai `Dat` reader: records have
no FAFA/FDFD identifier framing at all, just a dense array of fixed 46-byte
slots starting right after the records-region header. Reverse-engineered
from real projects; see CLAUDE.md "XRefs.Dat" for the full investigation.

Record layout (all little-endian):

  [0:2]   status -- 0x0000 live; first byte 0xFF = free slot ([1:5] is the
          file offset of the next free slot, the chain's head is the u32 at
          file offset 4)
  [2:6]   from_id     -- the referencing object (a rung id, a Comps object,
                         or a Nameless object for ST/FBD/SFC code)
  [6:10]  to_id       -- the referenced object
  [10:12] access      -- 1 read, 2 write, 3 read+write
  [12:14] kind        -- relationship type, see KINDS
  [14:18] instruction -- instruction code for a code reference, else 0
  [18:22] bit_offset  -- bit offset into the referenced tag's data
  [22:26] bit_width   -- bits referenced; 0xFFFFFFFF for a non-data target
  [26:34] sub-location (not decoded)
  [34:38] scope_id    -- program or AOI id of the referencing code, or NONE
  [38:42] routine_id  -- routine id of the referencing code, or NONE
  [42:46] count       -- multiplicity (e.g. a UDT's member count of to_id's type)
"""
import struct
from typing import List, NamedTuple

RECORD_SIZE = 46
NONE = 0xFFFFFFFF
_REGION_MARKER = b"\xfe\xfe"
_LIVE = struct.Struct("<2xIIHHIII8xIII")

PROGRAM_MAIN_ROUTINE = 15

# Relationship kinds confirmed against real projects. Unlisted values exist
# (compiler-internal links between Nameless code objects, trends, motion).
KINDS = {
    1: "alias_target",
    2: "tag_data_type",
    3: "io_tag_module",
    6: "msg_tag_config",
    7: "msg_config_tag",
    8: "data_type_member_type",
    9: "code_tag",
    10: "code_routine",
    13: "code_object",
    14: "code_label",
    PROGRAM_MAIN_ROUTINE: "program_main_routine",
    32: "task_program",
    115: "aoi_data_type",
    137: "st_code_tag",
    157: "code_member",
}


class XRef(NamedTuple):
    from_id: int
    to_id: int
    access: int
    kind: int
    instruction: int
    bit_offset: int
    bit_width: int
    scope_id: int
    routine_id: int
    count: int


def parse_xrefs(data: bytes) -> List[XRef]:
    """Every live record in an XRefs.Dat file. Raises ValueError if the file
    header doesn't have the expected region markers."""
    if len(data) < 32:
        raise ValueError("XRefs.Dat too short for a header")
    end = min(struct.unpack_from("<I", data, 8)[0], len(data))
    region_ptr = struct.unpack_from("<I", data, 12)[0]
    if data[region_ptr:region_ptr + 2] != _REGION_MARKER:
        raise ValueError(f"XRefs.Dat: no region marker at {region_ptr}")
    records_region = struct.unpack_from("<I", data, region_ptr + 18)[0]
    if data[records_region:records_region + 2] != _REGION_MARKER:
        raise ValueError(f"XRefs.Dat: no records-region marker at {records_region}")
    offset = records_region + struct.unpack_from("<I", data, records_region + 2)[0]

    out: List[XRef] = []
    while offset + RECORD_SIZE <= end:
        if data[offset] == 0 and data[offset + 1] == 0:
            out.append(XRef._make(_LIVE.unpack_from(data, offset)))
        offset += RECORD_SIZE
    return out
