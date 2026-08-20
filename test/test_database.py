import sqlite3
import struct
import sys

import pytest
from loguru import logger as loguru_logger

from acd.database.dbextract import DbExtract
from acd.l5x.elements import ControllerBuilder, ModuleBuilder, ProgramBuilder, RoutineBuilder
from acd.l5x.export_l5x import (
    ExportL5x,
    _dedupe_comps_records,
    _iter_region_map_entries_v38,
    _iter_region_map_entries_v_pre38,
    _MAX_INLINE_FAILURE_DETAILS,
    _parse_records,
    configure_logging,
)
from acd.l5x import export_l5x as export_l5x_module
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


def _connection_record(code: int, rpi: int) -> bytes:
    # Type code is a u16 at offset 90, RPI (microseconds) a u32 immediately
    # after it at offset 92 -- see ModuleBuilder.build()'s own docstring.
    record = bytearray(96)
    struct.pack_into("<H", record, 90, code)
    struct.pack_into("<I", record, 92, rpi)
    return bytes(record)


def test_module_builder_skips_hex_named_connection():
    # Regression test for a real project: a "Local" chassis module had one
    # connection whose own comp_name was a hex placeholder ("$0ce232bb$",
    # the same "unnamed internal object" convention already handled for a
    # Module's own name a few lines above in ModuleBuilder.build()) and an
    # otherwise-unrecognized CIP connection-type code (10).
    #
    # This was investigated twice. The first attempt concluded "not in a
    # real Studio 5000 L5X export of this project, so skip it" -- that
    # conclusion was correct but the REASONING was wrong (L5X silence is not
    # proof of ACD absence) and was reverted. The second, real investigation
    # (with the user directly checking Studio 5000) confirmed what this
    # actually is: a cached CIP MESSAGE connection. The project has 5 real
    # `MSG` instructions (SLC_Stacker_Write/Read, SLC_Planer_Write/Read/
    # Read2) talking to an external SLC 5/04 over DH+ through a 1756-DHRIO
    # bridge module, all with "Cache Connections" enabled -- confirmed
    # directly in Studio's own Message Configuration dialog -- and all
    # sharing the same destination path (one DHRIO module/channel/node),
    # matching "one cached connection per unique destination", not per `MSG`
    # instruction, which is exactly why there's only one such connection
    # (not five) in a real, otherwise very large (246-module) project. Real
    # Studio 5000 output confirms this category of connection is never
    # rendered as an L5X <Connection> element at all (physical CIP I/O
    # connections only) -- fixed by skipping hex-named connections entirely
    # rather than guessing an Input/Output Type= for something that was
    # never an I/O connection to begin with. See CLAUDE.md's "Connection
    # Type / RPI" section for the full investigation.
    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    local_object_id = 3363100451
    conn_collection_id = 1867864125

    # A genuine, normally-named connection with a recognized code must still
    # come through unaffected -- this isn't a blanket "ignore odd codes" fix.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (9001, conn_collection_id, "Standard", 0, 256, _connection_record(code=5, rpi=20000)),
    )
    # The hex-named connection with the unrecognized code -- must be excluded.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (9002, conn_collection_id, "$0ce232bb$", 1, 256, _connection_record(code=10, rpi=0)),
    )
    exp._db.commit()

    module = ModuleBuilder(cur, local_object_id).build()

    assert module._connections == [("Standard", "20000", "Input")]


def _rx_generic_header(cip_type=999):
    # 14-byte fixed header + 60-byte opaque main_record, enough for
    # RxGeneric.from_bytes() to parse.
    header = struct.pack("<IIHHH", 0, 0, 40, cip_type, 0)
    main_record = b"\x00" * 60
    return header + main_record


def test_program_builder_resolves_main_routine_name_via_local_ref():
    # Regression test for a real, severe downstream-reported bug:
    # Program.main_routine_name was ALWAYS None, for every program in a real
    # project (13 of 13) -- Studio 5000 import of a db_export_program()
    # output for the first time then genuinely dropped the Main Routine
    # designation (confirmed by the user in a copy project first).
    #
    # The old code read a raw routine object_id from ext[0x12D] -- an
    # attribute that, checked directly against a real project, was NEVER
    # actually present in any Program's parsed extended records at all.
    #
    # The real mechanism, found by diffing a real Program's raw comps bytes
    # between two saves of the same project differing by exactly ONE change
    # (reassigning the Program's own Main Routine designation to a
    # different, non-"Main"-named routine in Studio 5000 itself): exactly 2
    # bytes changed. ext[0x01] (the same blob already used for the Disabled
    # flag) has a small, fixed-size trailing footer whose u16 at byte offset
    # `len(ext01) - 8` is a "local reference number" -- NOT the routine's own
    # object_id -- that matches a u16 field at raw offset 16 of the
    # designated routine's OWN comps record. Independently confirmed against
    # a SECOND, unrelated real program (same footer position despite
    # completely different content), and against all 13 real programs in the
    # project -- including 6 whose real main routine is NOT literally named
    # "Main" at all (e.g. "PowerUp", "Trimmer_LS", "Main_Motors"), ruling out
    # a "just guess the routine named Main" heuristic as a coincidence.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE comps(object_id int, parent_id int, comp_name text, "
        "seq_number int, record_type int, record BLOB NOT NULL)"
    )
    db.execute("CREATE TABLE comments(parent int, tag_reference text, record_string text)")
    cur = db.cursor()

    PROG_ID = 900
    COLL_ID = 901
    MAIN_LOCAL_REF = 4242

    # ext01: 40 bytes: the local-ref u16 lives at len(ext01) - 8 = offset 32.
    ext01 = bytearray(40)
    struct.pack_into("<H", ext01, 32, MAIN_LOCAL_REF)
    ext01_attr = struct.pack("<II", 0x01, len(ext01)) + bytes(ext01)
    dummy_last_attr = struct.pack("<II", 0x02, 4) + b"\x00" * 4  # left unparsed by RxGeneric
    count_record = 2  # 1 parsed (0x01) + 1 left unparsed
    prog_record = (
        _rx_generic_header() + struct.pack("<II", 0, count_record) + ext01_attr + dummy_last_attr
    )

    def _routine_record(local_ref: int) -> bytes:
        rec = bytearray(20)
        struct.pack_into("<H", rec, 16, local_ref)
        return bytes(rec)

    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (PROG_ID, 0, "TestProgram", 0, 256, prog_record),
    )
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (COLL_ID, PROG_ID, "RxRoutineCollection", 0, 256, b""),
    )
    # NOT the main routine -- its own local ref doesn't match.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (902, COLL_ID, "RoutineA", 0, 256, _routine_record(9999)),
    )
    # The real main routine -- its own local ref matches the Program's footer.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (903, COLL_ID, "RoutineB", 0, 256, _routine_record(MAIN_LOCAL_REF)),
    )
    db.commit()

    program = ProgramBuilder(cur, PROG_ID).build()

    assert program.main_routine_name == "RoutineB"


def test_routine_builder_disambiguates_colliding_object_id_by_parent():
    # Regression test for a real, downstream-reported bug: object_id is not
    # always unique in Comps.Dat (see CLAUDE.md's "object_id is not always
    # unique"). A real project had a genuinely live, normally-parented RLL
    # routine share its object_id with a completely unrelated object under a
    # different parent. RoutineBuilder.build()'s own "WHERE object_id=..."
    # re-query returned BOTH rows and blindly used fetchall()[0] -- when
    # SQLite happened to return the unrelated row first, its bytes got fed to
    # RxGeneric in the real routine's place, and the real routine vanished
    # (build() returned None) with no error anywhere.
    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    real_routine_id = 2802757537   # "B001_Main", a real RLL routine
    real_parent_id = 1543291182    # its real RxRoutineCollection

    # An unrelated object sharing the SAME object_id under a DIFFERENT
    # parent -- garbage bytes too short for RxGeneric to parse, mirroring
    # the real case (an unrelated ~12KB record whose bytes, read in the
    # real routine's place, made RxGeneric resolve to a bogus routine_type).
    # Inserted with a lower rowid than the real routine so a naive
    # "WHERE object_id=..." query (no ORDER BY) returns it FIRST, matching
    # the real, observed row order.
    cur.execute(
        "INSERT INTO comps VALUES (?,?,?,?,?,?)",
        (real_routine_id, 11, "\x0b", 9, 0, b"\x00" * 8),
    )
    exp._db.commit()

    # Confirm the collision itself is real: an object_id-only query is now
    # genuinely ambiguous (2 rows) -- this is what made the OLD "WHERE
    # object_id=..." + fetchall()[0] query unsafe (whichever row SQLite
    # happens to return first, which isn't something to rely on and wasn't
    # reliably reproducible in this test either -- the real bug was found
    # against a real project's own physical row order, not forced here).
    ambiguous = cur.execute(
        "SELECT parent_id FROM comps WHERE object_id=?", (real_routine_id,)
    ).fetchall()
    assert len(ambiguous) == 2

    # With the parent_id disambiguation, the real routine is found correctly
    # regardless of the collision or row order.
    fixed = RoutineBuilder(cur, real_routine_id, real_parent_id).build()
    assert fixed is not None
    assert fixed.name == "B001_Main"
    assert fixed.type == "RLL"

    # And the colliding object's own (wrong) parent correctly returns
    # nothing -- proving the filter is a real disambiguation, not a no-op.
    assert RoutineBuilder(cur, real_routine_id, 11).build() is None


def _region_map_entry(parent_id, unknown, seq_no, object_id) -> bytes:
    # Same 16-byte field order on BOTH sides of the V38 boundary -- only the
    # header size (and therefore the entries' starting offset) differs. See
    # _iter_region_map_entries_v38's own docstring for how a first attempt
    # at this got the header offset wrong by exactly one field and wrongly
    # concluded the field order itself had changed.
    return struct.pack("<IIII", parent_id, unknown, seq_no, object_id)


def test_iter_region_map_entries_v_pre38_reads_dense_16_byte_entries():
    header = b"\x00" * 78
    entries = [
        _region_map_entry(parent_id=111, unknown=0, seq_no=0xFFFFFFFF, object_id=1001),
        _region_map_entry(parent_id=111, unknown=1, seq_no=0xFFFFFFFF, object_id=1002),
    ]
    record = header + b"".join(entries)

    result = list(_iter_region_map_entries_v_pre38(record, 78, len(record)))

    assert [(r[0], r[1], r[2], r[3]) for r in result] == [
        (1001, 111, 0, 0xFFFFFFFF),
        (1002, 111, 1, 0xFFFFFFFF),
    ]


def test_iter_region_map_entries_v38_reads_dense_16_byte_entries_from_offset_7():
    # Regression test for a real Studio 5000 V38.02 project (schema revision
    # 1.0): the "Region Map" comps record no longer has a trustworthy
    # region_length field at the old pre-V38 header offset, and its entries
    # -- same 16-byte (parent_id, unknown, seq_no, object_id) tuple, SAME
    # field order as the pre-V38 layout -- start right after a 7-byte header
    # instead of the old 78-byte one. Reverse-engineered by resolving real
    # ground-truth rung TEXT (from a Studio 5000 L5X export) to real
    # object_ids via the independently-decoded SbRegion.Dat `rungs` table,
    # then confirming those specific object_ids land in the LAST field of
    # the entry at this offset -- not, as a first (wrong) attempt at this
    # concluded, the FIRST field 4 bytes earlier (which is actually the
    # PRECEDING entry's own object_id -- still a real rung id, just for the
    # wrong routine). See CLAUDE.md "Region Map format change".
    header = b"\x00" * 7
    entries = [
        _region_map_entry(parent_id=222, unknown=0, seq_no=0xFFFFFFFF, object_id=2001),
        _region_map_entry(parent_id=222, unknown=1, seq_no=0xFFFFFFFF, object_id=2002),
        _region_map_entry(parent_id=222, unknown=2, seq_no=0xFFFFFFFF, object_id=2003),
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

    header = b"\x00" * 7
    entries = [
        _region_map_entry(parent_id=444, unknown=0, seq_no=0xFFFFFFFF, object_id=3001),
        _region_map_entry(parent_id=444, unknown=1, seq_no=0xFFFFFFFF, object_id=3002),
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


def test_routine_builder_recovers_rung_missing_from_region_map_via_regnlink_chain():
    # Regression test for a real, observed data-loss case (not a parsing gap):
    # two real rungs on a real V38.02 project had their Region Map entry go
    # missing entirely between two saves of the same project, even though the
    # rung's own text still decodes fine from SbRegion.Dat -- while
    # RegnLink.Dat still carried an intact, correctly-typed (routine, own_rung,
    # next_rung) link record for each. See CLAUDE.md "Region Map entries can
    # go missing independently of the format" for the full investigation
    # (including confirming, via a genuinely earlier save, that one of the two
    # rungs *was* correctly indexed before a later save dropped it).
    #
    # R020_Program_Control (routine object_id 765662755) in the real
    # CuteLogix.ACD fixture has 14 rungs, region_map-ordered 0..13. Simulate
    # losing the Region Map entry for rung index 3 (object_id 1957188902,
    # "NOP();") while its RegnLink.Dat chain record survives, pointing at the
    # real next rung (108740561, index 4) -- exactly the shape of the real
    # bug.
    routine_id = 765662755
    missing_rung = 1957188902
    real_next_rung = 108740561

    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    original_order = [row[0] for row in cur.execute(
        "SELECT object_id FROM region_map WHERE parent_id=? ORDER BY unknown", (routine_id,)
    ).fetchall()]
    assert missing_rung in original_order  # sanity: fixture shape hasn't drifted

    cur.execute("DELETE FROM region_map WHERE parent_id=? AND object_id=?", (routine_id, missing_rung))
    cur.execute(
        "INSERT INTO regnlink_chain VALUES (?,?,?)", (routine_id, missing_rung, real_next_rung)
    )
    exp._db.commit()

    routine = RoutineBuilder(cur, routine_id).build()

    assert routine is not None
    assert routine._rung_ids == original_order


def test_routine_builder_appends_recovered_rung_when_no_chain_neighbor_is_present():
    # Same recovery mechanism, but the chain's own "next" pointer isn't (and
    # no other chain record's "next" points at this rung either) -- there's
    # no position to splice into, so the recovered rung must still surface
    # (better than silently dropping real logic) via an append-at-end
    # fallback, not be lost a second time.
    routine_id = 765662755
    missing_rung = 1957188902

    unzip = Unzip("../resources/CuteLogix.ACD").write_files("build")
    exp = ExportL5x("../resources/CuteLogix.ACD", "build")
    cur = exp._cur

    original_order = [row[0] for row in cur.execute(
        "SELECT object_id FROM region_map WHERE parent_id=? ORDER BY unknown", (routine_id,)
    ).fetchall()]

    cur.execute("DELETE FROM region_map WHERE parent_id=? AND object_id=?", (routine_id, missing_rung))
    # Replace whatever real chain record(s) the fixture's own RegnLink.Dat
    # produced for this rung -- both its own forward pointer AND the
    # preceding rung's real record pointing AT it -- with a single
    # controlled row whose "next" points nowhere resolvable. Isolates the
    # append-at-end fallback from the real, correctly-chained data already
    # present in this fixture (which the previous test relies on instead,
    # and which also independently resolves the same missing rung to the
    # same original position via the reverse/predecessor direction unless
    # removed here too).
    cur.execute("DELETE FROM regnlink_chain WHERE own_rung=? OR next_rung=?", (missing_rung, missing_rung))
    cur.execute(
        "INSERT INTO regnlink_chain VALUES (?,?,?)", (routine_id, missing_rung, 0xFFFFFFFF)
    )
    exp._db.commit()

    routine = RoutineBuilder(cur, routine_id).build()

    assert routine is not None
    expected = [oid for oid in original_order if oid != missing_rung] + [missing_rung]
    assert routine._rung_ids == expected


class _FakeRecord:
    def __init__(self, identifier, len_record):
        self.identifier = identifier
        self.len_record = len_record


class _FakeRecords:
    def __init__(self, records):
        self.record = records


class _FakeDat:
    def __init__(self, records):
        self.records = _FakeRecords(records)


def _patch_fake_dat(monkeypatch, records):
    class _FakeDbExtract:
        def __init__(self, path):
            self._path = path

        def read(self):
            return _FakeDat(records)

    monkeypatch.setattr(export_l5x_module, "DbExtract", _FakeDbExtract)
    monkeypatch.setattr(export_l5x_module.os.path, "exists", lambda p: True)


def test_parse_records_warning_inlines_index_and_exception_for_a_single_failure(monkeypatch, capsys):
    # Regression test for a real report: a single genuinely-dropped real
    # object (a routine's own Comps.Dat record) was indistinguishable from
    # routine multi-record padding/noise -- the warning only ever said
    # "skipped 1 unparseable record(s) of N", with no record index and no
    # real exception, anywhere, even under verbose=True.
    configure_logging(False)
    records = [
        _FakeRecord(64250, 100),
        _FakeRecord(64250, 50),
        _FakeRecord(64250, 200),
    ]
    _patch_fake_dat(monkeypatch, records)

    def parse_one(record):
        if record.len_record == 50:
            raise ValueError("requested 2 bytes, but only 0 bytes available")
        return (record.len_record,)

    result = _parse_records("fake.Dat", parse_one, "FakeLabel")
    captured = capsys.readouterr()

    assert result == [(100,), (200,)]
    assert "skipped 1 unparseable record(s) of 3" in captured.err
    assert "record 1" in captured.err
    assert "identifier=64250" in captured.err
    assert "len_record=50" in captured.err
    assert "requested 2 bytes, but only 0 bytes available" in captured.err


def test_parse_records_debug_detail_hidden_unless_verbose(monkeypatch, capsys):
    configure_logging(False)
    records = [_FakeRecord(64250, 50)]
    _patch_fake_dat(monkeypatch, records)

    def parse_one(record):
        raise ValueError("boom")

    _parse_records("fake.Dat", parse_one, "FakeLabel")
    captured = capsys.readouterr()

    # The summary WARNING line (<= _MAX_INLINE_FAILURE_DETAILS failures)
    # already carries the detail even in quiet mode -- but a per-record
    # DEBUG line should not appear twice/redundantly gate on verbose in a
    # way that breaks quiet mode's own WARNING-level guarantee.
    assert "skipped 1 unparseable record(s) of 1" in captured.err
    assert captured.err.count("boom") == 1


def test_parse_records_many_failures_summary_omits_inline_detail(monkeypatch, capsys):
    configure_logging(False)
    records = [_FakeRecord(64250, i) for i in range(_MAX_INLINE_FAILURE_DETAILS + 1)]
    _patch_fake_dat(monkeypatch, records)

    def parse_one(record):
        raise ValueError("boom")

    _parse_records("fake.Dat", parse_one, "FakeLabel")
    captured = capsys.readouterr()

    assert (
        f"skipped {_MAX_INLINE_FAILURE_DETAILS + 1} unparseable record(s) of "
        f"{_MAX_INLINE_FAILURE_DETAILS + 1}" in captured.err
    )
    assert "re-run with verbose=True" in captured.err
    # The per-record detail must not be dumped into the WARNING for a large
    # failure count -- that's exactly the noisy case this threshold exists
    # to avoid inlining.
    assert "record 0 " not in captured.err


def test_parse_records_per_record_detail_visible_under_verbose(monkeypatch, capsys):
    # configure_logging(True) is intentionally a no-op (verbose=True just
    # means "don't touch the sink") -- fine for a real process, but a prior
    # test's configure_logging(False) can leave an explicit sink bound to
    # THAT test's own (now-closed) capsys stream still registered, which
    # verbose=True's no-op wouldn't fix. Manage the sink directly here so
    # this test reliably observes DEBUG output regardless of run order,
    # same effect a real verbose=True caller gets in a real process (where
    # sys.stderr is never swapped out from under the sink like capsys does).
    loguru_logger.remove()
    loguru_logger.add(sys.stderr, level="DEBUG")
    try:
        configure_logging(True)
        records = [_FakeRecord(64250, i) for i in range(_MAX_INLINE_FAILURE_DETAILS + 1)]
        _patch_fake_dat(monkeypatch, records)

        def parse_one(record):
            raise ValueError("boom")

        _parse_records("fake.Dat", parse_one, "FakeLabel")
        captured = capsys.readouterr()

        # Every failure gets its own DEBUG line, visible under verbose=True,
        # even when there are too many to inline into the summary WARNING.
        for i in range(_MAX_INLINE_FAILURE_DETAILS + 1):
            assert f"record {i} " in captured.err
    finally:
        configure_logging(False)
