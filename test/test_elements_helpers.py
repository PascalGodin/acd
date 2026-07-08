import math
import sqlite3
import struct
from datetime import datetime
from xml.dom import minidom

from acd.l5x.elements import (
    _decorated_real_literal,
    _escape_xml_attr,
    _filetime_to_iso,
    _l5k_real_literal,
    _read_tag_initial_value,
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


def test_decorated_real_literal_finite_uses_short_form():
    assert _decorated_real_literal(0.4047619, in_array=False) == "0.404762"
