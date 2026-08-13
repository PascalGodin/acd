import os
import shutil
import sqlite3
import threading
import time

import pytest

from acd import (
    db_diff_io_addresses,
    db_diff_project,
    db_diff_routine,
    db_export_routine,
    db_find_tag_references,
    db_get_project_summary,
    db_get_routine,
    db_get_tag_value,
    db_insert_rung,
    db_io_addresses_by_routine,
    db_new_member,
    db_new_tag,
    db_tag_exists,
    open_project_db,
)
from acd.api import get_routine, load_acd
from acd.l5x import project_db as project_db_module
from acd.l5x.project_db import _ProjectLock


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


def test_set_tag_comment_element_path(acd_copy):
    db = open_project_db(str(acd_copy), verbose=False)
    try:
        db.new_tag("PDB_COMMENT_TAG", "DINT")
        db.set_tag_comment("PDB_COMMENT_TAG", "[0]", "element zero comment")
        project = db.to_controller()
        tag = next(t for t in project.controller.tags if t.name == "PDB_COMMENT_TAG")
        assert ("[0]", "element zero comment") in tag._comments
    finally:
        db.close()


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
