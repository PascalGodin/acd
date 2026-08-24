import os
import re
import shutil
import sqlite3
import threading
import time

import pytest

from acd import (
    db_delete_member,
    db_delete_routine,
    db_delete_tag,
    db_diff_io_addresses,
    db_diff_project,
    db_diff_routine,
    db_export_aoi,
    db_export_datatype,
    db_export_program,
    db_export_routine,
    db_find_tag_references,
    db_get_aoi,
    db_get_datatype,
    db_get_project_summary,
    db_get_routine,
    db_get_tag_value,
    db_insert_rung,
    db_insert_st_line,
    db_io_addresses_by_routine,
    db_list_aois,
    db_list_datatypes,
    db_list_routines,
    db_new_aoi,
    db_new_aoi_local_tag,
    db_new_aoi_parameter,
    db_new_datatype,
    db_new_member,
    db_new_routine,
    db_new_tag,
    db_set_rung_comment,
    db_set_tag_element_value,
    db_tag_exists,
    db_transaction,
    open_project_db,
)
from acd.api import get_routine, load_acd
from acd.l5x.elements import DataType, Routine, new_tag
from acd.l5x import project_db as project_db_module
from acd.l5x.project_db import (
    _materialize,
    _parse_tag_element_path,
    _ProjectLock,
    _TEMPORARY_IMPORT_DATATYPE_PREFIX,
)


@pytest.fixture
def acd_copy(tmp_path):
    """A private copy of the small fixture ACD in a scratch directory --
    open_project_db() creates a project_dir (extracted files + acd.db)
    next to whatever path it's given, so tests must never point it at the
    real resources/CuteLogix.ACD directly.
    """
    dst = tmp_path / "CuteLogix.ACD"
    shutil.copy(os.path.join("..", "resources", "CuteLogix.ACD"), dst)
    return dst


@pytest.fixture
def st_acd_copy(tmp_path):
    """A private copy of a fixture ACD that actually has an ST routine --
    CuteLogix.ACD (used by acd_copy above) has none, so ST-specific edit
    primitives (insert_st_line/delete_st_line/replace_st_line_safe) and the
    RLL-vs-ST type-guard tests need this instead.
    """
    dst = tmp_path / "ACDTestsNonRedundant.ACD"
    shutil.copy(os.path.join("..", "resources", "ACDTestsNonRedundant.ACD"), dst)
    return dst


@pytest.fixture
def aoi_acd_copy(tmp_path):
    """A private copy of a fixture ACD that has a real, pre-existing AOI
    (with real LocalTags) -- for confirming new_aoi()/db_new_aoi() never
    touches/replaces a real project's own AOIs, only appends alongside them.
    """
    dst = tmp_path / "ACDTestsWithAOI.ACD"
    shutil.copy(os.path.join("..", "resources", "ACDTestsWithAOI.ACD"), dst)
    return dst


def test_open_project_db_materializes_matching_summary(acd_copy):
    reference = load_acd(str(acd_copy), verbose=False)
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        summary = db.get_project_summary()
        assert summary["controller_name"] == reference.controller.name
        assert summary["controller_tag_count"] == len(reference.controller.tags)
        assert summary["routine_count"] == sum(len(p.routines) for p in reference.controller.programs)
        assert summary["data_types"] == [dt.name for dt in reference.controller.data_types]
    finally:
        db.close()


def test_materialize_skips_duplicate_temporary_import_placeholder_datatypes(acd_copy):
    # Regression test for a real reported blocker: a real .ACD legitimately
    # contained two distinct DataType records both rendering to the exact
    # same "ZZZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000" name (a Rockwell-
    # internal in-flight-import placeholder, left behind by back-to-back
    # partial-L5X imports) -- proj_data_types.name has a global UNIQUE
    # index, so the second INSERT crashed every rebuild, which blocks
    # EVERY db_* call (reads included) against that project, not just edits.
    project = load_acd(str(acd_copy), verbose=False)
    placeholder_name = f"{_TEMPORARY_IMPORT_DATATYPE_PREFIX}_000"
    placeholder1 = DataType(placeholder_name, placeholder_name, "NoFamily", "User", [])
    placeholder2 = DataType(placeholder_name, placeholder_name, "NoFamily", "User", [])
    real_count = len(project.controller.data_types)
    project.controller.data_types = list(project.controller.data_types) + [placeholder1, placeholder2]

    conn = sqlite3.connect(":memory:")
    try:
        _materialize(conn, project, str(acd_copy))  # must not raise
        count = conn.execute(
            "SELECT COUNT(*) FROM proj_data_types WHERE name LIKE ?",
            (f"{_TEMPORARY_IMPORT_DATATYPE_PREFIX}%",),
        ).fetchone()[0]
        assert count == 0
        total = conn.execute("SELECT COUNT(*) FROM proj_data_types").fetchone()[0]
        assert total == real_count
    finally:
        conn.close()


def test_materialize_raises_clear_error_on_genuine_datatype_name_collision(acd_copy):
    # A DUPLICATE name that ISN'T the known placeholder pattern is a real,
    # unhandled ambiguity this schema doesn't support -- must fail with a
    # clear, named error instead of a raw/opaque sqlite3.IntegrityError, so
    # a future instance of this (a differently-named collision, since the
    # exact placeholder name isn't the only theoretically possible one) is
    # fast to diagnose rather than another silent full-blocker.
    project = load_acd(str(acd_copy), verbose=False)
    dup1 = DataType("PDB_REAL_DUPLICATE", "PDB_REAL_DUPLICATE", "NoFamily", "User", [])
    dup2 = DataType("PDB_REAL_DUPLICATE", "PDB_REAL_DUPLICATE", "NoFamily", "User", [])
    project.controller.data_types = list(project.controller.data_types) + [dup1, dup2]

    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="PDB_REAL_DUPLICATE"):
            _materialize(conn, project, str(acd_copy))
    finally:
        conn.close()


def test_materialize_raises_clear_error_on_controller_tag_name_collision(acd_copy):
    # Same class of exposure the bug report explicitly asked about ("worth
    # checking whether tags/routines/AOIs have the same exposure") --
    # proj_tags is scoped by (program_id, name), so this needs two
    # controller-scope tags sharing a name, which is a narrower scenario
    # than the DataType case (global, unscoped) but not provably impossible.
    project = load_acd(str(acd_copy), verbose=False)
    dup1 = new_tag("PDB_DUP_TAG", "DINT")
    dup2 = new_tag("PDB_DUP_TAG", "DINT")
    project.controller.tags = list(project.controller.tags) + [dup1, dup2]

    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="PDB_DUP_TAG"):
            _materialize(conn, project, str(acd_copy))
    finally:
        conn.close()


def test_materialize_raises_clear_error_on_routine_name_collision(acd_copy):
    project = load_acd(str(acd_copy), verbose=False)
    program = project.controller.programs[0]
    dup1 = Routine("PDB_DUP_ROUTINE", "PDB_DUP_ROUTINE", "RLL", [])
    dup2 = Routine("PDB_DUP_ROUTINE", "PDB_DUP_ROUTINE", "RLL", [])
    program.routines = list(program.routines) + [dup1, dup2]

    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="PDB_DUP_ROUTINE"):
            _materialize(conn, project, str(acd_copy))
    finally:
        conn.close()


def test_open_project_db_does_not_rebuild_on_second_open(acd_copy, monkeypatch):
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.close()

    calls = []
    original = project_db_module._rebuild_project_db

    def _spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(project_db_module, "_rebuild_project_db", _spy)

    db2 = open_project_db(str(acd_copy), verbose=False)
    db2.close()

    assert calls == []


def test_open_project_db_rebuilds_when_source_mtime_changes(acd_copy):
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.new_tag("PDB_STALE_TEST_TAG", "DINT")
    db1.close()

    future = time.time() + 5
    os.utime(acd_copy, (future, future))

    db2 = open_project_db(str(acd_copy), verbose=False)
    try:
        assert db2.tag_exists("PDB_STALE_TEST_TAG") is False
    finally:
        db2.close()


def test_open_project_db_rebuild_true_forces_rebuild(acd_copy):
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.new_tag("PDB_FORCE_REBUILD_TAG", "DINT")
    db1.close()

    db2 = open_project_db(str(acd_copy), rebuild=True, verbose=False)
    try:
        assert db2.tag_exists("PDB_FORCE_REBUILD_TAG") is False
    finally:
        db2.close()


def test_open_project_db_warns_when_rebuild_discards_dirty_edits(acd_copy, capsys):
    """`configure_logging(verbose)` runs as the very first thing inside
    open_project_db() and calls log.remove() -- any sink added by the test
    BEFORE that call would be wiped before the warning below ever fires, so
    this asserts against real captured stderr (what configure_logging()'s
    own log.add(sys.stderr, level="WARNING") sink writes to), not a
    separately-added loguru sink.
    """
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.new_tag("PDB_DIRTY_TAG", "DINT")  # never exported
    db1.close()

    future = time.time() + 5
    os.utime(acd_copy, (future, future))

    capsys.readouterr()  # discard anything captured so far
    db2 = open_project_db(str(acd_copy), verbose=False)
    db2.close()
    captured = capsys.readouterr()

    assert "DISCARDS" in captured.err


def test_open_project_db_dirty_warning_only_fires_once_per_identical_message(acd_copy, capsys):
    # Regression test for a real report: this exact warning (message text
    # unchanged -- same acd_path/db_file each time) fired in full on every
    # triggering rebuild across a long session. A second, byte-identical
    # occurrence in the same process should log at DEBUG (invisible under
    # the default WARNING-level sink _log_once() relies on), not WARNING
    # again -- see _log_once()'s own docstring (export_l5x.py).
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.new_tag("PDB_DIRTY_TAG_1", "DINT")
    db1.close()
    future = time.time() + 5
    os.utime(acd_copy, (future, future))
    capsys.readouterr()
    open_project_db(str(acd_copy), verbose=False).close()
    first = capsys.readouterr()
    assert "DISCARDS" in first.err

    db2 = open_project_db(str(acd_copy), verbose=False)
    db2.new_tag("PDB_DIRTY_TAG_2", "DINT")
    db2.close()
    future2 = time.time() + 10
    os.utime(acd_copy, (future2, future2))
    capsys.readouterr()
    open_project_db(str(acd_copy), verbose=False).close()
    second = capsys.readouterr()
    assert "DISCARDS" not in second.err


def test_open_project_db_does_not_warn_when_rebuild_has_nothing_to_discard(acd_copy, capsys):
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.close()  # no edits made -- nothing dirty

    future = time.time() + 5
    os.utime(acd_copy, (future, future))

    capsys.readouterr()
    db2 = open_project_db(str(acd_copy), verbose=False)
    db2.close()
    captured = capsys.readouterr()

    assert "DISCARDS" not in captured.err


def test_open_project_db_configures_quiet_logging_even_without_rebuild(acd_copy, monkeypatch):
    """Regression test for a real gap: to_controller() calls
    ControllerBuilder directly against an already-open connection, bypassing
    ExportL5x.__post_init__ entirely on the "reuse existing DB, no rebuild"
    path -- the only other place configure_logging() was previously applied.
    Verified by spying on configure_logging() itself (deterministic) rather
    than depending on some specific internal log.info() call the small
    CuteLogix fixture may not happen to trigger (it has no deleted UDT
    members, so the exact message a real downstream report saw never fires
    against this fixture regardless of whether the fix is present).
    """
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.close()

    calls = []
    original = project_db_module.configure_logging

    def _spy(verbose):
        calls.append(verbose)
        return original(verbose)

    monkeypatch.setattr(project_db_module, "configure_logging", _spy)

    db2 = open_project_db(str(acd_copy), verbose=False)  # reuse path -- no rebuild
    db2.to_controller()
    db2.close()

    assert calls == [False]


def test_new_tag_persists_across_separate_open_calls(acd_copy):
    db1 = open_project_db(str(acd_copy), verbose=False)
    db1.new_tag("PDB_NEW_TAG", "DINT", description="a test tag", value=7)
    db1.close()

    db2 = open_project_db(str(acd_copy), verbose=False)
    try:
        assert db2.tag_exists("PDB_NEW_TAG") is True
        project = db2.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_NEW_TAG")
        assert tag.data_type == "DINT"
        assert tag.description == "a test tag"
        assert tag._initial_value == 7
    finally:
        db2.close()


def test_new_tag_program_scope_is_independent_of_controller_scope(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        reference_project = db.to_controller()
        program_name = reference_project.controller.programs[0].name

        db.new_tag("PDB_SCOPED_TAG", "DINT", program_name=program_name)
        assert db.tag_exists("PDB_SCOPED_TAG", program_name=program_name) is True
        assert db.tag_exists("PDB_SCOPED_TAG") is False

        project = db.to_controller()
        program = next(p for p in project.controller.programs if p.name == program_name)
        assert any(t.name == "PDB_SCOPED_TAG" for t in program.tags)
        assert not any(t.name == "PDB_SCOPED_TAG" for t in project.controller.tags)
    finally:
        db.close()


def test_new_tag_duplicate_name_raises_integrity_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_DUP_TAG", "DINT")
        with pytest.raises(sqlite3.IntegrityError):
            db.new_tag("PDB_DUP_TAG", "DINT")
    finally:
        db.close()


def test_edit_tag_updates_description_and_value(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_EDIT_TAG", "DINT", value=1)
        db.edit_tag("PDB_EDIT_TAG", description="edited description", value=99)
        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_EDIT_TAG")
        assert tag.description == "edited description"
        assert tag._initial_value == 99
    finally:
        db.close()


def test_edit_tag_missing_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.edit_tag("NO_SUCH_TAG", description="x")
    finally:
        db.close()


class TestParseTagElementPath:
    def test_index_and_single_member(self):
        assert _parse_tag_element_path("[3].PRE") == (3, ["PRE"])

    def test_index_and_nested_member_chain(self):
        assert _parse_tag_element_path("[3].Sub.PRE") == (3, ["Sub", "PRE"])

    def test_scalar_member_only_no_index(self):
        assert _parse_tag_element_path("PRE") == (None, ["PRE"])

    def test_scalar_member_with_leading_dot(self):
        assert _parse_tag_element_path(".PRE") == (None, ["PRE"])

    def test_bare_index_no_members(self):
        assert _parse_tag_element_path("[7]") == (7, [])

    def test_multidim_index_rejected(self):
        with pytest.raises(ValueError, match="single, single-dimension"):
            _parse_tag_element_path("[2,2]")

    def test_member_level_array_index_rejected(self):
        with pytest.raises(ValueError, match="single, single-dimension"):
            _parse_tag_element_path("[0].Times[2]")

    def test_empty_path_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_tag_element_path("")

    def test_invalid_member_segment_rejected(self):
        with pytest.raises(ValueError, match="invalid member segment"):
            _parse_tag_element_path("[0].1BadName")


def _build_motor_udt_pair(db):
    """PDB_Timer{PRE, ACC} and PDB_Motor{Run, StartFaultTimer: PDB_Timer} --
    the exact real shape from the motivating report (a Motor-shaped struct
    with a nested Timer-shaped struct member, used as an array-of-struct
    tag's own element type).
    """
    db.new_datatype("PDB_Timer")
    db.new_member("PDB_Timer", "PRE", "DINT")
    db.new_member("PDB_Timer", "ACC", "DINT")
    db.new_datatype("PDB_Motor")
    db.new_member("PDB_Motor", "Run", "BOOL")
    db.new_member("PDB_Motor", "StartFaultTimer", "PDB_Timer")


def test_set_tag_element_value_zero_fills_then_sets_one_leaf_of_array_of_struct(acd_copy):
    # The literal motivating case: StartFaultTimer.PRE for motor index 1 of
    # an 11-element array of Motor-shaped structs, with no other field
    # touched. Also confirms element 0/2 stay at their zero-filled default.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors", "PDB_Motor", dimensions="3")

        db.set_tag_element_value("Motors", "[1].StartFaultTimer.PRE", 5000)

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "Motors")
        iv = tag._initial_value
        assert len(iv) == 3
        assert iv[1]["StartFaultTimer"]["PRE"] == 5000
        assert iv[1]["StartFaultTimer"]["ACC"] == 0
        assert iv[1]["Run"] == 0
        assert iv[0] == {"Run": 0, "StartFaultTimer": {"PRE": 0, "ACC": 0}}
        assert iv[2] == {"Run": 0, "StartFaultTimer": {"PRE": 0, "ACC": 0}}
    finally:
        db.close()


def test_set_tag_element_value_second_call_preserves_first_edit(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors2", "PDB_Motor", dimensions="2")

        db.set_tag_element_value("Motors2", "[0].StartFaultTimer.PRE", 111)
        db.set_tag_element_value("Motors2", "[1].StartFaultTimer.PRE", 222)

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "Motors2")
        assert tag._initial_value[0]["StartFaultTimer"]["PRE"] == 111
        assert tag._initial_value[1]["StartFaultTimer"]["PRE"] == 222
    finally:
        db.close()


def test_set_tag_element_value_scalar_struct_tag_no_index(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("SingleMotor", "PDB_Motor")  # no dimensions -- scalar

        db.set_tag_element_value("SingleMotor", "StartFaultTimer.PRE", 42)

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "SingleMotor")
        assert tag._initial_value["StartFaultTimer"]["PRE"] == 42
        assert tag._initial_value["Run"] == 0
    finally:
        db.close()


def test_set_tag_element_value_bare_index_on_primitive_array(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_PrimArray", "DINT", dimensions="5")

        db.set_tag_element_value("PDB_PrimArray", "[2]", 777)

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_PrimArray")
        assert tag._initial_value == [0, 0, 777, 0, 0]
    finally:
        db.close()


def test_set_tag_element_value_zero_fills_missing_member_on_existing_value(acd_copy):
    # Simulates the real "mutating a UDT with live tag instances" gap this
    # method reuses _zero_value_for_member() to patch around: an existing
    # stored value that's missing a member entirely (e.g. added to the type
    # after the value was first decoded/set).
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors3", "PDB_Motor", dimensions="2", value=[
            {"Run": 1},  # StartFaultTimer entirely missing on purpose
            {"Run": 0, "StartFaultTimer": {"PRE": 9, "ACC": 9}},
        ])

        db.set_tag_element_value("Motors3", "[0].StartFaultTimer.PRE", 55)

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "Motors3")
        assert tag._initial_value[0]["Run"] == 1  # untouched
        assert tag._initial_value[0]["StartFaultTimer"]["PRE"] == 55
        assert tag._initial_value[0]["StartFaultTimer"]["ACC"] == 0  # zero-filled
        assert tag._initial_value[1]["StartFaultTimer"]["PRE"] == 9  # untouched
    finally:
        db.close()


def test_set_tag_element_value_missing_tag_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.set_tag_element_value("NO_SUCH_TAG", "[0].X", 1)
    finally:
        db.close()


def test_set_tag_element_value_missing_member_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors4", "PDB_Motor", dimensions="1")
        with pytest.raises(KeyError):
            db.set_tag_element_value("Motors4", "[0].NoSuchMember", 1)
    finally:
        db.close()


def test_set_tag_element_value_out_of_bounds_index_raises(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors5", "PDB_Motor", dimensions="3")
        with pytest.raises(ValueError, match="out of bounds"):
            db.set_tag_element_value("Motors5", "[3].Run", 1)
    finally:
        db.close()


def test_set_tag_element_value_index_required_for_array_tag(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors6", "PDB_Motor", dimensions="3")
        with pytest.raises(ValueError, match="must start with"):
            db.set_tag_element_value("Motors6", "Run", 1)
    finally:
        db.close()


def test_set_tag_element_value_index_forbidden_for_scalar_tag(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors7", "PDB_Motor")  # scalar
        with pytest.raises(ValueError, match="not an array"):
            db.set_tag_element_value("Motors7", "[0].Run", 1)
    finally:
        db.close()


def test_set_tag_element_value_multidim_tag_rejected(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        _build_motor_udt_pair(db)
        db.new_tag("Motors8", "PDB_Motor", dimensions="2,2")
        with pytest.raises(ValueError, match="single-dimension"):
            db.set_tag_element_value("Motors8", "[0].Run", 1)
    finally:
        db.close()


def test_db_set_tag_element_value_stateless_wrapper(acd_copy):
    db_new_datatype(str(acd_copy), "PDB_Timer2")
    db_new_member(str(acd_copy), "PDB_Timer2", "PRE", "DINT")
    db_new_datatype(str(acd_copy), "PDB_Motor2")
    db_new_member(str(acd_copy), "PDB_Motor2", "StartFaultTimer", "PDB_Timer2")
    db_new_tag(str(acd_copy), "MotorsWrap", "PDB_Motor2", dimensions="2")

    db_set_tag_element_value(str(acd_copy), "MotorsWrap", "[1].StartFaultTimer.PRE", 321)

    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "MotorsWrap")
        assert tag._initial_value[1]["StartFaultTimer"]["PRE"] == 321
    finally:
        db.close()


def test_set_tag_comment_element_path(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_COMMENT_TAG", "DINT")
        db.set_tag_comment("PDB_COMMENT_TAG", "PDB_COMMENT_TAG[0]", "element zero comment")
        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_COMMENT_TAG")
        assert ("PDB_COMMENT_TAG[0]", "element zero comment") in tag._comments
        # path must carry the tag-name prefix, per _build_comments_xml's own
        # `if not ref.startswith(tag_name): continue` filter -- confirm it
        # actually renders, not just that it's stored in the raw list.
        assert 'Operand="[0]"' in tag.to_xml()
    finally:
        db.close()


def test_set_tag_comment_without_tag_name_prefix_is_silently_dropped_at_render(acd_copy):
    """Documents the exact footgun a downstream agent hit: `path` must be
    the FULL tag-qualified address (tag name included), same convention as
    `Tag._comments`/`_build_comments_xml`'s own `ref.startswith(tag_name)`
    check -- passing just the suffix (e.g. "[0]") stores fine (no error)
    but is silently filtered out of the rendered XML.
    """
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_COMMENT_TAG2", "DINT")
        db.set_tag_comment("PDB_COMMENT_TAG2", "[0]", "element zero comment")
        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_COMMENT_TAG2")
        assert ("[0]", "element zero comment") in tag._comments
        assert "<Comments>" not in tag.to_xml()
    finally:
        db.close()


def test_set_tag_comment_empty_text_clears_it(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_COMMENT_TAG3", "DINT")
        db.set_tag_comment("PDB_COMMENT_TAG3", "PDB_COMMENT_TAG3[0]", "will be cleared")
        db.set_tag_comment("PDB_COMMENT_TAG3", "PDB_COMMENT_TAG3[0]", "")

        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_COMMENT_TAG3")
        assert "<Comments>" not in tag.to_xml()
    finally:
        db.close()


def test_get_tag_comment_element_path(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_GETCOMMENT_TAG", "DINT")
        db.set_tag_comment("PDB_GETCOMMENT_TAG", "PDB_GETCOMMENT_TAG[0]", "Tray 1 Full")

        assert db.get_tag_comment("PDB_GETCOMMENT_TAG", "PDB_GETCOMMENT_TAG[0]") == "Tray 1 Full"
    finally:
        db.close()


def test_get_tag_comment_whole_tag_description_via_none_or_empty_path(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_GETCOMMENT_TAG2", "DINT")
        db.edit_tag("PDB_GETCOMMENT_TAG2", description="the whole tag description")

        assert db.get_tag_comment("PDB_GETCOMMENT_TAG2") == "the whole tag description"
        assert db.get_tag_comment("PDB_GETCOMMENT_TAG2", "") == "the whole tag description"
        assert db.get_tag_comment("PDB_GETCOMMENT_TAG2", None) == "the whole tag description"
    finally:
        db.close()


def test_get_tag_comment_returns_none_when_no_comment_stored(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_GETCOMMENT_TAG3", "DINT")
        assert db.get_tag_comment("PDB_GETCOMMENT_TAG3", "PDB_GETCOMMENT_TAG3[5]") is None
        assert db.get_tag_comment("PDB_GETCOMMENT_TAG3") is None  # no description either
    finally:
        db.close()


def test_get_tag_comment_missing_tag_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.get_tag_comment("NO_SUCH_TAG", "NO_SUCH_TAG[0]")
    finally:
        db.close()


def test_list_tag_comments_returns_all_at_once(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_LISTCOMMENT_TAG", "DINT", dimensions="8")
        db.edit_tag("PDB_LISTCOMMENT_TAG", description="whole tag desc")
        db.set_tag_comment("PDB_LISTCOMMENT_TAG", "PDB_LISTCOMMENT_TAG[0]", "Tray 1 Full")
        db.set_tag_comment("PDB_LISTCOMMENT_TAG", "PDB_LISTCOMMENT_TAG[1]", "Tray 2 Full")

        comments = db.list_tag_comments("PDB_LISTCOMMENT_TAG")
        assert comments == {
            "": "whole tag desc",
            "PDB_LISTCOMMENT_TAG[0]": "Tray 1 Full",
            "PDB_LISTCOMMENT_TAG[1]": "Tray 2 Full",
        }
    finally:
        db.close()


def test_list_tag_comments_empty_when_no_comments(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_LISTCOMMENT_TAG2", "DINT")
        assert db.list_tag_comments("PDB_LISTCOMMENT_TAG2") == {}
    finally:
        db.close()


def test_list_tag_comments_missing_tag_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.list_tag_comments("NO_SUCH_TAG")
    finally:
        db.close()


def test_db_get_tag_comment_and_list_tag_comments_stateless_wrappers(acd_copy):
    from acd import db_get_tag_comment, db_list_tag_comments, db_set_tag_comment as _db_set_tag_comment

    db_new_tag(str(acd_copy), "PDB_GETCOMMENT_WRAP", "DINT")
    _db_set_tag_comment(str(acd_copy), "PDB_GETCOMMENT_WRAP", "PDB_GETCOMMENT_WRAP[0]", "hi")

    assert db_get_tag_comment(str(acd_copy), "PDB_GETCOMMENT_WRAP",
                               "PDB_GETCOMMENT_WRAP[0]") == "hi"
    assert db_list_tag_comments(str(acd_copy), "PDB_GETCOMMENT_WRAP") == {
        "PDB_GETCOMMENT_WRAP[0]": "hi",
    }


def test_new_datatype_creates_empty_udt(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_NEW_UDT", description="a test type")

        project = db.to_controller()
        dt = next(d for d in project.controller.data_types if d.name == "PDB_NEW_UDT")
        assert dt.family == "NoFamily"
        assert dt.cls == "User"
        assert dt.members == []
        assert dt._description == "a test type"
    finally:
        db.close()


def test_new_datatype_can_be_populated_with_new_member(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_NEW_UDT")
        db.new_member("PDB_NEW_UDT", "Field1", "DINT")
        db.new_member("PDB_NEW_UDT", "Flag1", "BIT")

        project = db.to_controller()
        dt = next(d for d in project.controller.data_types if d.name == "PDB_NEW_UDT")
        names = [m.name for m in dt.members]
        assert "Field1" in names
        assert "Flag1" in names
        bit_member = next(m for m in dt.members if m.name == "Flag1")
        assert bit_member.target is not None
        assert bit_member.bit_number == 0
    finally:
        db.close()


def test_new_datatype_duplicate_name_raises(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_NEW_UDT")
        with pytest.raises(sqlite3.IntegrityError):
            db.new_datatype("PDB_NEW_UDT")
    finally:
        db.close()


def test_get_datatype_returns_members_without_full_rehydration(acd_copy, monkeypatch):
    # Regression test for a real report: inspecting one UDT's members
    # previously forced a full to_controller() rehydration (every tag's
    # value, every routine, every AOI) just to answer "what members does
    # this ONE type have" -- get_datatype() must go SQL-direct instead.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_GD_UDT", description="a test type")
        db.new_member("PDB_GD_UDT", "Field1", "DINT")
        db.new_member("PDB_GD_UDT", "Flag1", "BIT")

        calls = []
        original = project_db_module.ControllerBuilder.build

        def _spy(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(project_db_module.ControllerBuilder, "build", _spy)

        dt = db.get_datatype("PDB_GD_UDT")

        assert calls == []
        assert dt["name"] == "PDB_GD_UDT"
        assert dt["description"] == "a test type"
        assert dt["family"] == "NoFamily"
        assert dt["cls"] == "User"
        # "Flag1" (a BIT member) allocates its own hidden backing field
        # (see new_bit_member()), so the member list also has a
        # "ZZZZZZZZZZ..."-named hidden SINT alongside Field1/Flag1.
        names = [m["name"] for m in dt["members"]]
        assert "Field1" in names
        assert "Flag1" in names
        field1 = next(m for m in dt["members"] if m["name"] == "Field1")
        assert field1["data_type"] == "DINT"
        bit_member = next(m for m in dt["members"] if m["name"] == "Flag1")
        assert bit_member["target"] is not None
        assert bit_member["bit_number"] == 0
    finally:
        db.close()


def test_get_datatype_missing_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.get_datatype("NO_SUCH_TYPE")
    finally:
        db.close()


def test_list_datatypes_includes_real_and_new_types_with_member_counts(acd_copy):
    reference = load_acd(str(acd_copy), verbose=False)
    real_names = {dt.name for dt in reference.controller.data_types}

    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_LD_UDT")
        db.new_member("PDB_LD_UDT", "Field1", "DINT")
        db.new_member("PDB_LD_UDT", "Field2", "DINT")

        listing = {d["name"]: d for d in db.list_datatypes()}
        assert real_names <= set(listing)
        assert listing["PDB_LD_UDT"]["member_count"] == 2
        # No "members" key at all -- this is the summary-only listing.
        assert "members" not in listing["PDB_LD_UDT"]
    finally:
        db.close()


def test_db_get_datatype_and_list_datatypes_stateless_wrappers(acd_copy):
    db_new_tag(str(acd_copy), "PDB_UNUSED_TAG", "DINT")  # ensure DB exists
    from acd import db_new_datatype as _db_new_datatype, db_new_member as _db_new_member
    _db_new_datatype(str(acd_copy), "PDB_GD2_UDT")
    _db_new_member(str(acd_copy), "PDB_GD2_UDT", "Field1", "DINT")

    dt = db_get_datatype(str(acd_copy), "PDB_GD2_UDT")
    assert dt["name"] == "PDB_GD2_UDT"
    assert [m["name"] for m in dt["members"]] == ["Field1"]

    listing = db_list_datatypes(str(acd_copy))
    assert any(d["name"] == "PDB_GD2_UDT" and d["member_count"] == 1 for d in listing)


def test_get_aoi_fast_path_for_db_created_aoi(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_GA_AOI", description="a test AOI")
        db.new_aoi_parameter("PDB_GA_AOI", "In1", "DINT", usage="Input")
        db.new_aoi_local_tag("PDB_GA_AOI", "Scratch1", "DINT")

        aoi = db.get_aoi("PDB_GA_AOI")
        assert aoi["name"] == "PDB_GA_AOI"
        assert aoi["description"] == "a test AOI"
        assert [p["name"] for p in aoi["parameters"]] == ["In1"]
        assert aoi["parameters"][0]["usage"] == "Input"
        assert [lt["name"] for lt in aoi["local_tags"]] == ["Scratch1"]
    finally:
        db.close()


def test_get_aoi_falls_back_to_rehydration_for_real_pre_existing_aoi(aoi_acd_copy):
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    real_aoi = reference.controller.aois[0]

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        aoi = db.get_aoi(real_aoi.name)
        assert aoi["name"] == real_aoi.name
        assert [p["name"] for p in aoi["parameters"]] == [p.name for p in real_aoi.parameters]
        assert [lt["name"] for lt in aoi["local_tags"]] == [lt.name for lt in real_aoi.local_tags]
    finally:
        db.close()


def test_get_aoi_missing_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.get_aoi("NO_SUCH_AOI")
    finally:
        db.close()


def test_list_aois_includes_real_and_new_aois(aoi_acd_copy):
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    real_names = {a.name for a in reference.controller.aois}

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_LA_AOI")
        db.new_aoi_parameter("PDB_LA_AOI", "In1", "DINT", usage="Input")

        listing = {a["name"]: a for a in db.list_aois()}
        assert real_names <= set(listing)
        assert listing["PDB_LA_AOI"]["parameter_count"] == 1
    finally:
        db.close()


def test_db_get_aoi_and_list_aois_stateless_wrappers(acd_copy):
    from acd import db_new_aoi as _db_new_aoi
    _db_new_aoi(str(acd_copy), "PDB_GA2_AOI")

    aoi = db_get_aoi(str(acd_copy), "PDB_GA2_AOI")
    assert aoi["name"] == "PDB_GA2_AOI"

    listing = db_list_aois(str(acd_copy))
    assert any(a["name"] == "PDB_GA2_AOI" for a in listing)


def test_db_new_datatype_stateless_wrapper_and_export(acd_copy, tmp_path):
    db_new_datatype(str(acd_copy), "PDB_NEW_UDT", description="a test type")
    db_new_member(str(acd_copy), "PDB_NEW_UDT", "Field1", "DINT")

    output_path = tmp_path / "new_datatype.L5X"
    db_export_datatype(str(acd_copy), "PDB_NEW_UDT", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert 'Name="PDB_NEW_UDT"' in content
    assert 'Name="Field1"' in content


def test_new_member_appends_by_default(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name
        before = len(project.controller.data_types[0].members)

        db.new_member(dt_name, "PDB_NEW_MEMBER", "DINT")

        project2 = db.to_controller()
        dt2 = next(d for d in project2.controller.data_types if d.name == dt_name)
        assert len(dt2.members) == before + 1
        assert dt2.members[-1].name == "PDB_NEW_MEMBER"
        assert dt2.members[-1].data_type == "DINT"
    finally:
        db.close()


def test_new_member_inserts_at_requested_index(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name
        original_first = project.controller.data_types[0].members[0].name

        db.new_member(dt_name, "PDB_FIRST_MEMBER", "DINT", index=0)

        project2 = db.to_controller()
        dt2 = next(d for d in project2.controller.data_types if d.name == dt_name)
        assert dt2.members[0].name == "PDB_FIRST_MEMBER"
        assert dt2.members[1].name == original_first
    finally:
        db.close()


def test_new_member_missing_data_type_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.new_member("NO_SUCH_UDT", "X", "DINT")
    finally:
        db.close()


def test_edit_member_updates_only_passed_fields(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT")
        db.new_member("PDB_EM_UDT", "Action_12", "BOOL", description="Auto Index Accum Chain")

        db.edit_member("PDB_EM_UDT", "Action_12", description="Auto Index Storage Chain")

        dt = db.get_datatype("PDB_EM_UDT")
        member = next(m for m in dt["members"] if m["name"] == "Action_12")
        assert member["description"] == "Auto Index Storage Chain"
        assert member["data_type"] == "BOOL"  # untouched
    finally:
        db.close()


def test_edit_member_changes_data_type_and_dimension_via_new_member_defaults(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT2")
        db.new_member("PDB_EM_UDT2", "Field1", "DINT")

        db.edit_member("PDB_EM_UDT2", "Field1", member_data_type="REAL", dimension=5)

        dt = db.get_datatype("PDB_EM_UDT2")
        member = next(m for m in dt["members"] if m["name"] == "Field1")
        assert member["data_type"] == "REAL"
        assert member["dimension"] == 5
        assert member["radix"] == "Float"  # re-derived default for REAL
    finally:
        db.close()


def test_edit_member_rejects_conversion_to_bit(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT3")
        db.new_member("PDB_EM_UDT3", "Field1", "DINT")
        with pytest.raises(ValueError, match="cannot convert"):
            db.edit_member("PDB_EM_UDT3", "Field1", member_data_type="BIT")
    finally:
        db.close()


def test_edit_member_rejects_type_change_on_existing_bit_member(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT4")
        db.new_member("PDB_EM_UDT4", "Flag1", "BIT")
        with pytest.raises(ValueError, match="BIT-overlay member"):
            db.edit_member("PDB_EM_UDT4", "Flag1", member_data_type="DINT")
        with pytest.raises(ValueError, match="BIT-overlay member"):
            db.edit_member("PDB_EM_UDT4", "Flag1", dimension=2)
    finally:
        db.close()


def test_edit_member_allows_description_on_existing_bit_member(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT5")
        db.new_member("PDB_EM_UDT5", "Flag1", "BIT")

        db.edit_member("PDB_EM_UDT5", "Flag1", description="a bit flag")

        dt = db.get_datatype("PDB_EM_UDT5")
        member = next(m for m in dt["members"] if m["name"] == "Flag1")
        assert member["description"] == "a bit flag"
        assert member["target"] is not None  # untouched
    finally:
        db.close()


def test_edit_member_missing_data_type_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.edit_member("NO_SUCH_UDT", "X", description="x")
    finally:
        db.close()


def test_edit_member_missing_member_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_datatype("PDB_EM_UDT6")
        with pytest.raises(KeyError):
            db.edit_member("PDB_EM_UDT6", "NoSuchMember", description="x")
    finally:
        db.close()


def test_db_edit_member_stateless_wrapper(acd_copy):
    from acd import db_edit_member

    db_new_datatype(str(acd_copy), "PDB_EM_UDT7")
    db_new_member(str(acd_copy), "PDB_EM_UDT7", "Field1", "DINT", description="original")

    db_edit_member(str(acd_copy), "PDB_EM_UDT7", "Field1", description="edited")

    db = open_project_db(str(acd_copy), verbose=False)
    try:
        dt = db.get_datatype("PDB_EM_UDT7")
        assert dt["members"][0]["description"] == "edited"
    finally:
        db.close()


def test_new_member_bit_type_allocates_backing_field(acd_copy):
    # Regression test for a real reported bug: db_new_member(dt, name, "BIT")
    # used to commit with no error but with hidden=0/target=NULL/bit_number=NULL
    # -- persisted that way even if the in-memory Member had a real
    # target/bit_number, because the SQL INSERT itself hardcoded those
    # columns. Verify both the allocation AND that it survives a rehydration
    # (a fresh to_controller() call, not just the in-memory return value).
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name

        db.new_member(dt_name, "PDB_BIT_1", "BIT")

        project2 = db.to_controller()
        dt2 = next(d for d in project2.controller.data_types if d.name == dt_name)
        bit_member = next(m for m in dt2.members if m.name == "PDB_BIT_1")
        assert bit_member.data_type == "BIT"
        assert bit_member.hidden is False
        assert bit_member.target is not None
        assert bit_member.bit_number == 0
        backing = next(m for m in dt2.members if m.name == bit_member.target)
        assert backing.hidden is True
        assert backing.data_type == "SINT"
    finally:
        db.close()


def test_new_member_bit_type_reuses_free_bit_across_separate_calls(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name

        db.new_member(dt_name, "PDB_BIT_A", "BIT")
        db.new_member(dt_name, "PDB_BIT_B", "BIT")

        project2 = db.to_controller()
        dt2 = next(d for d in project2.controller.data_types if d.name == dt_name)
        bit_a = next(m for m in dt2.members if m.name == "PDB_BIT_A")
        bit_b = next(m for m in dt2.members if m.name == "PDB_BIT_B")
        assert bit_a.target == bit_b.target
        assert bit_a.bit_number == 0
        assert bit_b.bit_number == 1
        assert sum(1 for m in dt2.members if m.hidden) == 1
    finally:
        db.close()


def test_new_member_bit_type_creates_second_backing_field_once_full(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name

        for i in range(9):
            db.new_member(dt_name, f"PDB_BIT_{i}", "BIT")

        project2 = db.to_controller()
        dt2 = next(d for d in project2.controller.data_types if d.name == dt_name)
        assert sum(1 for m in dt2.members if m.hidden) == 2
        last = next(m for m in dt2.members if m.name == "PDB_BIT_8")
        first = next(m for m in dt2.members if m.name == "PDB_BIT_0")
        assert last.target != first.target
        assert last.bit_number == 0
    finally:
        db.close()


def test_db_new_member_bit_type_persists_through_export_datatype(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name
        db.new_member(dt_name, "PDB_BIT_EXPORT", "BIT")

        output_path = tmp_path / "exported_datatype.L5X"
        db.export_datatype(dt_name, str(output_path))
        content = output_path.read_text(encoding="utf-8")
        assert 'Name="PDB_BIT_EXPORT"' in content
        assert 'DataType="BIT"' in content
        assert re.search(r'Name="PDB_BIT_EXPORT"[^>]*Target="[^"]+"', content)
        assert re.search(r'Name="PDB_BIT_EXPORT"[^>]*BitNumber="0"', content)
    finally:
        db.close()


def test_new_routine_rll_can_be_populated_and_read_back(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program_name = project.controller.programs[0].name

        db.new_routine("PDB_NEW_ROUTINE", "RLL", program_name, description="a test routine")
        db.insert_rung("PDB_NEW_ROUTINE", 0, "NOP();", program_name=program_name)

        routine = db.get_routine("PDB_NEW_ROUTINE", program_name)
        assert routine["type"] == "RLL"
        assert routine["description"] == "a test routine"
        assert routine["rungs"] == ["NOP();"]

        listed = db.list_routines(program_name)
        assert any(r["routine"] == "PDB_NEW_ROUTINE" for r in listed)
    finally:
        db.close()


def test_new_routine_st_can_be_populated_and_read_back(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program_name = project.controller.programs[0].name

        db.new_routine("PDB_NEW_ST_ROUTINE", "ST", program_name)
        db.insert_st_line("PDB_NEW_ST_ROUTINE", 0, "X := 1;", program_name=program_name)

        routine = db.get_routine("PDB_NEW_ST_ROUTINE", program_name)
        assert routine["type"] == "ST"
        assert routine["st_lines"] == ["X := 1;"]
    finally:
        db.close()


def test_new_routine_requires_program_name(acd_copy):
    # program_name=None with no aoi_name given either is the same "neither
    # scope given" case as test_new_routine_requires_exactly_one_of_program_or_aoi
    # -- kept as its own test since this is the exact call shape a caller
    # migrating from the pre-AOI-support signature would still write.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(ValueError, match="exactly one of program_name/aoi_name"):
            db.new_routine("PDB_NEW_ROUTINE", "RLL", None)
    finally:
        db.close()


def test_new_routine_rejects_invalid_type(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program_name = project.controller.programs[0].name
        with pytest.raises(ValueError, match="must be 'RLL' or 'ST'"):
            db.new_routine("PDB_NEW_ROUTINE", "SFC", program_name)
    finally:
        db.close()


def test_new_routine_missing_program_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.new_routine("PDB_NEW_ROUTINE", "RLL", "NO_SUCH_PROGRAM")
    finally:
        db.close()


def test_new_routine_duplicate_name_raises(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program_name = project.controller.programs[0].name
        db.new_routine("PDB_NEW_ROUTINE", "RLL", program_name)
        with pytest.raises(sqlite3.IntegrityError):
            db.new_routine("PDB_NEW_ROUTINE", "RLL", program_name)
    finally:
        db.close()


def test_db_new_routine_stateless_wrapper_and_export(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    project = db.to_controller()
    program_name = project.controller.programs[0].name
    db.close()

    db_new_routine(str(acd_copy), "PDB_NEW_ROUTINE", "RLL", program_name)
    db_insert_rung(str(acd_copy), "PDB_NEW_ROUTINE", 0,
                    "XIC(Always_Off)OTE(Always_Off);", program_name=program_name)

    output_path = tmp_path / "new_routine.L5X"
    db_export_routine(str(acd_copy), "PDB_NEW_ROUTINE", str(output_path), program_name=program_name)
    content = output_path.read_text(encoding="utf-8")
    assert 'Name="PDB_NEW_ROUTINE"' in content
    assert "OTE(Always_Off)" in content


def test_new_aoi_creates_empty_aoi(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI", description="a test AOI")

        project = db.to_controller()
        aoi = next(a for a in project.controller.aois if a.name == "PDB_NEW_AOI")
        assert aoi.parameters == []
        assert aoi.routines == []
        assert aoi._description == "a test AOI"
    finally:
        db.close()


def test_new_aoi_can_be_populated_with_parameters_and_routine(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_aoi_parameter("PDB_NEW_AOI", "In1", "DINT", usage="Input")
        db.new_aoi_parameter("PDB_NEW_AOI", "Out1", "DINT", usage="Output")
        db.new_routine("Logic", "RLL", aoi_name="PDB_NEW_AOI")
        db.insert_rung("Logic", 0, "MOV(In1,Out1);", aoi_name="PDB_NEW_AOI")

        project = db.to_controller()
        aoi = next(a for a in project.controller.aois if a.name == "PDB_NEW_AOI")
        assert [p.name for p in aoi.parameters] == ["In1", "Out1"]
        assert len(aoi.routines) == 1
        assert aoi.routines[0].name == "Logic"
        assert aoi.routines[0].rungs == ["MOV(In1,Out1);"]
    finally:
        db.close()


def test_new_aoi_parameter_missing_aoi_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.new_aoi_parameter("NO_SUCH_AOI", "In1", "DINT")
    finally:
        db.close()


def test_edit_aoi_parameter_updates_only_passed_fields(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_EAP_AOI")
        db.new_aoi_parameter("PDB_EAP_AOI", "In1", "DINT", usage="Input",
                              description="original")

        db.edit_aoi_parameter("PDB_EAP_AOI", "In1", description="updated")

        aoi = db.get_aoi("PDB_EAP_AOI")
        param = next(p for p in aoi["parameters"] if p["name"] == "In1")
        assert param["description"] == "updated"
        assert param["data_type"] == "DINT"  # untouched
        assert param["usage"] == "Input"  # untouched
    finally:
        db.close()


def test_edit_aoi_parameter_revalidates_merged_shape(acd_copy):
    # Switching usage from Input (elementary-only) to Output while ALSO
    # setting a STRING data_type must be rejected the same way
    # new_aoi_parameter() itself would reject it -- an edit can't be used
    # to sneak a parameter into a shape creation never could.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_EAP_AOI2")
        db.new_aoi_parameter("PDB_EAP_AOI2", "In1", "DINT", usage="Input")

        with pytest.raises(ValueError):
            db.edit_aoi_parameter("PDB_EAP_AOI2", "In1", data_type="STRING")
    finally:
        db.close()


def test_edit_aoi_parameter_missing_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_EAP_AOI3")
        with pytest.raises(KeyError):
            db.edit_aoi_parameter("PDB_EAP_AOI3", "NoSuchParam", description="x")
    finally:
        db.close()


def test_delete_aoi_parameter_removes_it(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_DAP_AOI")
        db.new_aoi_parameter("PDB_DAP_AOI", "In1", "DINT")
        db.new_aoi_parameter("PDB_DAP_AOI", "In2", "DINT")

        db.delete_aoi_parameter("PDB_DAP_AOI", "In1")

        aoi = db.get_aoi("PDB_DAP_AOI")
        assert [p["name"] for p in aoi["parameters"]] == ["In2"]
    finally:
        db.close()


def test_delete_aoi_parameter_missing_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_DAP_AOI2")
        with pytest.raises(KeyError):
            db.delete_aoi_parameter("PDB_DAP_AOI2", "NoSuchParam")
    finally:
        db.close()


def test_db_edit_aoi_parameter_and_delete_aoi_parameter_stateless_wrappers(acd_copy, tmp_path):
    from acd import db_delete_aoi_parameter, db_edit_aoi_parameter

    db_new_aoi(str(acd_copy), "PDB_EAP_WRAP_AOI")
    db_new_aoi_parameter(str(acd_copy), "PDB_EAP_WRAP_AOI", "In1", "DINT",
                          description="original")
    db_new_aoi_parameter(str(acd_copy), "PDB_EAP_WRAP_AOI", "In2", "DINT")

    db_edit_aoi_parameter(str(acd_copy), "PDB_EAP_WRAP_AOI", "In1", description="edited")
    db_delete_aoi_parameter(str(acd_copy), "PDB_EAP_WRAP_AOI", "In2")

    db = open_project_db(str(acd_copy), verbose=False)
    try:
        aoi = db.get_aoi("PDB_EAP_WRAP_AOI")
        assert [p["name"] for p in aoi["parameters"]] == ["In1"]
        assert aoi["parameters"][0]["description"] == "edited"
    finally:
        db.close()


def test_new_aoi_duplicate_name_raises(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        with pytest.raises(sqlite3.IntegrityError):
            db.new_aoi("PDB_NEW_AOI")
    finally:
        db.close()


def test_new_routine_requires_exactly_one_of_program_or_aoi(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program_name = project.controller.programs[0].name
        db.new_aoi("PDB_NEW_AOI")

        with pytest.raises(ValueError, match="exactly one"):
            db.new_routine("BAD1", "RLL")
        with pytest.raises(ValueError, match="exactly one"):
            db.new_routine("BAD2", "RLL", program_name=program_name, aoi_name="PDB_NEW_AOI")
    finally:
        db.close()


def test_new_routine_rejects_invalid_aoi_routine_name(acd_copy):
    # Regression test: real Studio 5000 rejected import of a routine named
    # after the AOI itself ("Lug_Advance_Logic") with "Invalid name for
    # Add-On Instruction routine." -- unlike a Program's routine, an AOI's
    # own routine name is a fixed, Rockwell-reserved set.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        with pytest.raises(ValueError, match="not a valid AOI routine name"):
            db.new_routine("PDB_NEW_AOI_Logic", "RLL", aoi_name="PDB_NEW_AOI")
    finally:
        db.close()


def test_new_routine_accepts_reserved_aoi_routine_names(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_routine("Logic", "RLL", aoi_name="PDB_NEW_AOI")
        db.new_routine("Prescan", "RLL", aoi_name="PDB_NEW_AOI")

        project = db.to_controller()
        aoi = next(a for a in project.controller.aois if a.name == "PDB_NEW_AOI")
        assert {r.name for r in aoi.routines} == {"Logic", "Prescan"}
    finally:
        db.close()


def test_routine_content_functions_use_aoi_name_to_disambiguate_logic_routines(acd_copy):
    # The actual reported gap: db_new_routine(..., aoi_name=...) could
    # create an AOI-scoped "Logic" routine, but nothing downstream could
    # address it back -- program_name= doesn't resolve AOI scope, and with
    # two AOIs both named "Logic" is instantly ambiguous. aoi_name= on the
    # content functions is the fix.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_AOI_ONE")
        db.new_aoi("PDB_AOI_TWO")
        db.new_routine("Logic", "RLL", aoi_name="PDB_AOI_ONE")
        db.new_routine("Logic", "RLL", aoi_name="PDB_AOI_TWO")

        # Ambiguous without a scope.
        with pytest.raises(ValueError, match="ambiguous"):
            db.insert_rung("Logic", 0, "NOP();")

        db.insert_rung("Logic", 0, "MOV(1,Out1);", aoi_name="PDB_AOI_ONE")
        db.insert_rung("Logic", 0, "MOV(2,Out2);", aoi_name="PDB_AOI_TWO")

        one = db.get_routine("Logic", aoi_name="PDB_AOI_ONE")
        two = db.get_routine("Logic", aoi_name="PDB_AOI_TWO")
        assert one["rungs"] == ["MOV(1,Out1);"]
        assert two["rungs"] == ["MOV(2,Out2);"]
    finally:
        db.close()


def test_db_routine_content_functions_accept_aoi_name(acd_copy, tmp_path):
    db_new_aoi(str(acd_copy), "PDB_AOI_ONE")
    db_new_aoi(str(acd_copy), "PDB_AOI_TWO")
    db_new_routine(str(acd_copy), "Logic", "RLL", aoi_name="PDB_AOI_ONE")
    db_new_routine(str(acd_copy), "Logic", "RLL", aoi_name="PDB_AOI_TWO")
    db_insert_rung(str(acd_copy), "Logic", 0, "MOV(1,Out1);", aoi_name="PDB_AOI_ONE")
    db_insert_rung(str(acd_copy), "Logic", 0, "MOV(2,Out2);", aoi_name="PDB_AOI_TWO")

    one = db_get_routine(str(acd_copy), "Logic", aoi_name="PDB_AOI_ONE")
    two = db_get_routine(str(acd_copy), "Logic", aoi_name="PDB_AOI_TWO")
    assert one["rungs"] == ["MOV(1,Out1);"]
    assert two["rungs"] == ["MOV(2,Out2);"]

    # export_routine() has no "Import Routine" mechanism for an AOI-owned
    # routine at all -- only export_aoi() (whole-AOI import) can export it.
    output_path = tmp_path / "aoi_one.L5X"
    with pytest.raises(ValueError, match="export_aoi"):
        db_export_routine(str(acd_copy), "Logic", str(output_path), aoi_name="PDB_AOI_ONE")

    db_export_aoi(str(acd_copy), "PDB_AOI_ONE", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert "MOV(1,Out1)" in content


def test_export_routine_raises_clear_error_for_aoi_owned_routine(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_routine("Logic", "RLL", aoi_name="PDB_NEW_AOI")
        db.insert_rung("Logic", 0, "NOP();", aoi_name="PDB_NEW_AOI")

        with pytest.raises(ValueError, match="export_aoi"):
            db.export_routine("Logic", "build/should_not_be_written.L5X",
                               aoi_name="PDB_NEW_AOI")
    finally:
        db.close()


def test_new_aoi_never_touches_real_pre_existing_aoi(aoi_acd_copy):
    # Regression/design-confirmation test: proj_aois holds ONLY brand-new
    # AOIs, never a real project's own -- confirms to_controller() APPENDS
    # rather than replacing .aois, so a real AOI's own LocalTags (never
    # persisted through proj_aois at all) survive rehydration untouched.
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    real_aoi_names = {a.name for a in reference.controller.aois}
    assert real_aoi_names, "fixture should have at least one real AOI"
    real_local_tag_counts = {a.name: len(a.local_tags) for a in reference.controller.aois}

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")

        project = db.to_controller()
        all_names = {a.name for a in project.controller.aois}
        assert real_aoi_names <= all_names
        assert "PDB_NEW_AOI" in all_names
        for a in project.controller.aois:
            if a.name in real_local_tag_counts:
                assert len(a.local_tags) == real_local_tag_counts[a.name]
    finally:
        db.close()


def test_new_aoi_wins_name_collision_against_real_pre_existing_aoi(aoi_acd_copy):
    # Regression test for a real report: once a db_new_aoi()-authored AOI is
    # actually imported into Studio and the project re-saved, the next
    # rebuild's fresh ControllerBuilder decode picks up that AOI for real,
    # while proj_aois keeps its own separate, still-being-edited row under
    # the same name -- to_controller() used to APPEND the fresh proj_aois
    # object after the real one, so a name-keyed lookup (this class's own
    # export_aoi(), or any next(a for a in aois if a.name == ...)) could
    # silently resolve to the stale real one instead. The fixture's real
    # AOI is named "AddOnInstruction" -- reuse that exact name for a
    # brand-new proj_aois entry to force the collision.
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    real_aoi = next(a for a in reference.controller.aois if a.name == "AddOnInstruction")
    real_param_names = [p.name for p in real_aoi.parameters]

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        db.new_aoi("AddOnInstruction")
        db.new_aoi_parameter("AddOnInstruction", "FreshParam", "DINT", usage="Input")

        project = db.to_controller()
        matches = [a for a in project.controller.aois if a.name == "AddOnInstruction"]
        assert len(matches) == 1, "the real and proj_aois-sourced AOI must not both survive"
        aoi = matches[0]
        assert [p.name for p in aoi.parameters] == ["FreshParam"]
        assert [p.name for p in aoi.parameters] != real_param_names
    finally:
        db.close()


def test_db_export_aoi_uses_fresh_parameters_on_name_collision(aoi_acd_copy, tmp_path):
    db_new_aoi(str(aoi_acd_copy), "AddOnInstruction")
    db_new_aoi_parameter(str(aoi_acd_copy), "AddOnInstruction", "FreshParam", "DINT",
                          usage="Input")

    output_path = tmp_path / "collided_aoi.L5X"
    db_export_aoi(str(aoi_acd_copy), "AddOnInstruction", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert 'Name="FreshParam"' in content


def test_db_new_aoi_stateless_wrappers_and_export(acd_copy, tmp_path):
    db_new_aoi(str(acd_copy), "PDB_NEW_AOI", description="a test AOI")
    db_new_aoi_parameter(str(acd_copy), "PDB_NEW_AOI", "In1", "DINT", usage="Input")
    db_new_aoi_parameter(str(acd_copy), "PDB_NEW_AOI", "Out1", "DINT", usage="Output")
    db_new_routine(str(acd_copy), "Logic", "RLL", aoi_name="PDB_NEW_AOI")
    db_insert_rung(str(acd_copy), "Logic", 0, "MOV(In1,Out1);", aoi_name="PDB_NEW_AOI")

    output_path = tmp_path / "new_aoi.L5X"
    db_export_aoi(str(acd_copy), "PDB_NEW_AOI", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert 'TargetName="PDB_NEW_AOI"' in content
    assert 'Name="In1"' in content
    assert "MOV(In1,Out1)" in content


def test_db_export_routine_resolves_instance_of_not_yet_real_aoi(acd_copy, tmp_path):
    # Regression test for a real report: db_export_routine() failed
    # validation outright for a routine referencing an instance tag typed
    # as a brand-new AOI (created via db_new_aoi() this same session, not
    # yet imported into real Studio) -- the exact repro from the report,
    # through the real db_* surface (not the lower-level acd.api layer),
    # since db_new_tag() correctly wires Tag._data_types_map (via
    # ProjectDB._load_tags()) in a way a bare new_tag() call does not.
    db_new_aoi(str(acd_copy), "Value_To_Str", description="test")
    db_new_aoi_parameter(str(acd_copy), "Value_To_Str", "Value", "DINT", usage="Input",
                          required="true")
    db_new_aoi_parameter(str(acd_copy), "Value_To_Str", "Result", "STRING", usage="InOut")
    db_new_routine(str(acd_copy), "Logic", "ST", aoi_name="Value_To_Str")
    db_insert_st_line(str(acd_copy), "Logic", 0, "Result := 'x';", aoi_name="Value_To_Str")

    db_export_aoi(str(acd_copy), "Value_To_Str", str(tmp_path / "aoi.L5X"))

    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_new_tag(str(acd_copy), "MyInstance", "Value_To_Str", program_name=program_name)
    db_insert_rung(str(acd_copy), routine_name, 0, "XIC(Always_Off)Value_To_Str(MyInstance,1);",
                    program_name=program_name)

    output_path = tmp_path / "routine.L5X"
    db_export_routine(str(acd_copy), routine_name, str(output_path), program_name=program_name,
                       validate=True)
    content = output_path.read_text(encoding="utf-8")
    assert '<AddOnInstructionDefinition Name="Value_To_Str"' in content
    assert 'Tag Name="MyInstance"' in content
    # MyInstance renders NO <Data> element at all -- a real Studio import
    # rejected a guessed instance value ("Data type mismatch") once the AOI
    # had actually been imported for real, because our guess didn't match
    # Studio's own real internal layout for it (see
    # test_tag_to_xml_omits_data_for_synthetic_aoi_instance_type in
    # test_api.py). "Result" (InOut) still correctly appears in the AOI's
    # own <Parameters> definition.
    tag_start = content.index('Tag Name="MyInstance"')
    tag_end = content.index('</Tag>', tag_start)
    assert '<Data' not in content[tag_start:tag_end]
    assert 'Name="Result"' in content


def test_new_aoi_parameter_required_visible_external_access_overrides(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_aoi_parameter(
            "PDB_NEW_AOI", "EnableIn", "BOOL", usage="Input",
            required="false", visible="false", external_access="Read Only",
        )

        project = db.to_controller()
        aoi = next(a for a in project.controller.aois if a.name == "PDB_NEW_AOI")
        p = aoi.parameters[0]
        assert p.required == "false"
        assert p.visible == "false"
        assert p.external_access == "Read Only"
    finally:
        db.close()


def test_new_aoi_local_tag_creates_and_persists(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_aoi_local_tag("PDB_NEW_AOI", "Scratch1", "DINT", description="scratch value")

        project = db.to_controller()
        aoi = next(a for a in project.controller.aois if a.name == "PDB_NEW_AOI")
        assert [lt.name for lt in aoi.local_tags] == ["Scratch1"]
        assert aoi.local_tags[0].data_type == "DINT"
        assert aoi.local_tags[0]._description == "scratch value"
    finally:
        db.close()


def test_new_aoi_local_tag_missing_aoi_raises_key_error(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        with pytest.raises(KeyError):
            db.new_aoi_local_tag("NO_SUCH_AOI", "Scratch1", "DINT")
    finally:
        db.close()


def test_new_aoi_local_tag_duplicate_name_raises(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_aoi_local_tag("PDB_NEW_AOI", "Scratch1", "DINT")
        with pytest.raises(sqlite3.IntegrityError):
            db.new_aoi_local_tag("PDB_NEW_AOI", "Scratch1", "DINT")
    finally:
        db.close()


def test_new_aoi_never_touches_real_pre_existing_aoi_local_tags(aoi_acd_copy):
    # Companion to test_new_aoi_never_touches_real_pre_existing_aoi above --
    # confirms this holds even after a brand-new AOI ALSO adds its own
    # LocalTags via proj_aoi_local_tags, not just proj_aois itself.
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    real_local_tag_counts = {a.name: len(a.local_tags) for a in reference.controller.aois}

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        db.new_aoi("PDB_NEW_AOI")
        db.new_aoi_local_tag("PDB_NEW_AOI", "Scratch1", "DINT")

        project = db.to_controller()
        for a in project.controller.aois:
            if a.name in real_local_tag_counts:
                assert len(a.local_tags) == real_local_tag_counts[a.name]
    finally:
        db.close()


def test_db_new_aoi_local_tag_stateless_wrapper_and_export(acd_copy, tmp_path):
    db_new_aoi(str(acd_copy), "PDB_NEW_AOI")
    db_new_aoi_local_tag(str(acd_copy), "PDB_NEW_AOI", "Scratch1", "DINT",
                          description="scratch value")

    output_path = tmp_path / "new_aoi.L5X"
    db_export_aoi(str(acd_copy), "PDB_NEW_AOI", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert 'TargetName="PDB_NEW_AOI"' in content
    assert '<LocalTags' in content
    assert 'Name="Scratch1"' in content


def test_export_program_stateless_wrapper_includes_every_routine(acd_copy, tmp_path):
    output_path = tmp_path / "branching.L5X"
    db_export_program(str(acd_copy), "Branching", str(output_path))
    content = output_path.read_text(encoding="utf-8")
    assert 'TargetType="Program"' in content
    assert '<Program Use="Target" Name="Branching"' in content
    assert '<Routine Name="B001_Main" Type="RLL">' in content
    assert '<Routine Name="B002_Timers" Type="RLL">' in content


def test_export_program_raises_on_missing_program(acd_copy, tmp_path):
    with pytest.raises(KeyError, match="No program named"):
        db_export_program(str(acd_copy), "NoSuchProgram", str(tmp_path / "bad.L5X"))


def test_export_program_instance_method(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        output_path = tmp_path / "branching.L5X"
        db.export_program("Branching", str(output_path))
        content = output_path.read_text(encoding="utf-8")
        assert '<Program Use="Target" Name="Branching"' in content
    finally:
        db.close()


def test_db_export_program_validate_rejects_out_of_bounds_array_index(acd_copy, tmp_path):
    db_new_tag(str(acd_copy), "PDB_ARR_TAG", "DINT", dimensions="3", program_name="Branching")
    db_insert_rung(str(acd_copy), "B001_Main", 0, "MOV(PDB_ARR_TAG[3],1);",
                    program_name="Branching")

    with pytest.raises(ValueError, match=r"'PDB_ARR_TAG'.*indexed at 3.*0\.\.2"):
        db_export_program(str(acd_copy), "Branching", str(tmp_path / "bad.L5X"))


def test_db_export_program_validate_accepts_in_bounds_array_index(acd_copy, tmp_path):
    db_new_tag(str(acd_copy), "PDB_ARR_TAG2", "DINT", dimensions="3", program_name="Branching")
    db_insert_rung(str(acd_copy), "B001_Main", 0, "MOV(PDB_ARR_TAG2[2],1);",
                    program_name="Branching")

    output_path = tmp_path / "ok.L5X"
    db_export_program(str(acd_copy), "Branching", str(output_path))
    assert output_path.exists()


def test_get_routine_does_not_rehydrate_full_controller(acd_copy, monkeypatch):
    # Regression test for a real report: looping db_get_routine() (or even
    # ProjectDB.get_routine() on a single reused connection) over every
    # routine in a ~180-routine project took multiple minutes of real CPU
    # time, because this method used to call self.to_controller() -- a
    # FULL project rehydration (every tag's initial value decoded, every
    # UDT/Module/AOI/Task rebuilt) -- just to answer "what's in this one
    # routine." Fixed to read only proj_routines/proj_rungs/proj_st_lines
    # directly via SQL. Spy on ControllerBuilder.build (the expensive part
    # of to_controller()) to confirm it's never invoked by get_routine().
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        program = project.controller.programs[0]
        routine = next(r for r in program.routines if r.type == "RLL")

        calls = []
        original = project_db_module.ControllerBuilder.build

        def _spy(self):
            calls.append(1)
            return original(self)

        monkeypatch.setattr(project_db_module.ControllerBuilder, "build", _spy)

        result = db.get_routine(routine.name, program_name=program.name)

        assert calls == []
        assert result["rungs"] == list(routine.rungs)
        assert result["type"] == "RLL"
    finally:
        db.close()


def test_list_routines_still_includes_real_aoi_routines(aoi_acd_copy):
    # Deliberately NOT converted to the SQL-only fast path get_routine() now
    # uses -- proj_routines never contains a real, pre-existing AOI's own
    # routines (_materialize() only walks ctrl.programs), so a SQL-only
    # list_routines() would silently omit them. Confirms the (still
    # to_controller()-based) implementation continues to see them.
    reference = load_acd(str(aoi_acd_copy), verbose=False)
    expected_aoi_routines = {
        (f"AOI:{a.name}", r.name) for a in reference.controller.aois for r in a.routines
    }
    assert expected_aoi_routines, "fixture should have at least one real AOI routine"

    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        listed = {(r["program"], r["routine"]) for r in db.list_routines()}
        assert expected_aoi_routines <= listed
    finally:
        db.close()


def test_list_routines_and_get_routine_line_counts_agree(acd_copy):
    # list_routines()'s own line_count and get_routine()'s own len(rungs)/
    # len(st_lines) are now sourced by two independent SQL paths -- confirm
    # they still agree for every routine, not just that each one runs.
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        for entry in db.list_routines():
            routine = db.get_routine(entry["routine"], program_name=entry["program"])
            actual = len(routine["rungs"]) if entry["type"] == "RLL" else len(routine["st_lines"])
            assert actual == entry["line_count"], entry
    finally:
        db.close()


def test_get_routine_accepts_aoi_prefixed_program_name_from_list_routines(aoi_acd_copy):
    # list_routines()'s own "program" field for an AOI-owned routine is
    # "AOI:<name>" (matching acd.api._all_routines()'s own keying) --
    # get_routine() must accept that shorthand directly (not just the
    # separate aoi_name= parameter), so a caller can feed a list_routines()
    # entry straight into get_routine() without special-casing AOI entries.
    db = open_project_db(str(aoi_acd_copy), verbose=False)
    try:
        entries = [e for e in db.list_routines() if e["program"].startswith("AOI:")]
        assert entries, "fixture should have at least one AOI-owned routine"
        entry = entries[0]

        via_prefix = db.get_routine(entry["routine"], program_name=entry["program"])
        via_aoi_name = db.get_routine(entry["routine"], aoi_name=entry["program"][len("AOI:"):])
        assert via_prefix == via_aoi_name
        assert len(via_prefix["rungs"]) + len(via_prefix["st_lines"]) == entry["line_count"]
    finally:
        db.close()


def _first_routine(db):
    listed = db.list_routines()
    rll = next(r for r in listed if r["type"] == "RLL" and r["line_count"] > 0)
    return rll["program"], rll["routine"]


def _first_routine_via_path(acd_path):
    """Same as _first_routine(), but opens+closes its own ProjectDB --
    NEVER pass a throwaway open_project_db(...) straight into
    _first_routine() without closing it: the lock file it holds would
    otherwise leak for the rest of the test, hanging any later db_*/
    open_project_db() call against the same acd_path.
    """
    db = open_project_db(str(acd_path))
    try:
        return _first_routine(db)
    finally:
        db.close()


def _first_st_routine(db):
    listed = db.list_routines()
    st = next(r for r in listed if r["type"] == "ST" and r["line_count"] > 0)
    return st["program"], st["routine"]


def test_insert_rung_shifts_later_rungs(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        project_before = db.to_controller()
        routine_before = get_routine(project_before, routine_name, program_name)
        original_first_text = routine_before.rungs[0]

        db.insert_rung(routine_name, 0, "NOP();", comment="inserted", program_name=program_name)

        project_after = db.to_controller()
        routine_after = get_routine(project_after, routine_name, program_name)
        assert routine_after.rungs[0] == "NOP();"
        assert routine_after._rung_comments[0] == "inserted"
        assert routine_after.rungs[1] == original_first_text
        assert len(routine_after.rungs) == len(routine_before.rungs) + 1
    finally:
        db.close()


def test_insert_rung_rejects_malformed_rll_syntax(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        project_before = db.to_controller()
        routine_before = get_routine(project_before, routine_name, program_name)

        with pytest.raises(ValueError, match="only 1 member"):
            db.insert_rung(routine_name, 0, "[MOVE(A,B) FOR(C,D,E) ];",
                            program_name=program_name)

        project_after = db.to_controller()
        routine_after = get_routine(project_after, routine_name, program_name)
        assert routine_after.rungs == routine_before.rungs
    finally:
        db.close()


def test_delete_rung_shifts_later_rungs_down(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        project_before = db.to_controller()
        routine_before = get_routine(project_before, routine_name, program_name)
        assert len(routine_before.rungs) >= 2
        second_text = routine_before.rungs[1]

        db.delete_rung(routine_name, 0, program_name=program_name)

        project_after = db.to_controller()
        routine_after = get_routine(project_after, routine_name, program_name)
        assert routine_after.rungs[0] == second_text
        assert len(routine_after.rungs) == len(routine_before.rungs) - 1
    finally:
        db.close()


def test_replace_rung_safe_raises_on_mismatch(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(ValueError):
            db.replace_rung_safe(routine_name, 0, "not the real text", "NOP();",
                                  program_name=program_name)
    finally:
        db.close()


def test_replace_rung_safe_applies_matching_edit(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        project = db.to_controller()
        routine = get_routine(project, routine_name, program_name)
        original_text = routine.rungs[0]

        db.replace_rung_safe(routine_name, 0, original_text, "NOP();", program_name=program_name)

        project2 = db.to_controller()
        routine2 = get_routine(project2, routine_name, program_name)
        assert routine2.rungs[0] == "NOP();"
    finally:
        db.close()


def test_replace_rung_safe_rejects_malformed_rll_syntax(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        project = db.to_controller()
        routine = get_routine(project, routine_name, program_name)
        original_text = routine.rungs[0]

        with pytest.raises(ValueError, match="only 1 member"):
            db.replace_rung_safe(routine_name, 0, original_text,
                                  "[MOVE(A,B) FOR(C,D,E) ];", program_name=program_name)

        project2 = db.to_controller()
        routine2 = get_routine(project2, routine_name, program_name)
        assert routine2.rungs[0] == original_text
    finally:
        db.close()


def test_replace_rung_safe_mismatch_takes_priority_over_syntax_error(acd_copy):
    """A mismatch should always be reported as a mismatch -- never masked
    by new_text also happening to be syntactically malformed."""
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(ValueError, match="changed since last read"):
            db.replace_rung_safe(routine_name, 0, "not the real text",
                                  "[MOVE(A,B) FOR(C,D,E) ];", program_name=program_name)
    finally:
        db.close()


def test_insert_rung_raises_on_st_routine(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        st_lines_before = db.get_routine(routine_name, program_name)["st_lines"]

        with pytest.raises(ValueError, match="only applies to an RLL routine"):
            db.insert_rung(routine_name, 0, "NOP();", program_name=program_name)

        # The real bug this guards against: the call used to silently
        # write into "rungs" (never read by export_routine() for an ST
        # routine) while "st_lines" -- the routine's real content -- was
        # left untouched, looking like a successful, committed edit.
        routine = db.get_routine(routine_name, program_name)
        assert routine["rungs"] == []
        assert routine["st_lines"] == st_lines_before
    finally:
        db.close()


def test_delete_rung_raises_on_st_routine(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        with pytest.raises(ValueError, match="only applies to an RLL routine"):
            db.delete_rung(routine_name, 0, program_name=program_name)
    finally:
        db.close()


def test_replace_rung_safe_raises_on_st_routine(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        with pytest.raises(ValueError, match="only applies to an RLL routine"):
            db.replace_rung_safe(routine_name, 0, "whatever", "NOP();",
                                  program_name=program_name)
    finally:
        db.close()


def test_insert_st_line_shifts_later_lines(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        lines_before = db.get_routine(routine_name, program_name)["st_lines"]
        original_first = lines_before[0]

        db.insert_st_line(routine_name, 0, "NEW_LINE := 1;", program_name=program_name)

        lines_after = db.get_routine(routine_name, program_name)["st_lines"]
        assert lines_after[0] == "NEW_LINE := 1;"
        assert lines_after[1] == original_first
        assert len(lines_after) == len(lines_before) + 1
    finally:
        db.close()


def test_delete_st_line_shifts_later_lines_down(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        lines_before = db.get_routine(routine_name, program_name)["st_lines"]
        assert len(lines_before) >= 2
        second_line = lines_before[1]

        db.delete_st_line(routine_name, 0, program_name=program_name)

        lines_after = db.get_routine(routine_name, program_name)["st_lines"]
        assert lines_after[0] == second_line
        assert len(lines_after) == len(lines_before) - 1
    finally:
        db.close()


def test_replace_st_line_safe_applies_matching_edit(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        original_text = db.get_routine(routine_name, program_name)["st_lines"][0]

        db.replace_st_line_safe(routine_name, 0, original_text, "NEW_LINE := 2;",
                                 program_name=program_name)

        assert db.get_routine(routine_name, program_name)["st_lines"][0] == "NEW_LINE := 2;"
    finally:
        db.close()


def test_replace_st_line_safe_raises_on_mismatch(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_st_routine(db)
        with pytest.raises(ValueError, match="changed since last read"):
            db.replace_st_line_safe(routine_name, 0, "not the real text",
                                     "NEW_LINE := 2;", program_name=program_name)
    finally:
        db.close()


def test_insert_st_line_raises_on_rll_routine(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(ValueError, match="only applies to an ST routine"):
            db.insert_st_line(routine_name, 0, "NEW_LINE := 1;", program_name=program_name)
    finally:
        db.close()


def test_delete_st_line_raises_on_rll_routine(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(ValueError, match="only applies to an ST routine"):
            db.delete_st_line(routine_name, 0, program_name=program_name)
    finally:
        db.close()


def test_replace_st_line_safe_raises_on_rll_routine(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(ValueError, match="only applies to an ST routine"):
            db.replace_st_line_safe(routine_name, 0, "whatever", "NEW_LINE := 1;",
                                     program_name=program_name)
    finally:
        db.close()


def test_db_insert_st_line_stateless_wrapper(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    program_name, routine_name = _first_st_routine(db)
    db.close()

    db_insert_st_line(str(st_acd_copy), routine_name, 0, "NEW_LINE := 1;",
                       program_name=program_name)

    routine = db_get_routine(str(st_acd_copy), routine_name, program_name=program_name)
    assert routine["st_lines"][0] == "NEW_LINE := 1;"


def test_db_insert_rung_raises_on_st_routine(st_acd_copy):
    db = open_project_db(str(st_acd_copy), verbose=False)
    program_name, routine_name = _first_st_routine(db)
    db.close()

    with pytest.raises(ValueError, match="only applies to an RLL routine"):
        db_insert_rung(str(st_acd_copy), routine_name, 0, "NOP();", program_name=program_name)


def test_set_rung_comment_changes_comment_without_touching_text(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        db.insert_rung(routine_name, 0, "NOP();", comment="original",
                        program_name=program_name)

        db.set_rung_comment(routine_name, 0, "renamed", program_name=program_name)

        routine = get_routine(db.to_controller(), routine_name, program_name)
        assert routine.rungs[0] == "NOP();"
        assert routine._rung_comments[0] == "renamed"
    finally:
        db.close()


def test_set_rung_comment_none_clears_it(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        db.insert_rung(routine_name, 0, "NOP();", comment="original",
                        program_name=program_name)

        db.set_rung_comment(routine_name, 0, None, program_name=program_name)

        routine = get_routine(db.to_controller(), routine_name, program_name)
        assert 0 not in routine._rung_comments
    finally:
        db.close()


def test_set_rung_comment_raises_on_missing_rung(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        with pytest.raises(KeyError):
            db.set_rung_comment(routine_name, 999999, "x", program_name=program_name)
    finally:
        db.close()


def test_db_set_rung_comment_stateless_wrapper(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_insert_rung(str(acd_copy), routine_name, 0, "NOP();", comment="original",
                    program_name=program_name)

    db_set_rung_comment(str(acd_copy), routine_name, 0, "renamed", program_name=program_name)

    result = db_get_routine(str(acd_copy), routine_name, program_name)
    assert result["rung_comments"][0] == "renamed"


def test_db_get_routine_rung_comments_keyed_by_int_not_str(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_insert_rung(str(acd_copy), routine_name, 0, "NOP();", comment="hi",
                    program_name=program_name)

    result = db_get_routine(str(acd_copy), routine_name, program_name)

    assert result["rung_comments"].get(0) == "hi"
    assert result["rung_comments"].get("0") is None
    assert all(isinstance(k, int) for k in result["rung_comments"])


def test_export_routine_includes_db_created_tag(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        program_name, routine_name = _first_routine(db)
        db.new_tag("PDB_EXPORT_TEST_TAG", "DINT", program_name=program_name, value=5)
        db.insert_rung(
            routine_name, 0,
            "XIC(Always_Off)MOV(5,PDB_EXPORT_TEST_TAG);",
            program_name=program_name,
        )

        output_path = tmp_path / "exported_routine.L5X"
        db.export_routine(routine_name, str(output_path), program_name=program_name)

        content = output_path.read_text(encoding="utf-8")
        assert 'Name="PDB_EXPORT_TEST_TAG"' in content
        assert "PDB_EXPORT_TEST_TAG" in content
    finally:
        db.close()


def test_export_datatype_includes_db_created_member(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        project = db.to_controller()
        dt_name = project.controller.data_types[0].name
        db.new_member(dt_name, "PDB_EXPORT_MEMBER", "DINT")

        output_path = tmp_path / "exported_datatype.L5X"
        db.export_datatype(dt_name, str(output_path))

        content = output_path.read_text(encoding="utf-8")
        assert 'Name="PDB_EXPORT_MEMBER"' in content
    finally:
        db.close()


# ---- db_* stateless functions ----

def test_db_new_tag_leaves_no_lock_file_behind(acd_copy):
    db_new_tag(str(acd_copy), "PDB_FLAT_TAG", "DINT", description="via flat api", value=3)
    assert db_tag_exists(str(acd_copy), "PDB_FLAT_TAG") is True
    lock_path = acd_copy.parent / acd_copy.stem / project_db_module._LOCK_FILENAME
    assert not lock_path.exists()


def test_db_new_member_persists(acd_copy):
    summary = db_get_project_summary(str(acd_copy))
    dt_name = summary["data_types"][0]

    db_new_member(str(acd_copy), dt_name, "PDB_FLAT_MEMBER", "DINT")

    db = open_project_db(str(acd_copy))
    try:
        project = db.to_controller()
        dt = next(d for d in project.controller.data_types if d.name == dt_name)
        assert any(m.name == "PDB_FLAT_MEMBER" for m in dt.members)
    finally:
        db.close()


def test_db_insert_rung_and_db_export_routine(acd_copy, tmp_path):
    db = open_project_db(str(acd_copy), verbose=False)
    program_name, routine_name = _first_routine(db)
    db.close()

    db_insert_rung(str(acd_copy), routine_name, 0, "NOP();", program_name=program_name)

    output_path = tmp_path / "flat_export.L5X"
    db_export_routine(str(acd_copy), routine_name, str(output_path), program_name=program_name)
    content = output_path.read_text(encoding="utf-8")
    assert "NOP()" in content


def test_db_insert_rung_rejects_malformed_rll_syntax(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)

    with pytest.raises(ValueError, match="only 1 member"):
        db_insert_rung(str(acd_copy), routine_name, 0, "[MOVE(A,B) FOR(C,D,E) ];",
                        program_name=program_name)

    routine = db_get_routine(str(acd_copy), routine_name, program_name=program_name)
    assert "[MOVE(A,B) FOR(C,D,E) ];" not in routine["rungs"]


def test_db_get_routine_returns_current_rungs_and_comments(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_insert_rung(str(acd_copy), routine_name, 0, "NOP();", comment="hi",
                    program_name=program_name)

    routine = db_get_routine(str(acd_copy), routine_name, program_name=program_name)
    assert routine["name"] == routine_name
    assert routine["type"] == "RLL"
    assert routine["rungs"][0] == "NOP();"
    assert routine["rung_comments"][0] == "hi"


def test_db_get_tag_value_returns_scalar_and_paginates_array(acd_copy):
    db_new_tag(str(acd_copy), "PDB_SCALAR_VALUE_TAG", "DINT", value=17)
    scalar = db_get_tag_value(str(acd_copy), "PDB_SCALAR_VALUE_TAG")
    assert scalar["value"] == 17

    db_new_tag(str(acd_copy), "PDB_ARRAY_VALUE_TAG", "DINT", dimensions="5",
               value=[0, 1, 2, 3, 4])
    paged = db_get_tag_value(str(acd_copy), "PDB_ARRAY_VALUE_TAG", offset=1, limit=2)
    assert paged["value"] == [1, 2]
    assert paged["total_elements"] == 5


def test_db_find_tag_references_locates_usage(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_new_tag(str(acd_copy), "PDB_REFERENCED_TAG", "DINT", program_name=program_name)
    db_insert_rung(str(acd_copy), routine_name, 0, "XIC(Always_Off)OTE(PDB_REFERENCED_TAG);",
                    program_name=program_name)

    matches = db_find_tag_references(str(acd_copy), "PDB_REFERENCED_TAG")
    assert any(routine_name in m for m in matches)


def test_db_find_tag_references_include_text_false_returns_3_tuples(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_new_tag(str(acd_copy), "PDB_REFERENCED_TAG2", "DINT", program_name=program_name)
    db_insert_rung(str(acd_copy), routine_name, 0, "XIC(Always_Off)OTE(PDB_REFERENCED_TAG2);",
                    program_name=program_name)

    with_text = db_find_tag_references(str(acd_copy), "PDB_REFERENCED_TAG2")
    without_text = db_find_tag_references(str(acd_copy), "PDB_REFERENCED_TAG2", include_text=False)
    assert all(len(m) == 4 for m in with_text)
    assert all(len(m) == 3 for m in without_text)
    assert {m[:3] for m in with_text} == set(without_text)


def test_db_io_addresses_by_routine_returns_dict(acd_copy):
    result = db_io_addresses_by_routine(str(acd_copy))
    assert isinstance(result, dict)


def test_db_diff_project_same_project_reports_no_routine_or_tag_changes(acd_copy):
    diff = db_diff_project(str(acd_copy), str(acd_copy))
    assert diff.get("routines", {}) == {}
    assert diff.get("tags", {}) == {}


def test_db_diff_project_reports_a_new_tag(acd_copy, tmp_path):
    other_copy = tmp_path / "CuteLogix_other.ACD"
    shutil.copy(str(acd_copy), other_copy)
    db_new_tag(str(other_copy), "PDB_DIFF_ONLY_TAG", "DINT")

    diff = db_diff_project(str(acd_copy), str(other_copy))
    assert ("", "PDB_DIFF_ONLY_TAG") in diff.get("tags", {})


def test_db_diff_routine_reports_a_change(acd_copy, tmp_path):
    other_copy = tmp_path / "CuteLogix_other2.ACD"
    shutil.copy(str(acd_copy), other_copy)
    program_name, routine_name = _first_routine_via_path(acd_copy)
    db_insert_rung(str(other_copy), routine_name, 0, "NOP();", program_name=program_name)

    diff = db_diff_routine(str(acd_copy), routine_name, str(other_copy), routine_name,
                            program_name_a=program_name, program_name_b=program_name)
    assert diff["status"] == "changed"


def test_db_diff_io_addresses_same_project_reports_nothing(acd_copy):
    diff = db_diff_io_addresses(str(acd_copy), str(acd_copy))
    assert diff == {}


# ---- _ProjectLock ----

def test_project_lock_blocks_a_second_acquirer(tmp_path, monkeypatch):
    monkeypatch.setattr(project_db_module, "_LOCK_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(project_db_module, "_LOCK_POLL_SECONDS", 0.02)

    lock_a = _ProjectLock(tmp_path)
    lock_b = _ProjectLock(tmp_path)
    lock_a.acquire()
    try:
        with pytest.raises(TimeoutError):
            lock_b.acquire()
    finally:
        lock_a.release()


def test_project_lock_waiter_succeeds_once_released(tmp_path, monkeypatch):
    monkeypatch.setattr(project_db_module, "_LOCK_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(project_db_module, "_LOCK_POLL_SECONDS", 0.02)

    lock_a = _ProjectLock(tmp_path)
    lock_b = _ProjectLock(tmp_path)
    lock_a.acquire()

    result = {}

    def waiter():
        lock_b.acquire()
        result["acquired"] = True
        lock_b.release()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.2)
    lock_a.release()
    t.join(timeout=3)

    assert result.get("acquired") is True


def test_project_lock_steals_a_stale_lock(tmp_path):
    lock_path = tmp_path / project_db_module._LOCK_FILENAME
    lock_path.write_text("99999")
    old_time = time.time() - (project_db_module._LOCK_STALE_SECONDS + 5)
    os.utime(lock_path, (old_time, old_time))

    lock = _ProjectLock(tmp_path)
    start = time.monotonic()
    lock.acquire()
    elapsed = time.monotonic() - start
    try:
        assert elapsed < 2  # stolen quickly, not waited out for the full timeout
    finally:
        lock.release()


# ---- transactions ----

def test_transaction_commits_all_edits_together(acd_copy):
    with db_transaction(str(acd_copy)) as db:
        db.new_tag("TXN_TAG_A", "DINT")
        db.new_tag("TXN_TAG_B", "DINT")

    check = open_project_db(str(acd_copy))
    try:
        assert check.tag_exists("TXN_TAG_A") is True
        assert check.tag_exists("TXN_TAG_B") is True
    finally:
        check.close()


def test_transaction_rolls_back_all_edits_on_exception(acd_copy):
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with db_transaction(str(acd_copy)) as db:
            db.new_tag("TXN_ROLLBACK_TAG", "DINT")
            raise _Boom("simulated failure partway through")

    check = open_project_db(str(acd_copy))
    try:
        assert check.tag_exists("TXN_ROLLBACK_TAG") is False
    finally:
        check.close()


def test_transaction_partial_multi_step_edit_rolls_back_completely(acd_copy):
    """The exact scenario reported: add a UDT member, create tags, edit a
    rung, then fail partway through -- nothing from the whole attempt
    should be durably visible afterward.
    """
    program_name, routine_name = _first_routine_via_path(acd_copy)
    summary_before = db_get_project_summary(str(acd_copy))
    dt_name = summary_before["data_types"][0]

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with db_transaction(str(acd_copy)) as db:
            db.new_member(dt_name, "TXN_PARTIAL_MEMBER", "DINT")
            db.new_tag("TXN_PARTIAL_TAG_1", "DINT")
            db.new_tag("TXN_PARTIAL_TAG_2", "DINT")
            db.insert_rung(routine_name, 0, "NOP();", program_name=program_name)
            raise _Boom("simulated failure on step 5")

    check = open_project_db(str(acd_copy))
    try:
        assert check.tag_exists("TXN_PARTIAL_TAG_1") is False
        assert check.tag_exists("TXN_PARTIAL_TAG_2") is False
        project = check.to_controller()
        dt = next(d for d in project.controller.data_types if d.name == dt_name)
        assert not any(m.name == "TXN_PARTIAL_MEMBER" for m in dt.members)
        routine = get_routine(project, routine_name, program_name)
        assert routine.rungs[0] != "NOP();"
    finally:
        check.close()


def test_transaction_cannot_be_nested(acd_copy):
    with db_transaction(str(acd_copy)) as db:
        with pytest.raises(RuntimeError):
            with db.transaction():
                pass


def test_transaction_sees_its_own_uncommitted_writes(acd_copy):
    with db_transaction(str(acd_copy)) as db:
        db.new_tag("TXN_VISIBLE_MID_BLOCK", "DINT")
        assert db.tag_exists("TXN_VISIBLE_MID_BLOCK") is True


# ---- delete_tag / delete_routine / delete_member ----

def test_delete_tag_removes_it(acd_copy):
    db_new_tag(str(acd_copy), "DEL_TAG_TEST", "DINT")
    assert db_tag_exists(str(acd_copy), "DEL_TAG_TEST") is True

    db_delete_tag(str(acd_copy), "DEL_TAG_TEST")

    assert db_tag_exists(str(acd_copy), "DEL_TAG_TEST") is False


def test_delete_tag_missing_raises_key_error(acd_copy):
    with pytest.raises(KeyError):
        db_delete_tag(str(acd_copy), "NO_SUCH_TAG_XYZ")


def test_delete_tag_respects_program_scope(acd_copy):
    program_name, _ = _first_routine_via_path(acd_copy)
    db_new_tag(str(acd_copy), "DEL_SCOPED_TAG", "DINT", program_name=program_name)

    with pytest.raises(KeyError):
        db_delete_tag(str(acd_copy), "DEL_SCOPED_TAG")  # wrong scope (controller)

    db_delete_tag(str(acd_copy), "DEL_SCOPED_TAG", program_name=program_name)
    assert db_tag_exists(str(acd_copy), "DEL_SCOPED_TAG", program_name=program_name) is False


def test_delete_routine_removes_it_and_its_rungs(acd_copy):
    program_name, routine_name = _first_routine_via_path(acd_copy)

    db_delete_routine(str(acd_copy), routine_name, program_name=program_name)

    listed = [
        r for r in db_list_routines(str(acd_copy), program_name=program_name)
        if r["routine"] == routine_name
    ]
    assert listed == []
    with pytest.raises(KeyError):
        db_get_routine(str(acd_copy), routine_name, program_name=program_name)


def test_delete_member_removes_it(acd_copy):
    summary = db_get_project_summary(str(acd_copy))
    dt_name = summary["data_types"][0]
    db_new_member(str(acd_copy), dt_name, "DEL_MEMBER_TEST", "DINT")

    db_delete_member(str(acd_copy), dt_name, "DEL_MEMBER_TEST")

    project = open_project_db(str(acd_copy))
    try:
        dt = next(d for d in project.to_controller().controller.data_types if d.name == dt_name)
        assert not any(m.name == "DEL_MEMBER_TEST" for m in dt.members)
    finally:
        project.close()


def test_delete_member_missing_raises_key_error(acd_copy):
    summary = db_get_project_summary(str(acd_copy))
    dt_name = summary["data_types"][0]
    with pytest.raises(KeyError):
        db_delete_member(str(acd_copy), dt_name, "NO_SUCH_MEMBER_XYZ")


# ---- validate defaults to True on db_export_routine/db_export_datatype ----

def test_db_export_datatype_validate_defaults_true_and_raises_on_bad_member(acd_copy, tmp_path):
    summary = db_get_project_summary(str(acd_copy))
    dt_name = summary["data_types"][0]
    db_new_member(str(acd_copy), dt_name, "BAD_TYPE_MEMBER", "NoSuchTypeXYZ")

    with pytest.raises(ValueError):
        db_export_datatype(str(acd_copy), dt_name, str(tmp_path / "out.L5X"))


def test_db_export_datatype_validate_false_skips_the_check(acd_copy, tmp_path):
    summary = db_get_project_summary(str(acd_copy))
    dt_name = summary["data_types"][0]
    db_new_member(str(acd_copy), dt_name, "BAD_TYPE_MEMBER2", "NoSuchTypeXYZ")

    # Should NOT raise -- validate explicitly disabled.
    db_export_datatype(str(acd_copy), dt_name, str(tmp_path / "out2.L5X"), validate=False)
    assert (tmp_path / "out2.L5X").exists()
