import html
import os
import re
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from sqlite3 import Cursor
from typing import List, Tuple, Dict, Union

from loguru import logger as log

from acd.generated.comps.rx_generic import RxGeneric
from acd.l5x.catalog_numbers import CATALOG_NUMBERS
from acd.l5x.port_structures import PORT_STRUCTURES


# Characters that are illegal in XML 1.0: everything outside
# #x9 | #xA | #xD | #x20-#xD7FF | #xE000-#xFFFD | #x10000-#x10FFFF.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _escape_xml_attr(value: object) -> str:
    """Escape a value for use inside a double-quoted XML attribute.

    Beyond ``html.escape``, this strips characters that are illegal in XML 1.0
    and encodes the legal-but-delimiting whitespace (TAB/CR/LF) as numeric
    character references. Some binary records decode to garbage when an offset
    drifts (e.g. an AOI ``Vendor`` field read with ``errors="replace"``); without
    this, those raw control characters and newlines land verbatim in an
    attribute value, producing non-well-formed L5X that breaks downstream XML
    parsers.
    """
    text = _XML_ILLEGAL_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    return text.replace("\t", "&#x9;").replace("\r", "&#xD;").replace("\n", "&#xA;")


@dataclass
class L5xElementBuilder:
    _cur: Cursor
    _object_id: int = -1


# Maps Python attribute names to L5X XML section wrapper tag names.
# Entries here also control which list attributes are serialized as child sections.
_LIST_SECTION_NAMES = {
    "tags": "Tags",
    "local_tags": "LocalTags",
    "parameters": "Parameters",
    "data_types": "DataTypes",
    "members": "Members",
    "modules": "Modules",
    "programs": "Programs",
    "routines": "Routines",
    "aois": "AddOnInstructionDefinitions",
    "tasks": "Tasks",
    "scheduled_programs": "ScheduledPrograms",
}


@dataclass
class L5xElement:
    _name: str

    def __post_init__(self):
        self._export_name = ""

    def to_xml(self) -> str:
        attribute_list: List[str] = []
        child_list: List[str] = []
        for attribute in self.__dict__:
            if attribute[0] != "_":
                attribute_value = self.__getattribute__(attribute)
                if attribute_value is None:
                    continue
                if isinstance(attribute_value, L5xElement):
                    child_list.append(attribute_value.to_xml())
                elif isinstance(attribute_value, list):
                    if attribute in _LIST_SECTION_NAMES:
                        section_name = _LIST_SECTION_NAMES[attribute]
                        new_child_list: List[str] = []
                        for element in attribute_value:
                            if isinstance(element, L5xElement):
                                if getattr(element, "_l5x_exclude", False):
                                    continue
                                new_child_list.append(element.to_xml())
                            else:
                                new_child_list.append(f"<{element}/>")
                        child_list.append(
                            f'<{section_name}>{"".join(new_child_list)}</{section_name}>'
                        )
                else:
                    if attribute == "cls":
                        attribute = "class"
                    if isinstance(attribute_value, bool):
                        attribute_value = str(attribute_value).lower()
                    _overrides = getattr(self, "_xml_attr_overrides", {})
                    xml_attr_name = _overrides.get(attribute, attribute.title().replace("_", ""))
                    attribute_list.append(
                        f'{xml_attr_name}="{_escape_xml_attr(attribute_value)}"'
                    )

        _export_name = (
            getattr(self, "_export_name", "") or self.__class__.__name__.title().replace("_", "")
        )
        return f'<{_export_name} {" ".join(attribute_list)}>{"".join(child_list)}</{_export_name}>'


@dataclass
class Member(L5xElement):
    name: str
    data_type: str
    dimension: int
    radix: str
    hidden: bool
    target: Union[str, None]      # BIT members only; None omits the attribute
    bit_number: Union[int, None]  # BIT members only; None omits the attribute
    external_access: str
    byte_offset: int = 0
    _description: Union[str, None] = field(default=None)

    @property
    def description(self) -> Union[str, None]:
        if self._description is None:
            return None
        return ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()

    def to_xml(self) -> str:
        base = super().to_xml()
        desc = self.description
        if not desc:
            return base
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class DataType(L5xElement):
    name: str
    family: str
    cls: str
    members: List[Member]
    _description: Union[str, None] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "DataType"

    @property
    def _l5x_exclude(self) -> bool:
        return self.cls == "ProductDefined" or ":" in self.name

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc = ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


# Maps primitive DataType names to their L5K zero-default value string.
# UDT, STRING, ALARM_DIGITAL, MESSAGE, and array types are intentionally omitted —
# they require complex structured L5K encoding that is not yet implemented.
_PRIMITIVE_L5K_ZERO: Dict[str, str] = {
    "BOOL":  "0",
    "SINT":  "0",
    "INT":   "0",
    "DINT":  "0",
    "LINT":  "0",
    "USINT": "0",
    "UINT":  "0",
    "UDINT": "0",
    "ULINT": "0",
    "REAL":  "0.00000000e+000",
    "LREAL": "0.00000000e+000",
}

# Radix string used in Decorated DataValueMember for each numeric primitive.
# BOOL and BIT use no Radix attribute; REAL/LREAL use "Float"; all integers use "Decimal".
_PRIMITIVE_RADIX: Dict[str, str] = {
    "SINT":  "Decimal",
    "INT":   "Decimal",
    "DINT":  "Decimal",
    "LINT":  "Decimal",
    "USINT": "Decimal",
    "UINT":  "Decimal",
    "UDINT": "Decimal",
    "ULINT": "Decimal",
    "REAL":  "Float",
    "LREAL": "Float",
}

# Default zero value string for each primitive in Decorated output.
_PRIMITIVE_DECORATED_ZERO: Dict[str, str] = {
    "BOOL":  "0",
    "BIT":   "0",
    "SINT":  "0",
    "INT":   "0",
    "DINT":  "0",
    "LINT":  "0",
    "USINT": "0",
    "UINT":  "0",
    "UDINT": "0",
    "ULINT": "0",
    "REAL":  "0.0",
    "LREAL": "0.0",
}

# Built-in Logix struct types that are not in the user DataType list.
# Each entry is a list of (member_name, member_data_type) tuples.
# Only non-hidden, visible members are listed (as they appear in Decorated output).
_BUILTIN_STRUCT_MEMBERS: Dict[str, List[Tuple[str, str]]] = {
    "TIMER": [
        ("PRE", "DINT"), ("ACC", "DINT"),
        ("EN", "BOOL"), ("TT", "BOOL"), ("DN", "BOOL"),
    ],
    "COUNTER": [
        ("PRE", "DINT"), ("ACC", "DINT"),
        ("CU", "BOOL"), ("CD", "BOOL"), ("DN", "BOOL"), ("OV", "BOOL"), ("UN", "BOOL"),
    ],
    "CONTROL": [
        ("LEN", "DINT"), ("POS", "DINT"),
        ("EN", "BOOL"), ("EU", "BOOL"), ("DN", "BOOL"), ("EM", "BOOL"),
        ("ER", "BOOL"), ("UL", "BOOL"), ("IN", "BOOL"), ("FD", "BOOL"),
    ],
}

# Types for which we emit no Decorated element at all (they use other formats).
_SKIP_DECORATED: set = {
    "ALARM_DIGITAL", "MESSAGE", "AXIS_SERVO", "PID_ENHANCED",
    "AXIS_CIP_DRIVE", "MOTION_GROUP",
}


def _member_decorated_xml(member_name: str, member_dt: str, member_dim: int,
                           data_types_map: Dict[str, "DataType"]) -> str:
    """Return the Decorated XML fragment for a single UDT member.

    member_dt:  the DataType name of the member (already upper-cased by caller)
    member_dim: array dimension (0 = scalar)
    """
    if member_dim > 0:
        # Array member
        return _array_member_xml(member_name, member_dt, member_dim, data_types_map)

    if member_dt in ("BOOL", "BIT"):
        return f'<DataValueMember Name="{member_name}" DataType="BOOL" Value="0"/>'

    radix = _PRIMITIVE_RADIX.get(member_dt)
    zero = _PRIMITIVE_DECORATED_ZERO.get(member_dt)
    if radix is not None and zero is not None:
        return f'<DataValueMember Name="{member_name}" DataType="{member_dt}" Radix="{radix}" Value="{zero}"/>'

    # Struct member (nested UDT, TIMER, COUNTER, etc.)
    inner = _struct_members_xml(member_dt, data_types_map)
    if inner is None:
        return ""  # unknown / skip
    return f'<StructureMember Name="{member_name}" DataType="{member_dt}">{inner}</StructureMember>'


def _array_member_xml(member_name: str, member_dt: str, dim: int,
                      data_types_map: Dict[str, "DataType"]) -> str:
    """Generate an <ArrayMember> element for a member that is an array."""
    radix = _PRIMITIVE_RADIX.get(member_dt)
    zero = _PRIMITIVE_DECORATED_ZERO.get(member_dt)
    is_bool = member_dt in ("BOOL", "BIT")

    if is_bool:
        elems = "".join(
            f'<Element Index="[{i}]" Value="0"/>' for i in range(dim)
        )
        return (
            f'<ArrayMember Name="{member_name}" DataType="BOOL" Dimensions="{dim}" Radix="Decimal">'
            f'{elems}'
            f'</ArrayMember>'
        )

    if radix is not None and zero is not None:
        elems = "".join(
            f'<Element Index="[{i}]" Value="{zero}"/>' for i in range(dim)
        )
        return (
            f'<ArrayMember Name="{member_name}" DataType="{member_dt}" Dimensions="{dim}" Radix="{radix}">'
            f'{elems}'
            f'</ArrayMember>'
        )

    # Array of structs
    inner = _struct_members_xml(member_dt, data_types_map)
    if inner is None:
        return ""
    struct_xml = f'<Structure DataType="{member_dt}">{inner}</Structure>'
    elems = "".join(
        f'<Element Index="[{i}]">{struct_xml}</Element>' for i in range(dim)
    )
    return (
        f'<ArrayMember Name="{member_name}" DataType="{member_dt}" Dimensions="{dim}">'
        f'{elems}'
        f'</ArrayMember>'
    )


def _struct_members_xml(dt_name: str, data_types_map: Dict[str, "DataType"]) -> Union[str, None]:
    """Return the inner XML for a Structure/StructureMember of the given DataType.

    Returns None if the type is unknown or should be skipped.
    The returned string does NOT include the outer <Structure> wrapper.
    """
    if dt_name in _SKIP_DECORATED:
        return None

    # Handle STRING and custom string-family types (e.g. a user-defined type
    # named STRING_20 or ASCII_TWENTY, detected via the family flag rather
    # than the type's name) uniformly: LEN (DINT) + DATA (ASCII text). Studio
    # 5000 shows the DATA member's own DataType as the *outer* string type's
    # name (e.g. DataType="STRING_20"), not a generic "STRING" literal.
    if _is_string_family_type(dt_name, data_types_map):
        return (
            '<DataValueMember Name="LEN" DataType="DINT" Radix="Decimal" Value="0"/>'
            f'<DataValueMember Name="DATA" DataType="{dt_name}" Radix="ASCII">'
            f'{_string_literal_cdata("")}</DataValueMember>'
        )

    # Built-in struct types (TIMER, COUNTER, CONTROL)
    builtin_members = _BUILTIN_STRUCT_MEMBERS.get(dt_name)
    if builtin_members is not None:
        parts: List[str] = []
        for mname, mdt in builtin_members:
            radix = _PRIMITIVE_RADIX.get(mdt)
            zero = _PRIMITIVE_DECORATED_ZERO.get(mdt)
            if radix is not None and zero is not None:
                parts.append(
                    f'<DataValueMember Name="{mname}" DataType="{mdt}" Radix="{radix}" Value="{zero}"/>'
                )
            else:
                # BOOL member
                parts.append(f'<DataValueMember Name="{mname}" DataType="{mdt}" Value="0"/>')
        return "".join(parts)

    # User-defined type: look up in data_types_map
    dt_obj = data_types_map.get(dt_name)
    if dt_obj is None:
        return None

    parts = []
    for member in dt_obj.members:
        if member.hidden:
            continue
        mdt = member.data_type.upper()
        mname = member.name
        mdim = member.dimension

        fragment = _member_decorated_xml(mname, mdt, mdim, data_types_map)
        if fragment:
            parts.append(fragment)
    return "".join(parts)


def _build_elem_comments(tag_name: str, comments: List[Tuple[str, str]]) -> Dict[int, List[str]]:
    """Build a mapping from array element index to list of comment texts.

    Matches comment refs like ``tag_name[14]`` or ``tag_name[0].5``
    to the element index (the ``N`` in ``[N]``).
    """
    result: Dict[int, List[str]] = {}
    for ref, text in comments:
        if not ref or ref == ".":
            continue
        if ref.startswith(tag_name):
            suffix = ref[len(tag_name):]
            if suffix.startswith("["):
                end_bracket = suffix.find("]")
                if end_bracket > 0:
                    try:
                        idx = int(suffix[1:end_bracket])
                        result.setdefault(idx, []).append(text)
                    except ValueError:
                        pass
    return result


def _generate_decorated(dt_base: str, dimensions: Union[str, None],
                        data_types_map: Dict[str, "DataType"],
                        tag_name: str = "", comments: Union[List[Tuple[str, str]], None] = None) -> str:
    """Generate a complete <Data Format="Decorated"> XML string for a tag.

    dt_base:    the base DataType name (uppercase, array brackets already stripped)
    dimensions: comma-separated dimension string (e.g. "100" or "4,8") or None for scalar
    tag_name:   the owning tag name (used for comment matching)
    comments:   list of (ref, text) tuples from the tag's _comments field
    Returns "" if this type should not have a Decorated element.
    """
    if dt_base in _SKIP_DECORATED:
        return ""

    # dt_base is always upper-cased (matching data_types_map's key convention),
    # but Studio 5000 shows the DataType attribute in its real original casing
    # (e.g. "LugWrk", not "LUGWRK") -- recover it from the DataType object
    # itself when there is one (built-in reserved keywords like TIMER/STRING
    # have no ACD DataType record and are canonically all-caps anyway).
    dt_obj_for_name = data_types_map.get(dt_base)
    display_name = dt_obj_for_name.name if dt_obj_for_name is not None else dt_base

    if dimensions is None:
        # Scalar struct
        inner = _struct_members_xml(dt_base, data_types_map)
        if inner is None:
            return ""
        body = f'<Structure DataType="{display_name}">{inner}</Structure>'
    else:
        # Array tag: parse dimensions (up to 3D, comma-separated)
        dim_parts = [int(d) for d in dimensions.split(",") if d.strip().isdigit()]
        if not dim_parts:
            return ""

        # For multi-dimensional arrays the total element count is the product.
        # Multi-dimensional element indices are a single comma-separated list
        # inside one bracket pair (e.g. "[1,2,0]"), NOT separate brackets per
        # dimension (verified against a real 3D UDT array tag) -- Dimensions=
        # itself is also comma-separated on the <Array> element specifically
        # (the top-level <Tag Dimensions="..."> attribute uses spaces instead,
        # handled separately in Tag.to_xml's base attribute rendering).
        total = 1
        for d in dim_parts:
            total *= d

        dim_str = ",".join(str(d) for d in dim_parts)

        radix = _PRIMITIVE_RADIX.get(dt_base)
        zero = _PRIMITIVE_DECORATED_ZERO.get(dt_base)
        is_bool = dt_base in ("BOOL", "BIT")

        if is_bool:
            # BOOL array: flat indexed elements with Radix="Decimal"
            def _bool_elems(parts: List[int], remaining: List[int]) -> str:
                if not remaining:
                    idx = "[" + ",".join(str(p) for p in parts) + "]"
                    return f'<Element Index="{idx}" Value="0"/>'
                return "".join(
                    _bool_elems(parts + [i], remaining[1:]) for i in range(remaining[0])
                )
            elems = _bool_elems([], dim_parts)
            body = f'<Array DataType="BOOL" Dimensions="{dim_str}" Radix="Decimal">{elems}</Array>'

        elif radix is not None and zero is not None:
            # Primitive array (DINT, REAL, etc.)
            def _prim_elems(parts: List[int], remaining: List[int]) -> str:
                if not remaining:
                    idx = "[" + ",".join(str(p) for p in parts) + "]"
                    return f'<Element Index="{idx}" Value="{zero}"/>'
                return "".join(
                    _prim_elems(parts + [i], remaining[1:]) for i in range(remaining[0])
                )
            elems = _prim_elems([], dim_parts)
            body = f'<Array DataType="{dt_base}" Dimensions="{dim_str}" Radix="{radix}">{elems}</Array>'

        else:
            # Struct array (UDT, TIMER, COUNTER, STRING, ...)
            inner = _struct_members_xml(dt_base, data_types_map)
            if inner is None:
                return ""
            struct_xml = f'<Structure DataType="{display_name}">{inner}</Structure>'

            def _struct_elems(parts: List[int], remaining: List[int]) -> str:
                if not remaining:
                    idx = "[" + ",".join(str(p) for p in parts) + "]"
                    return f'<Element Index="{idx}">{struct_xml}</Element>'
                return "".join(
                    _struct_elems(parts + [i], remaining[1:]) for i in range(remaining[0])
                )
            elems = _struct_elems([], dim_parts)
            body = f'<Array DataType="{display_name}" Dimensions="{dim_str}">{elems}</Array>'

    # Inject <Comment> children for array elements that have inline comments.
    elem_comments = _build_elem_comments(tag_name, comments) if comments else {}
    if comments and tag_name and elem_comments:
        def _inject_comment(m):
            idx_str = m.group(1)
            try:
                idx = int(idx_str)
                if idx in elem_comments:
                    cxml = "".join(
                        f'<Comment><![CDATA[{c}]]></Comment>' for c in elem_comments[idx]
                    )
                    return f'<Element Index="[{idx}]">{cxml}</Element>'
            except ValueError:
                pass
            return m.group(0)

        body = re.sub(r'<Element Index="\[(\d+)\]"\s*/>', _inject_comment, body)

    return f'<Data Format="Decorated">\n{body}\n</Data>'


def _udt_has_non_zero(d: dict) -> bool:
    """Recursively check if a decoded UDT element has any non-zero values."""
    for v in d.values():
        if isinstance(v, dict):
            if _udt_has_non_zero(v):
                return True
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    if _udt_has_non_zero(item):
                        return True
                elif item != 0 and item != "" and item is not None:
                    return True
        elif v != 0 and v != "" and v is not None:
            return True
    return False


def _udt_scalar_to_xml(dt_name: str, values: dict,
                        data_types_map: Dict[str, 'DataType']) -> str:
    """Generate inner member XML for a decoded UDT scalar.

    Returns a string of ``<DataValueMember>``, ``<StringValueMember>``,
    ``<ArrayMember>``, and ``<StructureMember>`` elements (no outer
    ``<Structure>`` wrapper).
    """
    # String-family types (built-in STRING or a custom type like STRING_20)
    # are always represented as {"LEN": int, "DATA": str} regardless of
    # nesting level (top-level tag, array element, or member nested inside
    # another struct) -- render the same LEN/DATA shape used everywhere else,
    # with the DATA member's DataType matching the outer type's own name.
    if _is_string_family_type(dt_name, data_types_map):
        length = values.get("LEN", 0)
        text = values.get("DATA", "")
        return (
            f'<DataValueMember Name="LEN" DataType="DINT" Radix="Decimal" Value="{length}"/>'
            f'<DataValueMember Name="DATA" DataType="{dt_name}" Radix="ASCII">'
            f'{_string_literal_cdata(text)}</DataValueMember>'
        )

    dt_obj = data_types_map.get(dt_name.upper())
    if dt_obj is None:
        return ""

    parts: List[str] = []
    for member in dt_obj.members:
        if member.hidden or member.data_type == "BIT":
            continue
        mname = member.name
        mdt = member.data_type
        mdt_upper = mdt.upper()
        val = values.get(mname)

        if val is None:
            continue

        if isinstance(val, dict):
            # Nested UDT
            inner = _udt_scalar_to_xml(mdt, val, data_types_map)
            if inner:
                parts.append(
                    f'<StructureMember Name="{mname}" DataType="{mdt}">{inner}</StructureMember>'
                )

        elif isinstance(val, list) and val and isinstance(val[0], dict):
            # Array of nested UDTs
            inner_parts: List[str] = []
            for i, elem in enumerate(val):
                struct = _udt_scalar_to_xml(mdt, elem, data_types_map)
                inner_parts.append(
                    f'<Element Index="[{i}]"><Structure DataType="{mdt}">{struct}</Structure></Element>'
                )
            parts.append(
                f'<ArrayMember Name="{mname}" DataType="{mdt}" Dimensions="{len(val)}">'
                f'{"".join(inner_parts)}</ArrayMember>'
            )

        elif isinstance(val, list):
            # Array of primitives
            radix = _PRIMITIVE_RADIX.get(mdt_upper, "Decimal")
            zero = _PRIMITIVE_DECORATED_ZERO.get(mdt_upper, "0")
            elems = "".join(
                f'<Element Index="[{i}]" Value="{v}"/>' for i, v in enumerate(val)
            )
            parts.append(
                f'<ArrayMember Name="{mname}" DataType="{mdt}" '
                f'Dimensions="{len(val)}" Radix="{radix}">'
                f'{elems}</ArrayMember>'
            )

        elif mdt_upper in ("BOOL", "BIT"):
            parts.append(
                f'<DataValueMember Name="{mname}" DataType="BOOL" '
                f'Value="{"1" if val else "0"}"/>'
            )

        else:
            radix = _PRIMITIVE_RADIX.get(mdt_upper, "Decimal")
            parts.append(
                f'<DataValueMember Name="{mname}" DataType="{mdt}" '
                f'Radix="{radix}" Value="{val}"/>'
            )

    return "".join(parts)


def _udt_array_to_xml(tag_name: str, dt_base: str, values: List[dict],
                       dim_str: str,
                       data_types_map: Dict[str, 'DataType']) -> str:
    """Generate an ``<Array>`` XML fragment for a decoded UDT array tag.

    For a 1-D array, trailing all-zero elements are omitted (matches
    verified Studio 5000 behavior for primitive/UDT 1-D arrays). For a
    multi-dimensional array (comma-separated dim_str, e.g. "4,3,2"), no
    truncation is applied -- every element is emitted with a comma-separated
    Index (e.g. "[3,2,1]"), and the flat *values* list (row-major, matching
    _decode_udt_initial_value's flat iteration and the ACD's own storage
    order) is mapped back to per-dimension indices -- verified against a
    real 3-D UDT array tag, where Studio 5000 shows all elements
    untruncated.
    """
    dt_obj_for_name = data_types_map.get(dt_base.upper())
    display_name = dt_obj_for_name.name if dt_obj_for_name is not None else dt_base

    dim_parts = [int(d) for d in dim_str.split(",") if d.strip().isdigit()]

    if len(dim_parts) > 1:
        # Multi-dimensional: emit every element, row-major indices.
        elems: List[str] = []
        for flat_idx, val in enumerate(values):
            # Compute per-dimension indices via successive divmod using each
            # dimension's own "weight" (product of the dimensions after it).
            weights = []
            acc = 1
            for d in reversed(dim_parts):
                weights.insert(0, acc)
                acc *= d
            idx_parts = []
            remaining = flat_idx
            for w in weights:
                idx_parts.append(remaining // w)
                remaining %= w
            idx = "[" + ",".join(str(p) for p in idx_parts) + "]"
            struct = _udt_scalar_to_xml(dt_base, val, data_types_map)
            elems.append(
                f'<Element Index="{idx}">'
                f'<Structure DataType="{display_name}">{struct}</Structure>'
                f'</Element>'
            )
        return (
            f'<Array Name="{tag_name}" DataType="{display_name}" Dimensions="{dim_str}">'
            f'{"".join(elems)}</Array>'
        )

    non_zero_end = 0
    for i in range(len(values) - 1, -1, -1):
        if _udt_has_non_zero(values[i]):
            non_zero_end = i + 1
            break
    non_zero_end = max(non_zero_end, 1)

    elems: List[str] = []
    for i in range(non_zero_end):
        struct = _udt_scalar_to_xml(dt_base, values[i], data_types_map)
        elems.append(
            f'<Element Index="[{i}]">'
            f'<Structure DataType="{display_name}">{struct}</Structure>'
            f'</Element>'
        )

    return (
        f'<Array Name="{tag_name}" DataType="{display_name}" Dimensions="{dim_str}">'
        f'{"".join(elems)}</Array>'
    )


_PRIM = {
    'BOOL':  ('B',   1),
    'SINT':  ('<b',  1),
    'INT':   ('<h',  2),
    'DINT':  ('<i',  4),
    'LINT':  ('<q',  8),
    'BYTE':  ('B',   1),
    'WORD':  ('<H',  2),
    'DWORD': ('<I',  4),
    'LWORD': ('<Q',  8),
    'REAL':  ('<f',  4),
    'LREAL': ('<d',  8),
}

# Logix STRING struct: DINT LEN (4 bytes) + SINT[82] DATA (82 bytes) + 2 bytes padding.
_STRING_SIZE = 88


def _string_literal_cdata(text: str) -> str:
    """Format a decoded string value as Rockwell's quoted-literal CDATA content.

    Studio 5000 renders a non-empty STRING/string-family DATA member's text
    as ``<![CDATA['the text']]>`` -- wrapped in a CDATA section AND in
    literal single quotes (matching its L5K string-literal convention), with
    any embedded single quote doubled (Pascal/Ada-style escaping). An empty
    string renders as bare ``<![CDATA[]]>`` with no quotes at all. Both
    verified against a real Studio 5000 L5X export.
    """
    if not text:
        return "<![CDATA[]]>"
    escaped = text.replace("'", "''")
    safe = Tag._sanitize_xml_text(escaped)
    return f"<![CDATA['{safe}']]>"


def _l5k_string_padded(text: str, capacity: int) -> str:
    """Format a string value as Rockwell's L5K array-literal string content.

    A scalar STRING/string-family tag's ``Data Format="L5K"`` block encodes
    the value as ``[LEN,'text$00$00...']`` -- the real text characters
    followed by one ``$00`` token per unused byte, padded out to the type's
    full declared capacity (e.g. 82 for the built-in STRING), verified
    against a real Studio 5000 L5X export. Any literal ``$`` or ``'`` in the
    text itself is escaped the same way ($-prefixed) per Rockwell's L5K
    string-literal convention.
    """
    escaped = text.replace("$", "$$").replace("'", "$'")
    pad = "$00" * max(capacity - len(text), 0)
    return f"'{escaped}{pad}'"


def _is_string_family_type(type_name: str, data_types_map: Dict[str, 'DataType']) -> bool:
    """Return True if type_name is the built-in STRING type or a custom
    string-family DataType (e.g. STRING20, STRING64, ...).

    Custom string-family types are detected generically via the DataType's own
    ``family`` flag (read from extended record 0x6C in the ACD binary by
    DataTypeBuilder), not by matching against a hardcoded name -- so any
    project-defined string type of any name/length is recognized correctly.
    """
    t = type_name.upper()
    if t == "STRING":
        return True
    dt = data_types_map.get(t)
    return dt is not None and dt.family == "StringFamily"


def _string_family_capacity(type_name: str, data_types_map: Dict[str, 'DataType']) -> int:
    """Return the configured character capacity of a string-family type.

    82 for the built-in STRING type. For a custom string-family type, reads
    the dimension of its DATA member from the ACD's own type definition
    (rather than assuming the default 82-character length).
    """
    t = type_name.upper()
    if t == "STRING":
        return 82
    dt = data_types_map.get(t)
    if dt is not None:
        for m in dt.members:
            if m.name.upper() == "DATA":
                return m.dimension
    return 82


def _get_type_size(type_name: str, data_types_map: Dict[str, 'DataType']) -> int:
    """Return the byte size of a DataType (primitive, STRING, or UDT).

    For UDTs, computes max(byte_offset + elem_size * max(dimension,1))
    across non-hidden non-BIT members.
    Returns 0 for unknown types.
    """
    t = type_name.upper()
    prim = _PRIM.get(t)
    if prim is not None:
        return prim[1]
    if t == "STRING":
        return _STRING_SIZE
    dt = data_types_map.get(t)
    if dt is None:
        return 0
    max_end = 0
    for m in dt.members:
        if m.hidden or m.data_type == "BIT":
            continue
        if m.data_type.upper() == "BOOL" and m.dimension > 0:
            # BOOL arrays are bit-packed into DINT-sized (4-byte) words, 32
            # bits per word -- NOT one byte per element. Verified against a
            # real UDT (LugWrk) whose true total size only matches when a
            # trailing BOOL[32] member is sized as 4 bytes, not 32; without
            # this, every member/struct size computed *after* a BOOL array
            # member is wrong, corrupting offsets for any subsequent element
            # in an array of that struct.
            member_end = m.byte_offset + (((m.dimension + 31) // 32) * 4)
        else:
            elem_sz = _get_type_size(m.data_type, data_types_map)
            if elem_sz == 0:
                continue
            member_end = m.byte_offset + (elem_sz * max(m.dimension, 1))
        max_end = max(max_end, member_end)
    return max_end if max_end > 0 else 0


def _read_tag_initial_value(cur: Cursor, data_table_instance: int,
                            data_type: str, n_elements: int):
    """Read the initial value(s) of a tag from its data-table blob in the comps table.

    Returns None if the type is not supported or the blob cannot be read.
    For scalar tags (n_elements <= 1), returns a single Python int/float.
    For array tags (n_elements > 1), returns a list of values.
    """
    dt_upper = data_type.upper() if data_type else ""
    # Strip array brackets for type lookup, but keep n_elements from caller.
    base_dt = re.sub(r'\[.*\]', '', dt_upper).strip()

    pack_fmt_info = _PRIM.get(base_dt)
    if pack_fmt_info is None:
        return None

    fmt, elem_size = pack_fmt_info
    expected_bytes = n_elements * elem_size

    cur.execute(
        "SELECT record FROM comps WHERE object_id = ?", (data_table_instance,)
    )
    rows = cur.fetchall()
    if not rows:
        return None

    raw_rec = bytes(rows[0][0])

    # Determine read offset based on whether this is an array or scalar.
    offset = 0x1A2 if n_elements > 1 else 0x19E

    values = []
    for i in range(n_elements):
        byte_off = offset + i * elem_size
        if byte_off + elem_size <= len(raw_rec):
            val = struct.unpack_from(fmt, raw_rec, byte_off)[0]
            # BOOL is stored as unsigned byte (B) — return int 0/1.
            values.append(val)
        else:
            # Pad with zero for truncated trailing elements.
            if fmt == 'B':
                values.append(0)
            else:
                values.append(0)

    if n_elements == 1:
        return values[0]
    return values


def _decode_udt_initial_value(
    cur: Cursor,
    data_table_instance: int,
    data_type_name: str,
    n_elements: int,
    data_types_map: Dict[str, 'DataType'],
) -> Union[dict, list, None]:
    """Decode initial values for a struct-typed tag from the data table blob.

    Works for any DataType found in data_types_map -- user-defined UDTs,
    Rockwell "ProductDefined"/module-defined types (e.g. AB:1794_IB32:I:0),
    and string-family types (built-in STRING or a custom type like
    STRING_20, decoded to {"LEN": int, "DATA": str}) -- since all of these
    are read from the same generic member/byte_offset structure and the
    same data-table blob layout (verified against real project data,
    including a real non-blank custom string-family array tag).

    Returns a dict for scalar structs, a list of dicts for struct arrays,
    or None if the type is unknown or cannot be decoded.
    """
    if not data_type_name:
        return None
    if n_elements > 10000:
        return None

    base_dt = re.sub(r'\[.*\]', '', data_type_name).strip()
    dt_obj = data_types_map.get(base_dt.upper())
    if dt_obj is None:
        return None

    cur.execute(
        "SELECT record FROM comps WHERE object_id = ?", (data_table_instance,)
    )
    rows = cur.fetchall()
    if not rows:
        return None
    raw_rec = bytes(rows[0][0])

    struct_size = _get_type_size(base_dt.upper(), data_types_map)
    if struct_size == 0:
        return None

    offset = 0x1A2

    results: list = []
    for elem_idx in range(n_elements):
        base = offset + elem_idx * struct_size
        decoded = _decode_single_udt_element(
            raw_rec, base, dt_obj, data_types_map, 0
        )
        results.append(decoded)

    if n_elements == 1:
        return results[0]
    return results


def _decode_string_family_value(
    blob: bytes, offset: int, type_name: str, data_types_map: Dict[str, 'DataType'],
) -> dict:
    """Decode a string-family struct's LEN+DATA fields from *blob* at *offset*.

    Returns {"LEN": int, "DATA": str} -- the same shape Studio 5000 uses to
    represent a STRING/string-family value at any nesting level (top-level
    tag, array element, or a member nested inside another struct), so the
    generic UDT-rendering code can treat it identically everywhere via
    _udt_scalar_to_xml's own string-family check.
    """
    cap = _string_family_capacity(type_name, data_types_map)
    if offset + 4 > len(blob):
        return {"LEN": 0, "DATA": ""}
    length = struct.unpack_from("<i", blob, offset)[0]
    length = max(0, min(length, cap))
    text = ""
    if offset + 4 + length <= len(blob):
        raw = blob[offset + 4: offset + 4 + length]
        text = raw.decode("utf-8", errors="replace")
    return {"LEN": length, "DATA": text}


def _decode_single_udt_element(
    blob: bytes,
    base_offset: int,
    data_type: 'DataType',
    data_types_map: Dict[str, 'DataType'],
    depth: int,
    _max_depth: int = 3,
) -> dict:
    """Decode one UDT element from *blob* at *base_offset*.

    Returns {member_name: value} for non-hidden, non-BIT members.
    Returns an empty dict when *depth* exceeds ``_max_depth``.
    """
    if depth > _max_depth:
        return {}

    if data_type.family == "StringFamily":
        return _decode_string_family_value(blob, base_offset, data_type.name, data_types_map)

    result: dict = {}
    for member in data_type.members:
        if member.hidden or member.data_type == "BIT":
            continue

        mname = member.name
        mdt = member.data_type
        mdt_upper = mdt.upper()
        off = base_offset + member.byte_offset

        bn = member.bit_number if mdt_upper == "BOOL" else None
        if member.dimension > 0:
            elem_size = _get_type_size(mdt_upper, data_types_map)
            if elem_size == 0:
                continue
            arr: list = []
            for i in range(member.dimension):
                elem_off = off + i * elem_size
                val = _decode_scalar_member(
                    blob, elem_off, mdt, data_types_map, depth + 1,
                    bit_number=bn,
                )
                arr.append(val)
            result[mname] = arr
        else:
            val = _decode_scalar_member(
                blob, off, mdt, data_types_map, depth + 1,
                bit_number=bn,
            )
            result[mname] = val

    return result


def _decode_scalar_member(
    blob: bytes,
    offset: int,
    data_type: str,
    data_types_map: Dict[str, 'DataType'],
    depth: int,
    bit_number: Union[int, None] = None,
):
    """Decode a single scalar member value from *blob* at *offset*.

    Handles primitives and nested UDTs directly. String-family types
    (built-in STRING or a custom type like STRING_20) return a
    {"LEN": int, "DATA": str} dict -- the same shape used at every other
    nesting level -- rather than a bare string, so callers always get a
    consistent struct-like value for any string-family type.
    Returns 0 for out-of-range primitive reads, ``None`` for unknown types.

    When *bit_number* is provided for a BOOL member, extracts that
    specific bit from the packed byte (Logix packs 8 BOOLs per byte
    in UDT data tables).
    """
    mdt_upper = data_type.upper()
    prim = _PRIM.get(mdt_upper)
    if prim is not None:
        fmt, sz = prim
        if offset + sz <= len(blob):
            val = struct.unpack_from(fmt, blob, offset)[0]
            if bit_number is not None and mdt_upper == "BOOL":
                val = (val >> bit_number) & 1
            return val
        return 0

    if _is_string_family_type(mdt_upper, data_types_map):
        return _decode_string_family_value(blob, offset, data_type, data_types_map)

    dt_obj = data_types_map.get(mdt_upper)
    if dt_obj is not None and depth <= 3:
        return _decode_single_udt_element(
            blob, offset, dt_obj, data_types_map, depth + 1
        )

    return None


def _count_array_elements(dimensions: Union[str, None]) -> int:
    """Return the total element count for a tag based on its dimensions string.

    Returns 1 for scalars (None or empty), or the product of dimension values.
    """
    if not dimensions:
        return 1
    try:
        parts = [int(d) for d in dimensions.split(",") if d.strip().isdigit()]
        count = 1
        for p in parts:
            count *= p
        return max(count, 1)
    except (ValueError, TypeError):
        return 1


@dataclass
class Tag(L5xElement):
    name: str
    tag_type: str
    data_type: str
    radix: Union[str, None]
    external_access: str
    constant: Union[str, None]  # "true" for constants; None omits the attribute
    dimensions: Union[str, None]
    target: Union[str, None] = None  # AliasFor target; None for non-alias tags
    _data_table_instance: int = 0
    _comments: List[Tuple[str, str]] = field(default_factory=list)
    _initial_value: Union[List, int, float, None] = None
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        if self.tag_type == "Alias":
            self._xml_attr_overrides = {"tag_type": "TagType", "target": "AliasFor"}

    @property
    def description(self) -> Union[str, None]:
        raw = next((d for p, d in self._comments if p == ''), None)
        if raw is None:
            return None
        return ' '.join(raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()

    @property
    def _l5x_exclude(self) -> bool:
        """Exclude tags with empty or non-identifier names (hex-address placeholders, etc.)."""
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
            or ":" in self.name
            or self.name.startswith("__l0")
            or self.name.startswith("__CLONE")
        )

    @staticmethod
    def _sanitize_xml_text(text: str) -> str:
        """Encode characters illegal in XML 1.0 as XML character references (&#xNN;).

        XML 1.0 allows only #x9, #xA, #xD, and #x20–#xD7FF, #xE000–#xFFFD.
        Control characters outside that range (e.g. #x02 STX) are emitted as
        character references rather than stripped, matching Logix Designer output.
        """
        parts = []
        for ch in text:
            cp = ord(ch)
            if ch in ("\t", "\n", "\r") or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD):
                parts.append(ch)
            else:
                parts.append(f"&#x{cp:04X};")
        return "".join(parts)

    def to_xml(self) -> str:
        base = super().to_xml()

        # The <Tag> element's own Dimensions attribute uses SPACE-separated
        # values (e.g. Dimensions="4 3 2"), verified against a real 3D-array
        # tag's L5X export -- unlike the comma-separated storage format used
        # internally everywhere else (array indexing, <Array Dimensions=...>,
        # etc., which stays comma-separated and is unaffected by this).
        if self.dimensions:
            base = base.replace(
                f'Dimensions="{self.dimensions}"',
                f'Dimensions="{self.dimensions.replace(",", " ")}"',
            )

        # An Alias tag never shows its own DataType attribute (it has no
        # value of its own -- Studio 5000 infers the type from AliasFor) --
        # verified against real alias tags of multiple underlying types
        # (REAL, BOOL, ...). Only strip it from the opening tag itself;
        # self.data_type is still needed internally elsewhere (unused here
        # since data_xml is skipped entirely for Alias tags below).
        if self.tag_type == "Alias" and self.data_type:
            base = re.sub(r'\s*DataType="' + re.escape(self.data_type) + r'"', '', base, count=1)

        # --- Description child element ---
        # Find tag-level description: empty tag_reference means the tag itself.
        # Tags with multiple empty-ref entries (e.g. array-element bit descriptions
        # alongside the tag description) store the real tag description as the
        # longest entry — short entries like "Spare" or "End CIP" are element labels.
        candidates = [text for ref, text in self._comments if ref in ("", ".") and text]
        desc_raw = max(candidates, key=len) if candidates else None
        if desc_raw is not None:
            desc_raw = ' '.join(desc_raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
        desc = self._sanitize_xml_text(desc_raw) if desc_raw else None
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>' if desc else ""

        # --- Data child element(s) ---
        # If _initial_value is available for a primitive type, emit <Data Format="Decorated">
        # with the actual values (scalar or array). Otherwise fall through to the default
        # zero-value L5K/Decorated rendering.
        # Alias tags never get a <Data> element at all (verified against real
        # alias tags) -- they have no value of their own, only a reference.
        data_xml = ""
        is_alias = self.tag_type == "Alias"

        if not is_alias and self._initial_value is not None:
            dt_base = self.data_type.split("[")[0].upper() if self.data_type else ""
            iv = self._initial_value

            if not self.dimensions and isinstance(iv, dict) and _is_string_family_type(dt_base, self._data_types_map):
                # Scalar string-family tag (built-in STRING or a custom type
                # like STRING_20): Studio 5000 never uses Decorated for these
                # -- only Data Format="L5K" (array-literal [LEN,'text...'],
                # padded with $00 to the type's full capacity) and a separate
                # Data Format="String" Length="N" block with the plain quoted
                # text, verified against a real scalar STRING tag's L5X.
                length = iv.get("LEN", 0)
                text = iv.get("DATA", "")
                cap = _string_family_capacity(dt_base, self._data_types_map)
                l5k_body = f"[{length},{_l5k_string_padded(text, cap)}]"
                data_xml = (
                    f'<Data Format="L5K">\n<![CDATA[{l5k_body}]]>\n</Data>'
                    f'<Data Format="String" Length="{length}">\n'
                    f'{_string_literal_cdata(text)}\n</Data>'
                )

            elif isinstance(iv, dict):
                # UDT scalar
                body = (
                    f'<Structure DataType="{dt_base}">'
                    f'{_udt_scalar_to_xml(dt_base, iv, self._data_types_map)}'
                    f'</Structure>'
                )
                data_xml = f'<Data Format="Decorated">\n{body}\n</Data>'

            elif isinstance(iv, list) and iv and isinstance(iv[0], dict):
                # UDT array
                dim_str = self.dimensions or "1"
                body = _udt_array_to_xml(
                    self.name, dt_base, iv, dim_str, self._data_types_map
                )
                if body:
                    data_xml = f'<Data Format="Decorated">\n{body}\n</Data>'

            elif dt_base in _PRIM:
                radix_attr = _PRIMITIVE_RADIX.get(dt_base, "Decimal")

                if isinstance(iv, list):
                    # Array: omit trailing zero elements.
                    non_zero_end = 0
                    for i in range(len(iv) - 1, -1, -1):
                        v = iv[i]
                        if dt_base in ("BOOL", "BIT"):
                            if v != 0:
                                non_zero_end = i + 1
                                break
                        elif isinstance(v, (int, float)):
                            if v != 0.0 and v != 0:
                                non_zero_end = i + 1
                                break
                    non_zero_end = max(non_zero_end, 1)

                    def _fmt_elem_val(val):
                        if dt_base in ("BOOL", "BIT"):
                            return "1" if val else "0"
                        if isinstance(val, float):
                            return f"{val:.6g}"
                        return str(int(val))

                    _elem_comments = _build_elem_comments(self.name, self._comments) if self._comments else {}
                    def _fmt_element_comment(c):
                        c = " ".join(c.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
                        return self._sanitize_xml_text(c)
                    elems_parts = []
                    for i in range(non_zero_end):
                        comments = _elem_comments.get(i, [])
                        val_attr = f'Value="{_fmt_elem_val(iv[i])}"'
                        if comments:
                            comment_xml = "".join(
                                f'<Comment><![CDATA[{_fmt_element_comment(c)}]]></Comment>'
                                for c in comments
                            )
                            elems_parts.append(
                                f'<Element Index="[{i}]" {val_attr}>{comment_xml}</Element>'
                            )
                        else:
                            elems_parts.append(
                                f'<Element Index="[{i}]" {val_attr}/>'
                            )
                    elems = "".join(elems_parts)
                    dim_str = self.dimensions or "1"
                    data_xml = (
                        f'<Data Format="Decorated">\n'
                        f'<Array Name="{self.name}" DataType="{dt_base}" '
                        f'Dimensions="{dim_str}" Radix="{radix_attr}">{elems}</Array>\n'
                        f'</Data>'
                    )
                else:
                    # Scalar primitive
                    if dt_base in ("BOOL", "BIT"):
                        val_str = "1" if iv else "0"
                    elif isinstance(iv, float):
                        val_str = f"{iv:.6g}"
                    else:
                        val_str = str(int(iv))

                    data_xml = (
                        f'<Data Format="Decorated">\n'
                        f'<{dt_base} Name="{self.name}" Value="{val_str}" '
                        f'Radix="{radix_attr}"/>\n'
                        f'</Data>'
                    )

        if not data_xml and not is_alias:
            # Scalar primitives get Format="L5K" only.
            # Scalar STRING gets Format="L5K" (the L5K encoder handles it separately; we emit
            # nothing here — Decorated is not used for scalar STRING tags).
            # Everything else (UDTs, arrays, TIMER, COUNTER, etc.) gets Format="Decorated".
            dt_base = self.data_type.split("[")[0].upper() if self.data_type else ""
            l5k_zero = _PRIMITIVE_L5K_ZERO.get(dt_base) if not self.dimensions else None
            data_xml = f'<Data Format="L5K">\n{l5k_zero}\n</Data>' if l5k_zero is not None else ""

            if not data_xml and dt_base not in _SKIP_DECORATED and dt_base != "STRING":
                # Generate Decorated data for non-primitive / array types
                decorated = _generate_decorated(
                    dt_base, self.dimensions, self._data_types_map,
                    tag_name=self.name, comments=self._comments,
                )
                if decorated:
                    data_xml = decorated

        if not desc_xml and not data_xml:
            return base

        # Insert Description (if any) then Data (if any) immediately after the opening tag.
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + data_xml + base[idx + 1:]


@dataclass
class LocalTag(L5xElement):
    """Represents a local (non-public) tag inside an AOI (<LocalTag> in L5X)."""
    name: str
    data_type: str
    dimensions: Union[str, None]  # array size; None for scalars (omitted from XML)
    radix: Union[str, None]   # None for complex/UDT types (omitted from XML)
    external_access: str
    _description: Union[str, None] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "LocalTag"

    @property
    def _l5x_exclude(self) -> bool:
        """Exclude hex-address placeholders, empty names, and ACD-internal runtime tags."""
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
            or ":" in self.name
            or self.name.startswith("__l0")
            or self.name.startswith("__CLONE")
        )

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc = ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class Parameter(L5xElement):
    """Represents a public parameter of an AOI (<Parameter> in L5X)."""
    name: str
    tag_type: str       # always "Base"
    data_type: str
    usage: str          # "Input", "Output", or "InOut"
    radix: Union[str, None]   # None for complex types (omitted from XML)
    required: str       # "true" or "false"
    visible: str        # "true" or "false"
    external_access: Union[str, None]  # None for InOut (omitted, replaced by Constant)
    constant: Union[str, None]  # "false" for non-MESSAGE InOut, None otherwise (omitted)
    dimensions: Union[str, None]  # array size; None for scalars (omitted from XML)
    _description: Union[str, None] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "Parameter"

    @property
    def _l5x_exclude(self) -> bool:
        return (
            not self.name
            or not (self.name[0].isalpha() or self.name[0] == "_")
        )

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        desc = ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]


@dataclass
class Module(L5xElement):
    """Represents a Logix hardware module (<Module> in L5X)."""
    name: str
    catalog_number: str
    vendor: int
    product_type: int
    product_code: int
    major: int
    minor: int
    parent_module: str
    parent_mod_port_id: int
    inhibited: str
    major_fault: str
    # Private fields (not serialised as XML attributes)
    _ekey_state: str = field(default="CompatibleModule")
    _slot: int = field(default=0)
    _ip_address: str = field(default="")
    _backplane_slot: Union[int, None] = field(default=None)
    _chassis_size: Union[int, None] = field(default=None)
    _port_child_counts: Dict[int, int] = field(default_factory=dict)
    # Communications / ExtendedProperties / Description (optional)
    _description: str = field(default="")
    _comm_method: Union[str, None] = field(default=None)
    # Each entry: (name, rpi_str, conn_type_str)
    _connections: List[Tuple[str, str, str]] = field(default_factory=list)
    _extended_properties: str = field(default="")

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "Module"

    def to_xml(self) -> str:
        # Hash-named drive peripherals have no Name attribute in Logix-exported L5X.
        name_attr = "" if self.name == "?" else f'Name="{self.name}" '
        attrs = (
            f'{name_attr}'
            f'CatalogNumber="{self.catalog_number}" '
            f'Vendor="{self.vendor}" '
            f'ProductType="{self.product_type}" '
            f'ProductCode="{self.product_code}" '
            f'Major="{self.major}" '
            f'Minor="{self.minor}" '
            f'ParentModule="{self.parent_module}" '
            f'ParentModPortId="{self.parent_mod_port_id}" '
            f'Inhibited="{self.inhibited}" '
            f'MajorFault="{self.major_fault}"'
        )

        # Optional <Description>
        desc_xml = ""
        if self._description:
            desc = ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
            desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'

        ekey = f'<EKey State="{self._ekey_state}"/>'
        ports = self._build_ports_xml()

        # <Communications> section — only emitted when a CommMethod is known.
        comm_xml = ""
        if self._comm_method is not None:
            conn_parts: List[str] = []
            for (conn_name, rpi_str, conn_type) in self._connections:
                safe_name = _escape_xml_attr(conn_name)
                # Derive InputTag / OutputTag stubs based on connection type.
                if conn_type == "Output":
                    tag_stubs = (
                        '<OutputTag ExternalAccess="Read/Write">'
                        '<Comments/>'
                        '</OutputTag>'
                    )
                else:
                    # Input or InputOutput: include both stubs.
                    tag_stubs = (
                        '<InputTag ExternalAccess="Read Only">'
                        '<Comments/>'
                        '</InputTag>'
                        '<OutputTag ExternalAccess="Read/Write">'
                        '<Comments/>'
                        '</OutputTag>'
                    )
                conn_parts.append(
                    f'<Connection Name="{safe_name}" RPI="{rpi_str}" Type="{conn_type}"'
                    f' EventID="0" ProgrammaticallySendEventTrigger="false" Unicast="false">'
                    f'{tag_stubs}'
                    f'</Connection>'
                )
            joined = "".join(conn_parts)
            connections_xml = f'<Connections>{joined}</Connections>' if joined else '<Connections/>'
            comm_xml = (
                f'<Communications CommMethod="{self._comm_method}">'
                f'{connections_xml}'
                f'</Communications>'
            )

        # <ExtendedProperties> section — only emitted when public data is known.
        ext_xml = ""
        if self._extended_properties:
            ext_xml = f'<ExtendedProperties><public>{self._extended_properties}</public></ExtendedProperties>'

        return f'<Module {attrs}>{desc_xml}{ekey}{ports}{comm_xml}{ext_xml}</Module>'

    def _build_ports_xml(self) -> str:
        """Build the <Ports>...</Ports> XML section for this module.

        Looks up the port structure from PORT_STRUCTURES by (vendor, product_type,
        product_code). Falls back to <Ports/> if the catalog number is not in the table.
        """
        key = (self.vendor, self.product_type, self.product_code)
        port_defs = PORT_STRUCTURES.get(key)
        if port_defs is None:
            return '<Ports/>'

        is_root = (self.major_fault == "true")
        port_parts: List[str] = []

        for pd in port_defs:
            # --- Upstream direction ---
            # Root modules (self-parenting CPU) have all ports downstream.
            # For other modules: if upstream_fixed=True, use the static upstream_port value.
            # If upstream_fixed=False, determine from parent_mod_port_id (the port on the
            # parent module that this module connects through — when that matches port_id,
            # this port faces upstream).
            if is_root:
                upstream_str = "false"
            elif pd.upstream_fixed:
                upstream_str = "true" if pd.upstream_port else "false"
            else:
                upstream_str = "true" if pd.port_id == self.parent_mod_port_id else "false"

            # --- Address attribute ---
            if pd.address_mode == "omit":
                addr_attr = ""
            elif pd.address_mode == "slot":
                # Non-upstream ICP ports (remote chassis owner): use _backplane_slot if known.
                if upstream_str == "false" and self._backplane_slot is not None:
                    addr_attr = f' Address="{self._backplane_slot}"'
                else:
                    addr_attr = f' Address="{self._slot if self._slot != 0xFFFFFFFF else 0}"'
            elif pd.address_mode == "zero":
                addr_attr = ' Address="0"'
            else:  # "empty" — use IP from binary if present, else omit value
                addr_attr = f' Address="{self._ip_address}"'

            # --- Bus element ---
            # Bus is only emitted on downstream (Upstream="false") ports.
            # upstream ports never carry a Bus element.
            is_upstream = (upstream_str == "true")
            bus_xml = self._bus_xml(pd, is_upstream)

            if bus_xml:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}">\n'
                    f'{bus_xml}\n'
                    f'</Port>\n'
                )
            else:
                port_parts.append(
                    f'<Port Id="{pd.port_id}"{addr_attr} Type="{pd.port_type}" Upstream="{upstream_str}"/>\n'
                )

        return f'<Ports>\n{"".join(port_parts)}</Ports>\n'

    def _bus_xml(self, pd, is_upstream: bool) -> str:
        """Return the Bus XML string for a port, or '' if no Bus element should be emitted.

        Bus elements are only present on downstream (Upstream=false) ports.
        """
        if is_upstream:
            return ""
        mode = pd.bus_mode
        if mode == "none":
            return ""
        if mode == "always":
            return "<Bus/>"
        if mode.startswith("fixed:"):
            # Use binary chassis size when available (read from RxDataCollection);
            # fall back to the hardcoded port_structures value.
            if self._chassis_size is not None:
                return f'<Bus Size="{self._chassis_size}"/>'
            size = mode.split(":")[1]
            return f'<Bus Size="{size}"/>'
        if mode == "children_or_none":
            child_count = self._port_child_counts.get(pd.port_id, 0)
            if self._chassis_size is not None:
                child_count = max(child_count, self._chassis_size)
            if child_count == 0:
                return ""
            return f'<Bus Size="{child_count}"/>'
        # "children" mode: child count, but never less than _chassis_size when known
        # (handles remote chassis with empty slots not represented as child modules).
        child_count = self._port_child_counts.get(pd.port_id, 0)
        if self._chassis_size is not None:
            child_count = max(child_count, self._chassis_size)
        return f'<Bus Size="{child_count}"/>'


@dataclass
class Routine(L5xElement):
    name: str
    type: str
    rungs: List[str]
    _rung_ids: List[int] = field(default_factory=list)
    _rung_comments: Dict[int, str] = field(default_factory=dict)

    def to_xml(self) -> str:
        rll_content = ""
        if self.type == "RLL" and self.rungs:
            rung_xmls = []
            for i, rung_text in enumerate(self.rungs):
                text = (rung_text or "").strip()
                if not text:
                    continue
                comment_xml = ""
                if i in self._rung_comments:
                    comment_text = self._rung_comments[i].replace('\r\n', '\n').replace('\r', '\n')
                    comment_xml = f'<Comment><![CDATA[{comment_text}]]></Comment>'
                rung_xmls.append(
                    f'<Rung Number="{i}" Type="N">'
                    f'{comment_xml}'
                    f'<Text><![CDATA[{text}]]></Text>'
                    f'</Rung>'
                )
            if rung_xmls:
                rll_content = f'<RLLContent>{"".join(rung_xmls)}</RLLContent>'
        return f'<Routine Name="{_escape_xml_attr(self.name)}" Type="{self.type}">{rll_content}</Routine>'


@dataclass
class AOI(L5xElement):
    name: str
    revision: str
    revision_extension: Union[str, None]  # None if absent (omitted from XML)
    vendor: Union[str, None]  # None if absent (omitted from XML)
    execute_prescan: str
    execute_postscan: str
    execute_enable_in_false: str
    created_date: str
    created_by: str
    edited_date: str
    edited_by: str
    software_revision: str
    parameters: List[Parameter]
    local_tags: List[LocalTag]
    routines: List[Routine]
    _description: Union[str, None] = field(default=None)
    _revision_note: str = field(default="")

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "AddOnInstructionDefinition"

    def to_xml(self) -> str:
        base = super().to_xml()
        idx = base.index(">")
        inject = ""
        if self._description:
            desc = ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
            inject += f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        if self._revision_note:
            note = ' '.join(self._revision_note.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()
            inject += f'<RevisionNote>\n<![CDATA[{note}]]>\n</RevisionNote>'
        return base[:idx + 1] + inject + base[idx + 1:]


@dataclass
class Program(L5xElement):
    name: str
    test_edits: str
    main_routine_name: Union[str, None]  # None if absent (omitted from XML)
    fault_routine_name: Union[str, None]  # None if absent (omitted from XML)
    disabled: str
    synchronize_redundancy_data_after_execution: Union[str, None]  # None → omit attr
    use_as_folder: str
    tags: List[Tag]        # Tags section before Routines (matches L5X export order)
    routines: List[Routine]


@dataclass
class ScheduledProgram(L5xElement):
    name: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "ScheduledProgram"


@dataclass
class EventInfo(L5xElement):
    event_trigger: str
    enable_timeout: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "EventInfo"


@dataclass
class Task(L5xElement):
    name: str
    type: str
    rate: Union[str, None]  # None for CONTINUOUS tasks (omitted from XML)
    priority: str
    watchdog: str
    disable_update_outputs: str
    inhibit_task: str
    event_info: Union[EventInfo, None]  # None for non-EVENT tasks
    scheduled_programs: List[ScheduledProgram]


@dataclass
class Controller(L5xElement):
    use: str
    name: str
    processor_type: Union[str, None]  # None if unknown (omitted from XML)
    major_rev: str
    minor_rev: str
    major_fault_program: Union[str, None]  # None if not set (omitted from XML)
    project_creation_date: str
    last_modified_date: str
    sfc_execution_control: str
    sfc_restart_position: str
    sfc_last_scan: str
    comm_path: Union[str, None]  # None if not set (omitted from XML)
    project_sn: str
    match_project_to_controller: str
    can_use_rpi_from_producer: str
    inhibit_automatic_firmware_update: str
    pass_through_configuration: str
    download_project_documentation_and_extended_properties: str
    download_project_custom_properties: str
    report_minor_overflow: str
    auto_diags_enabled: str
    web_server_enabled: str
    data_types: List[DataType]
    modules: List[Module]
    tags: List[Tag]
    programs: List[Program]
    tasks: List[Task]
    aois: List[AOI]
    # _redundancy_enabled is NOT serialised as a regular XML attribute (underscore prefix
    # skips it in the base to_xml()); it is used only to build the <RedundancyInfo> element.
    _redundancy_enabled: bool = field(default=False)

    def __post_init__(self):
        super().__post_init__()
        self._xml_attr_overrides = {
            "sfc_execution_control": "SFCExecutionControl",
            "sfc_restart_position": "SFCRestartPosition",
            "sfc_last_scan": "SFCLastScan",
            "project_sn": "ProjectSN",
            "can_use_rpi_from_producer": "CanUseRPIFromProducer",
        }
        self._io_tags: List[Tag] = [t for t in self.tags if ":" in t.name]
        self._alias_tags: List[Tag] = [t for t in self.tags if t.tag_type == "Alias"]

    @property
    def io_tags(self) -> List[Tag]:
        return self._io_tags

    @property
    def alias_tags(self) -> List[Tag]:
        return self._alias_tags

    def to_xml(self) -> str:
        base = super().to_xml()
        # Split at the end of the opening <Controller ...> tag so we can inject
        # structural stubs before the data sections and post-sections after them.
        idx = base.index(">")
        open_tag = base[: idx + 1]
        inner = base[idx + 1 : -len("</Controller>")]
        # RedundancyInfo: Enabled comes from binary; no pad attributes in golden.
        redundancy_enabled_str = "true" if self._redundancy_enabled else "false"
        redundancy_info = (
            f'<RedundancyInfo Enabled="{redundancy_enabled_str}" KeepTestEditsOnSwitchOver="false"/>'
        )
        return (
            open_tag
            + inner
            + redundancy_info
            + '<Security Code="0" ChangesToDetect="16#ffff_ffff_ffff_ffff"/>'
            + '<SafetyInfo/>'
            + '<CST MasterID="0"/>'
            + '<WallClockTime LocalTimeAdjustment="0" TimeZone="0"/>'
            + '<Trends/>'
            + '<DataLogs/>'
            + '<TimeSynchronize Priority1="128" Priority2="128" PTPEnable="true"/>'
            + '</Controller>'
        )


@dataclass
class RSLogix5000Content(L5xElement):
    """Controller Project"""

    controller: Union[Controller, None]
    schema_revision: str
    software_revision: str
    target_name: str
    target_type: str
    contains_context: str
    export_date: str
    export_options: str

    def __post_init__(self):
        super().__post_init__()
        self._export_name = "RSLogix5000Content"


def radix_enum(i: int) -> str:
    if i == 0:
        return "NullType"
    if i == 1:
        return "General"
    if i == 2:
        return "Binary"
    if i == 3:
        return "Octal"
    if i == 4:
        return "Decimal"
    if i == 5:
        return "Hex"
    if i == 6:
        return "Exponential"
    if i == 7:
        return "Float"
    if i == 8:
        return "ASCII"
    if i == 9:
        return "Unicode"
    if i == 10:
        return "Date/Time"
    if i == 11:
        return "Date/Time (ns)"
    if i == 12:
        return "UseTypeStyle"
    return "General"


def external_access_enum(i: int) -> str:
    default = "Read/Write"
    if i == 0:
        return default
    if i == 2:
        return "Read Only"
    if i == 3:
        return "None"
    return default


@dataclass
class MemberBuilder(L5xElementBuilder):
    record: bytes = field(default_factory=bytes)
    # Map from offset-0x60 value to backing member name, used to resolve BIT Target.
    # Built by DataTypeBuilder before iterating children and passed in here.
    _offset60_to_name: Dict[int, str] = field(default_factory=dict)
    # Fallback target name for Pattern-2 BIT members (0x68==0, 0x6c==0xFFFFFFFF).
    # Set by DataTypeBuilder to the most recent preceding hidden SINT/INT in member order.
    _fallback_target: Union[str, None] = field(default=None)

    def build(self) -> Member:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]
        r = RxGeneric.from_bytes(results[0][3])
        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Member(name, name, "", 0, "Decimal", False, None, None, "Read/Write")

        extended_records: Dict[int, List[int]] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = extended_record.value

        cip_data_typoe = struct.unpack_from("<I", self.record, 0x78)[0]
        dimension = struct.unpack_from("<I", self.record, 0x5C)[0]
        radix = radix_enum(struct.unpack_from("<I", self.record, 0x54)[0])
        data_type_id = struct.unpack_from("<I", self.record, 0x58)[0]
        hidden = bool(struct.unpack_from("<I", self.record, 0x70)[0])
        external_access = external_access_enum(
            struct.unpack_from("<I", self.record, 0x74)[0]
        )

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(data_type_id)
        )
        data_type_results = self._cur.fetchall()
        data_type = data_type_results[0][0]

        # BIT members: data_type resolves to BOOL but the encoding varies by sub-type.
        #
        # Pattern 1 (0x6c != 0xFFFFFFFF): 0x6c holds the 0x60 byte-offset of the backing
        #   field.  Resolve target via offset60_to_name.
        #
        # Pattern 2 (0x6c == 0xFFFFFFFF and 0x68 == 0): BIT member where the backing field
        #   pointer is absent.  Use _fallback_target (the most-recent preceding hidden SINT).
        #
        # Pattern 3 (0x6c == 0xFFFFFFFF and 0x68 == 1): BIT member where 0x60 directly
        #   matches the backing field's 0x60 value.  Resolve target via offset60_to_name
        #   using the member's own 0x60 value as the lookup key.
        #
        # Plain BOOL (0x6c == 0xFFFFFFFF and 0x68 == 0x800): not a BIT member; leave as BOOL.
        target: Union[str, None] = None
        bit_number: Union[int, None] = None
        if data_type == "BOOL":
            target_key = struct.unpack_from("<I", self.record, 0x6C)[0]
            val_68 = struct.unpack_from("<I", self.record, 0x68)[0]
            if target_key != 0xFFFFFFFF:
                # Pattern 1: direct backing-field reference via offset-60 map
                data_type = "BIT"
                # offset 0x5C holds a bit-offset into the host register, not an array
                # size — force dimension to 0 so _member_decorated_xml treats this as
                # a scalar rather than emitting thousands of <Element> entries.
                dimension = 0
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]
                target = self._offset60_to_name.get(target_key)
            elif val_68 == 0:
                # Pattern 2: BIT member without explicit backing-field pointer
                data_type = "BIT"
                dimension = 0  # same bit-offset field; not an array size
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]
                target = self._fallback_target
            elif val_68 == 1:
                # Pattern 3: BIT member — 0x60 of this member equals 0x60 of the backing
                # hidden field, allowing direct lookup in offset60_to_name.
                data_type = "BIT"
                dimension = 0
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]
                val_60 = struct.unpack_from("<I", self.record, 0x60)[0]
                target = self._offset60_to_name.get(val_60)
            else:
                # Plain BOOL (not a BIT sub-element) — bit_number is at 0x64 for data
                # table bit-packing extraction.
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]

        # --- Description ---
        # The member's description is identified in the comments table by a
        # member_ref value extracted from bytes [14:18] of the comps record.
        # This value is non-zero for sub-elements (members) and zero for the
        # owning object's own description.
        description: Union[str, None] = None
        raw_comps = bytes(results[0][3])
        if len(raw_comps) >= 18:
            member_ref = struct.unpack_from("<I", raw_comps, 14)[0]
            if member_ref:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND member_ref=? LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        byte_offset = struct.unpack_from("<I", self.record, 0x60)[0]
        return Member(
            name, name, data_type, dimension, radix, hidden,
            target, bit_number, external_access,
            byte_offset=byte_offset,
            _description=description,
        )


@dataclass
class DataTypeBuilder(L5xElementBuilder):
    @staticmethod
    def _decode_member_name(rec: bytes) -> str:
        u16 = rec.decode("utf-16-le", errors="replace")
        null_idx = u16.find("\x00")
        if null_idx >= 0:
            u16 = u16[:null_idx]
        return u16.strip()

    def build(self) -> DataType:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        name = results[0][0]

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return DataType(name, name, "NoFamily", "User", [])

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        string_family_int = struct.unpack("<I", extended_records[0x6C])[0]
        string_family = "StringFamily" if string_family_int == 1 else "NoFamily"

        built_in = struct.unpack("<I", extended_records[0x67])[0]
        module_defined = struct.unpack("<I", extended_records[0x69])[0]

        class_type = "User"
        if module_defined > 0:
            class_type = "IO"
        if built_in & 0x03:
            class_type = "ProductDefined"
        if 0x64 in extended_records and len(extended_records[0x64]) == 0x04:
            member_count = struct.unpack("<I", extended_records[0x64])[0]
        else:
            member_count = 0

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
        )
        member_results = self._cur.fetchall()
        children: List[Member] = []
        if len(member_results) == 1:
            member_collection_id = member_results[0][1]

            self._cur.execute(
                f"SELECT comp_name, object_id, parent_id, seq_number, record FROM comps WHERE parent_id={member_collection_id} ORDER BY seq_number"
            )
            children_results = self._cur.fetchall()

            # Build a name→child map so we pair members by name instead of
            # by seq_number position (seq_number can be stale when members
            # are deleted and re-added over a project's lifetime).
            name_to_child: Dict[str, Tuple] = {}
            for child in children_results:
                name_to_child[child[0]] = child

            # Gather member extended record keys (attribute_id >= 0x6E) in
            # declaration order (Logix Designer may renumber them to be
            # contiguous after deletions).
            member_keys = sorted(k for k in extended_records if k >= 0x6E)

            # Build offset60→name map so BIT members can resolve their Target name.
            # Each member's extended record stores the byte offset of that member's
            # data within the UDT at [0x60]; BIT members reference their backing
            # field's offset via [0x6c].  Non-BIT members have [0x6c]=0xFFFFFFFF
            # and 0x68=0x800.  Only include non-BIT members (0x68==0x800) so that
            # BIT members sharing the same 0x60 value as their backing field do
            # not overwrite the backing entry.
            offset60_to_name: Dict[int, str] = {}
            for key in member_keys:
                rec = extended_records[key]
                member_name = DataTypeBuilder._decode_member_name(rec)
                child = name_to_child.get(member_name)
                if child is None:
                    continue
                if len(rec) >= 0x70:
                    target_key2 = struct.unpack_from("<I", rec, 0x6C)[0]
                    val_68_2 = struct.unpack_from("<I", rec, 0x68)[0]
                    if target_key2 == 0xFFFFFFFF and val_68_2 == 0x800:
                        val_60 = struct.unpack_from("<I", rec, 0x60)[0]
                        offset60_to_name[val_60] = child[0]

            # Track the most recent preceding hidden SINT (fallback target for
            # Pattern-2 BIT members).
            last_hidden_backing: Union[str, None] = None
            for key in member_keys:
                rec = extended_records[key]
                member_name = DataTypeBuilder._decode_member_name(rec)
                child = name_to_child.get(member_name)
                if child is None:
                    continue
                # Update last_hidden_backing when we see a hidden member
                if len(rec) >= 0x74:
                    is_hidden = bool(struct.unpack_from("<I", rec, 0x70)[0])
                    if is_hidden:
                        last_hidden_backing = child[0]
                try:
                    children.append(
                        MemberBuilder(
                            self._cur, child[1], rec,
                            offset60_to_name,
                            last_hidden_backing,
                        ).build()
                    )
                    del name_to_child[member_name]
                except Exception:
                    pass

        # --- Description ---
        description: Union[str, None] = None
        self._cur.execute(
            "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 LIMIT 1",
            ((r.comment_id * 0x10000) + r.cip_type,),
        )
        desc_row = self._cur.fetchone()
        if desc_row and desc_row[0]:
            description = desc_row[0]

        return DataType(name, name, string_family, class_type, children, description)


# Connection Type CIP enum, read at absolute offset 90 (u16le) of a
# RxMapConnectionCollection child's raw record. Verified against a real
# Studio 5000 project (matching every connection's raw bytes, keyed by
# (comp_name, RPI) read from offset 92, against its actual L5X Type=
# attribute): a clean, zero-exception discriminator across 74 real
# connection records spanning all four values seen so far. Other CIP
# connection types (e.g. Safety Input/Output, Listen-Only, Config) may use
# additional codes not yet observed -- see the name-based fallback below.
_CONNECTION_TYPE_BY_CODE: Dict[int, str] = {
    5: "Input",
    6: "Output",
    7: "DiagnosticInput",
    23: "MotionSync",
}


@dataclass
class ModuleBuilder(L5xElementBuilder):
    # Map from modid (u32) → module name, built by ControllerBuilder and passed in.
    _modid_to_name: Dict[int, str] = field(default_factory=dict)

    def _ip_from_data_collection(self, icp_slot: int) -> str:
        """Look up the Ethernet IP for a local backplane module via RxDataCollection.

        Local bridge modules (e.g. EN2T in the main chassis) store their IP as XML
        in hash-named children of RxDataCollection. The record for a given module
        contains its ICP slot as Type="ICP" Addr="{slot}", which uniquely identifies it.
        """
        import re as _re
        needle = f'Type="ICP" Addr="{icp_slot}"'.encode()
        # Find RxDataCollection — it is a direct child of the controller object.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return ""
        coll_oid = row[0]
        # Fetch all children in batches and filter in Python (SQLite LIKE on BLOBs is unreliable).
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        for (raw,) in self._cur.fetchall():
            raw = bytes(raw)
            if needle not in raw:
                continue
            m = _re.search(rb'Type="EN" Addr="([^"]+)"', raw)
            return m.group(1).decode("ascii", errors="replace") if m else ""
        return ""

    def _comms_from_data_collection(
        self, icp_slot: int, ip_address: str = ""
    ) -> "Tuple[Union[str, None], str]":
        """Extract CommMethod and ExtendedProperties public data for a module.

        Searches RxDataCollection for the hash-named child whose <in> block contains
        a Port with Type="ICP" Addr="{icp_slot}".  For Ethernet-connected modules
        (icp_slot == 0 or not found by ICP slot) a second pass matches by the
        module's IP address instead.

        Returns a 2-tuple:
          (comm_method_str_or_None, public_content_str)

        comm_method_str: the numeric string from <CF>...</CF>, or None if absent.
        public_content_str: inner content of <public>...</public>, or "" if absent.

        The record XML is often stored without the closing </public> tag (it is
        truncated in the ACD binary). We reconstruct the content by extracting
        everything after <public>.
        """
        import re as _re

        def _extract(raw: bytes) -> "Tuple[Union[str, None], str]":
            xml_start = raw.find(b'<')
            if xml_start < 0:
                return (None, "")
            xml_text = raw[xml_start:].decode("latin-1", errors="replace")
            comm_method: Union[str, None] = None
            cf_m = _re.search(r'<CF>(\d+)</CF>', xml_text)
            if cf_m:
                comm_method = cf_m.group(1)
            pub_content = ""
            pub_start = xml_text.find("<public>")
            if pub_start >= 0:
                after_pub = xml_text[pub_start + len("<public>"):]
                end_tag_m = _re.search(r'</pub', after_pub)
                if end_tag_m:
                    pub_content = after_pub[:end_tag_m.start()]
                else:
                    pub_content = after_pub.rstrip("\x00 \r\n")
            return (comm_method, pub_content)

        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return (None, "")
        coll_oid = row[0]
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        all_recs = [(bytes(raw),) for (raw,) in self._cur.fetchall()]

        # First pass: match by ICP slot.
        if icp_slot:
            needle = f'Type="ICP" Addr="{icp_slot}"'.encode()
            for (raw,) in all_recs:
                if needle in raw:
                    result = _extract(raw)
                    if result[0] is not None or result[1]:
                        return result

        # Second pass: match by IP address (for EN-connected modules).
        if ip_address:
            ip_needle = f'Addr="{ip_address}"'.encode()
            for (raw,) in all_recs:
                if ip_needle in raw:
                    result = _extract(raw)
                    if result[0] is not None or result[1]:
                        return result

        return (None, "")

    def _chassis_size_from_data_collection(self) -> "Union[int, None]":
        """Read the local backplane Bus Size from the RxDataCollection record for the CPU.

        The root controller (Local) module stores its backplane configuration as XML in a
        hash-named child of RxDataCollection.  The record contains:
          <Port Id="1" Type="ICP" Addr="0" Ups="False"><Bus Max="17" Size="7"/></Port>
        We extract the Size attribute from the Bus element on the ICP port at Addr="0".
        """
        import re as _re
        self._cur.execute(
            "SELECT object_id FROM comps WHERE comp_name='RxDataCollection' LIMIT 1"
        )
        row = self._cur.fetchone()
        if not row:
            return None
        coll_oid = row[0]
        self._cur.execute(
            "SELECT record FROM comps WHERE parent_id=?", (coll_oid,)
        )
        needle = b'Type="ICP" Addr="0"'
        for (raw,) in self._cur.fetchall():
            raw = bytes(raw)
            if needle not in raw:
                continue
            text_start = raw.find(b"<")
            if text_start < 0:
                continue
            text = raw[text_start:].decode("latin-1", errors="replace")
            m = _re.search(r'<Bus\b[^>]*\bSize="(\d+)"', text)
            if m:
                return int(m.group(1))
        return None

    def build(self) -> Module:
        self._cur.execute(
            "SELECT comp_name, object_id, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        db_name = row[0]
        raw_rec = bytes(row[2])

        # Hex-encoded names like $02cc5e9d$ are unnamed peripheral modules (drive expansion
        # cards, etc.).  Logix Designer exports these with Name="?".
        name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name

        try:
            r = RxGeneric.from_bytes(raw_rec)
        except Exception:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        if r.cip_type != 0x69:
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", "false")

        exts: Dict[int, bytes] = {er.attribute_id: bytes(er.value) for er in r.extended_records}
        e1 = exts.get(0x001, b"")
        if len(e1) < 0x30:
            major_fault = "true" if name == "Local" else "false"
            return Module(name, name, "", 0, 0, 0, 0, 0, "Local", 1, "false", major_fault)

        vendor        = struct.unpack("<H", e1[0x02:0x04])[0]
        product_type  = struct.unpack("<H", e1[0x04:0x06])[0]
        product_code  = struct.unpack("<H", e1[0x06:0x08])[0]
        # bit 7 of the major byte is a flag; strip it to get the firmware revision.
        major         = e1[0x08] & 0x7F
        minor         = e1[0x09]
        parent_modid  = struct.unpack("<I", e1[0x16:0x1A])[0]
        parent_port   = struct.unpack("<H", e1[0x1A:0x1C])[0]
        slot          = struct.unpack("<I", e1[0x1C:0x20])[0]

        # Hash-named modules are drive peripheral expansion cards. The ACD binary stores
        # them with ProductType=123 and site-specific ProductCodes, but Logix exports them
        # all as PT=0 PC=28 (RHINOBP-DRIVE-PERIPHERAL-MODULE) without a Name attribute.
        if name == "?":
            product_type = 0
            product_code = 28

        # Resolve parent module name from the modid→name map built by ControllerBuilder.
        parent_name = self._modid_to_name.get(parent_modid, "Local")

        # MajorFault=true for the root controller module: its parent resolves to itself.
        major_fault = "true" if parent_name == name else "false"
        # bit 2 (0x04) of e1[0] → EKey Disabled (Local=0x06→Disabled, EN2T=0x11→CompatibleModule).
        ekey_state  = "Disabled" if (e1[0] & 0x04) else "CompatibleModule"

        # IP address: stored at e1[0x30] as a u16 length-prefixed ASCII string for modules
        # that connect via Ethernet upstream (parent_port == 2). Local backplane bridge
        # modules (parent_port == 1, e.g. local EN2T) leave e1[0x32] zero — their IP is
        # stored as XML in a child of RxDataCollection, keyed by ICP slot number.
        ip_address = ""
        if len(e1) > 0x32:
            ip_len = struct.unpack("<H", e1[0x30:0x32])[0]
            if ip_len:
                ip_address = e1[0x32:0x32 + ip_len].rstrip(b"\x00").decode("ascii", errors="replace")
        if not ip_address and slot:
            ip_address = self._ip_from_data_collection(slot)

        # For modules that own a remote backplane (e.g. remote chassis EN2T), the Output
        # connection record under RxMapConnectionCollection stores the chassis size at [0x4e]
        # and the module's own slot in that chassis at [0x6e].
        backplane_slot = None
        chassis_size = None
        self._cur.execute(
            "SELECT o.record FROM comps coll "
            "JOIN comps o ON o.parent_id = coll.object_id AND o.comp_name = 'Output' "
            "WHERE coll.parent_id = ? AND coll.comp_name = 'RxMapConnectionCollection'",
            (self._object_id,),
        )
        out_row = self._cur.fetchone()
        if out_row:
            out_rec = bytes(out_row[0])
            if len(out_rec) > 0x70:
                backplane_slot = struct.unpack("<H", out_rec[0x6e:0x70])[0]
                chassis_size   = struct.unpack("<H", out_rec[0x4e:0x50])[0]

        # For the root (Local) CPU module (slot=0xFFFFFFFF, self-parenting), the Output
        # connection record is absent.  The backplane chassis size is stored in the
        # RxDataCollection hash child that carries the Local module's ICP port at Addr="0".
        # Example: <Port Id="1" Type="ICP" Addr="0" Ups="False"><Bus Max="17" Size="7"/></Port>
        if chassis_size is None and slot == 0xFFFFFFFF:
            chassis_size = self._chassis_size_from_data_collection()

        # --- Description ---
        # Module descriptions are stored in the comments table keyed by
        # (comment_id * 0x10000 + cip_type), same as for tags.
        description = ""
        self._cur.execute(
            "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 LIMIT 1",
            ((r.comment_id * 0x10000) + r.cip_type,),
        )
        desc_row = self._cur.fetchone()
        if desc_row:
            description = desc_row[0] or ""

        # --- Communications and ExtendedProperties ---
        # Both are extracted from the hash-named child of RxDataCollection that
        # corresponds to this module's ICP backplane slot (primary) or its IP
        # address (secondary, for EN-connected modules).
        comm_method: Union[str, None] = None
        connections: List[Tuple[str, str, str]] = []
        extended_properties = ""
        if slot or ip_address:
            comm_method, extended_properties = self._comms_from_data_collection(
                slot, ip_address
            )

        # Read individual connection records from RxMapConnectionCollection children.
        # Each child's comp_name is the connection Name in the L5X output.
        # Connection Type is read from a u16le CIP enum at raw offset 90 (see
        # _CONNECTION_TYPE_BY_CODE) -- not from the connection's name, since a
        # connection can be named e.g. "Standard" and be either Input or Output
        # depending on the specific module (confirmed with real project data).
        # RPI (microseconds) is a u32le at raw offset 92, immediately after the
        # type code -- both found and verified together against a real project
        # by cross-referencing every connection's raw bytes (keyed by this same
        # RPI value) against its actual L5X Type= attribute.
        self._cur.execute(
            "SELECT c2.comp_name, c2.record FROM comps c1 "
            "JOIN comps c2 ON c2.parent_id = c1.object_id "
            "WHERE c1.parent_id = ? AND c1.comp_name = 'RxMapConnectionCollection' "
            "AND c2.comp_name NOT IN ('Output') "
            "ORDER BY c2.seq_number",
            (self._object_id,),
        )
        for (conn_name, conn_rec) in self._cur.fetchall():
            conn_raw = bytes(conn_rec)
            conn_type = None
            code = None
            rpi_str = "0"
            if len(conn_raw) >= 96:
                code = struct.unpack_from("<H", conn_raw, 90)[0]
                conn_type = _CONNECTION_TYPE_BY_CODE.get(code)
                rpi_str = str(struct.unpack_from("<I", conn_raw, 92)[0])
            if conn_type is None:
                # Unrecognized code (or too-short record): fall back to the
                # old name-based heuristic rather than guessing wrong, but
                # surface it so new codes can be identified and added to
                # _CONNECTION_TYPE_BY_CODE instead of silently mis-guessed.
                name_lower = conn_name.lower()
                conn_type = "Output" if ("output" in name_lower or name_lower == "config") else "Input"
                log.warning(
                    "Unrecognized connection type code {} for connection '{}' on module "
                    "'{}' (record length {}) -- falling back to name heuristic, guessed "
                    "'{}'. Please report this so the code can be added to "
                    "_CONNECTION_TYPE_BY_CODE.",
                    code, conn_name, name, len(conn_raw), conn_type,
                )
            connections.append((conn_name, rpi_str, conn_type))

        return Module(
            name,           # L5xElement._name (private)
            name,           # Module.name
            CATALOG_NUMBERS.get((vendor, product_type, product_code), ""),
            vendor,
            product_type,
            product_code,
            major,
            minor,
            parent_name,
            parent_port,
            "false",        # Inhibited: always false in practice; no known bit
            major_fault,
            _ekey_state=ekey_state,
            _slot=slot,
            _ip_address=ip_address,
            _backplane_slot=backplane_slot,
            _chassis_size=chassis_size,
            _description=description,
            _comm_method=comm_method,
            _connections=connections,
            _extended_properties=extended_properties,
        )


def _build_hex_oid_map(cur) -> Dict[int, str]:
    """Build map of hex OID → member name from comps member records."""
    cur.execute("""
        SELECT m.comp_name, m.record
        FROM comps m
        INNER JOIN comps c ON m.parent_id = c.object_id
        WHERE c.comp_name = 'RxTypeMemberCollection'
    """)
    hex_oid_map: Dict[int, str] = {}
    for name, rec in cur.fetchall():
        raw = bytes(rec)
        if len(raw) < 18:
            continue
        high = struct.unpack_from('<H', raw, 12)[0]
        low = struct.unpack_from('<H', raw, 16)[0]
        oid = (high << 16) | low
        if oid not in hex_oid_map or len(name) < len(hex_oid_map[oid]):
            hex_oid_map[oid] = name
    return hex_oid_map


def _resolve_tag_name_from_oid(cur, oid: int) -> Union[str, None]:
    """Resolve a comps record's display name from its OID.

    Returns the real tag name (from ext[0x01] if available) or the comp_name.
    Returns None if the OID does not exist.
    """
    cur.execute("SELECT comp_name, record FROM comps WHERE object_id=?", (oid,))
    row = cur.fetchone()
    if not row:
        return None
    comp_name, rec = row
    rec = bytes(rec)
    try:
        r = RxGeneric.from_bytes(rec)
        for er in r.extended_records:
            if er.attribute_id == 1:
                v = bytes(er.value)
                nl = struct.unpack("<H", v[:2])[0]
                return v[2:2+nl].decode("utf-8", errors="replace")
    except Exception:
        pass
    return comp_name


@dataclass
class TagBuilder(L5xElementBuilder):
    def build(self) -> Tag:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        # Extract ExternalAccess and Constant from the raw record at fixed offsets:
        #   raw[0x278]: ExternalAccess enum (0=Read/Write, 2=Read Only, 3=None)
        #   raw[0x279]: Constant flag (0=false, 1=true)
        # constant_flag is resolved to the final "true"/"false"/None Constant
        # value further down, once tag_type and data_type are known (Alias
        # tags and certain opaque built-in types like MESSAGE/AXIS_CIP_DRIVE
        # never show a Constant attribute at all; every other Base tag shows
        # it explicitly as "true" or "false" -- verified against a real
        # project where 3174 of 3205 tags need an explicit Constant="false"
        # that was previously omitted entirely).
        raw_rec = bytes(results[0][3])
        if len(raw_rec) > 0x279:
            external_access = external_access_enum(raw_rec[0x278])
            constant_flag = bool(raw_rec[0x279])
        else:
            external_access = "Read/Write"
            constant_flag = False

        try:
            r = RxGeneric.from_bytes(raw_rec)
        except Exception as e:
            return Tag(
                results[0][0], results[0][0], "Base", "", None, external_access,
                "true" if constant_flag else "false", None
            )

        if r.cip_type != 0x6B and r.cip_type != 0x68:
            return Tag(
                results[0][0], results[0][0], "Base", "", None, external_access,
                "true" if constant_flag else "false", None
            )
        if r.main_record.data_type == 0xFFFFFFFF:
            data_type = ""
        else:
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
                + str(r.main_record.data_type)
            )
            data_type_results = self._cur.fetchall()
            data_type = data_type_results[0][0]

        # scope_id: a 2-byte discriminator at absolute offset 16 of the tag's own raw
        # record. Multiple unrelated tags can share the same (comment_id, cip_type)
        # "parent" container key in Comments.Dat (e.g. tags that never got their own
        # unique comment_id assigned) — scope_id is the extra field Rockwell uses
        # internally to tell their comments apart within a shared container.
        own_scope_id = struct.unpack_from("<H", raw_rec, 16)[0] if len(raw_rec) >= 18 else 0
        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent=? AND scope_id=?",
            ((r.comment_id * 0x10000) + r.cip_type, own_scope_id),
        )
        comment_results = self._cur.fetchall()

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        # A Tag element's own Radix attribute is omitted entirely when the raw
        # value is 0 (verified: real Studio 5000 L5X never shows
        # Radix="NullType" on a <Tag> element itself -- always a primitive
        # type's real radix, e.g. "Decimal"/"Binary" -- unlike <Member>
        # elements, where "NullType" legitimately appears for struct-typed
        # members and must still be shown literally there).
        raw_radix = r.main_record.radix
        radix = radix_enum(raw_radix) if raw_radix != 0 else None

        dim_parts = []
        if r.main_record.dimension_1 != 0:
            dim_parts.append(str(r.main_record.dimension_1))
        if r.main_record.dimension_2 != 0:
            dim_parts.append(str(r.main_record.dimension_2))
        if r.main_record.dimension_3 != 0:
            dim_parts.append(str(r.main_record.dimension_3))
        dimensions = ",".join(dim_parts) if dim_parts else None
        dti = r.main_record.data_table_instance
        initial_value = _read_tag_initial_value(
            self._cur, dti, data_type, _count_array_elements(dimensions)
        )

        tag_name = results[0][0]
        if 0x01 in extended_records:
            name_length = struct.unpack("<H", extended_records[0x01][0:2])[0]
            tag_name = bytes(extended_records[0x01][2 : name_length + 2]).decode("utf-8", errors="replace")

        tag_type = "Base"
        target = None

        # Detect alias tags: look for remaining data after RxGeneric parsing
        # containing attribute_id=0x65 (UTF-16LE encoded alias path).
        consumed = 82 + sum(8 + len(bytes(er.value)) for er in r.extended_records)
        remaining = raw_rec[consumed:]
        if len(remaining) >= 8:
            extra_attr_id = struct.unpack("<I", remaining[:4])[0]
            if extra_attr_id == 0x65:
                tag_type = "Alias"
                extra_len = struct.unpack("<I", remaining[4:8])[0]
                if extra_len > 0 and 8 + extra_len <= len(remaining):
                    path_bytes = remaining[8:8+extra_len]
                    path_text = path_bytes.decode("utf-16-le", errors="replace").rstrip("\x00")
                    # Resolve the base target tag name from data_table_instance OID.
                    base_target = _resolve_tag_name_from_oid(self._cur, dti)
                    if base_target is not None:
                        target = base_target
                        # Path format: starts with @{target_oid_hex}@ followed by
                        # optional .@{member_oid_hex}@ or literal suffix.
                        # Examples:
                        #   @00d5cc47@.@fbc47e54@        -> OID Gate_Go
                        #   @5377b0d2@[5]                 -> literal [5]
                        #   @64ca9836@.@0c5708b9@[0].3   -> OID Data + literal [0].3
                        subpath_parts = []
                        if "@." in path_text:
                            # Multiple OID references separated by @.
                            parts = path_text.split("@.")
                            for part in parts[1:]:
                                if not part:
                                    continue
                                if part.startswith("@"):
                                    end = part.find("@", 1)
                                    hex_oid = part[1:end] if end >= 0 else part[1:]
                                    literal = part[end+1:] if end >= 0 else ""
                                    try:
                                        oid = int(hex_oid, 16)
                                    except ValueError:
                                        oid = 0
                                    if oid:
                                        member_name = _resolve_tag_name_from_oid(self._cur, oid)
                                        if member_name:
                                            subpath_parts.append(member_name)
                                    if literal:
                                        subpath_parts.append(literal)
                                else:
                                    subpath_parts.append(part)
                        elif "@" in path_text:
                            # Single OID reference with optional literal suffix.
                            end = path_text.find("@", 1)
                            if end >= 0:
                                rest = path_text[end+1:]
                                if rest:
                                    subpath_parts.append(rest)
                        if subpath_parts:
                            subpath_str = ""
                            for sp in subpath_parts:
                                if sp.startswith("["):
                                    subpath_str += sp
                                elif subpath_str:
                                    subpath_str += "." + sp
                                else:
                                    subpath_str = sp
                            if subpath_str.startswith("["):
                                target = base_target + subpath_str
                            else:
                                target = base_target + "." + subpath_str

        # Resolve !HEXOID references to member names for non-I/O tags.
        # I/O tags keep their !HEXOID refs for ControllerBuilder resolution.
        if ":" not in tag_name:
            hex_oid_map = _build_hex_oid_map(self._cur)
            if hex_oid_map:
                resolved_comments = []
                for ref, text in comment_results:
                    if ref and '!' in ref:
                        def _replace(m):
                            hex_val = int(m.group(1), 16)
                            name = hex_oid_map.get(hex_val)
                            return name if name else m.group(0)
                        new_ref = re.sub(r'!([0-9A-F]{8})', _replace, ref)
                        resolved_comments.append((new_ref, text))
                    else:
                        resolved_comments.append((ref, text))
                comment_results = resolved_comments

        # Normalize comment paths to full Studio 5000 addresses.
        # Empty paths (tag-level description) are kept as-is.
        # I/O .!HEXOID and !HEXOID paths are kept as-is for ControllerBuilder.
        # Numeric paths like "20" become "tag_name.20".
        # Bracket paths like "0]" become "tag_name[0]".
        # Resolved Member.Bit paths get the tag name prefix prepended.
        normalized = []
        for ref, text in comment_results:
            if not ref:
                normalized.append((ref, text))
            elif ref.startswith(".!") or ref.startswith("!"):
                # I/O tag paths — keep for ControllerBuilder resolution
                normalized.append((ref, text))
            elif ref.endswith("]"):
                if "[" in ref and not ref.startswith("["):
                    # Member[N] — resolved from !HEXOID[N], keeps dot prefix
                    normalized.append((f"{tag_name}.{ref}", text))
                elif ref.startswith("["):
                    # [N] — bare array element
                    normalized.append((f"{tag_name}{ref}", text))
                else:
                    # N] — bare element index (DB should fix to [N])
                    normalized.append((f"{tag_name}[{ref}", text))
            elif ref.isdigit():
                normalized.append((f"{tag_name}.{ref}", text))
            elif "]." in ref:
                # Array element.member.bit refs like "[0].5", "[0].Flags.1"
                normalized.append((f"{tag_name}{ref}", text))
            elif "." in ref and ":" not in ref:
                # Member.Bit — resolved from hex OID, needs tag name prefix.
                # ref may already carry its own leading "." (e.g. resolved from a
                # ".!HEXOID" reference into ".Gain") — avoid inserting a second dot.
                # For array tags, add [0] since no element index is specified.
                sep = "" if ref.startswith(".") else "."
                if dimensions is not None:
                    normalized.append((f"{tag_name}[0]{sep}{ref}", text))
                else:
                    normalized.append((f"{tag_name}{sep}{ref}", text))
            else:
                normalized.append((ref, text))

        # Constant is omitted entirely for Alias tags (they have no value of
        # their own) and for opaque built-in types that don't support it
        # (the same _SKIP_DECORATED set); every other Base tag shows it
        # explicitly as "true" or "false" (verified against a real project).
        if tag_type == "Alias" or data_type.upper() in _SKIP_DECORATED:
            constant: Union[str, None] = None
        else:
            constant = "true" if constant_flag else "false"

        return Tag(
            tag_name,
            tag_name,
            tag_type,
            data_type,
            radix,
            external_access,
            constant,
            dimensions,
            target,
            dti,
            normalized,
            initial_value,
        )


def _aoi_tag_usage_flags(ext01: bytes) -> int:
    """Return the usage-flags byte from an AOI tag record's ext[0x01] blob.

    Returns 0 if the record is too short.  The caller is responsible for
    interpreting the bits (0x04=Input, 0x08=Output; both set = InOut;
    neither set = local tag).
    """
    return ext01[0x20E] if len(ext01) > 0x20E else 0


def _aoi_tag_data_type(cur, raw_rec: bytes) -> str:
    """Look up the DataType name for an AOI tag record.

    The DataType OID is stored at offset 0x2A in the raw parameter record
    as a little-endian u32.  We look it up in the comps table by object_id.
    """
    if len(raw_rec) < 0x2E:
        return ""
    dt_oid = struct.unpack_from("<I", raw_rec, 0x2A)[0]
    cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(dt_oid))
    row = cur.fetchone()
    return row[0] if row else ""


@dataclass
class ParameterBuilder(L5xElementBuilder):
    """Build a Parameter from an AOI RxTagCollection child record."""

    def build(self) -> Parameter:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions (array size) at raw record offset 0x1A as u32; 0 means scalar.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts: Dict[int, bytes] = {
                er.attribute_id: bytes(er.value) for er in r.extended_records
            }
        except Exception:
            return Parameter(name, name, "Base", data_type, "Input", None, "false", "false", "Read/Write", None, dimensions)

        ext01 = exts.get(0x01, b"")
        flags = _aoi_tag_usage_flags(ext01)

        usage_bits = flags & 0x0C
        if usage_bits == 0x04:
            usage = "Input"
        elif usage_bits == 0x08:
            usage = "Output"
        else:
            usage = "InOut"

        required = "true" if (flags & 0x20) else "false"
        visible = "true" if (flags & 0x40) else "false"

        # ExternalAccess (u16 at ext01[0x21E])
        # MESSAGE-type InOut parameters don't carry Constant in L5X; all others do.
        if usage == "InOut":
            external_access = None
            constant: Union[str, None] = None if data_type == "MESSAGE" else "false"
        elif len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
            constant = None
        else:
            external_access = "Read/Write"
            constant = None

        # Radix (high nibble of ext01[0x20F]).  InOut params for complex/UDT types
        # omit Radix; InOut params for scalar/array types (BOOL, INT, DINT, REAL, etc.)
        # still carry Radix in the golden L5X, so include it whenever the radix index
        # is non-zero regardless of Usage.
        if not data_type or len(ext01) <= 0x20F:
            radix: Union[str, None] = None
        else:
            radix_idx = ext01[0x20F] >> 4
            radix = radix_enum(radix_idx) if radix_idx != 0 else None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific parameter description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND member_ref=? LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return Parameter(
            name,
            name,
            "Base",
            data_type,
            usage,
            radix,
            required,
            visible,
            external_access,
            constant,
            dimensions,
            description,
        )


@dataclass
class LocalTagBuilder(L5xElementBuilder):
    """Build a LocalTag from an AOI RxTagCollection child record."""

    def build(self) -> LocalTag:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name = row[0]
        raw_rec = bytes(row[1])

        data_type = _aoi_tag_data_type(self._cur, raw_rec)

        # Dimensions at raw record offset 0x1A.
        dimensions: Union[str, None] = None
        if len(raw_rec) >= 0x1E:
            dim_val = struct.unpack_from("<I", raw_rec, 0x1A)[0]
            if dim_val:
                dimensions = str(dim_val)

        try:
            r = RxGeneric.from_bytes(raw_rec)
            exts: Dict[int, bytes] = {
                er.attribute_id: bytes(er.value) for er in r.extended_records
            }
        except Exception:
            return LocalTag(name, name, data_type, dimensions, None, "Read/Write")

        ext01 = exts.get(0x01, b"")
        if len(ext01) > 0x21F:
            ea_val = struct.unpack_from("<H", ext01, 0x21E)[0]
            external_access = external_access_enum(ea_val)
        else:
            external_access = "Read/Write"

        if len(ext01) > 0x20F:
            radix_idx = ext01[0x20F] >> 4
            radix: Union[str, None] = radix_enum(radix_idx) if radix_idx != 0 else None
        else:
            radix = None

        # --- Description ---
        # Use bytes [14:18] of the comps record as member_ref to identify the
        # specific local tag description in the comments table.
        description: Union[str, None] = None
        if len(raw_rec) >= 18:
            member_ref = struct.unpack_from("<I", raw_rec, 14)[0]
            if member_ref:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND member_ref=? LIMIT 1",
                    ((r.comment_id * 0x10000) + r.cip_type, member_ref),
                )
                desc_row = self._cur.fetchone()
                if desc_row and desc_row[0]:
                    description = desc_row[0]

        return LocalTag(name, name, data_type, dimensions, radix, external_access, description)


def routine_type_enum(idx: int) -> str:
    if idx == 0:
        return "TypeLess"
    if idx == 1:
        return "RLL"
    if idx == 2:
        return "FBD"
    if idx == 3:
        return "SFC"
    if idx == 4:
        return "ST"
    if idx == 5:
        return "External"
    if idx == 6:
        return "Encrypted"
    return "Typeless"


@dataclass
class RoutineBuilder(L5xElementBuilder):
    def build(self) -> Routine:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        try:
            r = RxGeneric.from_bytes(results[0][3])
        except Exception as e:
            return Routine(results[0][0], results[0][0], "", [])

        record = results[0][3]
        name = results[0][0]
        routine_type = routine_type_enum(
            struct.unpack_from("<H", r.record_buffer, 0x30)[0]
        )

        self._cur.execute(
            "SELECT rm.object_id, r.rung FROM region_map rm "
            "LEFT JOIN rungs r ON r.object_id = rm.object_id "
            "WHERE rm.parent_id=" + str(self._object_id) + " ORDER BY rm.unknown"
        )
        rows = [(row[0], row[1]) for row in self._cur.fetchall() if row[1] is not None]
        rung_ids = [row[0] for row in rows]
        rungs = [row[1] for row in rows]

        # Resolve &hexid: placeholders (object ID references) to comp names.
        # The ACD binary stores tag references as &XXXXXXXX: where XXXXXXXX is the
        # object_id in hex. Batch-resolve all unique IDs to avoid per-rung queries.
        import re as _re
        all_hex = set(_re.findall(r'&([0-9a-f]{8}):', " ".join(r for r in rungs if r)))
        if all_hex:
            id_to_name: Dict[int, str] = {}
            for hex_id in all_hex:
                oid = int(hex_id, 16)
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=?", (oid,))
                row2 = self._cur.fetchone()
                if row2:
                    id_to_name[hex_id] = row2[0]
            if id_to_name:
                def _resolve(rung: str) -> str:
                    return _re.sub(
                        r'&([0-9a-f]{8}):',
                        lambda m: (id_to_name[m.group(1)] + ":") if m.group(1) in id_to_name else m.group(0),
                        rung,
                    )
                rungs = [_resolve(r) if r else r for r in rungs]

        # Fetch rung-level comments from the comments table.
        # Rung comments are stored under the routine's own comment parent key
        # (comment_id * 0x10000 + cip_type), as AsciiRecord (record_type=1) entries
        # where rung_content != 0 (distinguishes user rung comments from internal
        # metadata strings like FBDRoutineDescription which have rung_content=0).
        # The object_id field is the 1-based rung index (rung 0 -> object_id=1).
        rung_comments: Dict[int, str] = {}
        try:
            comment_parent = (r.comment_id * 0x10000) + r.cip_type
            self._cur.execute(
                "SELECT object_id, record_string FROM comments "
                "WHERE parent=? AND record_type=1 AND rung_content!=0",
                (comment_parent,),
            )
            for obj_id, rec_str in self._cur.fetchall():
                rung_index = obj_id - 1  # convert 1-based to 0-based
                if rung_index >= 0 and rec_str and rung_index not in rung_comments:
                    rung_comments[rung_index] = rec_str
        except Exception:
            pass

        return Routine(name, name, routine_type, rungs, rung_ids, rung_comments)


def _parse_fffeff(data: bytes, offset: int):
    """Parse one fffeff-encoded string at offset. Returns (str, new_offset)."""
    if offset + 3 > len(data) or not (data[offset] == 0xFF and data[offset+1] == 0xFE and data[offset+2] == 0xFF):
        return "", offset
    length = data[offset+3]
    s = data[offset+4:offset+4+length*2].decode("utf-16-le", errors="replace")
    return s, offset + 4 + length * 2


def _filetime_to_iso(ft: int) -> str:
    """Convert a Windows FILETIME (100-ns units since 1601-01-01) to ISO8601.

    Returns "" for an empty or out-of-range value. Some AOI nameless records
    carry a corrupt FILETIME (the variable-length field walk can drift and read
    8 garbage bytes), which would otherwise overflow ``datetime`` for years
    beyond 9999 and abort the whole export with ``OverflowError``.
    """
    if not ft:
        return ""
    try:
        dt = datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)
    except (OverflowError, OSError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_aoi_nameless(data: bytes) -> dict:
    """Extract AOI metadata from its large nameless record."""
    result: dict = {}

    offset = 0x1A
    # Three empty fffeff strings
    for _ in range(3):
        _, offset = _parse_fffeff(data, offset)

    # 8 bytes (unknown - some kind of date, skip)
    offset += 8

    # 2-byte constant (0x0002 observed)
    offset += 2

    # CreatedBy
    result["created_by"], offset = _parse_fffeff(data, offset)

    # Software revision at creation time (skip - we want the current one later)
    _, offset = _parse_fffeff(data, offset)

    # 4 zero bytes
    offset += 4

    # Empty fffeff placeholder
    _, offset = _parse_fffeff(data, offset)

    # CreatedDate FILETIME (8 bytes, Windows FILETIME in 100-ns units)
    result["created_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, offset)[0])
    offset += 8

    # EditedBy
    result["edited_by"], offset = _parse_fffeff(data, offset)

    # SoftwareRevision (current)
    result["software_revision"], offset = _parse_fffeff(data, offset)

    # 4 bytes (01 00 00 00)
    offset += 4

    # RevisionExtension
    rev_ext, offset = _parse_fffeff(data, offset)
    result["revision_extension"] = rev_ext or None

    # EditedDate FILETIME (always last 8 bytes)
    result["edited_date"] = _filetime_to_iso(struct.unpack_from("<Q", data, len(data) - 8)[0])

    return result


@dataclass
class AoiBuilder(L5xElementBuilder):
    def build(self) -> AOI:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        aoi_record = bytes(results[0][3])
        name = results[0][0]

        # --- Revision (major.minor) from ext[0x01] ---
        _r_aoi: Union[RxGeneric, None] = None
        try:
            r = RxGeneric.from_bytes(aoi_record)
            _r_aoi = r
            exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}
            e01 = exts.get(0x01, b"")
            rev_major = struct.unpack_from("<H", e01, 0x1A)[0] if len(e01) > 0x1B else 1
            rev_minor = struct.unpack_from("<H", e01, 0x1C)[0] if len(e01) > 0x1D else 0
        except Exception:
            rev_major, rev_minor = 1, 0
        revision = f"{rev_major}.{rev_minor}"

        # --- Vendor from comps record ---
        vlen = struct.unpack_from("<H", aoi_record, 0xA6)[0] if len(aoi_record) > 0xA8 else 0
        vendor: Union[str, None] = aoi_record[0xA8:0xA8+vlen].decode("utf-8", errors="replace") if vlen > 0 else None

        # --- Metadata from large nameless record ---
        self._cur.execute(
            "SELECT record FROM nameless WHERE parent_id=" + str(self._object_id)
            + " ORDER BY LENGTH(record) DESC LIMIT 1"
        )
        nameless_row = self._cur.fetchone()
        if nameless_row and len(bytes(nameless_row[0])) > 50:
            meta = _parse_aoi_nameless(bytes(nameless_row[0]))
        else:
            meta = {"created_by": "", "created_date": "", "edited_by": "", "edited_date": "",
                    "software_revision": "", "revision_extension": None}

        parameters: List[Parameter] = []
        local_tags: List[LocalTag] = []
        routines: List[Routine] = []

        # --- Extract Parameters and LocalTags from RxTagCollection ---
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        tag_coll_row = self._cur.fetchone()
        if tag_coll_row:
            tag_coll_oid = tag_coll_row[0]
            self._cur.execute(
                "SELECT object_id, record FROM comps WHERE parent_id="
                + str(tag_coll_oid)
                + " AND record_type != 512"
                + " ORDER BY seq_number"
            )
            for child_oid, child_rec in self._cur.fetchall():
                child_rec = bytes(child_rec)
                # Determine whether this is a parameter or a local tag by inspecting
                # ext01[0x20E]: bits 0x04 (Input) or 0x08 (Output) indicate a parameter.
                is_param = False
                try:
                    r_child = RxGeneric.from_bytes(child_rec)
                    exts_child: Dict[int, bytes] = {
                        er.attribute_id: bytes(er.value)
                        for er in r_child.extended_records
                    }
                    ext01 = exts_child.get(0x01, b"")
                    flags = _aoi_tag_usage_flags(ext01)
                    is_param = bool(flags & 0x0C)
                except Exception:
                    pass

                if is_param:
                    try:
                        parameters.append(ParameterBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass
                else:
                    try:
                        local_tags.append(LocalTagBuilder(self._cur, child_oid).build())
                    except Exception:
                        pass

        # --- Extract Routines from RxRoutineCollection ---
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        routine_coll_row = self._cur.fetchone()
        if routine_coll_row:
            routine_coll_oid = routine_coll_row[0]
            self._cur.execute(
                "SELECT object_id FROM comps WHERE parent_id=" + str(routine_coll_oid)
            )
            for (child_oid,) in self._cur.fetchall():
                try:
                    routines.append(RoutineBuilder(self._cur, child_oid).build())
                except Exception:
                    pass

        # --- Description + RevisionNote ---
        aoi_description: Union[str, None] = None
        revision_note = ""
        if _r_aoi is not None:
            aoi_comment_parent = (_r_aoi.comment_id * 0x10000) + _r_aoi.cip_type
            self._cur.execute(
                "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 LIMIT 1",
                (aoi_comment_parent,),
            )
            desc_row = self._cur.fetchone()
            if desc_row and desc_row[0]:
                aoi_description = desc_row[0]
            try:
                self._cur.execute(
                    "SELECT record_string FROM comments WHERE parent=? AND tag_reference='__REVISION_NOTE__' LIMIT 1",
                    (aoi_comment_parent,),
                )
                rn_row = self._cur.fetchone()
                if rn_row:
                    revision_note = rn_row[0] or ""
            except Exception:
                pass

        return AOI(
            name, name, revision,
            meta["revision_extension"],
            vendor,
            "false", "false", "false",
            meta["created_date"], meta["created_by"],
            meta["edited_date"], meta["edited_by"],
            meta["software_revision"],
            parameters, local_tags, routines,
            aoi_description,
            revision_note,
        )


@dataclass
class ProgramBuilder(L5xElementBuilder):
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)
    _redundancy_enabled: bool = field(default=False)

    def build(self) -> Program:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE object_id="
            + str(self._object_id)
        )
        results = self._cur.fetchall()

        prog_record = bytes(results[0][3])
        r = RxGeneric.from_bytes(prog_record)

        name = results[0][0]

        # --- MainRoutineName and FaultRoutineName from extended records ---
        # ext[0x12D] = MainRoutine object_id, ext[0x066] = FaultRoutine object_id
        exts: Dict[int, bytes] = {e.attribute_id: bytes(e.value) for e in r.extended_records}
        main_routine_name: Union[str, None] = None
        fault_routine_name: Union[str, None] = None
        if 0x12D in exts and len(exts[0x12D]) >= 4:
            main_oid = struct.unpack_from("<I", exts[0x12D], 0)[0]
            if main_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(main_oid))
                row = self._cur.fetchone()
                main_routine_name = row[0] if row else None
        if 0x066 in exts and len(exts[0x066]) >= 4:
            fault_oid = struct.unpack_from("<I", exts[0x066], 0)[0]
            if fault_oid:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(fault_oid))
                row = self._cur.fetchone()
                fault_routine_name = row[0] if row else None

        # --- Disabled flag from ext[0x01] at offset 0x24 ---
        # A u32 of 0xFFFFFFFF means the program is disabled; 0x00000000 means enabled.
        ext01 = exts.get(0x01, b"")
        disabled_flag = (
            struct.unpack_from("<I", ext01, 0x24)[0] != 0
            if len(ext01) >= 0x28
            else False
        )
        disabled = "true" if disabled_flag else "false"

        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxRoutineCollection'"
        )
        collection_results = self._cur.fetchall()

        routines = []
        if collection_results:
            collection_id = collection_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id="
                + str(collection_id)
            )
            routine_results = self._cur.fetchall()
            for child in routine_results:
                routines.append(RoutineBuilder(self._cur, child[1]).build())

        # Get the Program Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one program tag collection")

        tags: List[Tag] = []
        if results:
            self._cur.execute(
                "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
                + str(results[0][1])
            )
            results = self._cur.fetchall()
            for result in results:
                tag = TagBuilder(self._cur, result[1]).build()
                tag._data_types_map = self._data_types_map
                # Decode UDT initial values now that data_types_map is available.
                # Multi-dimensional arrays are supported: _decode_udt_initial_value
                # iterates the flat (row-major) element count, and Tag.to_xml's
                # UDT-array branch / _udt_array_to_xml re-derive the per-dimension
                # [i,j,k] indices from that same flat ordering when rendering.
                if tag._initial_value is None and tag._data_table_instance:
                    n = _count_array_elements(tag.dimensions)
                    udt_val = _decode_udt_initial_value(
                        self._cur, tag._data_table_instance, tag.data_type, n,
                        self._data_types_map,
                    )
                    if udt_val is not None:
                        tag._initial_value = udt_val
                tags.append(tag)

        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        # SynchronizeRedundancyDataAfterExecution: present only for redundant controllers.
        # The binary does not expose a per-program flag for this attribute — it is implicit
        # for all programs in a redundant controller project.
        sync_redundancy = "true" if self._redundancy_enabled else None

        return Program(name, name, "false", main_routine_name, fault_routine_name,
                       disabled, sync_redundancy, "false", tags, routines)


_TASK_TYPE_MAP = {1: "EVENT", 2: "PERIODIC", 4: "CONTINUOUS"}


@dataclass
class TaskBuilder(L5xElementBuilder):
    def build(self, comment_id_to_program: Dict[int, str]) -> Task:
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE object_id=" + str(self._object_id)
        )
        row = self._cur.fetchone()
        name, record = row[0], row[1]

        # All task config fields live within ext[0x01], accessed via absolute BLOB offsets.
        # These offsets were reverse-engineered from CIPDemo_RevEng.ACD.
        rate_us = struct.unpack_from("<I", record, 0x106C)[0]
        type_val = struct.unpack_from("<H", record, 0x10F6)[0]
        priority = struct.unpack_from("<H", record, 0x10F8)[0]
        watchdog_us = struct.unpack_from("<I", record, 0x110A)[0]
        disable_update = record[0x112E]

        task_type = _TASK_TYPE_MAP.get(type_val, "PERIODIC")
        rate_str = str(rate_us // 1000) if task_type != "CONTINUOUS" else None

        # Scheduled programs: ext[0x01] value starts at BLOB offset 0x5A.
        # Format: u16 count followed by N u32 comment_ids.
        prog_count = struct.unpack_from("<H", record, 0x5A)[0]
        scheduled_programs = []
        for i in range(prog_count):
            cid = struct.unpack_from("<I", record, 0x5A + 2 + i * 4)[0]
            prog_name = comment_id_to_program.get(cid)
            if prog_name:
                scheduled_programs.append(ScheduledProgram(prog_name, prog_name))

        event_info = None
        if task_type == "EVENT":
            event_info = EventInfo("EventInfo", "EVENT Instruction Only", "false")

        return Task(
            name,
            name,
            task_type,
            rate_str,
            str(priority),
            str(watchdog_us // 1000),
            "true" if disable_update else "false",
            "false",
            event_info,
            scheduled_programs,
        )


@dataclass
class ControllerBuilder(L5xElementBuilder):
    def build(self) -> Controller:
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id=0 AND record_type=256"
        )
        results = self._cur.fetchall()
        if len(results) != 1:
            raise Exception("Does not contain exactly one root controller node")

        r = RxGeneric.from_bytes(results[0][4])
        self._cur.execute(
            "SELECT tag_reference, record_string FROM comments WHERE parent="
            + str((r.comment_id * 0x10000) + r.cip_type)
        )
        comment_results = self._cur.fetchall()

        extended_records: Dict[int, bytes] = {}
        for extended_record in r.extended_records:
            extended_records[extended_record.attribute_id] = bytes(
                extended_record.value
            )

        def _decode_utf16(key):
            raw = extended_records.get(key)
            if raw is None or len(raw) < 2:
                return ""
            return bytes(raw[:-2]).decode("utf-16", errors="replace")

        sfc_execution_control = _decode_utf16(0x6F)
        sfc_restart_position = _decode_utf16(0x70)
        sfc_last_scan = _decode_utf16(0x71)

        # CommPath prefix (key 0x06A): may be in kaitai extended_records (usually empty),
        # or in the LastAttributeRecord appended after the counted records (value length
        # is stored as len_value where actual data = len_value - 4 bytes, UTF-16-LE).
        _comm_path_prefix: Union[str, None] = None
        if 0x06A in extended_records:
            _cp_raw = extended_records[0x06A]
            _cp_str = _cp_raw.decode("utf-16-le", errors="replace").rstrip("\x00")
            if _cp_str:
                _comm_path_prefix = _cp_str
        else:
            # LastAttributeRecord tail: located after the (count_record - 1) parsed records.
            # Header layout: parent_id(4) + unique_tag_id(4) + record_format_version(2) +
            #   cip_type(2) + comment_id(2) = 14 bytes, then main_record(60), then
            #   len_record(4) + count_record(4) = 82 bytes total before first AttributeRecord.
            _raw_record = bytes(results[0][4])
            _rec_offset = 82
            for _er in r.extended_records:
                _rec_offset += 4 + 4 + len(bytes(_er.value))
            _tail = _raw_record[_rec_offset:]
            if len(_tail) >= 8:
                _last_attr_id = struct.unpack_from("<I", _tail, 0)[0]
                _last_len_value = struct.unpack_from("<I", _tail, 4)[0]
                if _last_attr_id == 0x06A and _last_len_value >= 4:
                    _actual_len = _last_len_value - 4
                    if len(_tail) >= 8 + _actual_len and _actual_len > 0:
                        _cp_val = _tail[8: 8 + _actual_len]
                        _cp_str = _cp_val.decode("utf-16-le", errors="replace").rstrip("\x00")
                        if _cp_str:
                            _comm_path_prefix = _cp_str

        if 0x75 in extended_records:
            sn_raw = hex(struct.unpack("<I", extended_records[0x75])[0])[2:].zfill(8)
            project_sn = f"16#{sn_raw[:4]}_{sn_raw[4:]}"
        else:
            project_sn = "Unknown"

        raw_modified_date = struct.unpack("<Q", extended_records[0x66])[0] / 10000000
        last_modified_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_modified_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        raw_created_date = struct.unpack("<Q", extended_records[0x65])[0] / 10000000
        project_creation_date = (
            datetime(1601, 1, 1) + timedelta(seconds=raw_created_date)
        ).strftime("%a %b %d %H:%M:%S %Y")

        # MajorRev and MinorRev: derived later from the root controller module (see below)

        # MajorFaultProgram from ext[0x068] OID → comp_name lookup
        major_fault_program: Union[str, None] = None
        if 0x068 in extended_records and len(extended_records[0x068]) >= 4:
            mfp_oid = struct.unpack_from("<I", extended_records[0x068])[0]
            if mfp_oid and mfp_oid != 0xFFFFFFFF:
                self._cur.execute("SELECT comp_name FROM comps WHERE object_id=" + str(mfp_oid))
                mfp_row = self._cur.fetchone()
                major_fault_program = mfp_row[0] if mfp_row else None

        # RedundancyEnabled: controller extended record 0x001, byte offset 0x0E.
        # This byte is 0x01 for redundant controllers (e.g. 1756-L85E in redundancy mode)
        # and 0x00 for non-redundant controllers.
        _ctrl_ext001 = extended_records.get(0x001, b"")
        redundancy_enabled: bool = bool(_ctrl_ext001[0x0E]) if len(_ctrl_ext001) > 0x0E else False

        self._object_id = results[0][1]
        controller_name = results[0][0]

        # Get the data types
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxDataTypeCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller data type collection")

        _data_type_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_data_type_id)
        )
        results = self._cur.fetchall()

        data_types: List[DataType] = []
        # all_data_types_map includes ProductDefined types (excluded from L5X output but
        # needed for generating Decorated XML for tags that reference those types).
        all_data_types_map: Dict[str, DataType] = {}
        for result in results:
            _data_type_object_id = result[1]
            dt = DataTypeBuilder(self._cur, _data_type_object_id).build()
            all_data_types_map[dt.name.upper()] = dt
            if dt.cls == "User":
                data_types.append(dt)

        # data_types_map: case-insensitive name → DataType for all types (User + ProductDefined).
        # Used by Tag.to_xml() when generating Decorated XML.
        data_types_map: Dict[str, DataType] = all_data_types_map

        # Get the Controller Scoped Tags
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTagCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller tag collection")
        _tag_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_tag_collection_object_id)
        )
        results = self._cur.fetchall()
        tags: List[Tag] = []
        for result in results:
            _tag_object_id = result[1]
            tag = TagBuilder(self._cur, _tag_object_id).build()
            tag._data_types_map = data_types_map
            # Decode UDT initial values now that data_types_map is available.
            # Multi-dimensional arrays are supported: _decode_udt_initial_value
            # iterates the flat (row-major) element count, and Tag.to_xml's
            # UDT-array branch / _udt_array_to_xml re-derive the per-dimension
            # [i,j,k] indices from that same flat ordering when rendering.
            if tag._initial_value is None and tag._data_table_instance:
                n = _count_array_elements(tag.dimensions)
                udt_val = _decode_udt_initial_value(
                    self._cur, tag._data_table_instance, tag.data_type, n,
                    data_types_map,
                )
                if udt_val is not None:
                    tag._initial_value = udt_val
            # Exclude internal placeholder/auto-generated tags:
            #   "$..."      -- hash-named objects (not a valid Rockwell tag-name start
            #                  character; real tags always start with a letter or "_").
            #   "__l0"/"__CLONE" -- known Rockwell-internal AOI clone/runtime tag prefixes
            #                  (same set Tag._l5x_exclude uses). A bare "__" prefix is a
            #                  legal user tag name (e.g. "__MyFlag") and must NOT be
            #                  excluded here, or a legitimately-named user tag would be
            #                  silently dropped from the object model entirely.
            if (
                tag.data_type
                and not tag.name.startswith("$")
                and not tag.name.startswith("__l0")
                and not tag.name.startswith("__CLONE")
            ):
                tags.append(tag)

        # Resolve comment paths to full Studio 5000 addresses for I/O tags
        for tag in tags:
            if ":" not in tag.name:
                continue
            if not tag._comments:
                continue
            dt = data_types_map.get(tag.data_type.upper())
            data_member = None
            if dt:
                for m in dt.members:
                    if m.bit_number is None and m.name.upper() not in ("FAULT", "STATUS"):
                        data_member = m
                        break
                if data_member is None:
                    for m in dt.members:
                        if m.bit_number is None:
                            data_member = m
                            break
            resolved = []
            for path, text in tag._comments:
                if not path:
                    resolved.append((path, text))
                elif path.startswith(".!"):
                    # For .! references (type 16/17 bit-level I/O comments),
                    # find the first array member (dimension > 0) since these
                    # references always target array elements like Data[N].
                    array_member = None
                    if dt:
                        for m in dt.members:
                            if m.bit_number is None and m.dimension > 0:
                                array_member = m
                                break
                    _dm = array_member or data_member
                    if not _dm:
                        resolved.append((path, text))
                        continue
                    inner = path[2:]
                    if "[" in inner and "." in inner and inner.index("[") < inner.rindex("."):
                        hex_oid, bracket_part = inner.split("[", 1)
                        array_idx, bit_part = bracket_part.rsplit(".", 1)
                        array_idx = array_idx.rstrip("]")
                        resolved.append((f"{tag.name}.{_dm.name}[{array_idx}].{bit_part}", text))
                    elif "[" in inner:
                        hex_oid, suffix = inner.split("[", 1)
                        suffix = suffix.rstrip("]")
                        resolved.append((f"{tag.name}.{_dm.name}[{suffix}]", text))
                    else:
                        resolved.append((path, text))
                elif path.startswith("!"):
                    if not data_member:
                        resolved.append((path, text))
                        continue
                    rest = path[1:]
                    if "." in rest:
                        hex_oid, suffix = rest.split(".", 1)
                        if data_member.dimension > 0:
                            resolved.append((f"{tag.name}.{data_member.name}[{suffix}]", text))
                        else:
                            resolved.append((f"{tag.name}.{data_member.name}.{suffix}", text))
                    elif "[" in rest:
                        hex_oid, suffix = rest.split("[", 1)
                        suffix = suffix.rstrip("]")
                        resolved.append((f"{tag.name}.{data_member.name}[{suffix}]", text))
                    else:
                        resolved.append((path, text))
                elif path.endswith("]"):
                    resolved.append((f"{tag.name}[{path}", text))
                elif path.isdigit():
                    resolved.append((f"{tag.name}.{path}", text))
                else:
                    resolved.append((path, text))
            tag._comments = resolved

        # Get the Program Collection and get the programs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxProgramCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one controller program collection")

        _program_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_program_collection_object_id)
        )
        results = self._cur.fetchall()
        programs: List[Program] = []
        for result in results:
            _program_object_id = result[1]
            programs.append(
                ProgramBuilder(self._cur, _program_object_id, data_types_map, redundancy_enabled).build()
            )

        # Build comment_id → program name map for task scheduled-program resolution.
        # comment_id is a u16 at BLOB offset 0x0C in each program's RxGeneric record.
        self._cur.execute(
            "SELECT comp_name, record FROM comps WHERE parent_id=" + str(_program_collection_object_id)
        )
        comment_id_to_program: Dict[int, str] = {
            struct.unpack_from("<H", rec, 0x0C)[0]: pname
            for pname, rec in self._cur.fetchall()
        }

        # Get the Task Collection and build Tasks
        self._cur.execute(
            "SELECT comp_name, object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxTaskCollection'"
        )
        task_coll_results = self._cur.fetchall()
        tasks: List[Task] = []
        if task_coll_results:
            _task_collection_object_id = task_coll_results[0][1]
            self._cur.execute(
                "SELECT comp_name, object_id FROM comps WHERE parent_id="
                + str(_task_collection_object_id)
                + " AND record_type=256"
            )
            for task_result in self._cur.fetchall():
                tasks.append(TaskBuilder(self._cur, task_result[1]).build(comment_id_to_program))

        # Get the AOI Collection and get the AOIs
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxUDIDefinitionCollection'"
        )
        results = self._cur.fetchall()
        if len(results) > 1:
            raise Exception("Contains more than one AOI collection")
        _aoi_collection_object_id = results[0][1]
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record_type FROM comps WHERE parent_id="
            + str(_aoi_collection_object_id)
            + " AND record_type=256"
        )
        results = self._cur.fetchall()
        aois: List[AOI] = []
        for result in results:
            _aoi_object_id = result[1]
            aois.append(AoiBuilder(self._cur, _aoi_object_id).build())

        # Get the Module (IO) Collection and build all Module elements.
        self._cur.execute(
            "SELECT object_id FROM comps WHERE parent_id="
            + str(self._object_id)
            + " AND comp_name='RxMapDeviceCollection'"
        )
        row = self._cur.fetchone()
        if row is None:
            modules: List[Module] = []
        else:
            coll_oid = row[0]
            self._cur.execute(
                "SELECT comp_name, object_id, record FROM comps WHERE parent_id="
                + str(coll_oid)
                + " ORDER BY seq_number"
            )
            mod_rows = self._cur.fetchall()

            # First pass: build modid→name map so child modules can resolve their parent name.
            from acd.generated.comps.rx_generic import RxGeneric as _RxG
            modid_to_name: Dict[int, str] = {}
            for db_name, mod_oid, mod_rec in mod_rows:
                display_name = "?" if (db_name.startswith("$") and db_name.endswith("$")) else db_name
                try:
                    r = _RxG.from_bytes(bytes(mod_rec))
                    if r.cip_type == 0x69:
                        exts = {er.attribute_id: bytes(er.value) for er in r.extended_records}
                        e1 = exts.get(0x001, b"")
                        if len(e1) >= 0x30:
                            modid = struct.unpack("<I", e1[0x2C:0x30])[0]
                            modid_to_name[modid] = display_name
                except Exception:
                    pass

            # Second pass: build Module objects.
            modules = []
            for _, mod_oid, _ in mod_rows:
                modules.append(
                    ModuleBuilder(self._cur, mod_oid, modid_to_name).build()
                )

            # Third pass: compute (parent_name, parent_port_id) → child count,
            # then inject the relevant sub-dict into each Module as _port_child_counts.
            child_counts: Dict[tuple, int] = {}
            for m in modules:
                k = (m.parent_module, m.parent_mod_port_id)
                child_counts[k] = child_counts.get(k, 0) + 1
            for m in modules:
                m._port_child_counts = {
                    port_id: child_counts.get((m.name, port_id), 0)
                    for port_id in range(1, 20)
                    if (m.name, port_id) in child_counts
                }

        # ProcessorType is the CatalogNumber of the root controller module (the one
        # whose parent is itself, i.e. MajorFault="true").
        processor_type = next(
            (m.catalog_number for m in modules if m.major_fault == "true" and m.catalog_number),
            None,
        )

        # MajorRev and MinorRev come from the firmware version of the Local (backplane
        # controller) module, stored in its ext[0x01] bytes [0x08] and [0x09].
        local_module = next(
            (m for m in modules if m.name == "Local"),
            next((m for m in modules if m.major_fault == "true"), None),
        )
        if local_module is not None:
            major_rev = str(local_module.major)
            minor_rev = str(local_module.minor)
        else:
            major_rev = "0"
            minor_rev = "0"

        # CommPath: combine the stored path prefix (ends with "\") with the controller
        # module's backplane slot number (e.g. "EthernetModule\192.168.1.10\Backplane\4").
        comm_path: Union[str, None] = None
        if _comm_path_prefix is not None:
            _ctrl_slot = next(
                (m._slot for m in modules if m.major_fault == "true"), None
            )
            if _ctrl_slot is not None:
                comm_path = _comm_path_prefix + str(_ctrl_slot)

        return Controller(
            controller_name,
            "Target",
            controller_name,
            processor_type,
            major_rev,
            minor_rev,
            major_fault_program,
            project_creation_date,
            last_modified_date,
            sfc_execution_control,
            sfc_restart_position,
            sfc_last_scan,
            comm_path,
            project_sn,
            "false",        # MatchProjectToController
            "false",        # CanUseRPIFromProducer
            "0",            # InhibitAutomaticFirmwareUpdate
            "EnabledWithAppend",  # PassThroughConfiguration
            "true",         # DownloadProjectDocumentationAndExtendedProperties
            "true",         # DownloadProjectCustomProperties
            "false",        # ReportMinorOverflow
            "false",        # AutoDiagsEnabled
            "false",        # WebServerEnabled
            data_types,
            modules,
            tags,
            programs,
            tasks,
            aois,
            redundancy_enabled,
        )


@dataclass
class ProjectBuilder:
    quick_info_filename: PathLike

    def build(self) -> RSLogix5000Content:
        element = ET.parse(self.quick_info_filename)
        rslogix_content_element = element.find(".")
        if rslogix_content_element is not None:
            target_name = rslogix_content_element.attrib["Name"]

        schema_version_element = element.find("SchemaVersion")
        if schema_version_element is not None:
            schema_version_major = schema_version_element.attrib["Major"]
            schema_version_minor = schema_version_element.attrib["Minor"]
            schema_revision = f"{schema_version_major}.{schema_version_minor}"
        else:
            schema_revision = "1.0"

        # SWVersion reflects the Studio 5000 application version (e.g. "RSLogix 5000 v35.04"),
        # which is what RSLogix5000Content SoftwareRevision represents.  DeviceIdentity
        # MajorRevision/MinorRevision is the controller firmware version — a different value.
        sw_version_element = element.find("SWVersion")
        if sw_version_element is not None:
            sw_version_string = sw_version_element.attrib.get("String", "")
            # Extract the version number from the trailing "vXX.YY" portion.
            match = re.search(r"v(\d+\.\d+)$", sw_version_string.strip())
            if match:
                software_revision = match.group(1)
            else:
                # Unexpected format — fall back to DeviceIdentity firmware version.
                device_identity = element.find("DeviceIdentity")
                if device_identity is not None:
                    software_revision = (
                        f"{device_identity.attrib['MajorRevision']}"
                        f".{device_identity.attrib['MinorRevision']}"
                    )
                else:
                    software_revision = "33.01"
        else:
            # No SWVersion element — fall back to DeviceIdentity firmware version.
            device_identity = element.find("DeviceIdentity")
            if device_identity is not None:
                software_revision = (
                    f"{device_identity.attrib['MajorRevision']}"
                    f".{device_identity.attrib['MinorRevision']}"
                )
            else:
                software_revision = "33.01"

        target_type = "Controller"
        contains_context = "false"
        now = datetime.now()
        export_date = now.strftime("%a %b %d %H:%M:%S %Y")
        export_options = (
            "NoRawData L5KData DecoratedData ForceProtectedEncoding AllProjDocTrans"
        )
        return RSLogix5000Content(
            target_name,
            None,
            schema_revision,
            software_revision,
            target_name,
            target_type,
            contains_context,
            export_date,
            export_options,
        )


@dataclass
class DumpCompsRecords(L5xElementBuilder):
    base_directory: PathLike = Path("dump")

    def dump(self, parent_id: int = 0, log_file=None):
        self._cur.execute(
            f"SELECT comp_name, object_id, parent_id, record_type, record FROM comps WHERE parent_id={parent_id}"
        )
        results = self._cur.fetchall()

        for result in results:
            object_id = result[1]
            name = result[0]
            record = result[4]
            # Some comp names carry characters that are invalid in Windows paths
            # (e.g. AB module DataType names like "CHANNEL_DI_TIMESTAMP:O:0").
            # Sanitize only for filesystem use; log_file output keeps the original name.
            safe_name = re.sub(r'[<>:"|?*]', "_", name)
            new_path = Path(os.path.join(self.base_directory, safe_name))
            if os.path.exists(os.path.join(new_path)):
                shutil.rmtree(os.path.join(new_path))
            if not os.path.exists(os.path.join(new_path)):
                os.makedirs(new_path)
            with open(Path(os.path.join(new_path, safe_name + ".dat")), "wb") as file:
                log_file.write(
                    f"Class - {struct.unpack_from('<H', result[4], 0xA)[0]} Instance {struct.unpack_from('<H', result[4], 0xC)[0]}- {str(new_path) + '/' + name}\n"
                )
                file.write(record)

            DumpCompsRecords(self._cur, object_id, new_path).dump(object_id, log_file)
