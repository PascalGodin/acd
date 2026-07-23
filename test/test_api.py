import os
from pathlib import Path
from xml.dom import minidom

from acd.api import (
    ImportProjectFromFile,
    RSLogix5000Content,
    Extract,
    ExtractAcdDatabase,
    DumpCompsRecordsToFile,
    export_datatype,
    export_routine,
    find_io_addresses,
    io_addresses_by_routine,
    diff_io_addresses,
    diff_project,
    diff_routine,
    load_acd,
)
from acd.l5x.elements import new_member


def test_import_from_file():
    importer = ImportProjectFromFile(
        Path(os.path.join("..", "resources", "CuteLogix.ACD"))
    )
    project: RSLogix5000Content = importer.import_project()
    assert project is not None


def test_extract_database_files():
    extractor: Extract = ExtractAcdDatabase(
        Path(os.path.join("..", "resources", "CuteLogix.ACD")),
        Path(os.path.join("build")),
    )
    extractor.extract()


def test_dump_to_files():
    DumpCompsRecordsToFile(
        os.path.join("..", "resources", "CuteLogix.ACD"), "build"
    ).extract()


def test_to_xml():
    importer = ImportProjectFromFile(
        Path(os.path.join("..", "resources", "CuteLogix.ACD"))
    )
    project: RSLogix5000Content = importer.import_project()
    unformatted_string = project.to_xml()
    xmlstr = minidom.parseString(unformatted_string).toprettyxml(indent="   ")
    with open(os.path.join("build", "CuteLogix.L5X"), "w") as out_file:
        out_file.write(xmlstr)


def test_st_routine_content():
    """ST routine bodies are extracted from the nameless records: source
    lines in order, blank lines preserved, @hexid@ tag references resolved,
    and exported as an L5X STContent element."""
    importer = ImportProjectFromFile(
        Path(os.path.join("..", "resources", "ACDTestsNonRedundant.ACD"))
    )
    project: RSLogix5000Content = importer.import_project()
    st_routines = [
        rt
        for prog in project.controller.programs
        for rt in prog.routines
        if rt.type == "ST"
    ]
    assert st_routines, "fixture should contain an ST routine"
    st = st_routines[0]
    assert st._st_lines, "ST routine should have extracted source lines"
    body = "\n".join(st._st_lines)
    assert ":=" in body
    assert "@" not in body, "tag references should be resolved to names"
    xml = st.to_xml()
    assert "<STContent>" in xml
    assert '<Line Number="0"><![CDATA[' in xml
    # Line numbering must match source positions (blank lines preserved)
    assert f'<Line Number="{len(st._st_lines) - 1}">' in xml


def test_export_routine_st_routine_pulls_in_referenced_tags(tmp_path):
    # Regression test: export_routine()'s dependency scan used to only look
    # at routine.rungs (RLL text), which is empty for an ST routine (its
    # source lives in ._st_lines instead) -- so exporting an ST routine
    # silently produced an empty <Tags Use="Context"> with none of its real
    # tag references included. Fixed by routing every scan through
    # _routine_lines(), which already picks the right list per routine type.
    project = load_acd(os.path.join("..", "resources", "ACDTestsNonRedundant.ACD"))
    program = next(p for p in project.controller.programs if p.name == "MainProgram")
    st_routine = next(r for r in program.routines if r.type == "ST")
    assert st_routine._st_lines, "fixture ST routine should have source lines"

    out_path = tmp_path / "STRoutine_export.L5X"
    export_routine(project, st_routine, str(out_path))

    xml_text = out_path.read_text(encoding="utf-8")
    parsed = minidom.parseString(xml_text)  # raises on malformed XML

    root = parsed.documentElement
    assert root.getAttribute("TargetType") == "Routine"
    assert root.getAttribute("TargetSubType") == "ST"
    assert "<STContent>" in xml_text
    assert "<RLLContent>" not in xml_text

    # The routine's real source references controller-scope tags literally
    # named "DINT"/"UDINT"/"ULINT" (this fixture's own naming convention) --
    # these must show up as full <Tag> context elements, proving the ST
    # source was actually scanned for dependencies, not just rendered.
    tag_names = {t.getAttribute("Name") for t in parsed.getElementsByTagName("Tag")}
    assert {"DINT", "UDINT", "ULINT"} <= tag_names


def test_find_io_addresses():
    """A real I/O address always contains ':' (Rockwell reserves it for
    module addressing) so this must never match a plain UDT member path."""
    assert find_io_addresses(
        "XIC(Sorter_VFD:I.DriveStatus_Active)GT(Sorter_LPM,20)TON(Timer[8],?,?);"
    ) == ["Sorter_VFD:I.DriveStatus_Active"]
    assert find_io_addresses(
        "XIC(Remote_GraderConsole:1:I.Pt14.Data)ONS(BF_Override_ONS);"
    ) == ["Remote_GraderConsole:1:I.Pt14.Data"]
    assert find_io_addresses("MOVE(IO026:I.Data[0],I_26_0);") == ["IO026:I.Data[0]"]
    assert find_io_addresses(
        "XIC(M304_Sorter_Lug_Chain.VFD.Running)GT(Sorter_LPM,20);"
    ) == []
    assert find_io_addresses(
        "XIO(Remote_MCC050:2:O.Pt08.Data)XIO(Remote_MCC050:1:I.Pt08.Data);"
    ) == ["Remote_MCC050:2:O.Pt08.Data", "Remote_MCC050:1:I.Pt08.Data"]
    assert find_io_addresses("") == []
    assert find_io_addresses(None) == []


def test_io_addresses_by_routine_and_diff():
    """io_addresses_by_routine()/diff_io_addresses() must not assume two
    routines' rungs line up by index -- a routine with a different rung
    count between two projects should still diff cleanly by address set,
    not raise IndexError."""
    importer = ImportProjectFromFile(
        Path(os.path.join("..", "resources", "CuteLogix.ACD"))
    )
    project: RSLogix5000Content = importer.import_project()
    by_routine = io_addresses_by_routine(project)
    assert isinstance(by_routine, dict)
    for key in by_routine:
        assert isinstance(key, tuple) and len(key) == 2

    # Identical project vs itself: no I/O address differences anywhere.
    assert diff_io_addresses(project, project) == {}


def test_diff_io_addresses_survives_mismatched_rung_counts():
    """The bug this exists to prevent: a naive "zip rung i of A with rung i
    of B" comparison raises IndexError the moment two routines have a
    different rung count -- diff_io_addresses() must handle that cleanly by
    comparing address sets instead of positions."""
    from types import SimpleNamespace

    def make_project(program_name, routine_name, rungs):
        routine = SimpleNamespace(name=routine_name, rungs=rungs, _st_lines=[])
        program = SimpleNamespace(name=program_name, routines=[routine])
        controller = SimpleNamespace(programs=[program], aois=[])
        return SimpleNamespace(controller=controller)

    project_a = make_project(
        "MainProgram",
        "R01",
        [
            "XIC(Local:10:I.Data.11)OTE(Foo);",
            "XIC(IO024:I.Data[0].13)OTE(Bar);",
        ],
    )
    project_b = make_project(
        "MainProgram",
        "R01",
        [
            "XIC(Local:10:I.Data.11)OTE(Foo);",
            "XIC(Remote_GraderConsole:3:I.Pt13.Data)OTE(Bar);",
            "XIC(Baz)OTE(Qux);",  # extra rung, no I/O address at all
        ],
    )

    diff = diff_io_addresses(project_a, project_b)
    key = ("MainProgram", "R01")
    assert diff[key]["removed"] == ["IO024:I.Data[0].13"]
    assert diff[key]["added"] == ["Remote_GraderConsole:3:I.Pt13.Data"]
    assert diff[key]["common"] == ["Local:10:I.Data.11"]


def test_diff_project_covers_routines_tags_and_names():
    """diff_project() is the generic "what changed" entry point -- it must
    handle a routine with a different rung count between the two projects
    (the same IndexError-prone shape diff_io_addresses() guards against),
    plus tag value/description changes and data-type/module/AOI presence
    changes, all in one call."""
    from types import SimpleNamespace

    def make_project(rungs_a_style):
        tag_foo = SimpleNamespace(
            name="Foo", data_type="DINT", description="original", _initial_value=1
        )
        tag_bar = SimpleNamespace(
            name="Bar", data_type="DINT", description="original", _initial_value=2
        )
        routine = SimpleNamespace(
            name="R01", type="RLL", rungs=rungs_a_style, _st_lines=[]
        )
        program = SimpleNamespace(name="MainProgram", routines=[routine], tags=[tag_bar])
        controller = SimpleNamespace(
            programs=[program],
            aois=[],
            tags=[tag_foo],
            data_types=[SimpleNamespace(name="MY_UDT")],
            modules=[SimpleNamespace(name="Local")],
        )
        return SimpleNamespace(controller=controller)

    project_a = make_project(["XIC(Foo)OTE(Bar);", "XIC(Baz)OTE(Qux);"])
    project_b = make_project(
        ["XIC(Foo)OTE(Bar);", "XIC(Baz)OTE(Qux);", "XIC(Extra)OTE(Rung);"]
    )
    # Change a tag's description/value on the "b" side.
    project_b.controller.tags[0].description = "changed"
    project_b.controller.tags[0]._initial_value = 99
    # Add a data type and remove a module on the "b" side.
    project_b.controller.data_types.append(SimpleNamespace(name="MY_UDT_2"))
    project_b.controller.modules = []

    diff = diff_project(project_a, project_b)

    routine_key = ("MainProgram", "R01")
    assert diff["routines"][routine_key]["status"] == "changed"
    changes = diff["routines"][routine_key]["changes"]
    assert any(c["op"] == "insert" and c["new"] == ["XIC(Extra)OTE(Rung);"] for c in changes)

    tag_key = ("", "Foo")
    assert diff["tags"][tag_key]["status"] == "changed"
    assert diff["tags"][tag_key]["changed"]["description"] == {
        "old": "original",
        "new": "changed",
    }
    assert diff["tags"][tag_key]["changed"]["value"] == {"old": 1, "new": 99}

    assert diff["data_types"] == {"added": ["MY_UDT_2"], "removed": []}
    assert diff["modules"] == {"added": [], "removed": ["Local"]}
    assert "aois" not in diff  # identical (both empty) -- omitted entirely

    # Identical project vs itself: no differences of any kind.
    assert diff_project(project_a, project_a) == {}


def test_diff_project_summarizes_large_tag_values():
    """A UDT array tag's decoded value (a list of per-element dicts) must be
    summarized, not dumped in full -- otherwise a project with many changed
    array tags produces an unreadable, multi-megabyte result (the real
    failure this exists to prevent: 1601 changed tags on one real project
    comparison, many holding full UDT-array initial values)."""
    from types import SimpleNamespace

    def make_project(value):
        tag = SimpleNamespace(
            name="BigArrayTag", data_type="MY_UDT", description="", _initial_value=value
        )
        program = SimpleNamespace(name="MainProgram", routines=[], tags=[])
        controller = SimpleNamespace(
            programs=[program], aois=[], tags=[tag], data_types=[], modules=[]
        )
        return SimpleNamespace(controller=controller)

    old_value = [{"Field1": i, "Field2": i * 2, "Field3": "x" * 20} for i in range(50)]
    new_value = list(old_value)
    new_value[3] = {"Field1": 999, "Field2": 999, "Field3": "changed"}
    new_value[10] = {"Field1": 999, "Field2": 999, "Field3": "changed"}

    diff = diff_project(make_project(old_value), make_project(new_value))
    value_diff = diff["tags"][("", "BigArrayTag")]["changed"]["value"]
    assert "summary" in value_diff
    assert "old" not in value_diff
    assert value_diff["differing_indices"] == [3, 10]

    # A small scalar value must still be reported in full, not summarized.
    scalar_diff = diff_project(make_project(1), make_project(2))
    assert scalar_diff["tags"][("", "BigArrayTag")]["changed"]["value"] == {
        "old": 1,
        "new": 2,
    }


def _make_routine_project(rungs):
    from types import SimpleNamespace

    routine = SimpleNamespace(name="R01", type="RLL", rungs=rungs, _st_lines=[])
    program = SimpleNamespace(name="Main", routines=[routine], tags=[])
    controller = SimpleNamespace(
        programs=[program], aois=[], tags=[], data_types=[], modules=[]
    )
    return SimpleNamespace(controller=controller)


def test_diff_project_routine_insert_does_not_flag_unrelated_rungs():
    """Inserting one rung at the top of a routine must not flag the whole
    routine as different -- the other rungs shifted position by one but are
    otherwise byte-identical, and diff_routines() aligns by content
    (difflib.SequenceMatcher), not by index, so they must still show up as
    unchanged (i.e. absent from "changes")."""
    a = _make_routine_project(
        ["XIC(A)OTE(B);", "XIC(C)OTE(D);", "XIC(E)OTE(F);"]
    )
    b = _make_routine_project(
        ["XIC(NEW)OTE(TOP);", "XIC(A)OTE(B);", "XIC(C)OTE(D);", "XIC(E)OTE(F);"]
    )
    routine_diff = diff_project(a, b)["routines"][("Main", "R01")]
    assert routine_diff["status"] == "changed"
    assert routine_diff["changes"] == [
        {"op": "insert", "old": [], "new": ["XIC(NEW)OTE(TOP);"]}
    ]


def test_diff_project_routine_isolates_a_single_modified_rung():
    """A rung edited in place (e.g. one tag renamed), surrounded by
    unchanged rungs, must isolate to a single "replace" op, not spill over
    into flagging the surrounding unchanged rungs."""
    a = _make_routine_project(
        ["XIC(A)OTE(B);", "XIC(C)OTE(D);", "XIC(E)OTE(F);", "XIC(G)OTE(H);"]
    )
    b = _make_routine_project(
        ["XIC(A)OTE(B);", "XIC(Ccc)OTE(D);", "XIC(E)OTE(F);", "XIC(G)OTE(H);"]
    )
    routine_diff = diff_project(a, b)["routines"][("Main", "R01")]
    assert routine_diff["status"] == "changed"
    assert routine_diff["changes"] == [
        {"op": "replace", "old": ["XIC(C)OTE(D);"], "new": ["XIC(Ccc)OTE(D);"]}
    ]


def test_diff_routine_unchanged():
    """Two Routine objects with identical rungs must report "unchanged",
    not an empty "changed" -- callers should be able to branch on status
    without also checking whether "changes" happens to be empty."""
    from types import SimpleNamespace

    routine_a = SimpleNamespace(type="RLL", rungs=["XIC(A)OTE(B);"], _st_lines=[])
    routine_b = SimpleNamespace(type="RLL", rungs=["XIC(A)OTE(B);"], _st_lines=[])
    assert diff_routine(routine_a, routine_b) == {"status": "unchanged", "changes": []}


def test_diff_routine_reproduces_real_jsr_removal_scenario():
    """Real-world case that motivated diff_routine() as its own public
    function: a caller who already has two specific Routine objects (found
    by program/routine name) manually zipped their .rungs by index and
    concluded the whole routine had changed, because 3 JSR rungs were
    removed near the top of one project's copy and shifted every later
    rung's index. diff_routine() must isolate exactly the 3 removed rungs
    and report everything else as unchanged."""
    from types import SimpleNamespace

    rungs_a = [
        "JSR(P_Landing,0);",
        "JSR(Storage_Table,0)JSR(Lug_Backlog_Table,0)JSR(Lug_loader_Table_Wheels,0);",
        "JSR(Planer_Outfeed,0);",
        "JSR(Infeed_LandingTable,0);",
        "XIC(Local:12:I.Data.0)XIC(Local:12:I.Data.1)TON(DelayedControlPowe,?,?);",
        "XIC(B23[1].0)OTL(Clr_InfeedFaults);",
        "AOI_RPMtoFPM(TestFPM,VFD_P_INTBL2:I.OutputFreq);",
    ]
    rungs_b = [
        "JSR(Infeed_LandingTable,0);",
        "XIC(Local:12:I.Data.0)XIC(Local:12:I.Data.1)TON(DelayedControlPowe,?,?);",
        "XIC(B23[1].0)OTL(Clr_InfeedFaults);",
        "AOI_RPMtoFPM(TestFPM,VFD_P_INTBL2:I.OutputFreq);",
    ]
    routine_a = SimpleNamespace(type="RLL", rungs=rungs_a, _st_lines=[])
    routine_b = SimpleNamespace(type="RLL", rungs=rungs_b, _st_lines=[])

    result = diff_routine(routine_a, routine_b)
    assert result["status"] == "changed"
    assert result["changes"] == [
        {
            "op": "delete",
            "old": [
                "JSR(P_Landing,0);",
                "JSR(Storage_Table,0)JSR(Lug_Backlog_Table,0)JSR(Lug_loader_Table_Wheels,0);",
                "JSR(Planer_Outfeed,0);",
            ],
            "new": [],
        }
    ]


def test_new_member_defaults_radix_by_data_type():
    dint_member = new_member("Foo", "DINT")
    assert dint_member.radix == "Decimal"

    real_member = new_member("Bar", "REAL")
    assert real_member.radix == "Float"

    struct_member = new_member("Baz", "SomeUdt")
    assert struct_member.radix == "NullType"


def test_new_member_is_plain_non_bit_non_hidden():
    member = new_member("Foo", "DINT", dimension=5, description="a field")
    assert member.name == "Foo"
    assert member.data_type == "DINT"
    assert member.dimension == 5
    assert member.hidden is False
    assert member.target is None
    assert member.description == "a field"


def test_export_datatype_raises_if_data_type_not_in_project():
    from acd.l5x.elements import DataType

    project = load_acd(os.path.join("..", "resources", "ACDTestsWithAOI.ACD"))
    foreign_dt = DataType(_name="Foreign", name="Foreign", family="NoFamily", cls="User", members=[])
    try:
        export_datatype(project, foreign_dt, "unused.L5X")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_export_datatype_inserts_member_at_requested_position(tmp_path):
    project = load_acd(os.path.join("..", "resources", "ACDTestsWithAOI.ACD"))
    dt = next(d for d in project.controller.data_types if d.name == "UDT_Test")
    original_names = [m.name for m in dt.members]

    member = new_member("InsertedField", "DINT", description="test insert")
    insert_at = next(i for i, m in enumerate(dt.members) if m.name == "TestDINT") + 1
    dt.members.insert(insert_at, member)

    out_path = tmp_path / "UDT_Test_modified.L5X"
    export_datatype(project, dt, str(out_path))

    xml_text = out_path.read_text(encoding="utf-8")
    parsed = minidom.parseString(xml_text)  # raises on malformed XML

    root = parsed.documentElement
    assert root.getAttribute("TargetName") == "UDT_Test"
    assert root.getAttribute("TargetType") == "DataType"

    data_type_elems = parsed.getElementsByTagName("DataType")
    assert len(data_type_elems) == 1
    target_elem = data_type_elems[0]
    assert target_elem.getAttribute("Use") == "Target"
    assert target_elem.getAttribute("Name") == "UDT_Test"

    member_names = [
        m.getAttribute("Name")
        for m in target_elem.getElementsByTagName("Member")
    ]
    expected_names = list(original_names)
    expected_names.insert(insert_at, "InsertedField")
    assert member_names == expected_names
