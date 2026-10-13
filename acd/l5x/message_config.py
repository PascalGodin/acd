"""Decode a MESSAGE tag's configuration (the MSG instruction's setup) from the
configuration object Comps.Dat keeps for it, into the attributes of Studio's
``<Data Format="Message"><MessageParameters .../></Data>``.

Layout (all verified two ways: 22 real MESSAGE tags from three projects whose
Studio exports were compared byte for byte, and 43 controlled variants imported
into a scratch project through the Logix Designer SDK, saved, read back here, and
re-exported by Studio -- see ``test/data/message_config_cases.json``).

The configuration object is a ``$hex$``-named child of ``RxMsgCollection`` that
XRefs.Dat links to the tag (kind 6). Its record is an RxGeneric header followed by
attribute records (id u32, length u32, value), parsed here by ``read_attributes``:

  0x01  428-byte blob of numbers (offsets below)
  0x65  LocalElement, UTF-16, ``@tagid@.@memberid@[n]`` placeholders
  0x66  u16 service code (CIP) / 0x0fa2 read, 0x0faa write (SLC)
  0x67  RemoteElement, UTF-16 (SLC)
  0x68  1 byte: 2 = CIP Generic, 3 = SLC typed
  0x70  DestinationTag, same form as 0x65

Blob offsets: 139 u16 RequestedLength; 143 comm type (2 -> CommTypeCode 0,
1 -> 1); 144 u16 byte length of the encoded path that starts at 146; 275..
(compiled symbol-path bytes, unused here); 280 u16 transport code (0x01f6 CIP,
0x0fa2 CIP large-packet, 0x0102 DH+); 1 CacheConnections (2 = TRUE, 0 = FALSE);
320 DH+ channel letter;
324/326/328 u16 DH+ source link / destination link / destination node;
330 u16 service; 332 u16 ObjectType; 334 u16 TargetObject; 338 u16
AttributeNumber; 340 u32 LocalIndex.

``ConnectedFlag`` is not stored: Studio writes 2 for every CIP Generic
message, and importing 1 round-trips to 2.

Anything not covered by those samples (other message types, comm type codes,
transport codes, an unresolvable element reference) returns ``None``: callers
must then leave the tag's ``<Data>`` out, never guess a configuration.
"""
import re
import struct
from typing import Callable, Dict, List, Optional, Tuple

_SLC_TYPES = {0x0FA2: "SLC Typed Read", 0x0FAA: "SLC Typed Write"}
_KIND_CIP_GENERIC = 2
_KIND_SLC = 3
_TRANSPORT_CIP = 0x01F6
_TRANSPORT_CIP_LARGE = 0x0FA2
_TRANSPORT_DHPLUS = 0x0102


def read_attributes(record: bytes) -> Dict[int, bytes]:
    """All attribute records of a comps record (74-byte RxGeneric header, then
    len_record u32 and count_record u32, then attribute id u32 / length u32 /
    value), including the last one the Kaitai parser leaves unread."""
    attrs: Dict[int, bytes] = {}
    pos = 82
    while pos + 8 <= len(record):
        attr_id, length = struct.unpack_from("<II", record, pos)
        pos += 8
        if pos + length > len(record):
            break
        attrs[attr_id] = bytes(record[pos:pos + length])
        pos += length
    return attrs


def _utf16(value: Optional[bytes]) -> str:
    if not value:
        return ""
    return value.decode("utf-16-le", errors="replace").rstrip("\x00")


def decode_path(path: bytes) -> Optional[str]:
    """Studio's text form of an encoded CIP path: ``1, 3`` (port, link),
    ``2, 10.20.30.40`` (port with an address string), ``THIS`` for the
    "this controller" path, chained for multi-hop paths."""
    if path[:2] == b"\x1f\x00":
        return "THIS"
    parts: List[str] = []
    i = 0
    while i < len(path):
        seg = path[i]
        port = seg & 0x0F
        if seg & 0x10:
            if i + 1 >= len(path):
                return None
            n = path[i + 1]
            text = path[i + 2:i + 2 + n]
            if len(text) != n:
                return None
            parts += [str(port), text.decode("ascii", errors="replace")]
            i += 2 + n
            if (2 + n) % 2:
                i += 1  # segments are word aligned
        else:
            if i + 1 >= len(path):
                return None
            parts += [str(port), str(path[i + 1])]
            i += 2
    return ", ".join(parts) if parts else None


def _octal_node(value: int) -> str:
    digits = f"{value:06o}"
    return f"8#{digits[:3]}_{digits[3:]}"


def decode_message_params(
    attrs: Dict[int, bytes],
    resolve_element: Callable[[str], Optional[str]],
    resolve_path: Callable[[str], str] = lambda path: path,
) -> Optional["Dict[str, str]"]:
    """MessageParameters attributes, in Studio's order, or None when this
    configuration isn't one the verified samples cover.

    resolve_element turns an ``@tagid@.@memberid@[n]`` placeholder string into
    a tag address (None if it can't); resolve_path turns decoded path text into
    the module name Studio prints when the path points at a project module
    (e.g. ``2, 10.0.0.9`` -> ``Sorter_VFD``) or returns it unchanged."""
    blob = attrs.get(0x01)
    kind_raw = attrs.get(0x68)
    if blob is None or len(blob) < 342 or not kind_raw:
        return None
    kind = kind_raw[0]

    def u16(off: int) -> int:
        return struct.unpack_from("<H", blob, off)[0]

    def elem(attr_id: int) -> Optional[str]:
        text = _utf16(attrs.get(attr_id))
        if not text:
            return ""
        return resolve_element(text)

    comm = {2: 0, 1: 1}.get(blob[143])
    if comm is None:
        return None
    path_len = u16(144)
    path_text = decode_path(blob[146:146 + path_len])
    if path_text is None:
        return None
    path_text = resolve_path(path_text)
    local = elem(0x65)
    dest = elem(0x70)
    if local is None or dest is None:
        return None
    length = u16(139)
    local_index = struct.unpack_from("<I", blob, 340)[0]

    p: Dict[str, str] = {}
    if kind == _KIND_CIP_GENERIC:
        transport = u16(280)
        if transport not in (_TRANSPORT_CIP, _TRANSPORT_CIP_LARGE) or comm != 0:
            return None
        p["MessageType"] = "CIP Generic"
        p["RequestedLength"] = str(length)
        p["ConnectedFlag"] = "2"
        p["ConnectionPath"] = path_text
        p["CommTypeCode"] = "0"
        p["ServiceCode"] = f"16#{u16(330):04x}"
        p["ObjectType"] = f"16#{u16(332):04x}"
        p["TargetObject"] = str(u16(334))
        p["AttributeNumber"] = f"16#{u16(338):04x}"
        p["LocalIndex"] = str(local_index)
        if local:
            p["LocalElement"] = local
        if dest:
            p["DestinationTag"] = dest
        p["LargePacketUsage"] = "true" if transport == _TRANSPORT_CIP_LARGE else "false"
        return p

    if kind == _KIND_SLC:
        message_type = _SLC_TYPES.get(u16(330))
        remote = _utf16(attrs.get(0x67))
        if message_type is None or not remote:
            return None
        if comm == 1 and u16(280) != _TRANSPORT_DHPLUS:
            return None
        p["MessageType"] = message_type
        p["RemoteElement"] = remote
        p["RequestedLength"] = str(length)
        p["ConnectionPath"] = path_text
        p["CommTypeCode"] = str(comm)
        if comm == 1:
            p["Channel"] = f"'{chr(blob[320])}'"
            p["DHPlusSourceLink"] = str(u16(324))
            p["DHPlusDestinationLink"] = str(u16(326))
            p["DHPlusDestinationNode"] = _octal_node(u16(328))
        p["LocalIndex"] = str(local_index)
        if local:
            p["LocalElement"] = local
        if comm == 1:
            if blob[1] not in (0, 2):
                return None
            p["CacheConnections"] = "TRUE" if blob[1] else "FALSE"
        return p
    return None


_PLACEHOLDER = re.compile(r"@([0-9A-Fa-f]{1,8})@")


def resolve_element_placeholders(
    text: str,
    tag_name: Callable[[int], Optional[str]],
    member_name: Callable[[int], Optional[str]],
) -> Optional[str]:
    """``@1ab16c4c@.@4acffcb8@[2]`` -> ``SKT_DATA_Client.Connect_Source[2]``.
    The first placeholder (or any not preceded by ``.``) is a tag object id; one
    after a ``.`` is a UDT member id."""
    failed = False

    def sub(m: "re.Match") -> str:
        nonlocal failed
        oid = int(m.group(1), 16)
        after_dot = m.start() > 0 and text[m.start() - 1] == "."
        name = member_name(oid) if after_dot else tag_name(oid)
        if name is None:
            failed = True
            return m.group(0)
        return name

    out = _PLACEHOLDER.sub(sub, text)
    return None if failed else out


def message_parameters_xml(params: Dict[str, str]) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in params.items())
    return f'<Data Format="Message">\n<MessageParameters {attrs}/>\n</Data>'
