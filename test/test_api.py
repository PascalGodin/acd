import os
from pathlib import Path
from xml.dom import minidom

import pytest

from acd.api import (
    ImportProjectFromFile,
    RSLogix5000Content,
    Extract,
    ExtractAcdDatabase,
    DumpCompsRecordsToFile,
    diff_lines,
    export_datatype,
    export_routine,
    find_io_addresses,
    find_tag_references,
    get_routine,
    get_tag_value,
    io_addresses_by_routine,
    list_routines,
    list_tags,
    get_project_summary,
    diff_io_addresses,
    diff_project,
    diff_routine,
    load_acd,
    replace_rung_safe,
    replace_st_line_safe,
    tag_exists,
    _sync_data_types_map,
)
from acd.l5x.elements import (
    DataType,
    Member,
    Tag,
    new_bit_member,
    new_member,
    new_routine,
    new_tag,
    _validate_rll_rung_syntax,
)


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


def test_diff_lines_unchanged_returns_empty_list():
    assert diff_lines(["A", "B"], ["A", "B"]) == []


def test_diff_lines_insert_only():
    old = ["A", "B", "C"]
    new = ["A", "X", "B", "C"]
    result = diff_lines(old, new)
    assert result == [{"op": "insert", "old": [], "new": ["X"]}]
    assert all(c["op"] == "insert" for c in result)


def test_diff_lines_isolates_a_deletion_without_misreporting_the_shifted_tail():
    # Same rationale as diff_routine()'s own JSR-removal test, but for the
    # lower-level primitive directly: deleting lines near the start must not
    # make an untouched tail look like a wall of replacements.
    old = ["1", "2", "3", "4", "5"]
    new = ["3", "4", "5"]
    assert diff_lines(old, new) == [{"op": "delete", "old": ["1", "2"], "new": []}]


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


def test_new_member_rejects_none_dimension():
    # Regression test: a real caller passed dimension=None for a scalar
    # member, by analogy with radix=None/description=None meaning "use the
    # default" -- but dimension has a real default (0) for that purpose,
    # and None silently propagated all the way to an unrelated crash deep
    # in export rendering. Raise immediately instead, at the actual mistake.
    with pytest.raises(ValueError, match="dimension"):
        new_member("Foo", "DINT", dimension=None)


def test_new_member_rejects_bit_type():
    # Regression test for a real reported bug: new_member(name, "BIT")
    # used to silently return a member with target=None/bit_number=None --
    # no exception -- which committed/exported fine and only failed a real
    # Studio 5000 "Import Data Type..." on Target three steps later. Now
    # raises immediately and points at new_bit_member() instead.
    with pytest.raises(ValueError, match="new_bit_member"):
        new_member("Foo", "BIT")


def test_new_bit_member_creates_backing_field_when_none_exists():
    dt = DataType("MyUdt", "MyUdt", "", "User", [
        new_member("Field1", "DINT"),
    ])
    member = new_bit_member(dt, "Flag1")
    dt.members.append(member)

    assert member.data_type == "BIT"
    assert member.hidden is False
    assert member.target is not None
    assert member.bit_number == 0
    backing = next(m for m in dt.members if m.name == member.target)
    assert backing.hidden is True
    assert backing.data_type == "SINT"


def test_new_bit_member_reuses_free_bit_in_existing_backing_field():
    dt = DataType("MyUdt", "MyUdt", "", "User", [])
    first = new_bit_member(dt, "Flag1")
    dt.members.append(first)

    second = new_bit_member(dt, "Flag2")

    assert second.target == first.target
    assert second.bit_number == 1
    # No second backing field created -- still just the one.
    assert sum(1 for m in dt.members if m.hidden) == 1


def test_new_bit_member_creates_new_backing_field_once_full():
    dt = DataType("MyUdt", "MyUdt", "", "User", [])
    for i in range(8):
        m = new_bit_member(dt, f"Flag{i}")
        dt.members.append(m)
    first_backing_name = next(m.name for m in dt.members if m.hidden)

    ninth = new_bit_member(dt, "Flag8")
    dt.members.append(ninth)

    assert ninth.target != first_backing_name
    assert ninth.bit_number == 0
    assert sum(1 for m in dt.members if m.hidden) == 2


def test_new_bit_member_ignores_hidden_member_not_already_backing_a_bit():
    # A hidden member that ISN'T already backing any real BIT member must
    # never be repurposed -- there's no way to tell from the object model
    # alone whether it's genuinely free bit storage or something else.
    dt = DataType("MyUdt", "MyUdt", "", "User", [
        Member("SomeHidden", "SomeHidden", "SINT", 0, "Decimal", True, None, None, "Read/Write"),
    ])
    member = new_bit_member(dt, "Flag1")

    assert member.target != "SomeHidden"


def test_new_bit_member_rejects_duplicate_name():
    dt = DataType("MyUdt", "MyUdt", "", "User", [
        new_member("Existing", "DINT"),
    ])
    with pytest.raises(ValueError, match="already has a member"):
        new_bit_member(dt, "Existing")


def test_new_routine_rll():
    routine = new_routine("MyRoutine", "RLL", description="a test routine")
    assert routine.name == "MyRoutine"
    assert routine.type == "RLL"
    assert routine.rungs == []
    assert routine._st_lines == []
    assert routine._description == "a test routine"


def test_new_routine_st():
    routine = new_routine("MySTRoutine", "ST")
    assert routine.type == "ST"
    assert routine.rungs == []
    assert routine._st_lines == []
    assert routine._description is None


def test_new_routine_rejects_invalid_type():
    with pytest.raises(ValueError, match="must be 'RLL' or 'ST'"):
        new_routine("MyRoutine", "SFC")


def test_new_routine_result_can_be_populated_with_insert_rung():
    routine = new_routine("MyRoutine", "RLL")
    routine.insert_rung(0, "NOP();")
    assert routine.rungs == ["NOP();"]


def test_new_routine_result_can_be_populated_with_insert_st_line():
    routine = new_routine("MyRoutine", "ST")
    routine.insert_st_line(0, "X := 1;")
    assert routine._st_lines == ["X := 1;"]


def test_new_tag_primitive_defaults_radix_by_data_type():
    dint_tag = new_tag("Foo", "DINT")
    assert dint_tag.name == "Foo"
    assert dint_tag.tag_type == "Base"
    assert dint_tag.data_type == "DINT"
    assert dint_tag.radix == "Decimal"
    assert dint_tag.dimensions is None
    assert dint_tag._initial_value is None

    real_tag = new_tag("Bar", "REAL", value=1.5)
    assert real_tag.radix == "Float"
    assert real_tag._initial_value == 1.5


def test_new_tag_udt_type_omits_radix():
    # A struct-typed tag carries no Radix attribute at all, matching every
    # ACD-decoded UDT-typed Tag (see TagBuilder.build()) -- unlike
    # new_member(), which defaults an unknown/struct type's radix to the
    # string "NullType" instead of None.
    tag = new_tag("Baz", "SomeUdt", dimensions="10", description="a tag")
    assert tag.radix is None
    assert tag.data_type == "SomeUdt"
    assert tag.dimensions == "10"
    assert tag._comments == [("", "a tag")]


def test_new_tag_xml_shape():
    tag = new_tag("MyTag", "DINT", value=5)
    xml = tag.to_xml()
    assert '<Tag Name="MyTag"' in xml
    assert 'DataType="DINT"' in xml


def test_sync_data_types_map_propagates_new_type_to_existing_tags():
    # Regression test for a real report: appending a new DataType to
    # project.controller.data_types (the documented way to register a new
    # UDT) never updated the SEPARATE, already-captured _data_types_map
    # every existing Tag's own rendering uses (Tag._data_types_map is a
    # snapshot reference assigned once at load_acd() time) -- so a tag
    # whose value needed to resolve that new type silently fell through to
    # _zero_value_for_member's "unknown type" fallback (a wrong-shaped bare
    # scalar 0) instead of the correct nested structure, with no error at
    # all. Traced to a real case: adding a new struct-typed member to an
    # existing UDT with live tag instances, then exporting a routine
    # referencing one of those instances in the same session.
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)

    outer_dt = DataType("Outer", "Outer", "NoFamily", "User", [new_member("M", "DINT")])
    project.controller.data_types.append(outer_dt)
    project.controller._data_types_map["OUTER"] = outer_dt

    tag = Tag("MyTag", "MyTag", "Base", "Outer", None, "Read/Write", None, None)
    tag._data_types_map = project.controller._data_types_map  # same shared object TagBuilder assigns
    tag._initial_value = {"M": 5}  # decoded before "Inner"/"NewStruct" existed

    # Mutate Outer AFTER the tag's value was "decoded" -- register a new
    # struct type and add a member of it, exactly matching export_datatype()'s
    # own documented pattern: append to .data_types, then dt.members.insert(...).
    inner_dt = DataType("Inner", "Inner", "NoFamily", "User", [new_member("X", "DINT", dimension=2)])
    project.controller.data_types.append(inner_dt)
    outer_dt.members.append(new_member("NewStruct", "Inner"))

    _sync_data_types_map(project)

    xml = tag.to_xml()
    assert '<StructureMember Name="NewStruct" DataType="Inner">' in xml
    assert '<ArrayMember Name="X" DataType="DINT" Dimensions="2"' in xml
    # The pre-existing member must still render correctly, unaffected.
    assert '<DataValueMember Name="M" DataType="DINT" Radix="Decimal" Value="5"/>' in xml


def test_export_routine_validate_raises_on_unresolved_type(tmp_path):
    # validate=True should catch, before writing any XML, the exact failure
    # signature the stale-_data_types_map bug produced silently (see
    # CLAUDE.md "Mutating a UDT with live tag instances..."): a referenced
    # tag whose type doesn't resolve in data_types_map at all.
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    program = project.controller.programs[0]
    routine = next(r for r in program.routines if r.type == "RLL")

    bad_tag = Tag("BadTag", "BadTag", "Base", "NotARealType", None, "Read/Write", None, None)
    bad_tag._data_types_map = project.controller._data_types_map
    bad_tag._initial_value = {"X": 0}
    project.controller.tags.append(bad_tag)
    routine.rungs.append("XIC(BadTag)OTE(BadTag);")

    out_path = tmp_path / "validate_test.L5X"
    with pytest.raises(ValueError, match="NotARealType"):
        export_routine(project, routine, str(out_path), validate=True)
    assert not out_path.exists()  # must not write a file when validation fails

    # validate defaults to False -- this option must not change any existing
    # caller's behavior; the export still succeeds (just with a value that
    # would render wrong, which is exactly what validate=True is for).
    export_routine(project, routine, str(out_path))
    assert out_path.exists()


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


def _mock_project():
    from types import SimpleNamespace

    def routine(name, rungs):
        return SimpleNamespace(name=name, type="RLL", rungs=rungs, _st_lines=[])

    prog_a = SimpleNamespace(
        name="ProgA",
        routines=[routine("Main", ["XIC(Foo)OTE(Bar);"]), routine("Sub", ["MOV(Foo,Baz);"])],
        tags=[SimpleNamespace(name="ProgTag")],
    )
    prog_b = SimpleNamespace(
        name="ProgB",
        routines=[routine("Main", ["XIC(Qux)OTE(Foo);"])],
        tags=[],
    )
    aoi = SimpleNamespace(
        name="MyAOI",
        routines=[routine("Logic", ["MOV(Foo,1);"])],
    )
    controller = SimpleNamespace(
        programs=[prog_a, prog_b],
        aois=[aoi],
        tags=[SimpleNamespace(name="CtrlTag")],
    )
    return SimpleNamespace(controller=controller)


def test_get_routine_unique_name_no_program_given():
    project = _mock_project()
    routine = get_routine(project, "Sub")
    assert routine.name == "Sub"


def test_get_routine_ambiguous_name_without_program_raises():
    project = _mock_project()
    with pytest.raises(ValueError):
        get_routine(project, "Main")  # exists in both ProgA and ProgB


def test_get_routine_ambiguous_name_resolved_with_program():
    project = _mock_project()
    routine = get_routine(project, "Main", program_name="ProgB")
    assert routine.rungs == ["XIC(Qux)OTE(Foo);"]


def test_get_routine_aoi_logic_routine():
    project = _mock_project()
    routine = get_routine(project, "Logic", program_name="AOI:MyAOI")
    assert routine.rungs == ["MOV(Foo,1);"]


def test_get_routine_missing_raises_keyerror():
    project = _mock_project()
    with pytest.raises(KeyError):
        get_routine(project, "DoesNotExist")


def test_tag_exists_controller_scope():
    project = _mock_project()
    assert tag_exists(project, "CtrlTag") is True
    assert tag_exists(project, "ProgTag") is False


def test_tag_exists_program_scope():
    project = _mock_project()
    assert tag_exists(project, "ProgTag", program_name="ProgA") is True
    assert tag_exists(project, "CtrlTag", program_name="ProgA") is False


def test_tag_exists_unknown_program_raises():
    project = _mock_project()
    with pytest.raises(KeyError):
        tag_exists(project, "CtrlTag", program_name="NoSuchProgram")


def test_find_tag_references_word_boundary_by_default():
    project = _mock_project()
    # "Foo" appears in ProgA/Main, ProgA/Sub, ProgB/Main, and the AOI logic
    # routine -- "Foobar" (a different, longer identifier) must NOT match.
    results = find_tag_references(project, "Foo")
    keys = {(p, r) for p, r, _, _ in results}
    assert keys == {("ProgA", "Main"), ("ProgA", "Sub"), ("ProgB", "Main"), ("AOI:MyAOI", "Logic")}
    for _, _, idx, text in results:
        assert isinstance(idx, int)
        assert "Foo" in text


def test_find_tag_references_does_not_match_substring():
    from types import SimpleNamespace

    project = _mock_project()
    project.controller.programs[0].routines.append(
        SimpleNamespace(name="Extra", type="RLL", rungs=["XIC(Foobar)OTE(Foo2);"], _st_lines=[])
    )
    results = find_tag_references(project, "Foo")
    assert ("ProgA", "Extra", 0, "XIC(Foobar)OTE(Foo2);") not in results


def test_find_tag_references_regex_mode():
    project = _mock_project()
    results = find_tag_references(project, r"Ba[rz]", regex=True)
    keys = {(p, r) for p, r, _, _ in results}
    assert keys == {("ProgA", "Main"), ("ProgA", "Sub")}


def test_get_project_summary_is_names_and_counts_only():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    summary = get_project_summary(project)

    assert summary["controller_name"] == "CuteLogix"
    assert set(summary["programs"]) == {p.name for p in project.controller.programs}
    assert set(summary["tasks"]) == {t.name for t in project.controller.tasks}
    assert summary["controller_tag_count"] == len(project.controller.tags)
    assert summary["program_tag_counts"]["Instructions"] == len(
        next(p for p in project.controller.programs if p.name == "Instructions").tags
    )
    assert summary["routine_count"] == sum(len(p.routines) for p in project.controller.programs)
    # Never dumps actual content -- just names/counts.
    assert "rungs" not in str(summary.keys())


def test_list_routines_has_no_content_field():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routines = list_routines(project)
    assert routines  # fixture has real routines
    for r in routines:
        assert set(r.keys()) == {"program", "routine", "type", "line_count"}
    entry = next(r for r in routines if r["routine"] == "R033_ASCII_Conv")
    assert entry["type"] == "RLL"
    assert entry["line_count"] == 6


def test_list_routines_filtered_by_program():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    all_routines = list_routines(project)
    filtered = list_routines(project, program_name="Instructions")
    assert filtered
    assert all(r["program"] == "Instructions" for r in filtered)
    assert len(filtered) < len(all_routines)


def test_list_tags_excludes_io_and_omits_value():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    tags = list_tags(project)
    names = {t["name"] for t in tags}
    assert "Map:Local" not in names  # I/O tag (":" in name) -- excluded
    assert "AdvancedMath" in names
    for t in tags:
        assert set(t.keys()) == {"name", "data_type", "dimensions", "description"}


def test_list_tags_program_scope_unknown_program_raises():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    with pytest.raises(KeyError):
        list_tags(project, program_name="NoSuchProgram")


def test_get_tag_value_scalar_returned_in_full():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    result = get_tag_value(project, "EOT_Test")
    assert result["value"] == 0
    assert "total_elements" not in result  # pagination fields only apply to arrays


def test_get_tag_value_small_array_returns_everything():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    result = get_tag_value(project, "AdvancedMath")  # DINT[10]
    assert result["total_elements"] == 10
    assert result["returned"] == 10
    assert len(result["value"]) == 10


def test_get_tag_value_large_array_paginates_by_default():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    full = next(t for t in project.controller.tags if t.name == "Branching")._initial_value
    assert len(full) == 1000  # confirms the fixture actually exercises pagination

    first_page = get_tag_value(project, "Branching")  # default limit=50
    assert first_page["total_elements"] == 1000
    assert first_page["offset"] == 0
    assert first_page["returned"] == 50
    assert first_page["value"] == full[0:50]

    second_page = get_tag_value(project, "Branching", offset=50, limit=50)
    assert second_page["offset"] == 50
    assert second_page["value"] == full[50:100]


def test_get_tag_value_missing_tag_raises_keyerror():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    with pytest.raises(KeyError):
        get_tag_value(project, "NoSuchTag")


def _st_routine():
    project = load_acd(os.path.join("..", "resources", "ACDTestsNonRedundant.ACD"), verbose=False)
    program = next(p for p in project.controller.programs if p.name == "MainProgram")
    return project, next(r for r in program.routines if r.type == "ST")


def test_routine_insert_rung_raises_on_st_routine():
    _, st_routine = _st_routine()
    st_lines_before = list(st_routine._st_lines)

    with pytest.raises(ValueError, match="only applies to an RLL routine"):
        st_routine.insert_rung(0, "NOP();")

    assert st_routine.rungs == []
    assert st_routine._st_lines == st_lines_before


def test_routine_delete_rung_raises_on_st_routine():
    _, st_routine = _st_routine()
    with pytest.raises(ValueError, match="only applies to an RLL routine"):
        st_routine.delete_rung(0)


def test_replace_rung_safe_raises_on_st_routine():
    _, st_routine = _st_routine()
    with pytest.raises(ValueError, match="only applies to an RLL routine"):
        replace_rung_safe(st_routine, 0, "whatever", "NOP();")


def test_routine_insert_st_line_shifts_later_lines():
    _, st_routine = _st_routine()
    original_lines = list(st_routine._st_lines)

    st_routine.insert_st_line(0, "NEW_LINE := 1;")

    assert st_routine._st_lines[0] == "NEW_LINE := 1;"
    assert st_routine._st_lines[1:] == original_lines
    assert len(st_routine._st_lines) == len(original_lines) + 1

    st_routine.delete_st_line(0)
    assert st_routine._st_lines == original_lines


def test_routine_insert_st_line_raises_on_rll_routine():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    with pytest.raises(ValueError, match="only applies to an ST routine"):
        routine.insert_st_line(0, "NEW_LINE := 1;")


def test_routine_delete_st_line_raises_on_rll_routine():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    with pytest.raises(ValueError, match="only applies to an ST routine"):
        routine.delete_st_line(0)


def test_replace_st_line_safe_matching_text():
    _, st_routine = _st_routine()
    old_text = st_routine._st_lines[0]

    replace_st_line_safe(st_routine, 0, old_text, "NEW_LINE := 2;")

    assert st_routine._st_lines[0] == "NEW_LINE := 2;"


def test_replace_st_line_safe_mismatch_raises_and_does_not_mutate():
    _, st_routine = _st_routine()
    original = st_routine._st_lines[0]

    with pytest.raises(ValueError, match="doesn't match the expected text"):
        replace_st_line_safe(st_routine, 0, "definitely not the real text", "NEW_LINE := 2;")

    assert st_routine._st_lines[0] == original


def test_replace_st_line_safe_raises_on_rll_routine():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    with pytest.raises(ValueError, match="only applies to an ST routine"):
        replace_st_line_safe(routine, 0, "whatever", "NEW_LINE := 1;")


def test_routine_insert_and_delete_rung_shift_comments_atomically():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    original_rungs = list(routine.rungs)
    original_ids = list(routine._rung_ids)
    routine._rung_comments = {1: "comment on original rung 1"}

    routine.insert_rung(1, "NOP();", comment="comment on new rung")

    assert routine.rungs[1] == "NOP();"
    assert routine._rung_ids[1] is None
    assert routine._rung_comments == {1: "comment on new rung", 2: "comment on original rung 1"}
    assert routine.rungs[2:] == original_rungs[1:]
    assert len(routine.rungs) == len(routine._rung_ids) == len(original_rungs) + 1

    routine.delete_rung(1)

    assert routine.rungs == original_rungs
    assert routine._rung_ids == original_ids
    assert routine._rung_comments == {1: "comment on original rung 1"}


def test_replace_rung_safe_matching_text():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    old_text = routine.rungs[0]

    replace_rung_safe(routine, 0, old_text, "NEW_TEXT();")

    assert routine.rungs[0] == "NEW_TEXT();"


def test_replace_rung_safe_mismatch_raises_and_does_not_mutate():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    original = routine.rungs[0]

    with pytest.raises(ValueError, match="doesn't match the expected text"):
        replace_rung_safe(routine, 0, "definitely not the real text", "NEW_TEXT();")

    assert routine.rungs[0] == original


def test_replace_rung_safe_rejects_malformed_rll_syntax():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    old_text = routine.rungs[0]

    with pytest.raises(ValueError, match="only 1 member"):
        replace_rung_safe(routine, 0, old_text, "[MOVE(A,B) FOR(C,D,E) ];")

    assert routine.rungs[0] == old_text


def test_routine_insert_rung_rejects_malformed_rll_syntax():
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    original_rungs = list(routine.rungs)

    with pytest.raises(ValueError, match="only 1 member"):
        routine.insert_rung(0, "[MOVE(A,B) FOR(C,D,E) ];")

    assert routine.rungs == original_rungs


def test_export_routine_validate_rejects_malformed_rll_syntax(tmp_path):
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    # Bypass insert_rung()'s own guard to simulate a rung that became
    # malformed some other way (e.g. rehydrated from a DB written before
    # this check existed) -- export_routine(validate=True) must catch it
    # independently, not only rely on insert_rung()'s own guard.
    routine.rungs.append("[MOVE(A,B) FOR(C,D,E) ];")

    with pytest.raises(ValueError, match=r"Rung \d+ of routine .*only 1 member"):
        export_routine(project, routine, str(tmp_path / "bad.L5X"), validate=True)


def test_export_routine_validate_false_does_not_check_rll_syntax(tmp_path):
    project = load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    routine = get_routine(project, "R033_ASCII_Conv")
    routine.rungs.append("[MOVE(A,B) FOR(C,D,E) ];")

    export_routine(project, routine, str(tmp_path / "bad.L5X"), validate=False)

    assert (tmp_path / "bad.L5X").exists()


class TestValidateRllRungSyntax:
    def test_valid_two_branch_group(self):
        _validate_rll_rung_syntax("XIC(A)[OTE(B),OTE(C)];")

    def test_valid_nested_branch_group(self):
        _validate_rll_rung_syntax("[[XIC(A),XIC(B)],XIC(C)];")

    def test_valid_array_index_not_treated_as_branch(self):
        _validate_rll_rung_syntax("MOV(MyArray[5],Result);")

    def test_valid_multidim_array_index_not_treated_as_branch(self):
        _validate_rll_rung_syntax("MOV(MyArray[2,2,1],Result);")

    def test_single_branch_bracket_raises(self):
        with pytest.raises(ValueError, match="only 1 member"):
            _validate_rll_rung_syntax("[MOVE(A,B) FOR(C,D,E) ];")

    def test_empty_branch_bracket_raises_with_zero_members(self):
        with pytest.raises(ValueError, match="only 0 member"):
            _validate_rll_rung_syntax("XIC(A)[];")

    def test_unbalanced_open_paren_raises(self):
        with pytest.raises(ValueError, match="Unbalanced"):
            _validate_rll_rung_syntax("XIC(A OTE(B);")

    def test_unbalanced_close_paren_raises(self):
        with pytest.raises(ValueError, match="Unbalanced"):
            _validate_rll_rung_syntax("XIC(A))OTE(B);")

    def test_unbalanced_open_bracket_raises(self):
        with pytest.raises(ValueError, match="Unbalanced"):
            _validate_rll_rung_syntax("[XIC(A),XIC(B);")

    def test_string_literal_punctuation_does_not_trip_check(self):
        # A STRING tag's literal value can contain brackets/commas/parens --
        # none of it is real RLL syntax and must not be parsed as such.
        _validate_rll_rung_syntax("MOV('a[1,2](x',MyStringTag.DATA[0]);")

    def test_escaped_quote_in_string_literal_does_not_end_it_early(self):
        _validate_rll_rung_syntax("MOV('it$'s a test',MyStringTag.DATA[0]);")

    def test_unterminated_string_literal_raises(self):
        with pytest.raises(ValueError, match="Unterminated string literal"):
            _validate_rll_rung_syntax("MOV('unterminated,MyTag);")

    def test_empty_text_is_a_noop(self):
        _validate_rll_rung_syntax("")
        _validate_rll_rung_syntax("   ")

    def test_plain_series_instructions_no_brackets_valid(self):
        _validate_rll_rung_syntax("XIC(A)MOV(1,B)OTE(C);")


def test_load_acd_verbose_false_suppresses_info_and_debug(capsys):
    from loguru import logger as loguru_logger

    load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)
    captured = capsys.readouterr()

    # A quiet load must not emit the routine progress INFO lines this
    # library always logged unconditionally before verbose= existed.
    assert "Getting records from ACD" not in captured.err

    # WARNING and above must still come through -- verbose=False must not
    # go fully silent, only drop INFO/DEBUG progress noise.
    loguru_logger.warning("CHECK_WARNING_STILL_REACHES_THE_SINK")
    captured = capsys.readouterr()
    assert "CHECK_WARNING_STILL_REACHES_THE_SINK" in captured.err


def test_load_acd_defaults_to_quiet(capsys):
    # Regression test for the verbose default flip (True -> False): a bare
    # load_acd(path), with no verbose= argument at all, must already be
    # quiet -- a caller shouldn't have to discover and pass verbose=False
    # themselves to get the token-cheap behavior.
    load_acd(os.path.join("..", "resources", "CuteLogix.ACD"))
    captured = capsys.readouterr()
    assert "Getting records from ACD" not in captured.err
