"""Regression coverage for acd/record/comments.py's CommentsRecord.parse().

Real bug: a real project's own comment text ("LS pres ligne de bois
présence AC", a French-language description) decoded with two U+FFFD
replacement characters in place of the accented "é" -- CommentsRecord.parse()
decoded the array/bit-operand description text (record_type 8, and the
record_type 16/17 shape) as "ascii" with errors="replace", but the raw bytes
are genuine UTF-8 (confirmed directly: 0xC3 0xA9, the real UTF-8 encoding of
U+00E9). Fixed by decoding both as "utf-8" instead -- matching the record_type
1/2/12 (AsciiRecord/UDI) text, which already decoded as "utf-8" correctly.
"""
import struct
from types import SimpleNamespace

from acd.record.comments import CommentsRecord

_HEADER_SIZE = 10


def _fafa_record(record_type: int, body: bytes) -> SimpleNamespace:
    """A synthetic dat_record matching what CommentsRecord.parse() reads:
    dat_record.identifier (must be 64250) and dat_record.record.record_buffer
    (a FafaComents-shaped byte buffer -- record_length u4le, then a 10-byte
    header [seq_number u2le, record_type u2le, sub_record_length u2le,
    parent u4le], then the body bytes)."""
    header = struct.pack("<HHHI", 0, record_type, 0, 12345)  # seq, type, sub_len, parent
    assert len(header) == _HEADER_SIZE
    record_length = _HEADER_SIZE + len(body)
    buffer = struct.pack("<I", record_length) + header + body
    return SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=buffer))


def _operand_body(obj_id: int, tag_ref: str, text: str, text_encoding: str = "utf-8") -> bytes:
    """Body shape for record_type 5/6/7/8/11/15/19/21/24/29/30/37/39:
    8 bytes unknown, obj_id (u4le) @ [8:12], 4 bytes unknown @ [12:16], a
    UTF-16LE null-terminated tag_ref, null padding, then a null-terminated
    text string."""
    head = b"\x00" * 8 + struct.pack("<I", obj_id) + b"\x00" * 4
    tag_ref_bytes = tag_ref.encode("utf-16-le") + b"\x00\x00"
    return head + tag_ref_bytes + text.encode(text_encoding) + b"\x00"


def test_operand_record_decodes_real_utf8_accented_text():
    # The literal real-project repro: a French comment with "é" (0xC3 0xA9).
    text = "LS pres ligne de bois présence AC"
    body = _operand_body(obj_id=771861, tag_ref="[1].8", text=text)
    rec = _fafa_record(8, body)

    entry = CommentsRecord.parse(rec)

    assert entry is not None
    record_string = entry[3]
    assert "�" not in record_string, "accented text must not become U+FFFD"
    assert record_string == text


def test_record_type_16_also_decodes_utf8():
    # record_type 16/17 share the same bug (a different obj_id offset, [6:10]
    # instead of [8:12]) -- same fix, same real text.
    text = "présence"
    head = b"\x00" * 6 + struct.pack("<I", 1) + b"\x00" * 6
    tag_ref_bytes = "X".encode("utf-16-le") + b"\x00\x00"
    body = head + tag_ref_bytes + text.encode("utf-8") + b"\x00"
    rec = _fafa_record(16, body)

    entry = CommentsRecord.parse(rec)

    assert entry is not None
    assert entry[3] == text


def test_operand_record_without_accents_is_unaffected():
    # Non-regression: plain ASCII text (the overwhelming majority of real
    # comments) must still decode correctly after the encoding change.
    text = "Short Board Present Don't Load Stop Arms #2"
    body = _operand_body(obj_id=1, tag_ref="[0].5", text=text)
    rec = _fafa_record(5, body)

    entry = CommentsRecord.parse(rec)

    assert entry is not None
    assert entry[3] == text


def test_parse_returns_none_for_non_comments_identifier():
    rec = SimpleNamespace(identifier=12345, record=SimpleNamespace(record_buffer=b""))
    assert CommentsRecord.parse(rec) is None
