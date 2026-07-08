import pytest

from acd.database.dbextract import DbExtract
from acd.l5x.export_l5x import ExportL5x
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
    for p in controller.programs:
        for r in p.routines:
            if r.name == "R020_Program_Control":
                assert r._rung_comments.get(0) == "Add JXR when you figure it out."
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
    # Regression test for two bugs found while verifying export_routine()
    # against a real Studio 5000 "Export Routine" output: a scalar
    # primitive tag with a known initial value (1) only emitted a
    # <Data Format="Decorated"> block, silently dropping the <Data
    # Format="L5K"> block a real tag always has alongside it, and (2) the
    # Decorated block used the DataType name as the XML element itself
    # (e.g. <BOOL Name="Tag" Value="1" Radix="Decimal"/>) instead of the
    # real <DataValue DataType="BOOL" Radix="Decimal" Value="1"/> shape.
    toggle = next((t for t in controller.tags if t.name == "Toggle"), None)
    assert toggle is not None, "Toggle tag not found"
    xml = toggle.to_xml()
    assert '<Data Format="L5K">' in xml
    assert '<DataValue DataType="BOOL" Radix="Decimal" Value="1"/>' in xml
    assert "<BOOL " not in xml


def test_parse_comments_dat():
    db: DbExtract = DbExtract("build/Comments.Dat")


def test_parse_nameless_dat():
    db: DbExtract = DbExtract("build/Nameless.Dat")
