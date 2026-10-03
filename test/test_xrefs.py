import os
import re
import struct

import pytest

from acd.l5x.export_l5x import ExportL5x, _read_xrefs
from acd.record.xrefs import NONE, RECORD_SIZE, XRef, parse_xrefs


def _record(from_id, to_id, access=1, kind=9, instruction=143, bit_offset=0,
            bit_width=1, scope_id=NONE, routine_id=NONE, count=1, free_next=None):
    rec = bytearray(RECORD_SIZE)
    if free_next is not None:
        rec[0] = 0xFF
        struct.pack_into("<I", rec, 1, free_next)
        return bytes(rec)
    struct.pack_into("<IIHHIII", rec, 2, from_id, to_id, access, kind,
                     instruction, bit_offset, bit_width)
    rec[26:34] = b"\xfe" + b"\xff" * 7
    struct.pack_into("<III", rec, 34, scope_id, routine_id, count)
    return bytes(rec)


def _xrefs_file(records):
    region_ptr, records_region, header_len = 32, 64, 16
    start = records_region + header_len
    data = bytearray(start)
    struct.pack_into("<I", data, 8, start + RECORD_SIZE * len(records))
    struct.pack_into("<I", data, 12, region_ptr)
    data[region_ptr:region_ptr + 2] = b"\xfe\xfe"
    struct.pack_into("<I", data, region_ptr + 18, records_region)
    data[records_region:records_region + 2] = b"\xfe\xfe"
    struct.pack_into("<I", data, records_region + 2, header_len)
    for r in records:
        data += r
    return bytes(data)


def test_parse_xrefs_decodes_live_records_and_skips_free_slots():
    data = _xrefs_file([
        _record(100, 200, access=2, kind=9, instruction=115, bit_offset=21,
                bit_width=1, scope_id=7, routine_id=8),
        _record(0, 0, free_next=0),
        _record(300, 400, access=1, kind=2, instruction=0, bit_width=NONE),
    ])

    assert parse_xrefs(data) == [
        XRef(100, 200, 2, 9, 115, 21, 1, 7, 8, 1),
        XRef(300, 400, 1, 2, 0, 0, NONE, NONE, NONE, 1),
    ]


def test_parse_xrefs_rejects_unexpected_header():
    data = bytearray(_xrefs_file([_record(1, 2)]))
    data[32:34] = b"\x00\x00"
    with pytest.raises(ValueError):
        parse_xrefs(bytes(data))


def test_read_xrefs_degrades_to_empty_on_missing_or_bad_file(tmp_path):
    assert _read_xrefs(str(tmp_path / "XRefs.Dat")) == []
    bad = tmp_path / "bad.Dat"
    bad.write_bytes(b"\x00" * 64)
    assert _read_xrefs(str(bad)) == []


@pytest.fixture(scope="module")
def cute(tmp_path_factory):
    exp = ExportL5x("../resources/CuteLogix.ACD", str(tmp_path_factory.mktemp("xrefs")))
    yield exp
    exp._db.close()


def _names(exp):
    exp._cur.execute("SELECT object_id, comp_name FROM comps")
    return dict(exp._cur.fetchall())


def test_xrefs_table_count_matches_file_header(cute):
    with open(os.path.join(cute._temp_dir, "XRefs.Dat"), "rb") as f:
        header_count = struct.unpack_from("<I", f.read(28), 24)[0]
    cute._cur.execute("SELECT COUNT(*) FROM xrefs")
    # The header's own live-record field is always one more than the live
    # slots -- held on every real file checked.
    assert cute._cur.fetchone()[0] == header_count - 1 > 0


def test_xrefs_every_target_resolves_to_a_real_object(cute):
    names = _names(cute)
    cute._cur.execute("SELECT to_id FROM xrefs")
    targets = [t for (t,) in cute._cur.fetchall()]
    assert targets and all(t in names for t in targets)


def test_xrefs_program_main_routine(cute):
    names = _names(cute)
    cute._cur.execute("SELECT from_id, to_id FROM xrefs WHERE kind=15")
    main = {names[p]: names[r] for p, r in cute._cur.fetchall()}
    assert main["Branching"] == "B001_Main"
    assert main["Duh"] == "Stupid"


def test_xrefs_tag_data_type_matches_decoded_tags(cute):
    names = _names(cute)
    cute._cur.execute("SELECT from_id, to_id FROM xrefs WHERE kind=2")
    xref_type = {f: names[t] for f, t in cute._cur.fetchall()}
    cute._cur.execute(
        "SELECT t.object_id, t.comp_name FROM comps t JOIN comps c ON t.parent_id = c.object_id "
        "WHERE c.comp_name = 'RxTagCollection'"
    )
    tag_oid = dict((name, oid) for oid, name in cute._cur.fetchall())
    checked = 0
    for tag in cute.controller.tags:
        oid = tag_oid.get(tag.name)
        if tag.tag_type != "Base" or oid not in xref_type:
            continue
        assert xref_type[oid].upper() == tag.data_type.upper(), tag.name
        checked += 1
    assert checked > 50


def test_xrefs_udt_member_type_count(cute):
    names = _names(cute)
    data_types = cute.controller._data_types_map
    cute._cur.execute("SELECT from_id, to_id, count FROM xrefs WHERE kind=8")
    checked = 0
    for from_id, to_id, count in cute._cur.fetchall():
        dt = data_types.get(names[from_id].upper())
        if dt is None:
            continue
        member_type = names[to_id].upper()
        # Rockwell counts a BIT-overlay member as BOOL.
        n = sum(1 for m in dt.members
                if ("BOOL" if m.data_type == "BIT" else m.data_type.upper()) == member_type)
        assert n == count, (dt.name, member_type)
        checked += 1
    assert checked > 100


MSG = 100


def test_xrefs_rung_tag_references_appear_in_rung_text(cute):
    names = _names(cute)
    cute._cur.execute(
        "SELECT x.to_id, x.instruction, r.rung FROM xrefs x "
        "JOIN rungs r ON r.object_id = x.from_id WHERE x.kind=9"
    )
    rows = cute._cur.fetchall()
    assert len(rows) > 300
    for to_id, instruction, rung in rows:
        found = re.search(r"\b" + re.escape(names[to_id]) + r"\b", rung)
        if instruction == MSG:
            # A tag configured inside a message (its source/destination) is
            # attributed to the rung holding the MSG, without appearing in
            # the rung text -- e.g. "XIC(Toggle)MSG(WebPage);" -> DisableWeb.
            continue
        assert found, (names[to_id], rung)
    msg_refs = {names[t] for t, i, _ in rows if i == MSG}
    assert "DisableWeb" in msg_refs
