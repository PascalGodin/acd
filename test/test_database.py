import pytest

from acd.database.dbextract import DbExtract
from acd.l5x.export_l5x import ExportL5x, _dedupe_comps_records
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
