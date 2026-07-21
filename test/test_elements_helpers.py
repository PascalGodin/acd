import math
import sqlite3
import struct
from datetime import datetime
from xml.dom import minidom

from acd.l5x.elements import (
    DataType,
    Member,
    _apply_dead_member_byte_corrections,
    _decode_single_udt_element,
    _decorated_real_literal,
    _escape_xml_attr,
    _filetime_to_iso,
    _get_type_size,
    _l5k_real_literal,
    _read_tag_initial_value,
    _resolve_bit_target,
)


def test_filetime_to_iso_zero_is_empty():
    assert _filetime_to_iso(0) == ""


def test_filetime_to_iso_valid_roundtrip():
    # 2020-01-01T00:00:00.000Z expressed as a Windows FILETIME (100-ns units
    # since 1601-01-01).
    ft = int((datetime(2020, 1, 1) - datetime(1601, 1, 1)).total_seconds()) * 10_000_000
    assert _filetime_to_iso(ft) == "2020-01-01T00:00:00.000Z"


def test_filetime_to_iso_out_of_range_is_empty():
    # A corrupt/garbage FILETIME maps to a year far beyond datetime's 9999
    # ceiling; it must degrade to "" rather than raising OverflowError.
    assert _filetime_to_iso(0xFFFFFFFFFFFFFFFF) == ""


def test_escape_xml_attr_basic_entities():
    assert _escape_xml_attr('a&b<c>d"e') == "a&amp;b&lt;c&gt;d&quot;e"


def test_escape_xml_attr_strips_illegal_control_chars():
    assert _escape_xml_attr("a\x00\x15\x1fb") == "ab"


def test_escape_xml_attr_encodes_whitespace_delimiters():
    assert _escape_xml_attr("a\tb\nc\rd") == "a&#x9;b&#xA;c&#xD;d"


def test_escape_xml_attr_keeps_attribute_well_formed():
    # A mis-parsed binary field (e.g. an AOI Vendor) with a raw newline and
    # control bytes must not produce non-well-formed XML.
    garbage = "Acme\x15Corp\nv\t1.0\ufffd"
    xml = f'<AOI Vendor="{_escape_xml_attr(garbage)}"/>'
    parsed = minidom.parseString(xml)  # raises on malformed XML
    assert parsed.documentElement.tagName == "AOI"


def test_read_tag_initial_value_bool_array_bit_packing():
    # Regression test for a real bug found while verifying export_routine()
    # against a real Studio 5000 import: BOOL *array* values were read one
    # raw byte per element (naive per-element offset), but Rockwell
    # bit-packs BOOL arrays 32 bits per 4-byte DWORD. A real 256-element
    # BOOL array tag (BitFlags) decoded index [2] as 32 (a raw packed byte
    # value) instead of the correct 0/1 bit -- any non-zero "value" then
    # renders as BOOL True in the generated XML, silently corrupting every
    # BOOL array tag's exported initial value project-wide.
    #
    # Build a synthetic data-table blob: 40 logical bits spanning two
    # packed DWORDs at offset 0x1A2 (the array read offset), with only
    # bit 2 of the first DWORD and bit 5 of the second DWORD set.
    n_elements = 40
    blob = bytearray(0x1A2 + 8)
    struct.pack_into("<I", blob, 0x1A2, 1 << 2)
    struct.pack_into("<I", blob, 0x1A2 + 4, 1 << 5)

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE comps (object_id INTEGER, record BLOB)")
    db.execute("INSERT INTO comps VALUES (1, ?)", (bytes(blob),))
    cur = db.cursor()

    values = _read_tag_initial_value(cur, 1, "BOOL", n_elements)

    assert len(values) == n_elements
    expected = [0] * n_elements
    expected[2] = 1
    expected[32 + 5] = 1
    assert values == expected


def test_read_tag_initial_value_scalar_uses_0x1a2_offset():
    # Regression test for a major, previously-undiscovered bug: scalar
    # (non-array) primitive tags were read at offset 0x19E, not 0x1A2 (the
    # same offset already used for arrays -- there was never a real
    # scalar/array distinction). Verified against a real project: 758
    # controller-scope scalar BOOL tags and 812 DINT tags compared against
    # Studio 5000's own values -- the old 0x19E offset matched only
    # 21.4%/2.8% of the time, while 0x1A2 matched 100% for both. This
    # affected every scalar primitive tag's decoded initial value
    # project-wide (BOOL, DINT, REAL, etc.), not just BOOL.
    #
    # Build a synthetic data-table blob where 0x19E and 0x1A2 hold
    # deliberately different values, and confirm the function reads from
    # 0x1A2.
    blob = bytearray(0x1A2 + 4)
    struct.pack_into("<i", blob, 0x19E, 999)  # decoy -- must NOT be read
    struct.pack_into("<i", blob, 0x1A2, 42)   # the real value

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE comps (object_id INTEGER, record BLOB)")
    db.execute("INSERT INTO comps VALUES (1, ?)", (bytes(blob),))
    cur = db.cursor()

    value = _read_tag_initial_value(cur, 1, "DINT", 1)

    assert value == 42


def test_l5k_real_literal_nan_and_infinity_do_not_crash():
    # A real production project was found with several uninitialized REAL
    # tags decoding to NaN/Infinity, which crashed this function entirely
    # (str.split("e") on Python's bare "nan"/"inf" formatting, with no "e"
    # to split on). Verified against that same project's own Studio 5000
    # L5X export: NaN -> "1.#QNAN000e+000", +Infinity -> "1.#INF0000e+000"
    # (the classic MSVC CRT special-value convention, left-padded with
    # zeros into the normal 8-digit mantissa slot).
    assert _l5k_real_literal(float("nan")) == "1.#QNAN000e+000"
    assert _l5k_real_literal(float("inf")) == "1.#INF0000e+000"
    assert _l5k_real_literal(float("-inf")) == "-1.#INF0000e+000"


def test_decorated_real_literal_scalar_nan():
    # Verified against the real project referenced above: a scalar tag's
    # Decorated NaN value is the bare label "1.#QNAN" (no padding/exponent,
    # unlike the L5K form).
    assert _decorated_real_literal(float("nan"), in_array=False) == "1.#QNAN"


def test_decorated_real_literal_array_infinity_matches_real_quirk():
    # Verified against the real project referenced above: an array
    # Element's Decorated value for +Infinity is the truncated "1.$" --
    # a real, reproducible quirk in Studio 5000's own array Decorated-value
    # exporter (distinct from the scalar case above).
    assert _decorated_real_literal(float("inf"), in_array=True) == "1.$"


def test_resolve_bit_target_prefers_declaration_order_fallback():
    # Regression test for a real, previously-unresolved bug (a downstream
    # agent hit it live): a real UDT ("LugWrk") had 4 BIT members whose own
    # 0x6c value (596) matched no known member's 0x60 at all, leaving
    # Target unresolved entirely -- Studio 5000's Import Routine then
    # rejected the exported L5X ("Required property 'Target' was missing").
    # fallback_target (the most-recent preceding hidden member in
    # declaration order) must be preferred when available, since it was
    # confirmed correct against a real Studio 5000 export in every case
    # found, including ones where the offset60_to_name lookups below would
    # return a wrong-but-non-None name instead.
    offset60_to_name = {640: "SomeOtherPlainField"}  # coincidental collision
    assert (
        _resolve_bit_target(596, 640, offset60_to_name, "ZZZZZZZZZZLugWrk9")
        == "ZZZZZZZZZZLugWrk9"
    )


def test_resolve_bit_target_falls_back_to_target_key_lookup():
    # When no hidden member precedes (fallback_target is None), a valid
    # 0x6c-based offset60_to_name lookup is used (the TIMER/COUNTER-style
    # built-in overlay case).
    offset60_to_name = {12: "Control"}
    assert _resolve_bit_target(12, 999, offset60_to_name, None) == "Control"


def test_resolve_bit_target_falls_back_to_own_offset_lookup():
    # When fallback_target is None AND the 0x6c lookup fails (0x6c is the
    # sentinel 0xFFFFFFFF), fall back to this member's own 0x60 as the
    # lookup key.
    offset60_to_name = {8: "Backing"}
    assert _resolve_bit_target(0xFFFFFFFF, 8, offset60_to_name, None) == "Backing"


def test_resolve_bit_target_returns_none_when_nothing_resolves():
    assert _resolve_bit_target(0xFFFFFFFF, 999, {}, None) is None


def test_member_to_xml_bit_member_includes_target_and_bit_number():
    member = Member(
        "ActvtnArea", "ActvtnArea", "BIT", 0, "Decimal", False,
        "ZZZZZZZZZZLugWrk9", 0, "Read/Write",
    )
    xml = member.to_xml()
    assert 'Target="ZZZZZZZZZZLugWrk9"' in xml
    assert 'BitNumber="0"' in xml


def test_member_to_xml_plain_bool_array_omits_bit_number():
    # Regression test for a real bug found alongside the fix above: a
    # BOOL[32] array member ("Ons" in the same real UDT) was emitting a
    # spurious BitNumber="0" not present in Studio 5000's own export --
    # bit_number is set for every BOOL member internally (a data-table
    # decode hint, see the Member.bit_number field docstring) but must only
    # be rendered as an XML attribute for a genuine BIT pseudo-member.
    member = Member("Ons", "Ons", "BOOL", 32, "Decimal", False, None, 0, "Read/Write")
    xml = member.to_xml()
    assert "BitNumber" not in xml
    assert "Target" not in xml


def test_decorated_real_literal_finite_uses_short_form():
    assert _decorated_real_literal(0.4047619, in_array=False) == "0.404762"


def _member(name, data_type, byte_offset=0, dimension=0):
    return Member(
        name, name, data_type, dimension, "Decimal", False, None, None,
        "Read/Write", _byte_offset=byte_offset,
    )


def test_decode_single_udt_element_two_real_levels_of_struct_nesting():
    # Regression test for a real bug found via a real Studio 5000 import
    # rejection ("Data type mismatch"): the depth counter was incremented
    # TWICE per real struct-nesting level (once in _decode_single_udt_element
    # calling _decode_scalar_member(depth+1), again inside _decode_scalar_member
    # calling _decode_single_udt_element(depth+1)), silently halving the
    # usable nesting depth from the documented 3 levels to effectively 1. A
    # real UDT only 2 real levels deep (LugWrk -> Lug -> LugErrorCode) had
    # its innermost member ("ErrorCd") silently decode to {} well within the
    # intended limit -- which renders as a bare "[]" in the L5K literal,
    # a shape Studio 5000 rejects on import.
    c_dt = DataType("C", "C", "NoFamily", "User", [_member("d", "DINT")])
    b_dt = DataType("B", "B", "NoFamily", "User", [_member("c", "C")])
    a_dt = DataType("A", "A", "NoFamily", "User", [_member("b", "B")])
    data_types_map = {"A": a_dt, "B": b_dt, "C": c_dt}

    blob = struct.pack("<i", 42)
    result = _decode_single_udt_element(blob, 0, a_dt, data_types_map, 0)

    assert result == {"b": {"c": {"d": 42}}}


def test_decode_single_udt_element_still_truncates_beyond_max_depth():
    # The depth-limit safety net itself must still work after the fix above
    # -- 4 real levels of struct nesting beyond the top-level element must
    # still truncate the innermost level to {} (max_depth=3 means depths
    # 0/1/2/3 succeed, depth 4 is dropped).
    e_dt = DataType("E", "E", "NoFamily", "User", [_member("f", "DINT")])
    d_dt = DataType("D", "D", "NoFamily", "User", [_member("e", "E")])
    c_dt = DataType("C", "C", "NoFamily", "User", [_member("d", "D")])
    b_dt = DataType("B", "B", "NoFamily", "User", [_member("c", "C")])
    a_dt = DataType("A", "A", "NoFamily", "User", [_member("b", "B")])
    data_types_map = {"A": a_dt, "B": b_dt, "C": c_dt, "D": d_dt, "E": e_dt}

    blob = struct.pack("<i", 42)
    result = _decode_single_udt_element(blob, 0, a_dt, data_types_map, 0)

    assert result == {"b": {"c": {"d": {"e": {}}}}}


def test_get_type_size_includes_dead_member_bytes():
    # A DataType with a deleted member (no live descriptor, so it's not in
    # .members at all) still physically reserves its old byte range in an
    # already-allocated tag's data table -- _get_type_size() must add
    # _dead_member_bytes on top of what summing visible members gives.
    inner_dt = DataType(
        "Inner", "Inner", "NoFamily", "User",
        [_member("a", "DINT", byte_offset=0)],  # visible size: 4 bytes
        _dead_member_bytes=2,
    )
    data_types_map = {"INNER": inner_dt}
    assert _get_type_size("INNER", data_types_map) == 6


def test_apply_dead_member_byte_corrections_shifts_subsequent_members():
    # Regression test for the real bug this exists to fix: a scalar
    # (non-array) struct-typed member ("b", typed "Inner") whose nested
    # DataType has dead bytes must shift every member declared AFTER it in
    # the outer struct -- reproduces the real LugWrk/Lug/pntrTpStrt shape
    # (BfrLug -> Lug, which had a deleted member, followed by 6 scalar
    # members that were each read 2 bytes too early before this fix).
    inner_dt = DataType(
        "Inner", "Inner", "NoFamily", "User",
        [_member("a", "DINT", byte_offset=0)],
        _dead_member_bytes=2,
    )
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User",
        [
            _member("b", "Inner", byte_offset=0),
            _member("c", "INT", byte_offset=4),
            _member("d", "INT", byte_offset=6),
        ],
    )
    data_types_map = {"INNER": inner_dt, "OUTER": outer_dt}

    _apply_dead_member_byte_corrections(data_types_map)

    b, c, d = outer_dt.members
    assert b._byte_offset == 0  # unaffected -- nothing precedes it
    assert c._byte_offset == 6  # shifted by Inner's 2 dead bytes
    assert d._byte_offset == 8


def test_apply_dead_member_byte_corrections_noop_when_no_dead_bytes():
    inner_dt = DataType(
        "Inner", "Inner", "NoFamily", "User", [_member("a", "DINT", byte_offset=0)],
    )
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User",
        [_member("b", "Inner", byte_offset=0), _member("c", "INT", byte_offset=4)],
    )
    data_types_map = {"INNER": inner_dt, "OUTER": outer_dt}

    _apply_dead_member_byte_corrections(data_types_map)

    b, c = outer_dt.members
    assert b._byte_offset == 0
    assert c._byte_offset == 4
