import os
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
    db_export_datatype,
    db_export_routine,
    db_find_tag_references,
    db_get_project_summary,
    db_get_routine,
    db_get_tag_value,
    db_insert_rung,
    db_insert_st_line,
    db_io_addresses_by_routine,
    db_list_routines,
    db_new_member,
    db_new_tag,
    db_set_rung_comment,
    db_tag_exists,
    db_transaction,
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
