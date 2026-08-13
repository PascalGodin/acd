# Auto-split from the former acd/l5x/elements.py -- see CLAUDE.md's structural-cleanup notes.
import re
import struct
from sqlite3 import Cursor
from typing import Dict, Union

from acd.generated.comps.rx_generic import RxGeneric

from .types import _PRIM, _get_type_size, _is_string_family_type, _string_family_capacity


def _tag_value_blob_offset(raw_rec: bytes) -> int:
    """Compute the absolute byte offset where a tag's data-table blob's real
    value bytes begin, by parsing the record's own self-describing
    RxGeneric structure instead of guessing a fixed constant.

    A tag's `data_table_instance` comps record (a separate comps row from
    the tag itself) is itself an ordinary RxGeneric record whose header
    declares `count_record` attribute records, but whose Kaitai parser
    (`RxGeneric._read()`) only parses `count_record - 1` of them into
    `extended_records` -- the very last one (always attribute_id 0x66,
    seen as the marker byte 0x66 immediately after the parsed records) is
    left unparsed in the stream. That last, unparsed attribute record IS
    the tag's own value blob: its own 4-byte len_value field always exactly
    equals the tag's computed value size (n_elements * elem_size, or a
    UDT's own declared struct size -- verified against a real project for
    both scalar and array tags, any UDT type), and the real value bytes
    are its own value payload, starting 8 bytes (attribute_id + len_value)
    after where `extended_records` leaves off.

    This replaces a former "0x1A2, +2 if is_array" fixed-offset guess. That
    guess happened to match a large real project's array tags and its
    Lug/LugWrk-typed scalar tags, but was proven wrong in general: a fresh
    Studio 5000 project's own primitive/UDT array tags decode correctly at
    the *unshifted* base, and a real project's Encoder-typed *scalar* tag
    needed the +2 shift despite being a scalar. The actual, self-describing
    length is `82 + sum(8 + len(value) for each extended_record)` -- this
    varies by a couple of bytes between records/projects (observed 2 bytes
    shorter in a fresh Studio 5000 V32 project than an older V38 one) but
    is always computable from the record itself, with no need to guess
    "is_array" or special-case any UDT type. Confirmed against a real
    project's Trim_Decision tag (LugWrk-typed, scalar) via a Studio 5000
    screenshot: its populated fields (pntrTpStrt=24, pntrTpStp=25,
    pntrLug=183, Wrk[4]=32790) only decode correctly using this computed
    offset together with each member's own *raw*, uncorrected stored
    byte_offset -- see _apply_dead_member_byte_corrections's docstring for
    the matching fix on the member-offset side.
    """
    try:
        r = RxGeneric.from_bytes(raw_rec)
        consumed = 82 + sum(8 + len(bytes(er.value)) for er in r.extended_records)
        return consumed + 8
    except Exception:
        return 0x1A2

def _read_tag_initial_value(cur: Cursor, data_table_instance: int,
                            data_type: str, n_elements: int,
                            is_array: bool = False):
    """Read the initial value(s) of a tag from its data-table blob in the comps table.

    Returns None if the type is not supported or the blob cannot be read.
    For a genuine scalar (no dimensions declared at all), returns a single
    Python int/float. For an array tag -- including a *genuine one-element
    array* (Dimensions="1", not merely "no dimensions declared") -- returns
    a list of values.

    `is_array` (derived by the caller from whether the tag actually has a
    `dimensions` string, not from `n_elements`) is what distinguishes these
    two cases; `n_elements == 1` alone is ambiguous between them. This
    caught a real, reproducible Studio 5000 import failure ("Data type
    mismatch") for a real tag declared `Dimensions="1"`: the old code
    collapsed a 1-element array's value to a bare scalar whenever
    n_elements == 1 regardless of `is_array`, so `Tag.to_xml()` rendered a
    scalar `<DataValue>` for a tag whose `<Tag Dimensions="1">` attribute
    told Studio to expect an `<Array>` -- an internal inconsistency in our
    own output that a real Studio import correctly rejected.
    """
    dt_upper = data_type.upper() if data_type else ""
    # Strip array brackets for type lookup, but keep n_elements from caller.
    base_dt = re.sub(r'\[.*\]', '', dt_upper).strip()

    pack_fmt_info = _PRIM.get(base_dt)
    if pack_fmt_info is None:
        return None

    fmt, elem_size = pack_fmt_info

    cur.execute(
        "SELECT record FROM comps WHERE object_id = ?", (data_table_instance,)
    )
    rows = cur.fetchall()
    if not rows:
        return None

    raw_rec = bytes(rows[0][0])

    # See _tag_value_blob_offset()'s own docstring for why this is computed
    # from the record's own structure rather than a fixed 0x1A2 guess.
    offset = _tag_value_blob_offset(raw_rec)

    # BOOL/BIT *arrays* are bit-packed by Rockwell -- 32 bits per 4-byte
    # DWORD, NOT one byte per element (a scalar BOOL, n_elements == 1, is
    # NOT packed this way and is read as a plain byte below like every
    # other primitive). This matches the same bit-packing already
    # accounted for in _get_type_size()'s BOOL-array sizing
    # (ceil(N/32)*4) -- that fix covered how much space the array
    # occupies, but this function still read one raw byte per element
    # regardless, silently returning garbage (whichever byte of the
    # packed DWORD happened to line up with that element's naive
    # per-element offset) for every BOOL array tag. Verified against a
    # real project: BitFlags[2] decoded as 32 (a raw packed byte value)
    # instead of the correct 0/1 bit.
    if base_dt in ("BOOL", "BIT") and n_elements > 1:
        values = []
        for i in range(n_elements):
            dword_off = offset + (i // 32) * 4
            if dword_off + 4 <= len(raw_rec):
                dword = struct.unpack_from("<I", raw_rec, dword_off)[0]
                values.append((dword >> (i % 32)) & 1)
            else:
                values.append(0)
        return values

    values = []
    for i in range(n_elements):
        byte_off = offset + i * elem_size
        if byte_off + elem_size <= len(raw_rec):
            val = struct.unpack_from(fmt, raw_rec, byte_off)[0]
            # BOOL is stored as unsigned byte (B) — return int 0/1.
            values.append(val)
        else:
            # Pad with zero for truncated trailing elements.
            if fmt == 'B':
                values.append(0)
            else:
                values.append(0)

    if n_elements == 1 and not is_array:
        return values[0]
    return values

def _decode_udt_initial_value(
    cur: Cursor,
    data_table_instance: int,
    data_type_name: str,
    n_elements: int,
    data_types_map: Dict[str, 'DataType'],
    is_array: bool = False,
) -> Union[dict, list, None]:
    """Decode initial values for a struct-typed tag from the data table blob.

    Works for any DataType found in data_types_map -- user-defined UDTs,
    Rockwell "ProductDefined"/module-defined types (e.g. AB:1794_IB32:I:0),
    and string-family types (built-in STRING or a custom type like
    STRING_20, decoded to {"LEN": int, "DATA": str}) -- since all of these
    are read from the same generic member/byte_offset structure and the
    same data-table blob layout (verified against real project data,
    including a real non-blank custom string-family array tag).

    Returns a dict for a genuine scalar struct, a list of dicts for struct
    arrays -- including a genuine one-element array (`is_array=True`, e.g.
    Dimensions="1"), which must NOT collapse to a bare dict even though
    n_elements == 1 (see the identical, verified-via-real-Studio-import-
    failure fix in _read_tag_initial_value for why this distinction
    matters) -- or None if the type is unknown or cannot be decoded.
    """
    if not data_type_name:
        return None
    if n_elements > 10000:
        return None

    base_dt = re.sub(r'\[.*\]', '', data_type_name).strip()
    dt_obj = data_types_map.get(base_dt.upper())
    if dt_obj is None:
        return None

    cur.execute(
        "SELECT record FROM comps WHERE object_id = ?", (data_table_instance,)
    )
    rows = cur.fetchall()
    if not rows:
        return None
    raw_rec = bytes(rows[0][0])

    struct_size = _get_type_size(base_dt.upper(), data_types_map)
    if struct_size == 0:
        return None

    # See _tag_value_blob_offset()'s own docstring for why this is computed
    # from the record's own structure rather than a fixed 0x1A2 guess. The
    # per-element stride is the plain (non-dead-byte-adjusted) struct_size
    # above -- see _get_type_size()'s own docstring for why dead-member
    # bytes must NOT also be added here.
    offset = _tag_value_blob_offset(raw_rec)

    results: list = []
    for elem_idx in range(n_elements):
        base = offset + elem_idx * struct_size
        decoded = _decode_single_udt_element(
            raw_rec, base, dt_obj, data_types_map, 0
        )
        results.append(decoded)

    if n_elements == 1 and not is_array:
        return results[0]
    return results

def _decode_string_family_value(
    blob: bytes, offset: int, type_name: str, data_types_map: Dict[str, 'DataType'],
) -> dict:
    """Decode a string-family struct's LEN+DATA fields from *blob* at *offset*.

    Returns {"LEN": int, "DATA": str} -- the same shape Studio 5000 uses to
    represent a STRING/string-family value at any nesting level (top-level
    tag, array element, or a member nested inside another struct), so the
    generic UDT-rendering code can treat it identically everywhere via
    _udt_scalar_to_xml's own string-family check.

    Decoded as latin-1 (a 1:1 byte<->codepoint mapping that can never fail),
    NOT utf-8 -- a Rockwell STRING is just a raw SINT[] byte array with no
    guarantee of valid UTF-8 content, and utf-8-with-errors="replace" was
    found to insert U+FFFD (a non-ASCII codepoint) for any byte sequence
    that doesn't happen to be valid UTF-8 -- confirmed via a real project
    tag (an array element whose STRING member was uninitialized/garbage
    data, not real text) whose L5K export Studio 5000 rejected with "Only
    ASCII characters are supported": _l5k_string_padded() already $XX-hex-
    escapes every control character for exactly this reason, but had no
    non-ASCII bytes to escape in the first place until utf-8 decoding
    replaced them with an unescaped, illegal-for-L5K codepoint. latin-1
    preserves every original byte value 0x00-0xFF as its own codepoint, so
    _l5k_string_padded's escaping (extended below to cover >= 0x80 too) can
    correctly represent any byte, meaningful text or garbage alike.
    """
    cap = _string_family_capacity(type_name, data_types_map)
    if offset + 4 > len(blob):
        return {"LEN": 0, "DATA": ""}
    length = struct.unpack_from("<i", blob, offset)[0]
    length = max(0, min(length, cap))
    text = ""
    if offset + 4 + length <= len(blob):
        raw = blob[offset + 4: offset + 4 + length]
        text = raw.decode("latin-1")
    return {"LEN": length, "DATA": text}

def _decode_single_udt_element(
    blob: bytes,
    base_offset: int,
    data_type: 'DataType',
    data_types_map: Dict[str, 'DataType'],
    depth: int,
    _max_depth: int = 3,
) -> dict:
    """Decode one UDT element from *blob* at *base_offset*.

    Returns {member_name: value} for every member INCLUDING hidden ones
    (e.g. TIMER/COUNTER's own "Control" DINT -- needed by _l5k_udt_literal,
    which encodes its raw value even though it has no Decorated member of
    its own) and BIT-overlay pseudo-members (e.g. TIMER.EN/TT/DN,
    COUNTER.CU/CD/DN/OV/UN -- needed by _udt_scalar_to_xml, which renders
    them as real BOOL DataValueMembers). Returns an empty dict when *depth*
    exceeds ``_max_depth``.

    *depth* counts real struct-nesting levels (0 = the tag's own top-level
    struct), incremented exactly once per level by _decode_scalar_member
    when it actually descends into a nested UDT. Do NOT also increment it
    in the calls to _decode_scalar_member below -- a real bug here (fixed)
    incremented depth both here AND inside _decode_scalar_member, silently
    halving the usable nesting depth from the documented 3 levels to
    effectively 1: a real UDT only 2 real levels deep (LugWrk -> Lug ->
    LugErrorCode) had its innermost member silently decode to `{}` well
    within the intended limit. A `{}` for a struct-typed member renders as
    nothing in Decorated output (a member simply goes missing, easy to miss
    entirely) but as a bare "[]" in the L5K literal's fixed-position array
    -- a shape Studio 5000 rejects on import as "Data type mismatch", which
    is how this was actually caught.
    """
    if depth > _max_depth:
        return {}

    if data_type.family == "StringFamily":
        return _decode_string_family_value(blob, base_offset, data_type.name, data_types_map)

    result: dict = {}
    for member in data_type.members:
        # BIT-overlay pseudo-members have no storage of their own (they
        # alias a specific bit of a sibling member, via member.target) --
        # handled in the second pass below, once every sibling's own value
        # has been decoded. Genuinely hidden non-BIT members (e.g. the
        # "Control" DINT that TIMER/COUNTER's own EN/TT/DN or
        # CU/CD/DN/OV/UN bits live in) ARE decoded here like any other
        # member -- skipping them was a real bug (found via a real Studio
        # import rejecting a COUNTER-typed tag whose Decorated structure
        # was missing CU/CD/DN/OV/UN entirely): both the raw backing value
        # (needed for L5K) and the individual bits (needed for Decorated)
        # ultimately come from this same decode.
        if member.data_type == "BIT":
            continue

        mname = member.name
        mdt = member.data_type
        mdt_upper = mdt.upper()
        off = base_offset + member._byte_offset

        bn = member.bit_number if mdt_upper == "BOOL" else None
        if member.dimension > 0:
            if mdt_upper == "BOOL":
                # BOOL arrays are bit-packed 32 bits per 4-byte DWORD, the
                # same convention _get_type_size() already uses for BOOL-
                # array *sizing* and _read_tag_initial_value() already uses
                # for a top-level primitive BOOL-array *tag* -- but this
                # UDT-member path never got the equivalent fix, so a BOOL
                # array MEMBER (e.g. Encoder's "Ons") was read one raw byte
                # per element (elem_size=1 from _get_type_size("BOOL",...))
                # instead of extracting the correct bit from its shared
                # packed DWORD. Found via a real Studio 5000 "Tag Name
                # Collision / Data Compare" dialog: EncTrm.Ons[5] decoded
                # as 1 instead of the real 0 -- only one of 32 elements
                # differed because bit 0 of the wrong byte happens to
                # coincidentally match the true packed bit for most
                # positions, making this the kind of bug that's easy to
                # miss without checking every element.
                arr = []
                for i in range(member.dimension):
                    dword_off = off + (i // 32) * 4
                    if dword_off + 4 <= len(blob):
                        dword = struct.unpack_from("<I", blob, dword_off)[0]
                        arr.append((dword >> (i % 32)) & 1)
                    else:
                        arr.append(0)
                result[mname] = arr
                continue
            elem_size = _get_type_size(mdt_upper, data_types_map)
            if elem_size == 0:
                continue
            arr: list = []
            for i in range(member.dimension):
                elem_off = off + i * elem_size
                val = _decode_scalar_member(
                    blob, elem_off, mdt, data_types_map, depth,
                    bit_number=bn,
                )
                arr.append(val)
            result[mname] = arr
        else:
            val = _decode_scalar_member(
                blob, off, mdt, data_types_map, depth,
                bit_number=bn,
            )
            result[mname] = val

    # Second pass: extract each BIT-overlay member's own bit from its
    # already-decoded target sibling (e.g. TIMER.EN = bit 31 of "Control").
    # Verified against a real TIMER tag: raw Control=-1607863227 with
    # EN/TT/DN at bit_number 31/30/29 decodes to EN=1, TT=0, DN=1, matching
    # Studio 5000's own Decorated output exactly. Python's ">>" on a
    # negative int is an arithmetic (sign-extending) shift, which correctly
    # reproduces the 32-bit two's-complement bit pattern for any bit 0-31.
    for member in data_type.members:
        if member.data_type != "BIT":
            continue
        target_val = result.get(member.target)
        if isinstance(target_val, int) and member.bit_number is not None:
            result[member.name] = (target_val >> member.bit_number) & 1

    return result

def _decode_scalar_member(
    blob: bytes,
    offset: int,
    data_type: str,
    data_types_map: Dict[str, 'DataType'],
    depth: int,
    bit_number: Union[int, None] = None,
):
    """Decode a single scalar member value from *blob* at *offset*.

    Handles primitives and nested UDTs directly. String-family types
    (built-in STRING or a custom type like STRING_20) return a
    {"LEN": int, "DATA": str} dict -- the same shape used at every other
    nesting level -- rather than a bare string, so callers always get a
    consistent struct-like value for any string-family type.
    Returns 0 for out-of-range primitive reads, ``None`` for unknown types.

    When *bit_number* is provided for a BOOL member, extracts that
    specific bit from the packed byte (Logix packs 8 BOOLs per byte
    in UDT data tables).
    """
    mdt_upper = data_type.upper()
    prim = _PRIM.get(mdt_upper)
    if prim is not None:
        fmt, sz = prim
        if offset + sz <= len(blob):
            val = struct.unpack_from(fmt, blob, offset)[0]
            if bit_number is not None and mdt_upper == "BOOL":
                val = (val >> bit_number) & 1
            return val
        return 0

    if _is_string_family_type(mdt_upper, data_types_map):
        return _decode_string_family_value(blob, offset, data_type, data_types_map)

    dt_obj = data_types_map.get(mdt_upper)
    if dt_obj is not None and depth <= 3:
        return _decode_single_udt_element(
            blob, offset, dt_obj, data_types_map, depth + 1
        )

    return None

def _count_array_elements(dimensions: Union[str, None]) -> int:
    """Return the total element count for a tag based on its dimensions string.

    Returns 1 for scalars (None or empty), or the product of dimension values.
    """
    if not dimensions:
        return 1
    try:
        parts = [int(d) for d in dimensions.split(",") if d.strip().isdigit()]
        count = 1
        for p in parts:
            count *= p
        return max(count, 1)
    except (ValueError, TypeError):
        return 1
