import os
from pathlib import Path
from xml.dom import minidom

from acd.api import (
    ImportProjectFromFile,
    RSLogix5000Content,
    Extract,
    ExtractAcdDatabase,
    DumpCompsRecordsToFile,
    find_io_addresses,
    io_addresses_by_routine,
    diff_io_addresses,
    diff_project,
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
