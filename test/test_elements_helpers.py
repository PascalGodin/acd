import math
import sqlite3
import struct
from datetime import datetime
from xml.dom import minidom

import pytest

from acd.l5x.elements import (
    AoiBuilder,
    DataType,
    DataTypeBuilder,
    Member,
    Tag,
    _apply_dead_member_byte_corrections,
    _decode_single_udt_element,
    _decode_string_family_value,
    _decorated_real_literal,
    _escape_xml_attr,
    _filetime_to_iso,
    _get_type_size,
    _l5k_real_literal,
    _l5k_string_padded,
    _l5k_udt_literal,
    _read_tag_initial_value,
    _resolve_bit_target,
    _udt_scalar_to_xml,
    _validate_tag_types_resolve,
    _zero_value_for_member,
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


def _build_dti_record(value_blob: bytes, attr1_len: int = 288) -> bytes:
    """Build a synthetic data-table-instance comps record matching the real
    RxGeneric layout: a fixed 82-byte header, 3 parsed AttributeRecords
    (attribute_id 0x1/0x64/0x65 -- arbitrary content, only their lengths
    matter), then a 4th, deliberately *unparsed* AttributeRecord (see
    RxGeneric._read(): `for i in range(self.count_record - 1)` always
    leaves the last one unread) whose own value IS the tag's value blob.
    This mirrors real ACD data rather than assuming any fixed byte offset.
    """
    header = struct.pack("<IIHHH", 0, 0, 40, 106, 0)  # parent_id, uid, rfv, cip_type, comment_id
    main_record = b"\x00" * 60
    attr1 = struct.pack("<II", 0x1, attr1_len) + b"\x00" * attr1_len
    attr64 = struct.pack("<II", 0x64, 16) + b"\x00" * 16
    attr65 = struct.pack("<II", 0x65, 2) + b"\x00" * 2
    count_record = 4  # 3 parsed + 1 left unparsed (the value blob itself)
    len_and_count = struct.pack("<II", 0, count_record)
    value_attr = struct.pack("<II", 0x66, len(value_blob)) + value_blob
    return header + main_record + len_and_count + attr1 + attr64 + attr65 + value_attr


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
    # packed DWORDs, with only bit 2 of the first DWORD and bit 5 of the
    # second DWORD set.
    n_elements = 40
    value_blob = bytearray(8)
    struct.pack_into("<I", value_blob, 0, 1 << 2)
    struct.pack_into("<I", value_blob, 4, 1 << 5)
    blob = _build_dti_record(bytes(value_blob))

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE comps (object_id INTEGER, record BLOB)")
    db.execute("INSERT INTO comps VALUES (1, ?)", (blob,))
    cur = db.cursor()

    values = _read_tag_initial_value(cur, 1, "BOOL", n_elements)

    assert len(values) == n_elements
    expected = [0] * n_elements
    expected[2] = 1
    expected[32 + 5] = 1
    assert values == expected


def test_read_tag_initial_value_uses_structural_offset_not_fixed_constant():
    # Regression test for a major bug: the value blob's start was assumed
    # to be a fixed absolute offset (0x1A2, +2 for arrays), but this was
    # disproven by a real Studio 5000 screenshot of a populated tag
    # (Trim_Decision) whose real values only decoded correctly using the
    # record's own *computed* offset (see _tag_value_blob_offset), which
    # varies by a couple of bytes depending on the record's own
    # extended_records lengths -- not on whether the tag is a scalar or an
    # array, and not on which UDT type is involved.
    #
    # Build two synthetic records whose "attr 0x1" boilerplate blob differs
    # in length (as real ACD records from different projects do), and
    # confirm the same logical value decodes correctly from each despite
    # sitting at a different absolute byte offset.
    for attr1_len in (286, 288):
        blob = _build_dti_record(struct.pack("<i", 42), attr1_len=attr1_len)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE comps (object_id INTEGER, record BLOB)")
        db.execute("INSERT INTO comps VALUES (1, ?)", (blob,))
        cur = db.cursor()

        value = _read_tag_initial_value(cur, 1, "DINT", 1)

        assert value == 42


def test_read_tag_initial_value_array_uses_structural_offset():
    # A genuine one-element array (Dimensions="1") must decode via the same
    # structurally-computed offset as any other array -- n_elements alone
    # can't distinguish scalar vs array, only is_array can (see the
    # identical distinction for collapsing to a scalar return value).
    blob = _build_dti_record(struct.pack("<i", 42))

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE comps (object_id INTEGER, record BLOB)")
    db.execute("INSERT INTO comps VALUES (1, ?)", (blob,))
    cur = db.cursor()

    value = _read_tag_initial_value(cur, 1, "DINT", 1, is_array=True)

    assert value == [42]


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


def test_udt_scalar_to_xml_scalar_member_infinity_uses_truncated_form():
    # Regression test: _udt_scalar_to_xml()'s own scalar-member branch used
    # to call _decorated_real_literal(val, in_array=False) for a REAL/LREAL
    # member -- reasoning (at the time, unverified) that a scalar UDT member
    # should follow the same "bare 1.#INF label" convention as a top-level
    # scalar TAG's own Decorated value. Real ground truth disproves this: a
    # real Studio 5000 "Export Tag" of AOI instance tag TestFPM
    # (AOI_RPMtoFPM) shows its scalar REAL member SurfaceFPM (+Infinity,
    # never statically configured -- a runtime-computed value) rendering as
    # Value="1.$", the SAME truncated form already established for a
    # literal array Element, not "1.#INF". The quirk is apparently about
    # going through Studio's shared member-traversal exporter at all (array
    # element OR struct member), not specifically about being inside an
    # array -- fixed by passing in_array=True at this call site too.
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User", [_member("SurfaceFPM", "REAL")],
    )
    data_types_map = {"OUTER": outer_dt}

    xml = _udt_scalar_to_xml("OUTER", {"SurfaceFPM": float("inf")}, data_types_map)

    assert 'Value="1.$"' in xml
    assert "1.#INF" not in xml


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
    # Regression test for a real precision bug: a naive "%.6g" truncates
    # any value needing more than 6 significant digits to round-trip to
    # the same float32 bit pattern -- found via a real Studio 5000 "Tag
    # Name Collision / Data Compare" dialog showing several REAL members
    # (LugMn, Frequency, RPM, etc.) each differing from ours only in
    # digit count despite the underlying decoded bytes being correct. The
    # value below (a real float32 bit pattern) needs 7 significant digits
    # to round-trip -- "%.6g" silently produces "0.404762", which does NOT
    # round-trip to the same float32 bits (verified: struct.pack("<f",
    # 0.404762) != struct.pack("<f", 0.4047619)).
    assert _decorated_real_literal(0.4047619, in_array=False) == "0.4047619"


def _member(name, data_type, byte_offset=0, dimension=0):
    return Member(
        name, name, data_type, dimension, "Decimal", False, None, None,
        "Read/Write", _byte_offset=byte_offset,
    )


def test_decode_single_udt_element_bool_array_member_bit_packing():
    # Regression test for a real bug: a UDT member that's a BOOL array
    # (e.g. Encoder's "Ons", BOOL[32]) was decoded one raw byte per element
    # (elem_size=1 from _get_type_size("BOOL", ...)) instead of extracting
    # each element's bit from its shared, bit-packed 4-byte DWORD -- the
    # same class of bug already fixed for a top-level primitive BOOL-array
    # *tag* in _read_tag_initial_value, but never applied to this
    # UDT-member decode path. Found via a real Studio 5000 "Tag Name
    # Collision / Data Compare" dialog: EncTrm.Ons[5] decoded as 1 instead
    # of the real 0 -- only one of 32 elements differed, since the wrong
    # per-byte read coincidentally matches the true packed bit for most
    # positions.
    #
    # Build a blob with only bit 5 and bit 20 set in the packed DWORD.
    blob = bytearray(4)
    struct.pack_into("<I", blob, 0, (1 << 5) | (1 << 20))
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User",
        [_member("Ons", "BOOL", byte_offset=0, dimension=32)],
    )
    data_types_map = {"OUTER": outer_dt}

    result = _decode_single_udt_element(bytes(blob), 0, outer_dt, data_types_map, 0)

    expected = [0] * 32
    expected[5] = 1
    expected[20] = 1
    assert result["Ons"] == expected


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


def test_decode_single_udt_element_unsigned_integer_members_not_empty_dict():
    # Regression test for a real, severe report: USINT/UINT/UDINT/ULINT were
    # entirely missing from _PRIM (types.py) -- _decode_scalar_member() fell
    # through to data_types_map.get("USINT"), which resolves to a REAL but
    # EMPTY (cls="ProductDefined", zero members) DataType ControllerBuilder
    # seeds for every RxDataTypeCollection entry including Rockwell's own
    # placeholder records for primitive type names -- so a USINT member
    # silently decoded to {} (an empty dict, from looping over zero members)
    # instead of a real int. Downstream, _udt_scalar_to_xml() then treated
    # the empty dict as a nested UDT, got nothing back, and silently dropped
    # the member from Decorated output entirely -- confirmed against a real
    # AOI instance (VAB_SQL_ParseTDSResponse_Inst/LastErrorClass+LastErrorState,
    # both USINT) where this caused Studio 5000 to reject the exported L5X
    # ("Data type mismatch") since the L5K and Decorated blocks for the same
    # tag disagreed. Fixed by adding all four unsigned types to _PRIM with
    # the same raw byte format/size as their BYTE/WORD/DWORD/LWORD siblings.
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User",
        [
            _member("A", "USINT", byte_offset=0),
            _member("B", "UINT", byte_offset=1),
            _member("C", "UDINT", byte_offset=4),
            _member("D", "ULINT", byte_offset=8),
        ],
    )
    data_types_map = {"OUTER": outer_dt}
    blob = bytearray(16)
    struct.pack_into("<B", blob, 0, 200)        # A: USINT, > 127 -- would be negative if misread as signed SINT
    struct.pack_into("<H", blob, 1, 50000)      # B: UINT, > 32767 -- would be negative if misread as signed INT
    struct.pack_into("<I", blob, 4, 3000000000)  # C: UDINT, > 2^31 -- would be negative if misread as signed DINT
    struct.pack_into("<Q", blob, 8, 10000000000000000000)  # D: ULINT, > 2^63
    blob = bytes(blob)

    result = _decode_single_udt_element(blob, 0, outer_dt, data_types_map, 0)

    assert result == {"A": 200, "B": 50000, "C": 3000000000, "D": 10000000000000000000}


def test_get_type_size_unsigned_integer_types():
    assert _get_type_size("USINT", {}) == 1
    assert _get_type_size("UINT", {}) == 2
    assert _get_type_size("UDINT", {}) == 4
    assert _get_type_size("ULINT", {}) == 8


def test_udt_scalar_to_xml_renders_unsigned_integer_member():
    outer_dt = DataType(
        "Outer", "Outer", "NoFamily", "User", [_member("LastErrorClass", "USINT")],
    )
    data_types_map = {"OUTER": outer_dt}

    xml = _udt_scalar_to_xml("OUTER", {"LastErrorClass": 5}, data_types_map)

    assert '<DataValueMember Name="LastErrorClass" DataType="USINT" Radix="Decimal" Value="5"/>' == xml


def test_zero_value_for_member_scalar_primitive():
    assert _zero_value_for_member(_member("Max_Qty", "DINT"), {}) == 0
    assert _zero_value_for_member(_member("Ratio", "REAL"), {}) == 0.0
    assert _zero_value_for_member(_member("Enabled", "BOOL"), {}) == 0


def test_zero_value_for_member_array():
    assert _zero_value_for_member(_member("Specie", "DINT", dimension=3), {}) == [0, 0, 0]


def test_zero_value_for_member_handles_none_dimension():
    # Regression test for a real crash report: `member.dimension > 0`
    # assumed dimension is always an int, but a Member constructed with an
    # explicit dimension=None (e.g. a caller mistaking new_member()'s
    # dimension param for one of its OTHER params, where None means "use
    # the default") raised `TypeError: '>' not supported between instances
    # of 'NoneType' and 'int'`. None must be treated the same as 0 (scalar).
    assert _zero_value_for_member(_member("Weird", "DINT", dimension=None), {}) == 0


def test_zero_value_for_member_nested_struct():
    inner_dt = DataType("Inner", "Inner", "NoFamily", "User", [_member("Val", "DINT")])
    data_types_map = {"INNER": inner_dt}
    assert _zero_value_for_member(_member("Nested", "Inner"), data_types_map) == {"Val": 0}


def test_zero_value_for_member_builtin_timer():
    # Regression test for a real report: a member typed as Rockwell's own
    # built-in TIMER struct (never a row in data_types_map -- it's not a
    # project UDT at all) used to fall through to the generic "unknown
    # type" fallback and zero-fill as a bare 0 instead of the real
    # {"PRE": 0, "ACC": 0, "EN": 0, "TT": 0, "DN": 0} shape -- which then
    # broke navigating INTO it one level up the call stack
    # (db_set_tag_element_value()).
    assert _zero_value_for_member(_member("StartFaultTimer", "TIMER"), {}) == {
        "PRE": 0, "ACC": 0, "EN": 0, "TT": 0, "DN": 0,
    }


def test_zero_value_for_member_builtin_counter():
    assert _zero_value_for_member(_member("Cnt", "COUNTER"), {}) == {
        "PRE": 0, "ACC": 0, "CU": 0, "CD": 0, "DN": 0, "OV": 0, "UN": 0,
    }


def test_zero_value_for_member_builtin_timer_array():
    result = _zero_value_for_member(_member("Timers", "TIMER", dimension=2), {})
    assert result == [
        {"PRE": 0, "ACC": 0, "EN": 0, "TT": 0, "DN": 0},
        {"PRE": 0, "ACC": 0, "EN": 0, "TT": 0, "DN": 0},
    ]


def test_zero_value_for_member_struct_containing_builtin_timer_member():
    # The exact shape from the real report: a project UDT with a member
    # typed as the built-in TIMER struct, nested inside the recursion.
    motor_dt = DataType("Motor", "Motor", "NoFamily", "User",
                         [_member("Run", "BOOL"), _member("StartFaultTimer", "TIMER")])
    data_types_map = {"MOTOR": motor_dt}
    assert _zero_value_for_member(_member("M", "Motor"), data_types_map) == {
        "Run": 0,
        "StartFaultTimer": {"PRE": 0, "ACC": 0, "EN": 0, "TT": 0, "DN": 0},
    }


def test_validate_tag_types_resolve_raises_on_unresolved_type():
    # This is the exact failure signature the stale-Tag._data_types_map bug
    # (see CLAUDE.md "Mutating a UDT with live tag instances...") produced
    # silently: a struct-typed tag whose type name isn't in data_types_map
    # at all used to fall through to _zero_value_for_member's "harmless
    # scalar zero" fallback with no error anywhere. validate=True should
    # catch this eagerly instead.
    tag = Tag("MyTag", "MyTag", "Base", "NotARealType", None, "Read/Write", None, None)
    with pytest.raises(ValueError, match="NotARealType"):
        _validate_tag_types_resolve([tag], {})


def test_validate_tag_types_resolve_passes_when_type_resolves():
    inner_dt = DataType("Inner", "Inner", "NoFamily", "User", [_member("Val", "DINT")])
    tag = Tag("MyTag", "MyTag", "Base", "Inner", None, "Read/Write", None, None)
    _validate_tag_types_resolve([tag], {"INNER": inner_dt})  # must not raise


def test_validate_tag_types_resolve_raises_on_nested_unresolved_member():
    # The unresolved type doesn't have to be the tag's own top-level type --
    # a member several levels deep referencing an unresolved type must be
    # caught too, with the nested member path named in the error.
    inner_dt = DataType(
        "Outer", "Outer", "NoFamily", "User",
        [_member("Bad", "AlsoNotReal")],
    )
    tag = Tag("MyTag", "MyTag", "Base", "Outer", None, "Read/Write", None, None)
    with pytest.raises(ValueError, match="AlsoNotReal"):
        _validate_tag_types_resolve([tag], {"OUTER": inner_dt})


def test_validate_tag_types_resolve_allows_builtin_struct_types():
    # TIMER/COUNTER/CONTROL are real, legitimate struct types that are
    # deliberately NOT in data_types_map (they're built into Logix, not
    # user DataTypes) -- these must never be flagged as unresolved.
    for builtin in ("TIMER", "COUNTER", "CONTROL"):
        tag = Tag("MyTag", "MyTag", "Base", builtin, None, "Read/Write", None, None)
        _validate_tag_types_resolve([tag], {})  # must not raise


def test_validate_tag_types_resolve_allows_string_family_types():
    tag = Tag("MyTag", "MyTag", "Base", "STRING", None, "Read/Write", None, None)
    _validate_tag_types_resolve([tag], {})  # must not raise


def test_l5k_udt_literal_zero_fills_scalar_member_missing_from_decoded_value():
    # Regression test for a real Studio 5000 import rejection ("Data type
    # mismatch"): appending a new Member to an existing DataType's
    # .members list (e.g. via export_datatype()'s own documented pattern)
    # does NOT retroactively re-derive an already-decoded tag value for
    # that type -- the new member is simply absent as a key. Previously
    # this function skipped any member missing from the decoded value
    # dict entirely, so the L5K literal came out one element short of
    # what the type's own (freshly-rendered) declaration says it has.
    dt = DataType(
        "Bin", "Bin", "NoFamily", "User",
        [_member("Max_Qty", "DINT"), _member("Criteria_Qty", "DINT")],
    )
    data_types_map = {"BIN": dt}
    # Decoded before Criteria_Qty existed on the type -- no such key.
    values = {"Max_Qty": 5}

    assert _l5k_udt_literal("Bin", values, data_types_map) == "[5,0]"


def test_l5k_udt_literal_zero_fills_struct_member_missing_from_decoded_value():
    # Same bug, real shape: the new member (Criteria_Qty) was itself a
    # struct type (Bin_Criteria_Qty, one DINT[3] member) -- verified this
    # recurses through _zero_value_for_member correctly rather than just
    # handling a bare scalar.
    criteria_dt = DataType(
        "Bin_Criteria_Qty", "Bin_Criteria_Qty", "NoFamily", "User",
        [_member("Specie", "DINT", dimension=3)],
    )
    bin_dt = DataType(
        "Bin", "Bin", "NoFamily", "User",
        [_member("Max_Qty", "DINT"), _member("Criteria_Qty", "Bin_Criteria_Qty")],
    )
    data_types_map = {"BIN": bin_dt, "BIN_CRITERIA_QTY": criteria_dt}
    values = {"Max_Qty": 5}

    assert _l5k_udt_literal("Bin", values, data_types_map) == "[5,[[0,0,0]]]"


def test_udt_scalar_to_xml_zero_fills_member_missing_from_decoded_value():
    # Decorated-format counterpart of the L5K tests above -- same root
    # cause, same fix, different renderer.
    criteria_dt = DataType(
        "Bin_Criteria_Qty", "Bin_Criteria_Qty", "NoFamily", "User",
        [_member("Specie", "DINT", dimension=3)],
    )
    bin_dt = DataType(
        "Bin", "Bin", "NoFamily", "User",
        [_member("Max_Qty", "DINT"), _member("Criteria_Qty", "Bin_Criteria_Qty")],
    )
    data_types_map = {"BIN": bin_dt, "BIN_CRITERIA_QTY": criteria_dt}
    values = {"Max_Qty": 5}

    xml = _udt_scalar_to_xml("Bin", values, data_types_map)

    assert '<DataValueMember Name="Max_Qty" DataType="DINT" Radix="Decimal" Value="5"/>' in xml
    assert '<StructureMember Name="Criteria_Qty" DataType="Bin_Criteria_Qty">' in xml
    assert '<ArrayMember Name="Specie" DataType="DINT" Dimensions="3" Radix="Decimal">' in xml
    assert xml.count('Value="0"') == 3  # the three zero-filled Specie elements


def test_get_type_size_rounds_up_to_multiple_of_4_not_merely_even():
    # Regression test for a real, previously-undetected bug: _get_type_size()
    # rounded a UDT's computed size up to the next EVEN byte count
    # (`max_end + max_end % 2`), not a true multiple of 4, despite both the
    # docstring and the commit that "fixed" this explicitly saying "multiple
    # of 4" (confirmed by the user: "UDT can only have a multiple of 4 byte
    # total size"). The bug went undetected because the one real-world case
    # used to verify that fix (Encoder, 263 -> 264) is ambiguous: 264 is
    # both the next even number AND the next multiple of 4 above 263, so it
    # can't distinguish the two rounding rules. A real UDT ("FenceSkid",
    # members summing to 13 bytes) exposed it: the old code returned 14
    # (even, wrong) instead of 16 (multiple of 4, correct) -- and because
    # this size was also used as an ARRAY element's stride
    # (FenceGate.Skid[2], and by extension every element of a FenceGate[]
    # array tag beyond index 0), the 2-byte shortfall corrupted every
    # subsequent array element's decoded field values, confirmed via a real
    # Studio 5000 "Tag Name Collision" dialog.
    #
    # 13 bytes is the distinguishing case this test locks in: even-rounding
    # gives 14, multiple-of-4 gives 16 -- only the latter is correct.
    dt = DataType(
        "Odd13", "Odd13", "NoFamily", "User",
        [
            _member("a", "DINT", byte_offset=0),   # 0-3
            _member("b", "DINT", byte_offset=4),   # 4-7
            _member("c", "DINT", byte_offset=8),   # 8-11
            _member("d", "SINT", byte_offset=12),  # 12 (1 byte) -> max_end=13
        ],
    )
    data_types_map = {"ODD13": dt}
    assert _get_type_size("ODD13", data_types_map) == 16


def test_get_type_size_does_not_add_dead_member_bytes():
    # _get_type_size() must NOT add dt._dead_member_bytes -- an earlier
    # version of this function did, on the untested assumption that it
    # would also apply to array-element striding the same way it applies
    # to a scalar struct member's trailing siblings. Verified wrong against
    # a real 200-element array of the exact UDT this was found on: the true
    # per-element stride matched the plain max(offset+size) computation
    # with NO dead-byte addition. _apply_dead_member_byte_corrections()
    # handles the scalar-sibling case separately and correctly.
    inner_dt = DataType(
        "Inner", "Inner", "NoFamily", "User",
        [_member("a", "DINT", byte_offset=0)],  # size: 4 bytes
        _dead_member_bytes=2,
    )
    data_types_map = {"INNER": inner_dt}
    assert _get_type_size("INNER", data_types_map) == 4


def test_apply_dead_member_byte_corrections_is_a_noop():
    # Regression test for a real, disproven theory: a scalar struct-typed
    # member ("b", typed "Inner") whose nested DataType has dead/deleted
    # bytes used to shift every member declared AFTER it in the outer
    # struct, on the theory that a deleted member's old byte range keeps
    # occupying space in an already-allocated tag's data table. A real
    # Studio 5000 screenshot of a populated tag with exactly this shape
    # (Trim_Decision: LugWrk.BfrLug -> Lug, which has a deleted member)
    # proved this wrong -- the real values only decode correctly using
    # each member's own *raw*, uncorrected stored byte_offset (the +2 the
    # tag's real data needed came entirely from _tag_value_blob_offset()'s
    # own, per-tag structural offset, not from any member-level shift).
    # This function is now a no-op; member offsets must be left exactly as
    # DataTypeBuilder stored them, dead bytes or not.
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
    assert b._byte_offset == 0
    assert c._byte_offset == 4  # unchanged -- no correction applied
    assert d._byte_offset == 6  # unchanged -- no correction applied


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


def test_decode_string_family_value_uses_latin1_never_replacement_char():
    # Regression test for a real bug: decoding raw STRING bytes as utf-8
    # (with errors="replace") inserted U+FFFD for any byte sequence that
    # wasn't valid UTF-8 -- found via a real array tag whose STRING member
    # held uninitialized/garbage data. latin-1 is a 1:1 byte<->codepoint
    # mapping that can never fail, so U+FFFD must never appear.
    blob = struct.pack("<i", 4) + bytes([0xC7, 0x65, 0x02, 0x01]) + b"\x00" * 82
    result = _decode_string_family_value(blob, 0, "STRING", {})
    assert result["LEN"] == 4
    assert "�" not in result["DATA"]
    assert result["DATA"] == "\xc7\x65\x02\x01"


def test_l5k_string_padded_escapes_non_ascii_bytes():
    # Regression test for a real Studio 5000 import rejection ("Only ASCII
    # characters are supported") on a tag's <Data Format="L5K"> element:
    # a non-ASCII character (originating from a byte that isn't valid
    # UTF-8, previously mis-decoded as U+FFFD -- see the decode test above)
    # must be $XX-hex-escaped the same way control characters already are,
    # not embedded raw.
    result = _l5k_string_padded("\xc7\x65", capacity=4)
    assert result == "'$C7e$00$00'"
    assert all(ord(c) <= 0x7E for c in result)


def test_l5k_string_padded_still_escapes_control_chars():
    result = _l5k_string_padded("\x00\x1b", capacity=2)
    assert result == "'$00$1B'"


def test_string_literal_cdata_escapes_control_and_non_ascii_as_dollar_hex():
    # Real Studio export of a binary STRING_480 tag's <Data Format="String">
    # and of a Decorated DATA member: '$10$01$00$D6...p$00...'. We used to
    # write XML character references (&#x0010;), which Studio reads as a
    # different string and rejected on import ("Invalid size.").
    from acd.l5x.elements.rendering import _string_literal_cdata
    assert _string_literal_cdata("\x10\x01\x00\xd6p") == "<![CDATA['$10$01$00$D6p']]>"
    assert _string_literal_cdata("192.168.5.100?port=1433") == "<![CDATA['192.168.5.100?port=1433']]>"
    assert _string_literal_cdata("a$b") == "<![CDATA['a$$b']]>"


def test_string_literal_cdata_empty_forms():
    # 1,444 real empty Decorated DATA members are bare; 10 real empty
    # <Data Format="String"> blocks are ''.
    from acd.l5x.elements.rendering import _string_literal_cdata
    assert _string_literal_cdata("") == "<![CDATA[]]>"
    assert _string_literal_cdata("", empty_quoted=True) == "<![CDATA['']]>"


def test_string_tag_string_format_block_uses_dollar_escapes_and_quoted_empty():
    from acd.l5x.elements import DataType, new_member, new_tag
    string_dt = DataType("STRING_8", "STRING_8", "StringFamily", "User", [
        new_member("LEN", "DINT"),
        new_member("DATA", "SINT", dimension=8, radix="ASCII"),
    ])
    types = {"STRING_8": string_dt}

    def render(value):
        tag = new_tag("T", "STRING_8")
        tag._initial_value = value
        tag._data_types_map = types
        return tag.to_xml()

    binary = render({"LEN": 3, "DATA": "\x10\x00\xd6"})
    assert "<![CDATA['$10$00$D6']]>" in binary
    assert "&#x" not in binary
    assert "<![CDATA['']]>" in render({"LEN": 0, "DATA": ""})


def _rx_generic_header(cip_type=999):
    # 14-byte fixed header + 60-byte opaque main_record, enough for
    # RxGeneric.from_bytes() to parse regardless of cip_type (main_record
    # content is never consulted by DataTypeBuilder/MemberBuilder).
    header = struct.pack("<IIHHH", 0, 0, 40, cip_type, 0)
    main_record = b"\x00" * 60
    return header + main_record


def _member_ext_record_value(name: str, data_type_id: int, dimension: int = 0,
                              byte_offset: int = 0, radix: int = 4) -> bytes:
    # The "value" payload of a member's own extended record (attribute_id
    # >= 0x6E on the owning DataType's comps row) -- decoded for its name by
    # DataTypeBuilder._decode_member_name and for its type/dimension/offset
    # etc. by MemberBuilder.build(), both via fixed byte offsets into this
    # same blob. Must be at least 0x7C bytes for every offset MemberBuilder
    # reads (up to 0x78) to stay in bounds.
    blob = bytearray(0x7C)
    encoded_name = name.encode("utf-16-le") + b"\x00\x00"
    blob[0:len(encoded_name)] = encoded_name
    struct.pack_into("<I", blob, 0x54, radix)
    struct.pack_into("<I", blob, 0x58, data_type_id)
    struct.pack_into("<I", blob, 0x5C, dimension)
    struct.pack_into("<I", blob, 0x60, byte_offset)
    struct.pack_into("<I", blob, 0x68, 0x800)
    struct.pack_into("<I", blob, 0x6C, 0xFFFFFFFF)
    return bytes(blob)


def _child_record() -> bytes:
    # A minimal member-collection child's own comps record -- just needs to
    # parse via RxGeneric (member_ref at byte [14:18] left 0 so MemberBuilder
    # skips the comment lookup entirely).
    return _rx_generic_header() + struct.pack("<II", 0, 1)  # len_record, count_record=1 (0 parsed)


def test_datatype_builder_excludes_deleted_member_with_stale_extended_record():
    # Regression test for a real bug: deleting a UDT member marks its own
    # member-collection child row's record_type 512 (the same live/deleted
    # marker already used elsewhere for Program/Module/Tag/Routine phantom
    # filtering), but does NOT necessarily remove that member's own
    # extended-record descriptor from the *type's* own comps row. Before
    # this fix, DataTypeBuilder matched a stale extended record's name
    # against ANY child row regardless of record_type, silently resurrecting
    # a deleted member as if it were live. Found via a real project (UDT
    # "Trimmer", 15 scalar members consolidated into an array "Saw_Pos[32]"
    # and re-saved): the type's own declared member_count correctly read
    # back as 1, but 15 stale extended records for the deleted scalars were
    # still present, each matching its own still-present (record_type=512)
    # child row.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute(
        "CREATE TABLE comments(parent int, member_ref int, record_string text)"
    )
    cur = db.cursor()

    DINT_ID = 100
    TYPE_ID = 200
    MEMBER_COLLECTION_ID = 300
    LIVE_CHILD_ID = 400
    DELETED_CHILD_ID = 401

    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (DINT_ID, 0, "DINT", 0, 256, b""),
    )

    foo_member = _member_ext_record_value("Foo", DINT_ID, dimension=0, byte_offset=0)
    old_member = _member_ext_record_value("OldMember", DINT_ID, dimension=0, byte_offset=4)
    extended_records = (
        struct.pack("<II", 0x6C, 4) + struct.pack("<I", 0) +   # string_family = NoFamily
        struct.pack("<II", 0x67, 4) + struct.pack("<I", 0) +   # built_in = 0
        struct.pack("<II", 0x69, 4) + struct.pack("<I", 0) +   # module_defined = 0
        struct.pack("<II", 0x64, 4) + struct.pack("<I", 1) +   # declared member_count = 1
        struct.pack("<II", 0x6E, len(foo_member)) + foo_member +
        struct.pack("<II", 0x6F, len(old_member)) + old_member
    )
    count_record = 7  # 6 parsed + 1 always left unparsed by RxGeneric._read()
    type_record = (
        _rx_generic_header() + struct.pack("<II", 0, count_record) + extended_records
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (TYPE_ID, 0, "TestType", 0, 256, type_record),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (MEMBER_COLLECTION_ID, TYPE_ID, "RxTypeMemberCollection", 0, 0, b""),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (LIVE_CHILD_ID, MEMBER_COLLECTION_ID, "Foo", 0, 256, _child_record()),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (DELETED_CHILD_ID, MEMBER_COLLECTION_ID, "OldMember", 1, 512, _child_record()),
    )
    db.commit()

    dt = DataTypeBuilder(cur, TYPE_ID).build()

    member_names = [m.name for m in dt.members]
    assert member_names == ["Foo"], (
        "deleted member 'OldMember' (record_type=512) must not be resurrected "
        "just because a stale extended record still names it"
    )
    assert dt._dead_member_bytes == 2


def test_datatype_builder_returns_none_for_deleted_aoi_tombstone():
    # Regression test for a real, severe bug: deleting an AOI in Studio 5000
    # does not remove its implicit DataType comps entry (the synthetic
    # instance-shape record every AOI gets under RxDataTypeCollection) from
    # the raw ACD binary -- the entry (name, object_id) stays in place, but
    # ALL of its extended records are stripped away, leaving a completely
    # empty dict. Confirmed via a real project: deleting an AOI and
    # re-saving leaves its own DataType comps row in place with
    # extended_records parsing to zero keys. Before this fix,
    # DataTypeBuilder.build() unconditionally read extended_records[0x6C],
    # raising a bare KeyError that took down the ENTIRE project load (every
    # db_* call), not just this one object -- any project with AOI-deletion
    # history can hit this on an ordinary load.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()

    TYPE_ID = 950
    # count_record=1 -> RxGeneric's own "always leaves the last one unparsed"
    # quirk means 0 extended records actually get parsed -- the exact real
    # tombstone shape (same construction as this file's own _child_record()).
    tombstone_record = _child_record()
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (TYPE_ID, 0, "DeletedAoiTombstone", 0, 256, tombstone_record),
    )
    db.commit()

    dt = DataTypeBuilder(cur, TYPE_ID).build()

    assert dt is None


def test_datatype_builder_defaults_when_family_builtin_moduledefined_missing():
    # Companion to the tombstone test above, for the narrower case the bug
    # report also flagged: a type with SOME extended records present, just
    # not 0x6C/0x67/0x69 specifically (unlike a fully-empty tombstone, this
    # is otherwise a perfectly real, buildable type). Before this fix this
    # raised the identical bare KeyError.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()

    TYPE_ID = 951
    # Only 0x64 (member_count) present -- no 0x6C/0x67/0x69 at all.
    extended_records = struct.pack("<II", 0x64, 4) + struct.pack("<I", 0)
    count_record = 2  # 1 parsed (0x64) + 1 always left unparsed
    type_record = (
        _rx_generic_header() + struct.pack("<II", 0, count_record) + extended_records
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (TYPE_ID, 0, "PartialType", 0, 256, type_record),
    )
    db.commit()

    dt = DataTypeBuilder(cur, TYPE_ID).build()

    assert dt is not None
    assert dt.family == "NoFamily"
    assert dt.cls == "User"
    assert dt.members == []


_AOI_TAG_RECORD_DEFAULT_DT_OID = 999900  # arbitrary, must match a comps row callers insert


def _aoi_tag_record(member_ref: int, is_param: bool,
                     data_type_oid: int = _AOI_TAG_RECORD_DEFAULT_DT_OID,
                     dim1: int = 0, dim2: int = 0, dim3: int = 0,
                     usage: str = None) -> bytes:
    """Build a synthetic AOI RxTagCollection child (Parameter or LocalTag)
    comps record. `member_ref` (raw offset [14:18]) is the real Rockwell
    order key AoiBuilder now sorts by -- see its own docstring for how this
    was found: `seq_number` (a separate comps COLUMN, not part of this raw
    record) was found to be an unreliable/constant tie for most children of
    a real AOI, making `ORDER BY seq_number` alone produce an essentially
    arbitrary order. `is_param` sets ext01[0x20E] bit 0x04 (Input) so
    AoiBuilder classifies this child as a Parameter, or leaves it 0 so it's
    classified as a LocalTag (see `_aoi_tag_usage_flags`).

    `usage` overrides the plain `is_param` bool with an explicit
    "Input"/"Output"/"InOut" encoding (0x04/0x08/0x0C -- InOut is BOTH bits
    set, per `_aoi_tag_usage_flags`'s own docstring) -- needed for anything
    beyond the Input-vs-LocalTag distinction `is_param` alone can express,
    e.g. a real InOut Parameter (`is_param=True` alone only ever produces
    Input).

    `data_type_oid` (raw offset 0x2A, `_aoi_tag_data_type()`'s own DataType
    object_id pointer) defaults to `_AOI_TAG_RECORD_DEFAULT_DT_OID` --
    callers building a REAL (non-spurious) child must also insert a comps
    row with that object_id so it resolves to a real type name; pass 0 (or
    any object_id with no matching comps row) to build a spurious,
    blank-DataType child, the same shape AoiBuilder now filters out (see
    `test_aoi_builder_skips_*_with_unresolvable_data_type` below).

    `dim1`/`dim2`/`dim3` (raw offsets 0x1A/0x1E/0x22) are the same three
    dimension_1/2/3 u32 fields a regular Tag's RxGeneric main_record already
    exposes -- see the multi-dimensional AOI parameter/local tag decode fix.
    """
    header = struct.pack("<IIHHH", 0, 0, 40, 999, 0)  # 14 bytes
    main_record = bytearray(60)
    struct.pack_into("<I", main_record, 0, member_ref)  # this record's bytes [14:18]
    struct.pack_into("<I", main_record, 12, dim1)  # this record's bytes [26:30] (0x1A)
    struct.pack_into("<I", main_record, 16, dim2)  # this record's bytes [30:34] (0x1E)
    struct.pack_into("<I", main_record, 20, dim3)  # this record's bytes [34:38] (0x22)
    struct.pack_into("<I", main_record, 28, data_type_oid)  # this record's bytes [42:46] (0x2A)
    ext01 = bytearray(0x210)
    usage_bits = {"Input": 0x04, "Output": 0x08, "InOut": 0x0C}
    if usage is not None:
        ext01[0x20E] = usage_bits[usage]
    elif is_param:
        ext01[0x20E] = 0x04  # Input usage bit
    ext01_attr = struct.pack("<II", 0x01, len(ext01)) + bytes(ext01)
    dummy_last_attr = struct.pack("<II", 0x02, 4) + b"\x00" * 4  # left unparsed by RxGeneric
    count_record = 2  # 1 parsed (0x01) + 1 left unparsed
    return header + bytes(main_record) + struct.pack("<II", 0, count_record) + ext01_attr + dummy_last_attr


def test_aoi_builder_orders_parameters_by_member_ref_not_seq_number():
    # Regression test for a real, critical bug: a real project's AOI
    # (VAB_PowerFlex_753, 17 real parameters) had `seq_number` IDENTICAL
    # (a constant value) for 16 of its 19 RxTagCollection children --
    # AoiBuilder's old `ORDER BY seq_number` alone left SQLite's tie-break
    # order effectively arbitrary, silently scrambling the exported
    # <Parameters> order. Since AOI instructions are called POSITIONALLY in
    # RLL, a scrambled redefinition silently rebinds every existing call
    # site's arguments to the wrong parameter -- confirmed via a real Studio
    # 5000 import ("Differences exist between the instruction definitions",
    # no indication the difference is a reordering) followed by the user
    # directly comparing the AOI's own Parameters tab before/after. Root
    # cause: `member_ref` (already decoded by ParameterBuilder for comment
    # lookup, raw bytes [14:18]) turned out to already fully and correctly
    # encode the real order -- found by brute-force scanning every raw
    # record byte offset against the real AOI's own confirmed correct order.
    #
    # Three parameters inserted in an order that is the REVERSE of their
    # real (member_ref-sorted) order, all sharing the same seq_number --
    # exactly the real, observed tie-break scenario.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE nameless(parent_id int, record BLOB)")
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()

    AOI_ID = 500
    TAG_COLL_ID = 501
    THIRD_ID, FIRST_ID, SECOND_ID = 600, 601, 602  # deliberately reversed insertion order

    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (AOI_ID, 0, "TestAOI", 0, 256, b"\x00" * 20),  # too short to parse -- safe fallback path
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""),
    )
    # A resolvable DataType, matching _aoi_tag_record()'s default OID -- AoiBuilder
    # now filters out any child whose DataType OID doesn't resolve (see the
    # blank-DataType regression tests below), so a REAL/non-spurious child needs
    # one for these ordering tests to keep testing ordering, not that filter.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (_AOI_TAG_RECORD_DEFAULT_DT_OID, 0, "DINT", 0, 256, b""),
    )
    # Real intended order (by member_ref): First(10) < Second(20) < Third(30),
    # inserted here in the OPPOSITE order with an identical seq_number=0 for
    # every one, so a naive ORDER BY seq_number has nothing to discriminate on.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (THIRD_ID, TAG_COLL_ID, "Third", 0, 256, _aoi_tag_record(30, is_param=True)),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (FIRST_ID, TAG_COLL_ID, "First", 0, 256, _aoi_tag_record(10, is_param=True)),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (SECOND_ID, TAG_COLL_ID, "Second", 0, 256, _aoi_tag_record(20, is_param=True)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert [p.name for p in aoi.parameters] == ["First", "Second", "Third"], (
        "Parameters must be ordered by member_ref (the real Rockwell order key), "
        "not by seq_number/insertion order"
    )


def _order_list_record(oids) -> bytes:
    """A Nameless ordered-id-list record: u16 count at 24, then u32 ids."""
    return b"\x00" * 24 + struct.pack("<H", len(oids)) + b"".join(struct.pack("<I", o) for o in oids)


def test_aoi_builder_orders_by_nameless_display_list_over_member_ref():
    # A real AOI whose parameters were reordered in Studio after creation
    # (VAB_Unicode_To_ASCII_STRING: EN/DN/ER moved above Source/Dest) kept
    # creation-order member_refs, so ordering by member_ref exported the
    # wrong order -- which silently rebinds call-site arguments, since AOI
    # calls are positional. Studio's real display order is an ordered id
    # list in Nameless.Dat (AOI -> Nameless child -> one list for the
    # parameters, one for the local tags); verified 40/40 against Studio's
    # own export of a real project's 20 AOIs.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)")
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()

    AOI_ID, TAG_COLL_ID = 500, 501
    rows = [
        (AOI_ID, 0, "TestAOI", 0, 256, b"\x00" * 20),
        (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""),
        (_AOI_TAG_RECORD_DEFAULT_DT_OID, 0, "DINT", 0, 256, b""),
        # member_ref (creation) order: Source, Dest, EN; then locals A, B.
        (600, TAG_COLL_ID, "Source", 0, 256, _aoi_tag_record(10, is_param=True)),
        (601, TAG_COLL_ID, "Dest", 0, 256, _aoi_tag_record(20, is_param=True)),
        (602, TAG_COLL_ID, "EN", 0, 256, _aoi_tag_record(30, is_param=True)),
        (610, TAG_COLL_ID, "A", 0, 256, _aoi_tag_record(40, is_param=False)),
        (611, TAG_COLL_ID, "B", 0, 256, _aoi_tag_record(50, is_param=False)),
    ]
    cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", rows)
    cur.executemany("INSERT INTO nameless VALUES (?,?,?)", [
        (700, AOI_ID, b"\x00" * 32),                     # the AOI's Nameless child
        (701, 700, _order_list_record([602, 600, 601])),  # display order: EN, Source, Dest
        (702, 700, _order_list_record([611, 610])),       # local tags: B, A
    ])
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert [p.name for p in aoi.parameters] == ["EN", "Source", "Dest"]
    assert [t.name for t in aoi.local_tags] == ["B", "A"]


def test_aoi_builder_orders_local_tags_by_member_ref_too():
    # Same mechanism as the parameter-ordering test above, for LocalTags --
    # AoiBuilder applies the same sort to both lists from the same
    # RxTagCollection walk.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE nameless(parent_id int, record BLOB)")
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()

    AOI_ID = 700
    TAG_COLL_ID = 701
    THIRD_ID, FIRST_ID, SECOND_ID = 800, 801, 802

    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (AOI_ID, 0, "TestAOI2", 0, 256, b"\x00" * 20),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (_AOI_TAG_RECORD_DEFAULT_DT_OID, 0, "DINT", 0, 256, b""),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (THIRD_ID, TAG_COLL_ID, "LT_Third", 0, 256, _aoi_tag_record(30, is_param=False)),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (FIRST_ID, TAG_COLL_ID, "LT_First", 0, 256, _aoi_tag_record(10, is_param=False)),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (SECOND_ID, TAG_COLL_ID, "LT_Second", 0, 256, _aoi_tag_record(20, is_param=False)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert [lt.name for lt in aoi.local_tags] == ["LT_First", "LT_Second", "LT_Third"]


def test_aoi_builder_decodes_multi_dimensional_parameter():
    # Regression test for a real bug report: ParameterBuilder/LocalTagBuilder
    # only ever read dimension_1 (raw offset 0x1A), the same single u32 this
    # library's own new_aoi_parameter()/db_new_aoi_parameter() writer path
    # always round-tripped correctly (since it just stores/re-parses its own
    # comma-separated string verbatim, never actually re-encoding through
    # this raw offset) -- so the bug was invisible for any AOI this library
    # created and read back in-session. A REAL Studio 5000 save of a genuine
    # 2D InOut parameter (VAB_SQL_ParseResponseColumns/OutputRows,
    # VAB_SQL_Value[25,25], confirmed via Studio's own Properties dialog)
    # populates dimension_2 (raw offset 0x1E) too, which the old code never
    # read at all -- silently collapsing a real 2D array to dimensions="25"
    # on decode. Fixed by also reading dimension_2/dimension_3 (0x1E/0x22),
    # the same two fields TagBuilder.build() already reads for a regular Tag.
    db = _make_aoi_db()
    cur = db.cursor()

    AOI_ID, TAG_COLL_ID, DT_ID, PARAM_ID = 900, 901, 902, 903
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI3", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (DT_ID, 0, "DINT", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (PARAM_ID, TAG_COLL_ID, "OutputRows", 0, 256,
         _aoi_tag_record(10, is_param=True, data_type_oid=DT_ID, dim1=25, dim2=25)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert len(aoi.parameters) == 1
    assert aoi.parameters[0].dimensions == "25,25", (
        "A real 2D AOI parameter must decode both dimension_1 AND "
        "dimension_2, not collapse to rank 1 by only reading dimension_1"
    )


def test_aoi_builder_decodes_three_dimensional_local_tag():
    # Same fix, LocalTagBuilder side, and a real 3rd dimension too (dim3,
    # raw offset 0x22) -- not exercised by the 2D repro above.
    db = _make_aoi_db()
    cur = db.cursor()

    AOI_ID, TAG_COLL_ID, DT_ID, LT_ID = 910, 911, 912, 913
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI4", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (DT_ID, 0, "DINT", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (LT_ID, TAG_COLL_ID, "Cube", 0, 256,
         _aoi_tag_record(10, is_param=False, data_type_oid=DT_ID, dim1=4, dim2=3, dim3=2)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert len(aoi.local_tags) == 1
    assert aoi.local_tags[0].dimensions == "4,3,2"


def test_aoi_builder_scalar_parameter_still_decodes_no_dimensions():
    # Non-regression: a plain scalar parameter (dim1=dim2=dim3=0) must still
    # decode dimensions=None, not "0" or "".
    db = _make_aoi_db()
    cur = db.cursor()

    AOI_ID, TAG_COLL_ID, DT_ID, PARAM_ID = 920, 921, 922, 923
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI5", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (DT_ID, 0, "DINT", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (PARAM_ID, TAG_COLL_ID, "Scalar", 0, 256,
         _aoi_tag_record(10, is_param=True, data_type_oid=DT_ID)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()

    assert aoi.parameters[0].dimensions is None


def test_aoi_builder_inout_parameter_of_system_reference_type_omits_constant():
    # Real bug: a real project's own AOI exports show InOut parameters typed
    # MODULE ("Ethernet_Module") and AXIS_CIP_DRIVE ("Inp_Axis") with NO
    # Constant= attribute at all -- the old rule only omitted it for
    # data_type=="MESSAGE", so these two came out Constant="false", a real
    # difference from Studio's own export.
    db = _make_aoi_db()
    cur = db.cursor()

    AOI_ID, TAG_COLL_ID = 930, 931
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI6", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    rows = [
        (940, 0, "MODULE", 0, 256, b""),
        (941, 0, "AXIS_CIP_DRIVE", 0, 256, b""),
        (942, 0, "DINT", 0, 256, b""),
        (950, TAG_COLL_ID, "Ethernet_Module", 0, 256,
         _aoi_tag_record(10, is_param=True, data_type_oid=940, usage="InOut")),
        (951, TAG_COLL_ID, "Inp_Axis", 0, 256,
         _aoi_tag_record(20, is_param=True, data_type_oid=941, usage="InOut")),
        (952, TAG_COLL_ID, "PlainInOut", 0, 256,
         _aoi_tag_record(30, is_param=True, data_type_oid=942, usage="InOut")),
    ]
    cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", rows)
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()
    by_name = {p.name: p for p in aoi.parameters}

    assert by_name["Ethernet_Module"].usage == "InOut"
    assert by_name["Ethernet_Module"].constant is None
    assert by_name["Inp_Axis"].constant is None
    # Non-regression: an ordinary InOut parameter (a plain DINT, not a
    # system-reference type) still gets Constant="false".
    assert by_name["PlainInOut"].constant == "false"


def _make_aoi_db():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE nameless(parent_id int, record BLOB)")
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    return db


def test_aoi_builder_skips_parameter_with_unresolvable_data_type():
    # Regression test for a real, well-diagnosed report: AoiBuilder used to
    # include ANY RxTagCollection child classified as a parameter, even one
    # whose DataType OID (raw offset 0x2A) doesn't resolve to any live comps
    # row -- _aoi_tag_data_type() returns "" for exactly this case. A real
    # project had these show up with non-Logix-legal names ($11006696$,
    # __CLONE0000000E) and a blank DataType -- Rockwell-internal bookkeeping
    # (likely tied to in-place AOI rename, historically clone+delete under
    # the hood), not a real parameter, that AoiBuilder was picking up as if
    # it were one.
    db = _make_aoi_db()
    cur = db.cursor()
    AOI_ID, TAG_COLL_ID, JUNK_ID = 1000, 1001, 1100
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI3", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (JUNK_ID, TAG_COLL_ID, "$11006696$", 0, 256, _aoi_tag_record(10, is_param=True, data_type_oid=0)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()
    assert aoi.parameters == []


def test_aoi_builder_skips_local_tag_with_unresolvable_data_type():
    db = _make_aoi_db()
    cur = db.cursor()
    AOI_ID, TAG_COLL_ID, JUNK_ID = 1200, 1201, 1300
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI4", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (JUNK_ID, TAG_COLL_ID, "__CLONE0000000E", 0, 256, _aoi_tag_record(10, is_param=False, data_type_oid=0)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()
    assert aoi.local_tags == []


def test_aoi_builder_skips_colliding_named_junk_children_without_crashing():
    # The FATAL severity from the same report: several spurious children
    # sharing the IDENTICAL name (__CLONE0000000E x5 in the real project).
    # Before this fix, these were treated as real LocalTags and, once
    # inserted into proj_aoi_local_tags (which has a real UNIQUE(aoi_id,
    # name) index), a rebuild crashed with sqlite3.IntegrityError -- not a
    # scoped failure, it took down EVERY db_* call against that acd_path.
    # AoiBuilder itself must never even construct a LocalTag for these, so
    # there's nothing left for the SQL layer to collide on.
    db = _make_aoi_db()
    cur = db.cursor()
    AOI_ID, TAG_COLL_ID = 1400, 1401
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI5", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    for i, child_id in enumerate([1500, 1501, 1502, 1503, 1504]):
        cur.execute(
            "INSERT INTO comps VALUES (?,?,?,?,?,?)",
            (child_id, TAG_COLL_ID, "__CLONE0000000E", 0, 256,
             _aoi_tag_record(10 + i, is_param=False, data_type_oid=0)),
        )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()  # must not raise
    assert aoi.local_tags == []


def test_aoi_builder_skips_hex_named_local_tag_even_when_data_type_resolves_to_garbage():
    # Follow-up report, same failure shape as the two tests above but a real
    # data_type OID that does NOT dangle: after the user deleted local tags
    # directly in Studio's AOI editor, a leftover tombstone child named
    # "$ff73badc$" had its own DataType OID pointer resolve to a DIFFERENT
    # garbage/tombstone comps row (comp_name a single stray Unicode
    # combining-accent character) rather than to nothing at all -- so
    # `not local_tag.data_type` (blank-string check) never caught it, and
    # _validate_type_graph_resolves() correctly refused to export a member
    # whose type doesn't resolve to anything real, blocking db_get_aoi()/
    # db_export_aoi() entirely for this AOI. Fixed by also skipping any
    # child whose own name matches the same "$hex$" Rockwell-internal
    # placeholder convention already used elsewhere in this codebase
    # (builders_module.py), unconditionally -- regardless of whether its
    # DataType OID happens to dangle or happens to resolve to more garbage.
    db = _make_aoi_db()
    cur = db.cursor()
    AOI_ID, TAG_COLL_ID, GARBAGE_TYPE_ID, JUNK_ID = 1600, 1601, 1602, 1603
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (AOI_ID, 0, "TestAOI6", 0, 256, b"\x00" * 20))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TAG_COLL_ID, AOI_ID, "RxTagCollection", 0, 256, b""))
    # The garbage-named comps row the dangling-looking OID actually resolves
    # to -- a single stray Unicode combining-accent character, not blank and
    # not a real type name either.
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (GARBAGE_TYPE_ID, 0, "̀", 0, 256, b""))
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (JUNK_ID, TAG_COLL_ID, "$ff73badc$", 0, 256,
         _aoi_tag_record(10, is_param=False, data_type_oid=GARBAGE_TYPE_ID)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()
    assert aoi.local_tags == []


def _build_aoi_record(flags_byte: int) -> bytes:
    """Build a synthetic top-level AOI comps record with a given
    ExecutePrescan/ExecutePostscan/ExecuteEnableInFalse bitmask byte at
    ext01 relative offset 2 -- same RxGeneric-shaped record convention as
    `_aoi_tag_record()` above (header + main record + count_record +
    ext01 attribute + one deliberately-unparsed trailing attribute).
    """
    header = struct.pack("<IIHHH", 0, 0, 40, 999, 0)  # 14 bytes
    main_record = bytearray(60)
    ext01 = bytearray(40)
    ext01[2] = flags_byte
    ext01_attr = struct.pack("<II", 0x01, len(ext01)) + bytes(ext01)
    dummy_last_attr = struct.pack("<II", 0x02, 4) + b"\x00" * 4  # left unparsed by RxGeneric
    count_record = 2  # 1 parsed (0x01) + 1 left unparsed
    return (
        header + bytes(main_record) + struct.pack("<II", 0, count_record)
        + ext01_attr + dummy_last_attr
    )


@pytest.mark.parametrize(
    "flags_byte,expected",
    [
        (0x00, ("false", "false", "false")),
        (0x10, ("true", "false", "false")),
        (0x04, ("false", "true", "false")),
        (0x01, ("false", "false", "true")),
        (0x15, ("true", "true", "true")),
    ],
)
def test_aoi_builder_decodes_execute_flags_bitmask(flags_byte, expected):
    # Regression test for a real, confirmed bug: AoiBuilder.build() used to
    # hardcode ExecutePrescan/ExecutePostscan/ExecuteEnableInFalse to
    # "false", "false", "false" -- never actually reading them from the
    # real ACD binary at all. Reverse-engineered from 4 real, isolated
    # single-flag-at-a-time saves of the same real AOI
    # (VAB_SQL_BuildDelimString, Bethel_Planer project): baseline 0x00
    # (all false) -> +ExecutePrescan 0x10 -> +ExecutePostscan 0x14 ->
    # +ExecuteEnableInFalse 0x15, each save changing only the one expected
    # bit relative to the previous save, at ext01 relative offset 2. This
    # test locks in each bit independently (not just the combinations
    # actually observed in the real saves) so a future refactor can't
    # silently transpose two bits and still pass.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE nameless(parent_id int, record BLOB)")
    db.execute(
        "CREATE TABLE comments(parent int, member_ref int, record_string text, "
        "record_type int, tag_reference text)"
    )
    cur = db.cursor()

    AOI_ID = 900
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (AOI_ID, 0, "TestAOI", 0, 256, _build_aoi_record(flags_byte)),
    )
    db.commit()

    aoi = AoiBuilder(cur, AOI_ID).build()
    assert (aoi.execute_prescan, aoi.execute_postscan, aoi.execute_enable_in_false) == expected


def _st_line_record(seq: int, text: str) -> bytes:
    """Build a synthetic Structured Text source-line record: a 24-byte
    header (bytes[4:8] = _ST_LINE_RECORD_TYPE, bytes[20:24] = seq) followed
    by an fffeff-encoded UTF-16 line (the short, <=254-char form)."""
    header = bytearray(24)
    struct.pack_into("<I", header, 4, 0x01000002)
    struct.pack_into("<I", header, 20, seq)
    encoded_text = text.encode("utf-16-le")
    body = b"\xff\xfe\xff" + bytes([len(text)]) + encoded_text
    return bytes(header) + body


def _st_region_stub_record(child_object_ids) -> bytes:
    """Build a synthetic "region stub" record carrying the real, ordered
    child-line-object-id list (see `_st_line_order_index()`): a 24-byte
    header (record type deliberately NOT `_ST_LINE_RECORD_TYPE`, so it's
    never mistaken for a line record), a u16 count at offset 24, then that
    many little-endian u32 object ids."""
    header = bytearray(24)
    struct.pack_into("<I", header, 4, 2)  # any value != _ST_LINE_RECORD_TYPE
    count_and_list = struct.pack("<H", len(child_object_ids))
    for oid in child_object_ids:
        count_and_list += struct.pack("<I", oid)
    return bytes(header) + count_and_list


def test_st_line_order_index_detects_region_stub_shape():
    from acd.l5x.elements.builders_routine import _st_line_order_index

    rec = _st_region_stub_record([111, 222, 333])
    assert _st_line_order_index(rec) == [111, 222, 333]


def test_st_line_order_index_rejects_non_matching_shapes():
    from acd.l5x.elements.builders_routine import _st_line_order_index

    # A real line record must never be misdetected as an order-list record.
    assert _st_line_order_index(_st_line_record(5, "XIC(A)OTE(B);")) is None
    # Too short to even hold the count field.
    assert _st_line_order_index(b"\x00" * 10) is None
    # An empty list (count=0) -- excluded deliberately, see the function's
    # own docstring (an all-sentinel shadow region can spuriously satisfy
    # the length arithmetic with zero entries).
    assert _st_line_order_index(_st_region_stub_record([])) is None


def test_st_routine_lines_uses_region_order_list_not_scrambled_seq_numbers():
    # Regression test for a real, well-diagnosed report: after a user
    # hand-reordered a CASE statement directly in Studio 5000's ST editor,
    # Studio assigned the touched lines fresh, much-larger sequence numbers
    # (observed jumping from a tight ~94-134 range to 1429-1443 in the real
    # project) rather than renumbering the whole routine -- sorting by
    # sequence number alone (the old behavior) then scrambled the routine's
    # real order outright. The owning region's OWN record carries the real,
    # authoritative order as an explicit child-object-id list; this must be
    # used in preference to sequence-number sorting whenever it's present.
    #
    # Three lines, deliberately inserted with seq numbers in the WRONG
    # (reverse) order relative to their real, intended position -- only the
    # region stub's own ordered list says the real order is A, B, C.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)"
    )
    cur = db.cursor()

    ROUTINE_ID, REGION_ID = 1, 2
    LINE_A, LINE_B, LINE_C = 10, 11, 12

    cur.execute(
        "INSERT INTO nameless VALUES (?,?,?)",
        (REGION_ID, ROUTINE_ID, _st_region_stub_record([LINE_A, LINE_B, LINE_C])),
    )
    # Real order: A, B, C -- but seq numbers say C, B, A (as if C and A were
    # the ones freshly touched/reordered by an interactive Studio edit).
    cur.execute("INSERT INTO nameless VALUES (?,?,?)", (LINE_A, REGION_ID, _st_line_record(9000, "A;")))
    cur.execute("INSERT INTO nameless VALUES (?,?,?)", (LINE_B, REGION_ID, _st_line_record(50, "B;")))
    cur.execute("INSERT INTO nameless VALUES (?,?,?)", (LINE_C, REGION_ID, _st_line_record(9001, "C;")))
    db.commit()

    from acd.l5x.elements.builders_routine import _st_routine_lines

    assert _st_routine_lines(cur, ROUTINE_ID) == ["A;", "B;", "C;"]


def test_st_routine_lines_falls_back_to_seq_when_no_order_list_present():
    # Non-regression: a routine whose lines hang directly off a parent with
    # NO region-stub order-list record at all (e.g. an older/unverified
    # shape) must still work via the original sequence-number sort, not
    # silently return nothing or the wrong order.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)"
    )
    cur = db.cursor()

    ROUTINE_ID = 1
    cur.execute("INSERT INTO nameless VALUES (?,?,?)", (10, ROUTINE_ID, _st_line_record(2, "second;")))
    cur.execute("INSERT INTO nameless VALUES (?,?,?)", (11, ROUTINE_ID, _st_line_record(1, "first;")))
    db.commit()

    from acd.l5x.elements.builders_routine import _st_routine_lines

    assert _st_routine_lines(cur, ROUTINE_ID) == ["first;", "second;"]


def test_decorated_ascii_literal_is_big_endian_with_dollar_escapes():
    # Real Studio export of INT tags with Radix="ASCII".
    from acd.l5x.elements.rendering import _decorated_ascii_literal
    assert _decorated_ascii_literal(16717, 2) == "'AM'"
    assert _decorated_ascii_literal(8240, 2) == "' 0'"
    assert _decorated_ascii_literal(0, 2) == "'$00$00'"


def test_decorated_ascii_literal_escapes_xml_special_chars():
    # Real bug: this string is embedded directly into Value="..." -- an
    # unescaped "&" produced not-well-formed XML (an import would fail
    # outright). Studio's own export of the same value keeps the single
    # quotes literal and only XML-escapes "&" -> Value="'&amp; '".
    from acd.l5x.elements.rendering import _decorated_ascii_literal
    import xml.etree.ElementTree as ET

    value = (ord("&") << 8) | ord(" ")
    literal = _decorated_ascii_literal(value, 2)
    assert literal == "'&amp; '"
    ET.fromstring(f'<E Value="{literal}"/>')  # raises if not well-formed

    assert _decorated_ascii_literal((ord("<") << 8) | ord(">"), 2) == "'&lt;&gt;'"
    assert _decorated_ascii_literal((ord('"') << 8) | ord("'"), 2) == "'&quot;$''"
    ET.fromstring(f'<E Value="{_decorated_ascii_literal((ord(chr(60)) << 8) | ord(chr(62)), 2)}"/>')


def test_array_index_lists_every_dimension():
    # Studio writes [0,1] for element 1 of a [90,245] array; we wrote [1].
    from acd.l5x.elements.rendering import _array_index
    assert _array_index(1, [90, 245]) == "[0,1]"
    assert _array_index(245, [90, 245]) == "[1,0]"
    assert _array_index(7, [2, 2, 2]) == "[1,1,1]"
    assert _array_index(5, [10]) == "[5]"


def test_tag_to_xml_uses_tag_radix_and_full_array_index():
    from acd.l5x.elements import new_tag
    tag = new_tag("Flags", "DINT", dimensions="2,2")
    tag.radix = "Binary"
    tag._initial_value = [67, 514, 0, 1]
    xml = tag.to_xml()
    assert 'Radix="Binary"' in xml
    assert '<Element Index="[0,0]" Value="2#0000_0000_0000_0000_0000_0000_0100_0011"/>' in xml
    assert '<Element Index="[1,1]" Value="2#0000_0000_0000_0000_0000_0000_0000_0001"/>' in xml

    scalar = new_tag("Txt", "INT")
    scalar.radix = "ASCII"
    scalar._initial_value = 16717
    assert '<DataValue DataType="INT" Radix="ASCII" Value="\'AM\'"/>' in scalar.to_xml()


def test_tag_to_xml_ascii_array_element_with_ampersand_is_well_formed():
    # Real bug: an ASCII-radix array element whose value contained "&"
    # produced non-well-formed XML (ElementTree.ParseError on a real
    # project's own export -- an import would have failed outright).
    import xml.etree.ElementTree as ET
    from acd.l5x.elements import new_tag

    tag = new_tag("Txt", "INT", dimensions="1")
    tag.radix = "ASCII"
    tag._initial_value = [(ord("&") << 8) | ord(" ")]
    xml = tag.to_xml()
    assert "Value=\"'&amp; '\"" in xml
    ET.fromstring(xml)  # raises if not well-formed


def test_tag_to_xml_unverified_tag_radix_keeps_type_default():
    # Hex/Octal value formats haven't been seen in a real export yet.
    from acd.l5x.elements import new_tag
    tag = new_tag("H", "DINT")
    tag.radix = "Hex"
    tag._initial_value = 255
    assert '<DataValue DataType="DINT" Radix="Decimal" Value="255"/>' in tag.to_xml()


def test_string_escapes_use_r_and_l_for_cr_lf():
    # Real Studio exports write CR as $r (619 samples) and LF as $l, never
    # $0D/$0A, in L5K, <Data Format="String"> and Decorated values alike.
    from acd.l5x.elements.rendering import _l5k_string_padded, _string_literal_cdata
    assert _l5k_string_padded("\r\n\x1bS", capacity=5) == "'$r$l$1BS$00'"
    assert _string_literal_cdata("^01060000000004\r") == "<![CDATA['^01060000000004$r']]>"


def _bit_member_ext_record_value(name: str, bool_id: int, byte_offset: int, bit: int) -> bytes:
    # A BIT-overlay member: 0x68 != 0x800 marks it as not a plain BOOL, 0x64 is
    # the bit number, and 0x60 is -- exactly as on the real MOTION_INSTRUCTION/PID
    # built-ins -- the offset of the NEXT real member, not the backing field's.
    blob = bytearray(_member_ext_record_value(name, bool_id, 0, byte_offset))
    struct.pack_into("<I", blob, 0x64, bit)
    struct.pack_into("<I", blob, 0x68, 0)
    return bytes(blob)


def _build_flags_datatype(with_hidden_backing: bool):
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE comments(parent int, member_ref int, record_string text)")
    cur = db.cursor()
    DINT_ID, INT_ID, BOOL_ID, TYPE_ID, COLL_ID = 100, 101, 102, 200, 300
    for oid, nm in ((DINT_ID, "DINT"), (INT_ID, "INT"), (BOOL_ID, "BOOL")):
        cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (oid, 0, nm, 0, 256, b""))

    members = []
    if with_hidden_backing:
        hidden = bytearray(_member_ext_record_value("Backing", DINT_ID, 0, 0))
        struct.pack_into("<I", hidden, 0x70, 1)
        members.append(bytes(hidden))
        members.append(_member_ext_record_value("Plain", DINT_ID, 0, 4))
        members.append(_bit_member_ext_record_value("EN", BOOL_ID, 8, 31))
    else:
        members.append(_member_ext_record_value("FLAGS", DINT_ID, 0, 0))
        members.append(_bit_member_ext_record_value("EN", BOOL_ID, 4, 31))
        members.append(_bit_member_ext_record_value("DN", BOOL_ID, 4, 29))
        members.append(_member_ext_record_value("ERR", INT_ID, 0, 4))
    ext = (
        struct.pack("<II", 0x6C, 4) + struct.pack("<I", 0)
        + struct.pack("<II", 0x67, 4) + struct.pack("<I", 0)
        + struct.pack("<II", 0x69, 4) + struct.pack("<I", 0)
        + struct.pack("<II", 0x64, 4) + struct.pack("<I", len(members))
    )
    for i, m in enumerate(members):
        ext += struct.pack("<II", 0x6E + i, len(m)) + m
    type_record = _rx_generic_header() + struct.pack("<II", 0, 4 + len(members) + 1) + ext
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (TYPE_ID, 0, "T", 0, 256, type_record))
    cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (COLL_ID, TYPE_ID, "RxTypeMemberCollection", 0, 0, b""))
    names = ["Backing", "Plain", "EN"] if with_hidden_backing else ["FLAGS", "EN", "DN", "ERR"]
    for i, nm in enumerate(names):
        cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (400 + i, COLL_ID, nm, i, 256, _child_record()))
    db.commit()
    return DataTypeBuilder(cur, TYPE_ID).build()


def test_datatype_builder_bit_overlay_targets_preceding_plain_dint_when_no_hidden_backing():
    # Real bug (MOTION_INSTRUCTION, PID): the backing field is the plain DINT
    # (FLAGS/CTL) declared just before the BIT members, not hidden. A BIT's
    # own 0x60 is the NEXT member's offset (ERR), so the offset lookup used to
    # return ERR and EN/DN decoded 0 where Studio gives 1.
    dt = _build_flags_datatype(with_hidden_backing=False)
    bits = {m.name: m for m in dt.members if m.data_type == "BIT"}
    assert set(bits) == {"EN", "DN"}
    assert bits["EN"].target == "FLAGS" and bits["EN"].bit_number == 31
    assert bits["DN"].target == "FLAGS" and bits["DN"].bit_number == 29


def test_datatype_builder_bit_overlay_still_prefers_hidden_backing_over_plain_dint():
    # Non-regression: with a hidden backing field before the BIT member, the
    # existing declaration-order rule wins even though a plain DINT sits
    # between them.
    dt = _build_flags_datatype(with_hidden_backing=True)
    en = next(m for m in dt.members if m.name == "EN")
    assert en.target == "Backing"


def test_resolve_bit_target_plain_backing_beats_offset_lookup_but_not_hidden():
    from acd.l5x.elements.builders_common import _resolve_bit_target
    offsets = {4: "ERR"}
    assert _resolve_bit_target(0xFFFFFFFF, 4, offsets, None, "FLAGS") == "FLAGS"
    assert _resolve_bit_target(0xFFFFFFFF, 4, offsets, "Hidden", "FLAGS") == "Hidden"
    assert _resolve_bit_target(0xFFFFFFFF, 4, offsets, None, None) == "ERR"


def test_decorated_real_literal_scalar_infinity_is_truncated_form():
    # Real: a scalar REAL tag holding +Infinity is Value="1.$" in Studio's own
    # export (not "1.#INF"); scalar NaN stays "1.#QNAN".
    from acd.l5x.elements.rendering import _decorated_real_literal
    assert _decorated_real_literal(float("inf"), in_array=False) == "1.$"
    assert _decorated_real_literal(float("-inf"), in_array=False) == "-1.$"
    assert _decorated_real_literal(float("nan"), in_array=False) == "1.#QNAN"


def test_decorated_hex_literal_and_member_render():
    from acd.l5x.elements.rendering import _decorated_hex_literal
    assert _decorated_hex_literal(0, 32) == "16#0000_0000"
    assert _decorated_hex_literal(0xABCD, 16) == "16#ABCD"
    assert _decorated_hex_literal(-1, 32) == "16#FFFF_FFFF"
