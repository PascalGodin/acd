"""Context references Studio's own exports include that the rung-text tag
scan can't find on its own: GSV/SSV objects (modules, tasks, controller
objects) and the tags a MESSAGE tag's configuration uses. Shapes verified
against real Studio 5000 Export Routine/Program/Add-On Instruction output of
a test project doing one GSV per class plus a MSG (see CLAUDE.md)."""
import os
import sqlite3
from types import SimpleNamespace

import pytest

from acd.api import (
    _referenced_gsv_objects,
    export_aoi,
    export_program,
    export_routine,
    load_acd,
)
from acd.l5x.elements import new_aoi, new_routine
from acd.l5x.elements.builders_controller import _msg_config_tag_names

CUTE = os.path.join("..", "resources", "CuteLogix.ACD")


@pytest.fixture(scope="module")
def cute():
    return load_acd(CUTE, verbose=False)


def _routine(project, program_name, routine_name):
    program = next(p for p in project.controller.programs if p.name == program_name)
    return program, next(r for r in program.routines if r.name == routine_name)


def _tag_names(xml):
    import re
    return re.findall(r'<Tag Name="([^"]+)"', xml)


def test_controller_msg_config_tags_from_xrefs(cute):
    # CuteLogix's WebPage message is configured to use DisableWeb.
    assert cute.controller._msg_config_tags == {("", "WebPage"): ["DisableWeb"]}


def test_export_routine_includes_msg_configured_tag(tmp_path):
    # A routine whose only rung is "XIC(Toggle)MSG(WebPage);" never names
    # DisableWeb, but Studio's export of a routine with a MSG includes the
    # message's configured tag.
    project = load_acd(CUTE, verbose=False)
    program, _ = _routine(project, "Instructions", "MainRoutine")
    routine = new_routine("MsgOnly", "RLL")
    routine.insert_rung(0, "XIC(Toggle)MSG(WebPage);")
    program.routines.append(routine)
    out = tmp_path / "r.L5X"
    export_routine(project, routine, out)
    assert _tag_names(out.read_text(encoding="utf-8")) == ["DisableWeb", "Toggle", "WebPage"]


def test_export_routine_includes_gsv_task_reference(cute, tmp_path):
    # "SSV(Task,MainTask,InhibitTask,Task_State);"
    _, routine = _routine(cute, "Instructions", "R012_Input_Output")
    out = tmp_path / "r.L5X"
    export_routine(cute, routine, out)
    xml = out.read_text(encoding="utf-8")
    assert (
        '</Programs>\n<Tasks Use="Context">\n<Task Use="Reference" Name="MainTask">\n'
        "</Task>\n</Tasks>\n</Controller>" in xml
    )


def test_export_program_includes_gsv_and_msg_references(cute, tmp_path):
    program, _ = _routine(cute, "Instructions", "MainRoutine")
    out = tmp_path / "p.L5X"
    export_program(cute, program, out)
    xml = out.read_text(encoding="utf-8")
    assert '<Task Use="Reference" Name="MainTask">' in xml
    assert xml.index("</Programs>") < xml.index('<Tasks Use="Context">')
    assert "DisableWeb" in _tag_names(xml)


def test_export_aoi_includes_wall_clock_time_reference(tmp_path):
    project = load_acd(CUTE, verbose=False)
    aoi = new_aoi("ClockAOI")
    routine = new_routine("Logic", "RLL")
    routine.insert_rung(0, "GSV(WallClockTime,,CurrentValue,Clock);")
    aoi.routines.append(routine)
    project.controller.aois.append(aoi)
    out = tmp_path / "a.L5X"
    export_aoi(project, aoi, out)
    xml = out.read_text(encoding="utf-8")
    assert (
        '</AddOnInstructionDefinitions>\n<WallClockTime Use="Reference">\n'
        "</WallClockTime>\n</Controller>" in xml
    )


def test_export_aoi_without_gsv_has_no_object_reference(tmp_path):
    project = load_acd(CUTE, verbose=False)
    aoi = new_aoi("PlainAOI")
    project.controller.aois.append(aoi)
    out = tmp_path / "a.L5X"
    export_aoi(project, aoi, out)
    assert 'Use="Reference"' not in out.read_text(encoding="utf-8")


def _gsv_project():
    modules = [SimpleNamespace(name="Generic_Module"), SimpleNamespace(name="Other")]
    tasks = [SimpleNamespace(name="XrefTest_Task")]
    return SimpleNamespace(controller=SimpleNamespace(modules=modules, tasks=tasks))


def test_referenced_gsv_objects_classifies_each_class():
    modules, tasks, objects = _referenced_gsv_objects(
        [
            "GSV(WallClockTime,,CurrentValue,TestTime);",
            "GSV(Module,Generic_Module,EntryStatus,TestModStatus);",
            "GSV(Task,XrefTest_Task,LastScanTime,TestTaskScan);",
            "GSV(Program,XrefTest_Prog,DisableFlag,TestProgFlag);",
            "GSV(ControllerDevice,,ProductRev,TestRev);",
        ],
        _gsv_project(),
    )
    assert [m.name for m in modules] == ["Generic_Module"]
    assert [t.name for t in tasks] == ["XrefTest_Task"]
    # Studio's order, regardless of rung order.
    assert objects == ["ControllerDevice", "WallClockTime"]


def test_referenced_gsv_objects_is_case_insensitive_and_canonicalizes():
    # Real rung text has "WALLCLOCKTIME" too; Studio still writes WallClockTime.
    modules, tasks, objects = _referenced_gsv_objects(
        ["SSV(WALLCLOCKTIME,,ApplyDST,Wrk_DST) gsv(module,generic_module,Mode,X)"],
        _gsv_project(),
    )
    assert objects == ["WallClockTime"]
    assert [m.name for m in modules] == ["Generic_Module"]
    assert tasks == []


def test_referenced_gsv_objects_ignores_unknown_classes_and_instances():
    modules, tasks, objects = _referenced_gsv_objects(
        ["GSV(CST,,CurrentValue,X)", "GSV(Module,NoSuchModule,Mode,X)", "MOV(1,X)"],
        _gsv_project(),
    )
    assert (modules, tasks, objects) == ([], [], [])


def _scope_db(xrefs):
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE comps(object_id int, comp_name text, parent_id int)")
    cur.execute("CREATE TABLE xrefs(from_id int, to_id int, kind int)")
    cur.executemany("INSERT INTO comps VALUES (?,?,?)", [
        (1, "Ctrl", 0),
        (2, "RxTagCollection", 1),
        (3, "RxProgramCollection", 1),
        (4, "Prog", 3),
        (5, "RxTagCollection", 4),
        (10, "CtrlMsg", 2),
        (11, "CtrlData", 2),
        (20, "ProgMsg", 5),
        (21, "ProgData", 5),
        (90, "$cfg1$", 0),
        (91, "$cfg2$", 0),
    ])
    cur.executemany("INSERT INTO xrefs VALUES (?,?,?)", xrefs)
    return cur


def test_msg_config_tag_names_keys_by_scope():
    cur = _scope_db([
        (10, 90, 6), (90, 11, 7),               # controller MSG -> controller tag
        (20, 91, 6), (91, 21, 7), (91, 11, 7),  # program MSG -> program + controller tag
        (10, 11, 9),                            # unrelated kind, ignored
    ])
    result = {k: sorted(v) for k, v in _msg_config_tag_names(cur).items()}
    assert result == {
        ("", "CtrlMsg"): ["CtrlData"],
        ("Prog", "ProgMsg"): ["CtrlData", "ProgData"],
    }


def test_msg_config_tag_names_without_xrefs_table():
    conn = sqlite3.connect(":memory:")
    assert _msg_config_tag_names(conn.cursor()) == {}
