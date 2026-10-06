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
    _referenced_modules,
    _referenced_tag_names,
    _strip_comments_and_strings,
    _tag_context_dependencies,
    export_aoi,
    export_program,
    export_routine,
    load_acd,
)
from acd.l5x.elements import new_aoi, new_routine
from acd.l5x.elements.builders_controller import (
    _axis_motion_group_tags,
    _io_tag_owner_modules,
    _msg_config_tag_names,
)

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


# --- _io_tag_owner_modules (rack-optimized vs direct-connection ownership) --

def _io_module_db(xrefs, extra_comps=()):
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE comps(object_id int, comp_name text, parent_id int)")
    cur.execute("CREATE TABLE xrefs(from_id int, to_id int, kind int)")
    cur.executemany("INSERT INTO comps VALUES (?,?,?)", [
        (100, "Rack", 0),
        (101, "SlotModule", 0),
        (200, "Rack:0:C", 0),
        (201, "Rack:2:C", 0),
        (202, "Rack:2:I", 0),
        (203, "Direct:I", 0),
        (300, "DirectModule", 0),
        *extra_comps,
    ])
    cur.executemany("INSERT INTO xrefs VALUES (?,?,?)", xrefs)
    return cur


def test_io_tag_owner_modules_rack_optimized_slot_owns_only_config_tag():
    # A rack-optimized rack's slot module owns only its own :C config tag --
    # its :I/:O data tags belong to the rack adapter, not the slot module.
    cur = _io_module_db([
        (201, 101, 3),  # Rack:2:C  -> SlotModule
        (202, 100, 3),  # Rack:2:I  -> Rack (the adapter), NOT SlotModule
    ])
    owners = _io_tag_owner_modules(cur)
    assert owners["Rack:2:C"] == ["SlotModule"]
    assert owners["Rack:2:I"] == ["Rack"]


def test_io_tag_owner_modules_direct_connection_slot_co_owns_data_tag():
    # A direct-connection slot module co-owns its own :I/:O data tag
    # alongside the rack adapter.
    cur = _io_module_db([
        (202, 100, 3),  # Rack:2:I -> Rack
        (202, 101, 3),  # Rack:2:I -> SlotModule (co-owner, direct connection)
    ])
    owners = _io_tag_owner_modules(cur)
    assert sorted(owners["Rack:2:I"]) == ["Rack", "SlotModule"]


def test_io_tag_owner_modules_resolves_hex_encoded_tag_name():
    # An I/O tag's comp_name can itself be a "&hexid:2:O"-style placeholder,
    # the hex id pointing at the module's own real comps object_id.
    cur = _io_module_db(
        [(500, 300, 3)],
        extra_comps=[(500, "&0000012c:2:O", 0)],  # 0x12c == 300
    )
    owners = _io_tag_owner_modules(cur)
    assert owners["DirectModule:2:O"] == ["DirectModule"]


def test_io_tag_owner_modules_without_xrefs_table():
    conn = sqlite3.connect(":memory:")
    assert _io_tag_owner_modules(conn.cursor()) == {}


def test_io_tag_owner_modules_empty_links_returns_empty_dict():
    cur = _io_module_db([])
    assert _io_tag_owner_modules(cur) == {}


# --- _axis_motion_group_tags ------------------------------------------------

def _axis_db(xrefs):
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE comps(object_id int, comp_name text, parent_id int)")
    cur.execute("CREATE TABLE xrefs(from_id int, to_id int, kind int)")
    cur.executemany("INSERT INTO comps VALUES (?,?,?)", [
        (1, "FenceAxis_1", 0),       # AXIS_CIP_DRIVE tag
        (2, "$axisobj$", 0),         # the axis object the tag points at
        (3, "$motiongroupobj$", 0),  # the motion group object containing the axis
        (4, "MotionGroup", 0),       # the MOTION_GROUP tag
    ])
    cur.executemany("INSERT INTO xrefs VALUES (?,?,?)", xrefs)
    return cur


def test_axis_motion_group_tags_follows_the_three_hop_chain():
    cur = _axis_db([
        (1, 2, 5),    # FenceAxis_1 -(AXIS_TAG)-> axis object
        (3, 2, 80),   # motion group object -(MOTION_GROUP_AXIS)-> axis object
        (4, 3, 4),    # MotionGroup -(MOTION_GROUP_TAG)-> motion group object
    ])
    assert _axis_motion_group_tags(cur) == {"FenceAxis_1": ["MotionGroup"]}


def test_axis_motion_group_tags_without_xrefs_table():
    conn = sqlite3.connect(":memory:")
    assert _axis_motion_group_tags(conn.cursor()) == {}


def test_axis_motion_group_tags_empty_links_returns_empty_dict():
    cur = _axis_db([])
    assert _axis_motion_group_tags(cur) == {}


# --- _referenced_modules: XRefs I/O ownership over the slot/rack heuristic -

def _io_project(modules, io_tag_modules):
    return SimpleNamespace(controller=SimpleNamespace(
        modules=modules, _io_tag_modules=io_tag_modules,
    ))


def test_referenced_modules_prefers_xrefs_ownership_over_slot_heuristic():
    # Without XRefs data, the old slot heuristic would include SlotModule for
    # "Rack:2:I" too (same rack, same slot number) -- XRefs ownership (adapter
    # only, for a rack-optimized rack) takes priority when present.
    rack = SimpleNamespace(name="Rack", parent_module=None, _slot=None)
    slot = SimpleNamespace(name="SlotModule", parent_module="Rack", _slot=2)
    project = _io_project([rack, slot], {"Rack:2:I": ["Rack"]})
    found = {m.name for m in _referenced_modules(["XIC(Rack:2:I.Data.0)OTE(X);"], project)}
    assert found == {"Rack"}


def test_referenced_modules_falls_back_to_slot_heuristic_without_xrefs_data():
    rack = SimpleNamespace(name="Rack", parent_module=None, _slot=None)
    slot = SimpleNamespace(name="SlotModule", parent_module="Rack", _slot=2)
    project = _io_project([rack, slot], {})
    found = {m.name for m in _referenced_modules(["XIC(Rack:2:I.Data.0)OTE(X);"], project)}
    assert found == {"Rack", "SlotModule"}


def test_referenced_modules_direct_addressed_device_unaffected_by_xrefs_map():
    vfd = SimpleNamespace(name="VFD1", parent_module=None, _slot=None)
    project = _io_project([vfd], {})
    found = {m.name for m in _referenced_modules(["XIC(VFD1:I.Running)OTE(X);"], project)}
    assert found == {"VFD1"}


def test_referenced_modules_falls_back_when_xrefs_owner_name_does_not_resolve():
    # Defensive: XRefs names an owner, but it isn't among the project's
    # currently-known modules (not observed in a real project) -- fall back
    # to the slot heuristic rather than silently including nothing.
    rack = SimpleNamespace(name="Rack", parent_module=None, _slot=None)
    slot = SimpleNamespace(name="SlotModule", parent_module="Rack", _slot=2)
    project = _io_project([rack, slot], {"Rack:2:I": ["StaleModuleName"]})
    found = {m.name for m in _referenced_modules(["XIC(Rack:2:I.Data.0)OTE(X);"], project)}
    assert found == {"Rack", "SlotModule"}


# --- _strip_comments_and_strings --------------------------------------------

def test_strip_comments_and_strings_line_comment():
    assert _strip_comments_and_strings("x := a; // on a change of b") == "x := a; "


def test_strip_comments_and_strings_block_comments():
    assert _strip_comments_and_strings("x := a (* skip b *) + c;") == "x := a   + c;"
    assert _strip_comments_and_strings("x := a /* skip b */ + c;") == "x := a   + c;"


def test_strip_comments_and_strings_unterminated_block_comment_blanks_to_eol():
    assert _strip_comments_and_strings("x := a (* never closes") == "x := a  "


def test_strip_comments_and_strings_quoted_string_content_is_blanked():
    assert _strip_comments_and_strings("MOV('a string with b',G);") == "MOV('',G);"


def test_strip_comments_and_strings_escaped_quote_inside_string():
    assert _strip_comments_and_strings("MOV('it$'s f',G);") == "MOV('',G);"


# --- _referenced_tag_names: real-project exclusions -------------------------

def test_referenced_tag_names_ignores_tokens_in_st_comments():
    # A real tag literally named "a" was pulled in by a word inside a
    # comment ("...on a change of state...").
    names = _referenced_tag_names(["//Move value on a change of state", "x := y;"])
    assert "a" not in names
    assert names == {"x", "y"}


def test_referenced_tag_names_ignores_jsr_target_as_a_tag():
    # JSR's own first operand is a routine name, not a tag -- even when an
    # unrelated controller tag happens to share that name.
    names = _referenced_tag_names(["JSR(LS_Read,0);"])
    assert "LS_Read" not in names


def test_referenced_tag_names_ignores_io_module_and_type_tokens():
    # "TongLoader_XFR" (the module) and "I" (the I/O type) are not tag names
    # -- "DriveStatus_Faulted" is excluded separately, by the pre-existing
    # dot-member rule (it follows ".").
    names = _referenced_tag_names(["XIC(TongLoader_XFR:I.DriveStatus_Faulted)OTE(Out);"])
    assert "TongLoader_XFR" not in names
    assert "I" not in names
    assert names == {"Out"}


def test_referenced_tag_names_still_matches_st_assignment_colon_equals():
    # ":=" must not be mistaken for the I/O-module-token exclusion.
    names = _referenced_tag_names(["x := y;"])
    assert names == {"x", "y"}


# --- _tag_context_dependencies: axis -> motion group ------------------------

def test_tag_context_dependencies_pulls_in_axis_motion_group():
    tag = SimpleNamespace(tag_type="Base", target=None, data_type="AXIS_CIP_DRIVE",
                          name="FenceAxis_1")
    project = SimpleNamespace(controller=SimpleNamespace(
        _msg_config_tags={}, _axis_motion_groups={"FenceAxis_1": ["MotionGroup"]},
    ))
    assert _tag_context_dependencies(tag, "Continuous", project) == ["MotionGroup"]


def test_tag_context_dependencies_no_axis_group_is_a_noop():
    tag = SimpleNamespace(tag_type="Base", target=None, data_type="DINT", name="Plain")
    project = SimpleNamespace(controller=SimpleNamespace(
        _msg_config_tags={}, _axis_motion_groups={},
    ))
    assert _tag_context_dependencies(tag, "Continuous", project) == []
