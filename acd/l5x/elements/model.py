# Auto-split from the former acd/l5x/elements.py -- see CLAUDE.md's structural-cleanup notes.
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Union

from acd.l5x.port_structures import PORT_STRUCTURES

from .base import L5xElement, _escape_xml_attr, _multiline_xml_text, _sanitize_xml_text
from .rendering import (
    _PRIMITIVE_RADIX,
    _SKIP_DECORATED,
    _build_comments_xml,
    _decorated_real_literal,
    _generate_decorated,
    _l5k_array_literal,
    _l5k_real_literal,
    _l5k_string_padded,
    _l5k_udt_literal,
    _string_literal_cdata,
    _udt_array_to_xml,
    _udt_scalar_to_xml,
)
from .types import _PRIM, _is_string_family_type, _string_family_capacity


@dataclass
class Member(L5xElement):
    name: str
    data_type: str
    dimension: int
    radix: str
    hidden: bool
    target: Union[str, None]      # BIT members only; None omits the attribute
    # Doubles as an internal value-decode hint for a plain BOOL member (scalar
    # or array) -- see _decode_single_udt_element/_decode_scalar_member -- so
    # it is NOT None for those too, even though Studio's own L5X only ever
    # emits BitNumber= for a genuine BIT pseudo-member. to_xml() below strips
    # it back out for any non-BIT data_type; do not rely on "bit_number is
    # None" alone to mean "omit from XML" the way target does.
    bit_number: Union[int, None]
    external_access: str
    _byte_offset: int = 0
    _description: Union[str, None] = field(default=None)

    @property
    def description(self) -> Union[str, None]:
        if self._description is None:
            return None
        return ' '.join(self._description.replace('\r\n', '\n').replace('\r', '\n').split('\n')).strip()

    def to_xml(self) -> str:
        base = super().to_xml()
        if self.data_type != "BIT" and self.bit_number is not None:
            # A plain BOOL member (scalar or array) carries bit_number purely
            # as an internal data-table decode hint -- real Studio 5000 never
            # emits BitNumber= for it, only for a genuine BIT pseudo-member.
            # Found via a real UDT's BOOL[32] member ("Ons") emitting a
            # spurious BitNumber="0" not present in Studio's own export.
            base = re.sub(r'\s*BitNumber="[^"]*"', '', base, count=1)
        if not self._description:
            return base
        desc = _multiline_xml_text(self._description)
        desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + base[idx + 1:]

def new_member(name: str, data_type: str, dimension: int = 0,
               radix: Union[str, None] = None,
               description: Union[str, None] = None) -> Member:
    """Construct a new, plain (non-BIT, non-hidden) UDT `Member` for
    insertion into an existing (or brand-new) `DataType.members` list --
    e.g. to add a field to a UDT before calling `export_datatype()`.

    `radix` defaults to the conventional value for `data_type`
    (`_PRIMITIVE_RADIX` -- "Decimal" for integer types, "Float" for
    REAL/LREAL) if not given, or "NullType" for a struct-typed member
    (a UDT/AOI name, matching how a real Studio 5000 UDT member typed as
    another struct is declared). `target`/`bit_number` are always `None` --
    this only builds a plain member; a BIT-overlay pseudo-member needs a
    hidden backing field and `_resolve_bit_target`, which only applies to
    members actually read back from a real ACD record, not one authored
    fresh in Python.

    `dimension` must be an `int` (0 for a scalar member, matching every
    ACD-decoded Member) -- unlike `radix`/`description`, `None` is NOT a
    valid "use the default" sentinel here, even though it's an easy mistake
    to make by analogy with this function's other two params. A real caller
    made exactly this mistake (`dimension=None` for a scalar member) and it
    silently propagated all the way into an unrelated crash deep inside
    export rendering, well after this function returned -- raising here
    instead catches it immediately, at the actual point of the mistake.

    Byte offset is never included -- Member._byte_offset is a decode-only
    detail, never emitted in XML (Studio 5000 recomputes a UDT's real
    physical member layout itself on import), so a newly-authored member
    needs no offset of its own.
    """
    if dimension is None:
        raise ValueError(
            "new_member(): dimension must be an int (0 for a scalar member), not None -- "
            "unlike radix/description, None is not a valid 'use the default' value here."
        )
    if radix is None:
        radix = _PRIMITIVE_RADIX.get(data_type.upper(), "NullType")
    return Member(
        name, name, data_type, dimension, radix, False, None, None,
        "Read/Write", _description=description,
    )

def new_tag(name: str, data_type: str, dimensions: Union[str, None] = None,
            description: Union[str, None] = None, value=None,
            external_access: str = "Read/Write") -> "Tag":
    """Construct a new, plain (Base, non-alias) `Tag` for insertion into
    `project.controller.tags` (controller scope) or a `Program.tags` list
    (program scope) before calling `export_routine()` -- e.g. to introduce
    a brand-new tag that new rung/ST logic references. Confirmed working
    end-to-end against real Studio 5000 (see CLAUDE.md "Native-import
    escape hatches" -- "creating a brand-new tag from scratch").

    `radix` is always derived from `data_type` (`_PRIMITIVE_RADIX`, e.g.
    "Decimal" for DINT, "Float" for REAL) -- or omitted (`None`) for a
    UDT-typed tag, matching every ACD-decoded struct-typed tag, which
    carries no Radix attribute of its own; there is no override parameter
    since a tag's Radix is never a free choice independent of its type the
    way a UDT member's occasionally is.

    `value`, if given, becomes `_initial_value` directly -- an int/float
    for a scalar primitive tag, or a dict/list matching the shape
    `_decode_udt_initial_value`/`_zero_value_for_member` would produce for
    a UDT-typed tag. Not validated against `data_type`/`dimensions` here;
    an omitted `value` (the default) simply renders as Studio's own zero
    default via the same `_zero_value_for_member`-style fallback every
    other tag's own rendering already goes through.

    `dimensions`, if given, is the comma-separated internal form (e.g.
    "10" or "4,3,2" -- NOT `Tag.to_xml()`'s own space-separated XML
    attribute rendering, which is handled automatically). Unlike
    `new_member()`'s `dimension`, `None` here really does mean "scalar" --
    `Tag.dimensions` (unlike `Member.dimension`) already uses `None` as its
    own scalar convention on every ACD-decoded tag, so there is no
    None-vs-0 ambiguity to guard against for this field.
    """
    # .get() with no default -- unlike new_member()'s "NullType" fallback --
    # returns None for a non-primitive (UDT/AOI) data_type, which is what a
    # real UDT-typed Tag.radix already is (see TagBuilder.build()).
    radix = _PRIMITIVE_RADIX.get(data_type.upper())
    return Tag(
        name, name, "Base", data_type, radix, external_access, None, dimensions,
        _comments=[("", description)] if description else [],
        _initial_value=value,
    )

@dataclass
class DataType(L5xElement):
    name: str
    family: str
    cls: str
    members: List[Member]
    _description: Union[str, None] = field(default=None)
    # Extra bytes a deleted-but-still-space-occupying member contributes to
    # this UDT's true physical size, on top of what summing visible members
    # gives -- see _get_type_size()'s docstring and DataTypeBuilder.build()
    # for how this is detected/estimated. 0 for the overwhelming majority of
    # UDTs (no deleted members at all).
    _dead_member_bytes: int = field(default=0)

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
        desc = _multiline_xml_text(self._description)
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

@dataclass
class Tag(L5xElement):
    """`.description` (a property) is the tag's own whole-tag description,
    collapsed to one line -- for per-element/per-bit descriptions or the
    raw (uncollapsed, multi-line) whole-tag description, use `._comments`
    (a list of (path, text) tuples; path "" is the tag's own description).
    `._initial_value` is the decoded current value (int/float/list/dict
    depending on type) -- there is no separate "default value" concept
    exposed here. To edit a description, mutate the ("", ...) entry in
    `._comments` directly (no setter method exists) -- see CLAUDE.md's
    "Native-import escape hatches" for the only proven way to get a tag
    edit into a real ACD (export_routine(), not save_acd())."""

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

    # The real implementation lives in base.py (_sanitize_xml_text) so
    # rendering.py can share it without a model<->rendering import cycle --
    # kept as a static method here too since Tag._sanitize_xml_text(...) was
    # the existing call shape used elsewhere in this class.
    _sanitize_xml_text = staticmethod(_sanitize_xml_text)

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
            desc_raw = _multiline_xml_text(desc_raw)
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
                # UDT scalar. Verified against a real project: this also
                # gets a <Data Format="L5K"> block alongside Decorated,
                # same as every other known-value tag shape (scalar
                # primitives, primitive arrays) -- previously only
                # Decorated was emitted here.
                #
                # <Structure DataType="..."> must preserve the real
                # DataType's own declared casing (e.g. "Timing", a project
                # UDT), not the all-uppercase dt_base used internally for
                # dict lookups -- verified against a real project's own
                # export of a "Timing"-typed tag, which a prior version
                # rendered as "TIMING". _udt_array_to_xml already got this
                # right (via its own display_name lookup); the scalar case
                # here never did.
                display_name = self._data_types_map.get(dt_base.upper())
                display_name = display_name.name if display_name is not None else dt_base
                l5k_udt_val = _l5k_udt_literal(dt_base, iv, self._data_types_map)
                body = (
                    f'<Structure DataType="{display_name}">'
                    f'{_udt_scalar_to_xml(dt_base, iv, self._data_types_map)}'
                    f'</Structure>'
                )
                data_xml = (
                    f'<Data Format="L5K">\n<![CDATA[{l5k_udt_val}]]>\n</Data>'
                    f'<Data Format="Decorated">\n{body}\n</Data>'
                )

            elif isinstance(iv, list) and iv and isinstance(iv[0], dict):
                # UDT array. Same L5K-block fix as the scalar case above --
                # verified against a real 25-element UDT array tag
                # (To_Skip[25]): the real export has both L5K and
                # Decorated blocks, previously only Decorated was emitted.
                dim_str = self.dimensions or "1"
                body = _udt_array_to_xml(
                    dt_base, iv, dim_str, self._data_types_map
                )
                if body:
                    l5k_udt_val = _l5k_udt_literal(dt_base, iv, self._data_types_map)
                    data_xml = (
                        f'<Data Format="L5K">\n<![CDATA[{l5k_udt_val}]]>\n</Data>'
                        f'<Data Format="Decorated">\n{body}\n</Data>'
                    )

            elif dt_base in _PRIM:
                radix_attr = _PRIMITIVE_RADIX.get(dt_base, "Decimal")

                if isinstance(iv, list):
                    # Array: emit every element at the tag's full declared
                    # length. A previous version omitted trailing zero
                    # elements -- that was never verified against real
                    # Studio 5000 output, and a real "Export Routine"
                    # sample directly contradicts it (a 256-element BOOL
                    # array was shown in full, not truncated). Worse: a
                    # truncated array was the likely trigger for a real
                    # Logix Designer crash (0x80004003 "Invalid pointer")
                    # during a native Import Routine attempt using a
                    # context tag built from this data -- so this is not
                    # just a cosmetic fidelity gap, do not reintroduce
                    # truncation here without solid verification first.
                    def _fmt_elem_val(val):
                        if dt_base in ("BOOL", "BIT"):
                            return "1" if val else "0"
                        if isinstance(val, float):
                            return _decorated_real_literal(val, in_array=True)
                        return str(int(val))

                    # NOTE: array-element comments go in the tag's standalone
                    # <Comments><Comment Operand="..."> block (see
                    # _build_comments_xml), never embedded inline here
                    # (verified: zero such occurrences in a real project).
                    elems_parts = []
                    for i in range(len(iv)):
                        val_attr = f'Value="{_fmt_elem_val(iv[i])}"'
                        elems_parts.append(
                            f'<Element Index="[{i}]" {val_attr}/>'
                        )
                    elems = "".join(elems_parts)
                    dim_str = self.dimensions or "1"
                    # A primitive array tag with a known value gets BOTH a
                    # <Data Format="L5K"> block AND the <Data Format="Decorated">
                    # block, same as the scalar case below -- verified against a
                    # real 256-element BOOL array tag; previously only Decorated
                    # was emitted here, silently dropping L5K entirely (this was
                    # the likely trigger for a real Logix Designer crash during
                    # a native Import Routine attempt using this tag as context).
                    l5k_array_val = _l5k_array_literal(dt_base, iv)
                    data_xml = (
                        f'<Data Format="L5K">\n<![CDATA[{l5k_array_val}]]>\n</Data>'
                        f'<Data Format="Decorated">\n'
                        # Verified against a real project: a top-level tag's
                        # own <Array> Decorated element has no Name= attribute
                        # (unlike a nested ArrayMember inside a Structure,
                        # which does carry one) -- previously this incorrectly
                        # included Name="{self.name}".
                        f'<Array DataType="{dt_base}" '
                        f'Dimensions="{dim_str}" Radix="{radix_attr}">{elems}</Array>\n'
                        f'</Data>'
                    )
                else:
                    # Scalar primitive. Verified against a real project: a
                    # scalar tag with a known non-zero/non-default initial
                    # value gets BOTH a <Data Format="L5K"> block (like the
                    # zero-value fallback below already does) AND the
                    # <Data Format="Decorated"> block -- previously only the
                    # Decorated block was emitted, silently dropping L5K.
                    # The Decorated element itself is a <DataValue
                    # DataType="..." Radix="..." Value="..."/> -- previously
                    # this used the DataType name as the element name
                    # itself (e.g. <BOOL Name="Tag" .../>), which doesn't
                    # match Studio 5000's actual output at all.
                    if dt_base in ("BOOL", "BIT"):
                        val_str = "1" if iv else "0"
                        l5k_val = val_str
                    elif isinstance(iv, float):
                        val_str = _decorated_real_literal(iv, in_array=False)
                        l5k_val = _l5k_real_literal(iv)
                    else:
                        val_str = str(int(iv))
                        l5k_val = val_str

                    data_xml = (
                        f'<Data Format="L5K">\n<![CDATA[{l5k_val}]]>\n</Data>'
                        f'<Data Format="Decorated">\n'
                        f'<DataValue DataType="{dt_base}" Radix="{radix_attr}" '
                        f'Value="{val_str}"/>\n'
                        f'</Data>'
                    )

        if not data_xml and not is_alias:
            # Scalar primitives with no successfully-decoded initial value
            # (rare -- normally the branch above handles scalar primitives)
            # still get BOTH Format="L5K" and Format="Decorated" blocks at
            # the zero/default value, matching the pattern verified above
            # for the known-value case (e.g. a real scalar BOOL tag with
            # value 0 still shows both blocks, not just L5K).
            # Scalar STRING gets Format="L5K" (the L5K encoder handles it separately; we emit
            # nothing here — Decorated is not used for scalar STRING tags).
            # Everything else (UDTs, arrays, TIMER, COUNTER, etc.) gets Format="Decorated".
            dt_base = self.data_type.split("[")[0].upper() if self.data_type else ""
            l5k_zero = _PRIMITIVE_L5K_ZERO.get(dt_base) if not self.dimensions else None
            if l5k_zero is not None:
                zero_radix = _PRIMITIVE_RADIX.get(dt_base, "Decimal")
                data_xml = (
                    f'<Data Format="L5K">\n<![CDATA[{l5k_zero}]]>\n</Data>'
                    f'<Data Format="Decorated">\n'
                    f'<DataValue DataType="{dt_base}" Radix="{zero_radix}" '
                    f'Value="{l5k_zero}"/>\n'
                    f'</Data>'
                )
            else:
                data_xml = ""

            if not data_xml and dt_base not in _SKIP_DECORATED and dt_base != "STRING":
                # Generate Decorated data for non-primitive / array types
                decorated = _generate_decorated(
                    dt_base, self.dimensions, self._data_types_map,
                    tag_name=self.name, comments=self._comments,
                )
                if decorated:
                    data_xml = decorated

        # --- Comments child element (element/member/bit descriptions) ---
        # Verified against a real L5X export: appears after <Description>
        # and before <Data>.
        comments_xml = _build_comments_xml(self.name, self._comments) if self._comments else ""

        if not desc_xml and not comments_xml and not data_xml:
            return base

        # Insert Description (if any), then Comments (if any), then Data (if any)
        # immediately after the opening tag.
        idx = base.index(">")
        return base[:idx + 1] + desc_xml + comments_xml + data_xml + base[idx + 1:]

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
        desc = _multiline_xml_text(self._description)
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
        desc = _multiline_xml_text(self._description)
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
            desc = _multiline_xml_text(self._description)
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
    """`.rungs[i]` is the plain-text ladder logic for rung i (`.type` is
    usually "RLL"; "ST" routines have their text in `._st_lines` instead,
    `.rungs` empty). `._rung_comments` maps a rung index to its comment
    text. `._rung_ids[i]` is the integer object_id patch_rungs() needs to
    identify rung i for a write-back edit -- NOT the same as its index i --
    or `None` for a rung inserted via `insert_rung()` that doesn't exist in
    the source file yet (only `export_routine()` can write that back, not
    `patch_rungs()`). Use `insert_rung()`/`delete_rung()` rather than
    mutating `.rungs`/`._rung_ids`/`._rung_comments` directly -- keeping all
    three in sync by hand is easy to get wrong."""

    name: str
    type: str
    rungs: List[str]
    _rung_ids: List[int] = field(default_factory=list)
    _rung_comments: Dict[int, str] = field(default_factory=dict)
    _description: Union[str, None] = field(default=None)
    _st_lines: List[str] = field(default_factory=list)

    def to_xml(self) -> str:
        desc_xml = ""
        if self._description:
            desc = _multiline_xml_text(self._description)
            desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'

        content = ""
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
                content = f'<RLLContent>{"".join(rung_xmls)}</RLLContent>'
        elif self.type == "ST" and self._st_lines:
            line_xmls = [
                f'<Line Number="{i}"><![CDATA[{text}]]></Line>'
                for i, text in enumerate(self._st_lines)
            ]
            content = f'<STContent>{"".join(line_xmls)}</STContent>'
        return f'<Routine Name="{_escape_xml_attr(self.name)}" Type="{self.type}">{desc_xml}{content}</Routine>'

    def insert_rung(self, index: int, text: str, comment: Union[str, None] = None) -> None:
        """Insert a new rung at `index`, shifting every existing rung at or
        after that position -- including its own `_rung_comments` entry, if
        any -- up by one.

        Doing this by hand (`routine.rungs.insert(index, text)` plus
        manually re-keying every `_rung_comments` entry at or after
        `index`) is a real footgun: getting the shift boundary wrong
        silently attaches an existing comment to the wrong rung. This
        method does both atomically.

        The new rung has no real ACD object_id (it doesn't exist in the
        source file yet), so `_rung_ids` gets a `None` placeholder at this
        position to keep it the same length as `.rungs`. This is harmless
        for `export_routine()` (which only ever reads `.rungs`/
        `._rung_comments`, never `_rung_ids`) -- but `patch_rungs()` can
        only edit an EXISTING rung's text, identified by a real object_id
        in `_rung_ids`, never create a new one. Use `export_routine()` for
        a routine containing a newly-inserted rung.
        """
        self.rungs.insert(index, text)
        self._rung_ids.insert(index, None)
        self._rung_comments = {
            (i + 1 if i >= index else i): c for i, c in self._rung_comments.items()
        }
        if comment is not None:
            self._rung_comments[index] = comment

    def delete_rung(self, index: int) -> None:
        """Delete the rung at `index`, dropping its own comment (if any)
        and shifting every later rung's `_rung_comments` entry down by one
        to match -- the same footgun as `insert_rung()`, in reverse.
        """
        del self.rungs[index]
        del self._rung_ids[index]
        self._rung_comments = {
            (i - 1 if i > index else i): c for i, c in self._rung_comments.items() if i != index
        }

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
            desc = _multiline_xml_text(self._description)
            inject += f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        if self._revision_note:
            note = _multiline_xml_text(self._revision_note)
            inject += f'<RevisionNote>\n<![CDATA[{note}]]>\n</RevisionNote>'
        return base[:idx + 1] + inject + base[idx + 1:]

@dataclass
class Program(L5xElement):
    """One program (`project.controller.programs[i]`). `.routines[j].rungs`
    is where ladder text lives; `.tags` here are PROGRAM-scope tags, separate
    from `project.controller.tags` (controller-scope)."""

    name: str
    test_edits: str
    main_routine_name: Union[str, None]  # None if absent (omitted from XML)
    fault_routine_name: Union[str, None]  # None if absent (omitted from XML)
    disabled: str
    synchronize_redundancy_data_after_execution: Union[str, None]  # None → omit attr
    use_as_folder: str
    tags: List[Tag]        # Tags section before Routines (matches L5X export order)
    routines: List[Routine]
    _description: Union[str, None] = field(default=None)

    def to_xml(self) -> str:
        base = super().to_xml()
        if not self._description:
            return base
        idx = base.index(">")
        desc = _multiline_xml_text(self._description)
        inject = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        return base[: idx + 1] + inject + base[idx + 1 :]

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
    """The controller-scope object graph (`project.controller`).

    `.tags`/`.data_types`/`.aois`/`.modules` here are CONTROLLER-scope only.
    There is no `.routines` on this class at all -- routines live inside
    programs: `.programs[i].routines[j]`, and program-scope tags live at
    `.programs[i].tags`, separate from `.tags` here. `.io_tags`/
    `.alias_tags` are convenience filtered views over `.tags`.
    """

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
    _description: Union[str, None] = field(default=None)
    # The SAME dict object every Tag's own _data_types_map references (case-insensitive
    # name -> DataType, User + ProductDefined) -- exposed here so a caller that appends a
    # newly-authored DataType to `.data_types` (the documented way to register a new UDT,
    # see export_datatype()) has a reliable way to also register it here, keeping every
    # already-built Tag's rendering in sync. See CLAUDE.md "Mutating a UDT with live tag
    # instances" for why this needed to exist: appending to `.data_types` alone does NOT
    # update this map (it's a different collection), so a tag whose value-rendering needs
    # to resolve the new type silently got a wrong-shaped fallback instead of a real error.
    _data_types_map: Dict[str, "DataType"] = field(default_factory=dict)

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
        desc_xml = ""
        if self._description:
            desc = _multiline_xml_text(self._description)
            desc_xml = f'<Description>\n<![CDATA[{desc}]]>\n</Description>'
        # RedundancyInfo: Enabled comes from binary; no pad attributes in golden.
        redundancy_enabled_str = "true" if self._redundancy_enabled else "false"
        redundancy_info = (
            f'<RedundancyInfo Enabled="{redundancy_enabled_str}" KeepTestEditsOnSwitchOver="false"/>'
        )
        return (
            open_tag
            + desc_xml
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
    """The object returned by load_acd()/ExportL5x.project. Everything else
    hangs off `.controller` (a Controller) -- there is no top-level
    `.routines`/`.tags` shortcut here; e.g. to reach a routine's rungs:

        project.controller.programs[i].routines[j].rungs

    (Controller-scope tags/UDTs/AOIs/modules ARE directly on `.controller`
    -- see Controller's own docstring.) `save_acd()` reads
    `_raw_files`/`_file_order`/`_footer_unknown` from this object.
    """

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
