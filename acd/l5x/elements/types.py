# Auto-split from the former acd/l5x/elements.py -- see CLAUDE.md's structural-cleanup notes.
from typing import Dict


_STRING_SIZE = 88

_PRIM = {
    'BOOL':  ('B',   1),
    'SINT':  ('<b',  1),
    'INT':   ('<h',  2),
    'DINT':  ('<i',  4),
    'LINT':  ('<q',  8),
    'BYTE':  ('B',   1),
    'WORD':  ('<H',  2),
    'DWORD': ('<I',  4),
    'LWORD': ('<Q',  8),
    'REAL':  ('<f',  4),
    'LREAL': ('<d',  8),
}

def _is_string_family_type(type_name: str, data_types_map: Dict[str, 'DataType']) -> bool:
    """Return True if type_name is the built-in STRING type or a custom
    string-family DataType (e.g. STRING20, STRING64, ...).

    Custom string-family types are detected generically via the DataType's own
    ``family`` flag (read from extended record 0x6C in the ACD binary by
    DataTypeBuilder), not by matching against a hardcoded name -- so any
    project-defined string type of any name/length is recognized correctly.
    """
    t = type_name.upper()
    if t == "STRING":
        return True
    dt = data_types_map.get(t)
    return dt is not None and dt.family == "StringFamily"

def _string_family_capacity(type_name: str, data_types_map: Dict[str, 'DataType']) -> int:
    """Return the configured character capacity of a string-family type.

    82 for the built-in STRING type. For a custom string-family type, reads
    the dimension of its DATA member from the ACD's own type definition
    (rather than assuming the default 82-character length).
    """
    t = type_name.upper()
    if t == "STRING":
        return 82
    dt = data_types_map.get(t)
    if dt is not None:
        for m in dt.members:
            if m.name.upper() == "DATA":
                return m.dimension
    return 82

def _get_type_size(type_name: str, data_types_map: Dict[str, 'DataType']) -> int:
    """Return the byte size of a DataType (primitive, STRING, or UDT).

    For UDTs, computes max(byte_offset + elem_size * max(dimension,1))
    across non-hidden non-BIT members, then rounds up to the next multiple
    of 4 -- Rockwell always declares a UDT's total size this way (confirmed
    directly against Studio 5000's own "Data Type Size" field, shown in a
    UDT's Properties dialog, and directly stated by the user: "UDT can only
    have a multiple of 4 byte total size"). Returns 0 for unknown types.

    A real bug lived here for a while despite this being documented
    correctly: the actual rounding was `max_end + (max_end % 2)` (round up
    to even), not a true multiple-of-4 round-up -- a genuine typo-class
    slip between the intent (and this very docstring/commit message) and
    the code, undetected because the *test case cited when "fixing" this*
    (Encoder, 263 -> 264) happens to round to the same result either way
    (264 is both the next even number and the next multiple of 4 above
    263), so it couldn't distinguish the two rules. Found via a real project
    (`FenceSkid`, whose members sum to 13 bytes): the old code returned 14
    (even, wrong) instead of 16 (multiple of 4, correct), silently making
    every *array* of `FenceSkid`-sized structs (here, `FenceGate.Skid[2]`,
    and therefore every element of the `FenceGate[]` tag array beyond
    index 0) use a 2-byte-too-small stride -- confirmed via a real Studio
    5000 "Tag Name Collision" dialog showing `FenceGate[1]`/`FenceGate[2]`
    decoded with every field shifted by one position relative to the real
    project's existing values. Lesson worth repeating from elsewhere in
    this file: a fix verified against only one real-world example that
    happens to be ambiguous between two candidate rules is not verified at
    all -- the previous investigation's own 99-DataType whole-project sweep
    (see CLAUDE.md) apparently never happened to include an ODD-remainder
    struct whose size was also used as an ARRAY element's stride, which is
    exactly the combination needed to expose this.

    Deliberately does NOT add dt._dead_member_bytes here -- an earlier
    version of this function did, on the (untested) assumption that a
    deleted member's persisting footprint would also affect an *array*
    element's stride the same way it affects a scalar struct member's
    trailing siblings (see _apply_dead_member_byte_corrections(), which
    handles that latter case directly). Verified against a real array of
    the exact UDT this was found on ("Lug", 200 elements): the true
    per-element stride is the plain max(offset+size) value with NO dead-byte
    addition -- confirmed by finding two known-consecutive elements' own
    "No" field values at their real, empirically-located byte offsets 568
    bytes apart. Adding dead-member bytes here would have silently
    corrupted every array-of-that-struct tag project-wide; don't reinstate
    it without re-verifying against a real array case specifically, not
    just the scalar case this field was originally added for.
    """
    t = type_name.upper()
    prim = _PRIM.get(t)
    if prim is not None:
        return prim[1]
    if t == "STRING":
        return _STRING_SIZE
    dt = data_types_map.get(t)
    if dt is None:
        return 0
    max_end = 0
    for m in dt.members:
        # Skip only BIT-overlay pseudo-members (no storage of their own).
        # A hidden non-BIT member (e.g. TIMER/COUNTER's own "Control" DINT)
        # still occupies real bytes in the struct and must count toward the
        # total size, or a struct whose hidden member happens to be its
        # largest-offset field would be undersized.
        if m.data_type == "BIT":
            continue
        if m.data_type.upper() == "BOOL" and m.dimension > 0:
            # BOOL arrays are bit-packed into DINT-sized (4-byte) words, 32
            # bits per word -- NOT one byte per element. Verified against a
            # real UDT (LugWrk) whose true total size only matches when a
            # trailing BOOL[32] member is sized as 4 bytes, not 32; without
            # this, every member/struct size computed *after* a BOOL array
            # member is wrong, corrupting offsets for any subsequent element
            # in an array of that struct.
            member_end = m._byte_offset + (((m.dimension + 31) // 32) * 4)
        else:
            elem_sz = _get_type_size(m.data_type, data_types_map)
            if elem_sz == 0:
                continue
            member_end = m._byte_offset + (elem_sz * max(m.dimension, 1))
        max_end = max(max_end, member_end)
    if max_end == 0:
        return 0
    return -(-max_end // 4) * 4  # round up to the next multiple of 4
