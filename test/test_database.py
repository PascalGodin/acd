import struct

import pytest

from acd.database.dbextract import DbExtract
from acd.l5x.elements import ControllerBuilder
from acd.l5x.export_l5x import (
    ExportL5x,
    _dedupe_comps_records,
    _iter_region_map_entries_v38,
    _iter_region_map_entries_v_pre38,
)
from acd.zip.unzip import Unzip

from loguru import logger as log


@pytest.fixture()
async def sample_acd():
    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    yield unzip


@pytest.fixture()
async def sbregion_dat():
    db = DbExtract("build/SbRegion.Dat")
    yield db


@pytest.fixture()
async def comps_dat():
    db = DbExtract("build/Comps.Dat").read()
    yield db


@pytest.fixture(scope="module")
def controller():
    log.level("DEBUG")
    yield ExportL5x("../resources/CuteLogix.ACD", "build").controller


def test_open_file(sample_acd, sbregion_dat):
    assert sbregion_dat


def test_parse_rungs_dat(controller):
    # Verify the B002_Timers routine (last routine of last program) contains
    # the expected rungs. Use content search rather than positional index since
    # rung ordering is controlled by region_map.unknown (canonical display order).
    rungs = controller.programs[-1].routines[-1].rungs
    assert any("XIO(b_Timer[0].DN)TON(b_Timer[0],?,?);" in r for r in rungs), (
        f"Expected TON rung not found in {rungs}"
    )


def test_rung_comment_scoped_correctly(controller):
    # Regression test for a scope_id collision bug: rung comments were
    # fetched by (comment_id, cip_type) "parent" key alone, without also
    # matching scope_id -- in a real large project this caused a routine to
    # pick up a completely unrelated routine's rung comment (e.g. a simple
    # "Flasher" routine showing a comment that actually belonged to a
    # tally/sorting routine). This fixture is too small to exercise an
    # actual collision, but this still guards against a regression in the
    # basic rung-comment lookup itself.
    #
    # This routine actually has three rung comments (rungs 3, 8, 9 -- each a
    # placeholder NOP() noting a different instruction to fill in later: JXR,
    # SFR, SFP respectively). A previous version of this test only checked
    # rung 0 because the old (buggy) rung-attribution logic collapsed every
    # rung comment in a routine onto the same wrong slot (see "Rung comments"
    # in CLAUDE.md) -- rung 0's real content is a populated SBR() instruction,
    # not a placeholder, so "Add JXR when you figure it out" on rung 0 never
    # actually made semantic sense; it was simply the first comment processed
    # winning an always-computed-as-0 slot.
    for p in controller.programs:
        for r in p.routines:
            if r.name == "R020_Program_Control":
                assert r._rung_comments == {
                    3: "Add JXR when you figure it out.",
                    8: "Add SFR when you figure it out",
                    9: "Add SFP when you figure it out",
                }
                return
    pytest.fail("R020_Program_Control routine not found")


def test_parse_datatypes_dat(controller):
    # Look up by name rather than position — list order may vary across parser versions
    string20 = next((dt for dt in controller.data_types if dt.name == "STRING20"), None)
    assert string20 is not None, "STRING20 data type not found"
    data_member = next((m for m in string20.members if m.name == "DATA"), None)
    assert data_member is not None, "DATA member not found in STRING20"


def test_parse_tags_dat(controller):
    # Look up by name rather than index — index may shift across parser versions
    toggle = next((t for t in controller.tags if t.name == "Toggle"), None)
    assert toggle is not None, "Toggle tag not found"
    assert toggle.data_type == "BOOL"


def test_scalar_primitive_tag_xml_shape(controller):
    # Regression test for bugs found while verifying export_routine()
    # against a real Studio 5000 "Export Routine" output: a scalar
    # primitive tag with a known initial value only emitted a
    # <Data Format="Decorated"> block, silently dropping the <Data
    # Format="L5K"> block a real tag always has alongside it, and (2) the
    # Decorated block used the DataType name as the XML element itself
    # (e.g. <BOOL Name="Tag" Value="1" Radix="Decimal"/>) instead of the
    # real <DataValue DataType="BOOL" Radix="Decimal" Value="1"/> shape.
    #
    # The expected value here is 0, not 1 -- a separate, much larger bug
    # (fixed in the same investigation) turned out to affect which value
    # this even is: _read_tag_initial_value() used the wrong offset (0x19E)
    # for every scalar primitive tag project-wide. Verified against a real
    # project: comparing 758 controller-scope scalar BOOL tags and 812 DINT
    # tags against Studio 5000's own values, the old 0x19E offset matched
    # only 21.4%/2.8% of the time, while the correct offset (0x1A2, the
    # same one already used for arrays -- there never was a real
    # scalar/array distinction) matched 100% for both. This test's own
    # expected value (previously asserted as 1) was itself a casualty of
    # that bug -- it was never independently verified against real ground
    # truth for this small fixture, just whatever the wrong offset happened
    # to produce.
    toggle = next((t for t in controller.tags if t.name == "Toggle"), None)
    assert toggle is not None, "Toggle tag not found"
    xml = toggle.to_xml()
    assert '<Data Format="L5K">' in xml
    assert '<DataValue DataType="BOOL" Radix="Decimal" Value="0"/>' in xml
    assert "<BOOL " not in xml


def test_parse_comments_dat():
    db: DbExtract = DbExtract("build/Comments.Dat")


def test_parse_nameless_dat():
    db: DbExtract = DbExtract("build/Nameless.Dat")


def _comps_tuple(object_id, parent_id, comp_name, record_len):
    # (object_id, parent_id, comp_name, seq_number, record_type, record) --
    # only object_id/parent_id/comp_name/record length matter for dedup.
    return (object_id, parent_id, comp_name, 0, 256, b"\x00" * record_len)


def test_dedupe_comps_records_keeps_unrelated_objects_sharing_an_object_id():
    # Regression test for a real bug found via a real project that failed to
    # load entirely (IndexError in ControllerBuilder.build()): a genuine
    # object_id collision between three UNRELATED objects with different
    # parents -- the real "RxDataTypeCollection" (small, 78 bytes) and two
    # unrelated objects with much larger records. The old dedup (keyed by
    # bare object_id, "keep the largest") silently discarded the small but
    # correct RxDataTypeCollection in favor of an unrelated, larger object.
    # Keying by (object_id, parent_id) instead must keep all three, since
    # they have different parents.
    same_oid = 3954991832
    tuples = [
        _comps_tuple(same_oid, 4240912631, "RxDataTypeCollection", 78),
        _comps_tuple(same_oid, 4259926, "B_Manual_Solution", 7410),
        _comps_tuple(same_oid, 5898330, "ZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000", 6874),
    ]

    result = _dedupe_comps_records(tuples)

    assert len(result) == 3
    names_by_parent = {t[1]: t[2] for t in result.values()}
    assert names_by_parent[4240912631] == "RxDataTypeCollection"
    assert names_by_parent[4259926] == "B_Manual_Solution"
    assert names_by_parent[5898330] == "ZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000"


def test_dedupe_comps_records_still_collapses_truncated_duplicate_under_same_parent():
    # The original scenario this dedup logic was designed for: the SAME
    # object appears twice under the SAME parent (e.g. a routine with two
    # record_type variants, one a truncated/partial parse) -- must still
    # collapse to a single entry, keeping the larger (fuller) record.
    same_oid, same_parent = 111, 222
    tuples = [
        _comps_tuple(same_oid, same_parent, "SomeRoutine", 50),   # truncated
        _comps_tuple(same_oid, same_parent, "SomeRoutine", 500),  # full
    ]

    result = _dedupe_comps_records(tuples)

    assert len(result) == 1
    kept = next(iter(result.values()))
    assert len(kept[5]) == 500


def test_controller_builder_ignores_nameless_root_object():
    # Regression test for a real bug: a project re-saved from Studio 5000
    # turned up a second parent_id=0 row sharing the real controller's own
    # record_type (256): object_id=1, comp_name="" (empty), no children, its
    # raw record all zero bytes except a couple of 0xFFFFFFFF sentinel
    # values -- isolated scratch/reserved data, not a second controller (a
    # real controller always has a real project name). ControllerBuilder's
    # "exactly one root controller node" query used to match both rows,
    # raising "Does not contain exactly one root controller node" and
    # making the whole project unloadable.
    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (999999999, 0, "", 0, 256, b"\x00" * 154),
    )
    exp._db.commit()

    controller = ControllerBuilder(cur).build()

    assert controller.name == "CuteLogix"


def _pre38_entry(parent_id, unknown, seq_no, object_id) -> bytes:
    return struct.pack("<IIII", parent_id, unknown, seq_no, object_id)


def _v38_entry(object_id, parent_id, unknown, seq_no) -> bytes:
    return struct.pack("<IIII", object_id, parent_id, unknown, seq_no)


def test_iter_region_map_entries_v_pre38_reads_dense_16_byte_entries():
    header = b"\x00" * 78
    entries = [
        _pre38_entry(parent_id=111, unknown=0, seq_no=0xFFFFFFFF, object_id=1001),
        _pre38_entry(parent_id=111, unknown=1, seq_no=0xFFFFFFFF, object_id=1002),
    ]
    record = header + b"".join(entries)

    result = list(_iter_region_map_entries_v_pre38(record, 78, len(record)))

    assert [(r[0], r[1], r[2], r[3]) for r in result] == [
        (1001, 111, 0, 0xFFFFFFFF),
        (1002, 111, 1, 0xFFFFFFFF),
    ]


def test_iter_region_map_entries_v38_reads_dense_16_byte_entries_from_offset_3():
    # Regression test for a real Studio 5000 V38.02 project (schema revision
    # 1.0): the "Region Map" comps record no longer has a trustworthy
    # region_length field at the old pre-V38 header offset, and its entries
    # -- same 16-byte (object_id, parent_id, unknown, seq_no) tuple, fields
    # reordered -- start right after a 3-byte header instead of the old
    # 78-byte one. Reverse-engineered by byte-searching a known routine's
    # own object_id through a real project's raw record; see CLAUDE.md
    # "Region Map format change".
    header = b"\x00\x01\x00"  # 3 bytes
    entries = [
        _v38_entry(object_id=2001, parent_id=222, unknown=0, seq_no=0xFFFFFFFF),
        _v38_entry(object_id=2002, parent_id=222, unknown=1, seq_no=0xFFFFFFFF),
        _v38_entry(object_id=2003, parent_id=222, unknown=2, seq_no=0xFFFFFFFF),
    ]
    record = header + b"".join(entries)

    result = list(_iter_region_map_entries_v38(record))

    assert [(r[0], r[1], r[2], r[3]) for r in result] == [
        (2001, 222, 0, 0xFFFFFFFF),
        (2002, 222, 1, 0xFFFFFFFF),
        (2003, 222, 2, 0xFFFFFFFF),
    ]


def test_populate_region_map_falls_back_to_v38_layout_when_header_length_is_stale():
    # End-to-end version of the two tests above: a "Region Map" comps record
    # shaped like the real V38.02 case (stale/undersized region_length at the
    # old header offset) must still populate region_map correctly via the
    # fallback layout, not silently produce an empty/wrong table -- which is
    # exactly the bug a downstream user hit (every routine's rungs/rung_ids
    # came back empty against a real V38.02 project).
    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    header = b"\x00\x01\x00"
    entries = [
        _v38_entry(object_id=3001, parent_id=444, unknown=0, seq_no=0xFFFFFFFF),
        _v38_entry(object_id=3002, parent_id=444, unknown=1, seq_no=0xFFFFFFFF),
    ]
    # populate_region_map() bails out early on any record shorter than the
    # old 78-byte header, so pad well past that -- the padding lands after
    # our 2 real entries and reads back as zero-valued "entries" the
    # parent_id=444 filter below excludes. The padded length (85) must also
    # NOT happen to equal old_end (78 + whatever garbage region_length reads
    # as from the padding, here 0 -> old_end=78), or this wouldn't exercise
    # the fallback at all.
    record = header + b"".join(entries)
    record += b"\x00" * (85 - len(record))

    cur.execute("DELETE FROM comps WHERE comp_name='Region Map'")
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (555, 0, "Region Map", 0, 0, record),
    )
    cur.execute("DELETE FROM region_map")
    exp._db.commit()

    exp.populate_region_map()

    cur.execute(
        "SELECT object_id, parent_id, unknown, seq_no FROM region_map WHERE parent_id=444 ORDER BY unknown"
    )
    assert cur.fetchall() == [(3001, 444, 0, 0xFFFFFFFF), (3002, 444, 1, 0xFFFFFFFF)]
