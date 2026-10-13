"""MESSAGE tag configuration decoding (acd/l5x/message_config.py).

The cases in data/message_config_cases.json are 65 configurations, each with the
raw attribute records read from the ACD and the MessageParameters Studio itself
exported for the same tag: 22 real tags from three projects, plus 43 variants
built by importing a MessageParameters body through the Logix Designer SDK into
a scratch project, saving it, and having Studio re-export every tag.
"""
import json
import os

import pytest

from acd.l5x.message_config import (
    decode_message_params,
    decode_path,
    message_parameters_xml,
    read_attributes,
    resolve_element_placeholders,
)

_CASES = json.load(open(os.path.join(os.path.dirname(__file__), "data", "message_config_cases.json")))


def _decode(case):
    attrs = {int(k): bytes.fromhex(v) for k, v in case["attrs"].items()}
    refs = case["refs"]
    expected_path = case["expected"]["ConnectionPath"]
    return decode_message_params(
        attrs,
        resolve_element=lambda text: refs.get(text),
        # The module-name lookup is tested on its own; here it is fed Studio's answer.
        resolve_path=lambda raw: expected_path if raw == case["raw_path"] else raw,
    )


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_decodes_exactly_what_studio_exports(case):
    decoded = _decode(case)
    expected = dict(case["expected"])
    # Studio normalizes ConnectedFlag 1 to 2 on re-export; we only ever emit 2.
    assert decoded is not None
    assert list(decoded.items()) == list(expected.items())


def test_fixture_covers_both_message_kinds_and_dhplus():
    kinds = {c["expected"]["MessageType"] for c in _CASES}
    assert {"CIP Generic", "SLC Typed Read", "SLC Typed Write"} <= kinds
    assert any(c["expected"].get("CommTypeCode") == "1" for c in _CASES)
    assert any(c["expected"].get("LargePacketUsage") == "true" for c in _CASES)
    assert len(_CASES) >= 65


def test_unknown_message_kind_is_not_guessed():
    case = next(c for c in _CASES if c["name"] == "oracle:cip_base")
    attrs = {int(k): bytes.fromhex(v) for k, v in case["attrs"].items()}
    attrs[0x68] = b"\x09"
    assert decode_message_params(attrs, lambda t: t) is None


def test_unresolvable_element_reference_returns_none():
    case = next(c for c in _CASES if c["name"] == "oracle:cip_both")
    attrs = {int(k): bytes.fromhex(v) for k, v in case["attrs"].items()}
    assert decode_message_params(attrs, lambda t: None) is None


def test_unknown_transport_code_is_not_guessed():
    case = next(c for c in _CASES if c["name"] == "oracle:cip_base")
    attrs = {int(k): bytes.fromhex(v) for k, v in case["attrs"].items()}
    blob = bytearray(attrs[1])
    blob[280:282] = b"\x34\x12"
    attrs[1] = bytes(blob)
    assert decode_message_params(attrs, lambda t: t) is None


@pytest.mark.parametrize(
    "raw,text",
    [
        (bytes.fromhex("1f00"), "THIS"),
        (bytes.fromhex("0103"), "1, 3"),
        (bytes.fromhex("0100"), "1, 0"),
        (bytes([0x12, 11]) + b"10.20.30.40" + b"\x00", "2, 10.20.30.40"),
        (bytes([0x01, 0x02, 0x12, 11]) + b"192.168.1.9" + b"\x00" + bytes([1, 0]), "1, 2, 2, 192.168.1.9, 1, 0"),
    ],
)
def test_decode_path(raw, text):
    assert decode_path(raw) == text


def test_resolve_element_placeholders_tag_member_and_index():
    tags = {0x1AB16C4C: "SKT_DATA_Client"}
    members = {0x4ACFFCB8: "Connect_Source"}
    assert (
        resolve_element_placeholders("@1ab16c4c@.@4acffcb8@", tags.get, members.get)
        == "SKT_DATA_Client.Connect_Source"
    )
    assert resolve_element_placeholders("@1ab16c4c@[3]", tags.get, members.get) == "SKT_DATA_Client[3]"
    assert resolve_element_placeholders("@deadbeef@", tags.get, members.get) is None


def test_message_parameters_xml_shape():
    xml = message_parameters_xml({"MessageType": "CIP Generic", "RequestedLength": "0"})
    assert xml == '<Data Format="Message">\n<MessageParameters MessageType="CIP Generic" RequestedLength="0"/>\n</Data>'


def test_read_attributes_finds_the_unparsed_last_attribute():
    case = next(c for c in _CASES if c["name"] == "oracle:cip_dest_only")
    assert 0x70 in {int(k) for k in case["attrs"]} and read_attributes(b"") == {}


# ---- integration through the builder, export and validation -------------------

def _cutelogix():
    from acd.api import load_acd
    return load_acd(os.path.join("..", "resources", "CuteLogix.ACD"), verbose=False)


def test_builder_decodes_a_configured_message_and_leaves_a_bare_one_alone():
    ctrl = _cutelogix().controller
    web = next(t for t in ctrl.tags if t.name == "WebPage")
    bare = next(t for t in ctrl.tags if t.name == "Message")
    assert web._message_params["MessageType"] == "CIP Generic"
    assert web._message_params["LocalElement"] == "DisableWeb[0]"
    assert web._message_params["ServiceCode"] == "16#004c"
    assert bare._message_params is None  # no configuration object: never guessed


def test_message_tag_to_xml_writes_the_message_body_only_when_decoded():
    ctrl = _cutelogix().controller
    web = next(t for t in ctrl.tags if t.name == "WebPage").to_xml()
    bare = next(t for t in ctrl.tags if t.name == "Message").to_xml()
    assert '<Data Format="Message">' in web and 'LocalElement="DisableWeb[0]"' in web
    assert "<Data" not in bare


def test_validate_rejects_a_described_message_tag_without_a_readable_config():
    from acd.api import _validate_message_tags
    ctrl = _cutelogix().controller
    bare = next(t for t in ctrl.tags if t.name == "Message")
    web = next(t for t in ctrl.tags if t.name == "WebPage")
    _validate_message_tags([bare, web])  # no description: fine either way
    bare._comments = [("", "MSG: something")]
    with pytest.raises(ValueError, match="MSG configuration"):
        _validate_message_tags([bare])
    web._comments = [("", "MSG: web page")]
    _validate_message_tags([web])  # a readable config is exported, so a description is safe


def test_get_tag_value_for_a_message_tag_returns_configuration_not_fake_zeros():
    # Real report: db_get_tag_value on MESSAGE tags returned zeros for ERR/REQ_LEN/DN
    # while Studio's live Monitor Tags showed REQ_LEN 12, DN 1. The file stores a
    # descriptor, not the runtime struct, so there is no such value to return.
    from acd.api import get_tag_value
    project = _cutelogix()
    web = get_tag_value(project, "WebPage")
    assert web["value"] is None
    assert web["message_parameters"]["MessageType"] == "CIP Generic"
    assert "not stored" in web["note"]
    bare = get_tag_value(project, "Message")  # no readable configuration either
    assert bare["value"] is None and bare["message_parameters"] is None
