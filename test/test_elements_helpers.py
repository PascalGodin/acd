import math
import sqlite3
import struct
from datetime import datetime
from xml.dom import minidom

import pytest

from acd.l5x.elements import (
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
