# Auto-split from the former acd/l5x/elements.py -- see CLAUDE.md's structural-cleanup notes.
import math
import struct
from typing import Dict, List, Tuple, Union

from .base import _sanitize_xml_text
from .types import _PRIM, _get_type_size, _is_bit_overlay, _is_string_family_type, _string_family_capacity


def _l5k_prim_literal(dt_upper: str, val) -> str:
    """Format a single primitive value in Rockwell's L5K convention:
    "2#0"/"2#1" for BOOL/BIT, scientific notation for REAL/LREAL
    (_l5k_real_literal), plain decimal for every other integer type.
    """
    if dt_upper in ("BOOL", "BIT"):
        return f"2#{1 if val else 0}"
    if dt_upper in ("REAL", "LREAL"):
        return _l5k_real_literal(val)
    return str(int(val))

def _l5k_array_literal(dt_base: str, values: list) -> str:
    """Format a primitive array's values as an L5K array literal, e.g.
    "[2#0,2#1,2#0]" for BOOL, "[1,2,3]" for DINT.

    Verified against a real project's <Data Format="L5K"> for a 256-element
    BOOL array tag: BOOL/BIT use the "2#0"/"2#1" binary-literal prefix;
    other integer types are plain decimal; REAL/LREAL use the same
    scientific-notation convention as the scalar case (_l5k_real_literal).
    The real sample line-wraps the literal for readability, but that's
    whitespace-insignificant for both XML and L5K parsing, so this emits
    a single line.
    """
    return "[" + ",".join(_l5k_prim_literal(dt_base, v) for v in values) + "]"

def _l5k_udt_literal(dt_name: str, values, data_types_map: Dict[str, 'DataType']) -> str:
    """Format a decoded UDT scalar or array value as an L5K literal, e.g.
    "[1,0,0,0,0,7,0,0,0,0,38387892,0]" for a scalar struct, or
    "[[...],[...],...]" for an array of structs -- member values in
    declaration order (same order/skip rules as _udt_scalar_to_xml:
    hidden and BIT members omitted), comma-separated, recursively
    bracketed for nested structs/arrays. Verified against a real 25-element
    UDT array tag: every element's L5K literal matches Studio 5000's own
    <Data Format="L5K"> content exactly.
    """
    if isinstance(values, list):
        return "[" + ",".join(_l5k_udt_literal(dt_name, v, data_types_map) for v in values) + "]"

    if _is_string_family_type(dt_name, data_types_map):
        length = values.get("LEN", 0)
        text = values.get("DATA", "")
        cap = _string_family_capacity(dt_name, data_types_map)
        return f"[{length},{_l5k_string_padded(text, cap)}]"

    dt_obj = data_types_map.get(dt_name.upper())
    if dt_obj is None:
        return "[]"

    parts: List[str] = []
    for member in dt_obj.members:
        # Skip only BIT-overlay pseudo-members (no storage of their own --
        # aliased into a sibling's raw value, see _decode_single_udt_element).
        # A HIDDEN non-BIT member (e.g. TIMER/COUNTER's own "Control" DINT)
        # must still be included here: L5K encodes the raw backing value
        # even though it isn't shown as its own named Decorated member --
        # verified against a real TIMER tag's L5K literal
        # "[-1607863227,3000,3000]" (Control, PRE, ACC in declaration
        # order), which a prior version of this function silently dropped
        # down to "[3000,3000]" by skipping hidden members entirely.
        if _is_bit_overlay(member):
            continue
        val = values.get(member.name)
        if val is None:
            # Absent from the decoded value dict entirely (or genuinely
            # decoded to None, e.g. an unrecognized member type) -- most
            # commonly a member added to DataType.members AFTER this tag's
            # value was already decoded from raw bytes. Zero-fill rather
            # than skip: omitting it here would leave the L5K literal one
            # element short of what the type's own (freshly-rendered)
            # declaration says it has, which Studio rejects on import. See
            # _zero_value_for_member()'s own docstring for the full story.
            val = _zero_value_for_member(member, data_types_map)
        mdt = member.data_type
        if isinstance(val, dict) or (isinstance(val, list) and val and isinstance(val[0], dict)):
            parts.append(_l5k_udt_literal(mdt, val, data_types_map))
        elif isinstance(val, list):
            parts.append(_l5k_array_literal(mdt.upper(), val))
        else:
            parts.append(_l5k_prim_literal(mdt.upper(), val))
    return "[" + ",".join(parts) + "]"

def _l5k_real_literal(value: float) -> str:
    """Format a REAL/LREAL value in Rockwell's L5K scientific-notation
    convention: 8 decimal digits, 3-digit zero-padded exponent, e.g.
    1.0 -> "1.00000000e+000", -0.5 -> "-5.00000000e-001".

    NaN/Infinity: a real project was found with several uninitialized REAL
    tags decoding to non-finite values, which previously crashed this
    function entirely (str.split("e") on a mantissa with no "e", since
    Python formats these as bare "nan"/"inf"). Confirmed against that same
    project's own Studio 5000 L5X export -- Rockwell renders these using
    the classic MSVC CRT convention, with the special-value label left
    padded with zeros into the same 8-character mantissa slot a normal
    number would occupy: "1.#QNAN000e+000" for NaN, "1.#INF0000e+000" for
    +Infinity. The sign-prefixed forms ("-1.#QNAN...", "-1.#INF...") and
    the classic "-1.#IND" indeterminate-NaN special case were not observed
    in any real sample and are inferred by symmetry, not verified.
    """
    if math.isnan(value) or math.isinf(value):
        label = "#QNAN" if math.isnan(value) else "#INF"
        sign = "-" if math.copysign(1.0, value) < 0 else ""
        return f"{sign}1.{label.ljust(8, '0')}e+000"
    if value == 0.0:
        return "0.00000000e+000"
    formatted = f"{value:.8e}"  # e.g. "1.00000000e+00" or "-5.00000000e-01"
    mantissa, exp = formatted.split("e")
    sign = exp[0]
    exp_digits = exp[1:].zfill(3)
    return f"{mantissa}e{sign}{exp_digits}"

def _shortest_float32_repr(value: float) -> str:
    """Return the shortest fixed-point decimal string that round-trips to
    the same float32 bit pattern as *value* (a Python float already holding
    an exact-upconverted float32, e.g. via struct.unpack("<f", ...)).

    A REAL tag is stored as IEEE-754 single precision, but Python floats
    are double precision -- struct.unpack("<f", ...) upconverts exactly,
    so the resulting double carries the float32's full double-precision
    expansion (e.g. 81.0063247680664 for what was originally "81.006325"),
    not the "nice" short decimal a human (or Studio) would write. A naive
    fixed precision like "%.6g" either truncates real precision the value
    actually has (81.006325 -> "81.0063", silently wrong) or, for a
    different value, shows more digits than the float32 actually carries.
    Neither matches Studio 5000, which shows the shortest decimal that
    uniquely identifies the same float32 value -- found via a real Studio
    "Tag Name Collision / Data Compare" dialog showing several float
    members each differing from ours only in significant-digit count (e.g.
    real "81.006325" vs our "%.6g"-truncated "81.0063") even though the
    underlying decoded bytes were already correct.
    """
    target_bits = struct.pack("<f", value)
    for decimals in range(0, 15):
        candidate = f"{value:.{decimals}f}"
        if struct.pack("<f", float(candidate)) == target_bits:
            return candidate
    return f"{value:.6g}"  # pathological magnitude (subnormal/huge) fallback

def _decorated_real_literal(value: float, in_array: bool) -> str:
    """Format a REAL/LREAL value for a Decorated Value="..." attribute
    (DataValue/DataValueMember for a scalar, Array Element for an array).

    Finite values use the shortest decimal that round-trips to the same
    float32 bit pattern (see _shortest_float32_repr) -- distinct from
    L5K's fixed 8-digit scientific notation (_l5k_real_literal) -- with a
    mandatory decimal point even for a whole-number value: confirmed
    against a real AOI instance tag's own Decorated value (several REAL
    members with whole-number values, e.g. 1800.0, 6.0, 12.0, 14.0) all
    render with an explicit ".0" in real Studio output, but
    _shortest_float32_repr's plain fixed-point formatting omits the
    decimal point for an exact whole number (e.g. "1800", not "1800.0").
    NaN/Infinity: confirmed against a real project's own Studio 5000 export
    that a *scalar* tag's Decorated NaN value is the bare label "1.#QNAN"
    (no padding/exponent, unlike L5K) -- the analogous "1.#INF" for scalar
    Infinity was not observed but is inferred by direct symmetry. An
    *array* Element's Decorated value for the one non-finite case observed
    (+Infinity) was instead the truncated "1.$" -- a real, reproducible
    quirk/bug in Studio 5000's own array Decorated-value exporter (distinct
    from the scalar case), applied here for NaN too since no counter-
    evidence exists and the truncation looks like a generic "any
    '#'-prefixed special-value label gets mangled in this code path" bug
    rather than one specific to Infinity.
    """
    if math.isnan(value) or math.isinf(value):
        if in_array:
            return "-1.$" if math.copysign(1.0, value) < 0 else "1.$"
        label = "#QNAN" if math.isnan(value) else "#INF"
        sign = "-" if math.copysign(1.0, value) < 0 else ""
        return f"{sign}1.{label}"
    formatted = _shortest_float32_repr(value)
    if "." not in formatted and "e" not in formatted and "E" not in formatted:
        formatted += ".0"
    return formatted

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

def _zero_value_for_member(member: "Member", data_types_map: Dict[str, "DataType"]):
    """Synthesize a Studio-consistent zero/default value for a UDT member
    that has no decoded value at all in a tag's already-decoded
    `_initial_value` dict.

    This happens whenever a member is present in `DataType.members` but
    absent as a key in a value dict that was decoded (from the ACD's raw
    stored bytes, see `_decode_single_udt_element`) BEFORE that member
    existed -- e.g. a caller appends a new `Member` to an existing
    `DataType.members` list, in the same session, for a type that already
    has live tag instances whose values were decoded at `load_acd()` time.
    Mutating `DataType.members` in place does not retroactively re-derive
    or zero-fill those already-decoded values -- without this, the type's
    *declaration* (rendered fresh from the current `DataType.members` at
    export time) and the tag's *value* (rendered from the stale decoded
    dict) disagree on member count, which Studio 5000 correctly rejects on
    import ("Data type mismatch"). Found via a real case: adding a new
    member to an existing DataType with 50 live tag instances, then
    exporting a routine referencing one of those instances in the same
    session.

    Mirrors what Studio 5000 itself does natively when you add a UDT
    member to a type with existing instances via its own editor: the new
    member simply defaults to zero on every existing instance.

    Returns a plain Python value in the same shape `_decode_single_udt_element`
    would have produced (scalar / dict / list / list-of-dicts) -- callers
    that already handle a real decoded value's shape (`_l5k_udt_literal`,
    `_udt_scalar_to_xml`) can consume this exactly the same way.
    """
    mdt = member.data_type
    mdt_upper = mdt.upper()

    def _scalar_zero():
        if mdt_upper in ("BOOL", "BIT"):
            return 0
        if mdt_upper in _PRIMITIVE_DECORATED_ZERO:
            return 0.0 if mdt_upper in ("REAL", "LREAL") else 0
        if _is_string_family_type(mdt, data_types_map):
            return {"LEN": 0, "DATA": ""}
        dt_obj = data_types_map.get(mdt_upper)
        if dt_obj is None:
            return 0  # unknown type -- a harmless scalar zero beats crashing
        return {
            m.name: _zero_value_for_member(m, data_types_map)
            for m in dt_obj.members
            if not _is_bit_overlay(m)
        }

    # `member.dimension` is documented/typed as `int` (0 = scalar), and every
    # ACD-decoded Member (MemberBuilder.build()) always sets a real int here --
    # but a Member constructed directly (or via new_member()) with an
    # explicit `dimension=None`, mistakenly treating it like new_member()'s
    # OTHER params (radix/description) where None means "use the default",
    # crashes `> 0` with a real TypeError. Treat None the same as 0 (scalar)
    # rather than assume it can't happen -- found via a real crash report one
    # level removed from this function's own first real-world use (recursing
    # into a newly-authored struct type's own members, one of which had this
    # exact mistake).
    if member.dimension and member.dimension > 0:
        return [_scalar_zero() for _ in range(member.dimension)]
    return _scalar_zero()

def _validate_type_graph_resolves(dt_name: str, context: str,
                                   data_types_map: Dict[str, "DataType"], seen: set) -> None:
    """Shared recursive walker behind `_validate_tag_types_resolve()` (starts
    from a Tag's own `data_type`) and `_validate_data_type_resolves()`
    (starts from a bare DataType's own members, no tag involved) -- see
    either caller's docstring for why this check exists. `seen` is the
    caller's own set, so repeated calls from a loop share one dedup set
    across the whole run rather than re-walking an already-confirmed type.

    Raises ValueError on the first unresolved type found.
    """
    key = dt_name.upper()
    if (
        key in _PRIMITIVE_DECORATED_ZERO
        or key in _BUILTIN_STRUCT_MEMBERS
        or key in _SKIP_DECORATED
        or key in seen
        or _is_string_family_type(dt_name, data_types_map)
    ):
        return
    dt_obj = data_types_map.get(key)
    if dt_obj is None:
        raise ValueError(
            f"{context}: type {dt_name!r} does not resolve to a known "
            "primitive, string-family type, built-in Logix struct, or an "
            "entry in data_types_map -- most likely a stale/incomplete "
            "data_types_map (see _sync_data_types_map()) rather than a "
            "genuinely unknown type. Exporting anyway would silently "
            "render this member as a bare zero instead of its real "
            "nested structure."
        )
    seen.add(key)
    for member in dt_obj.members:
        if member.data_type:
            _validate_type_graph_resolves(member.data_type, f"{context}.{member.name}",
                                           data_types_map, seen)


def _validate_tag_types_resolve(tags: List["Tag"], data_types_map: Dict[str, "DataType"]) -> None:
    """Verify every struct-typed name reachable from `tags`' own DataType
    trees resolves to a real entry in `data_types_map` -- a primitive, a
    string-family type, a built-in Logix struct (TIMER/COUNTER/CONTROL/...,
    see `_BUILTIN_STRUCT_MEMBERS`/`_SKIP_DECORATED`), or a project UDT/AOI
    actually present in the map. Opt-in self-consistency check
    (`export_routine(..., validate=True)`), meant to run BEFORE any XML is
    written.

    Exists because an unresolved type never raises on its own: it silently
    falls into `_zero_value_for_member()`'s (and the equivalent live-value
    rendering paths') "unknown type -- a harmless scalar zero beats
    crashing" branch, producing a `<Tag>` whose declared member count and
    rendered value shape quietly disagree -- the exact bug class that was
    previously only ever caught by an actual Studio 5000 import rejecting
    the file (see CLAUDE.md "Mutating a UDT with live tag instances...",
    the `Tag._data_types_map` staleness bug). This walks the same
    member-type graph that rendering does, but eagerly, with a clear error
    naming the tag/member/type responsible, instead of waiting for that
    silent fallback to produce a wrong file.

    Raises ValueError on the first unresolved type found.
    """
    seen: set = set()
    for tag in tags:
        if tag.data_type:
            _validate_type_graph_resolves(tag.data_type, f"Tag {tag.name!r}", data_types_map, seen)


def _validate_data_type_resolves(data_type: "DataType", data_types_map: Dict[str, "DataType"]) -> None:
    """The `export_datatype()` counterpart to `_validate_tag_types_resolve()`
    -- checks a bare DataType's OWN member declarations resolve, with no
    tag/value involved (unlike the tag version, which checks a value tree
    reachable from a tag). Same rationale: a member typed with an
    unresolved struct name doesn't raise on its own during rendering, it
    silently falls into a zero-value fallback -- for `export_datatype()`
    that means a member the real UDT can't actually represent gets
    exported as a bare scalar instead of raising up front.

    Raises ValueError on the first unresolved type found.
    """
    seen: set = set()
    for member in data_type.members:
        if member.data_type:
            _validate_type_graph_resolves(member.data_type, f"DataType {data_type.name!r}.{member.name}",
                                           data_types_map, seen)


def _validate_aoi_parameters_resolve(aoi: "AOI", data_types_map: Dict[str, "DataType"]) -> None:
    """The `export_aoi()` counterpart to `_validate_data_type_resolves()` --
    checks an AOI's own `.parameters` declarations resolve. Deliberately
    does NOT walk `.local_tags` -- this whole AOI-support feature has no
    constructor support for creating LocalTags at all (out of scope, see
    CLAUDE.md's AOI support section), so there's nothing this validate pass
    would ever catch there that a caller could have caused through this
    library's own API.

    Raises ValueError on the first unresolved type found.
    """
    seen: set = set()
    for param in aoi.parameters:
        if param.data_type:
            _validate_type_graph_resolves(param.data_type, f"AOI {aoi.name!r} parameter {param.name!r}",
                                           data_types_map, seen)


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
    # (e.g. "MyUdt", not "MYUDT") -- recover it from the DataType object
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

    # NOTE: array-element comments are never embedded inline as <Comment>
    # children of <Element> (verified: zero such occurrences in a real
    # project's L5X export) -- they always go in the tag's standalone
    # <Comments><Comment Operand="..."> block instead (see
    # _build_comments_xml, used by Tag.to_xml()).
    return f'<Data Format="Decorated">\n{body}\n</Data>'

def _decorated_binary_literal(value: int, bit_width: int) -> str:
    """Format an integer value as Rockwell's Radix="Binary" literal:
    "2#" followed by the value's two's-complement bits at the type's full
    bit width, grouped into 4-digit chunks separated by underscores (e.g.
    "2#0000_0000_0000_0000_0000_0000_0000_0000" for a 32-bit DINT zero).

    Verified against a real project's own Studio 5000 export for exactly
    one case: a DINT UDT member declared Radix="Binary" with value 0
    (8 groups of 4 = 32 bits). Non-zero and negative values are inferred
    by symmetry with the documented Rockwell binary-literal convention,
    not independently verified -- revisit if a real non-zero sample
    disagrees.
    """
    mask = (1 << bit_width) - 1
    bits = format(value & mask, f"0{bit_width}b")
    groups = [bits[i:i + 4] for i in range(0, len(bits), 4)]
    return "2#" + "_".join(groups)

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
        # Skip only genuinely hidden members (e.g. TIMER/COUNTER's own
        # "Control" DINT, which has no visible Decorated representation of
        # its own). A BIT-overlay pseudo-member (e.g. TIMER.EN/TT/DN,
        # COUNTER.CU/CD/DN/OV/UN) DOES get its own <DataValueMember> here --
        # it falls through to the BOOL/BIT branch below, which renders its
        # already bit-extracted value (see _decode_single_udt_element).
        # Verified against a real TIMER tag's Decorated output, which shows
        # EN/TT/DN as real members alongside PRE/ACC -- a prior version of
        # this function silently dropped all three by skipping any
        # data_type=="BIT" member unconditionally.
        if member.hidden:
            continue
        mname = member.name
        mdt = member.data_type
        mdt_upper = mdt.upper()
        val = values.get(mname)

        if val is None:
            # Absent from the decoded value dict entirely (or genuinely
            # decoded to None, e.g. an unrecognized member type) -- most
            # commonly a member added to DataType.members AFTER this tag's
            # value was already decoded from raw bytes. Zero-fill rather
            # than skip: omitting it here would leave this Decorated
            # structure with fewer members than the type's own (freshly-
            # rendered) declaration says it has, which Studio rejects on
            # import. See _zero_value_for_member()'s own docstring.
            val = _zero_value_for_member(member, data_types_map)

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

            def _fmt_member_elem(v):
                if isinstance(v, float):
                    return _decorated_real_literal(v, in_array=True)
                return v

            elems = "".join(
                f'<Element Index="[{i}]" Value="{_fmt_member_elem(v)}"/>'
                for i, v in enumerate(val)
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
            # A member's OWN declared radix (e.g. "Binary" for a UDT member
            # meant to be read as bit flags) must be honored over the
            # generic per-type default -- verified against a real project's
            # DINT member declared Radix="Binary", which a prior version
            # rendered as plain Radix="Decimal" Value="0" instead of
            # "2#0000_0000_0000_0000_0000_0000_0000_0000".
            radix = (
                member.radix
                if member.radix and member.radix != "NullType"
                else _PRIMITIVE_RADIX.get(mdt_upper, "Decimal")
            )
            if radix == "Binary" and isinstance(val, int):
                elem_size = _PRIM.get(mdt_upper, (None, 4))[1]
                member_val = _decorated_binary_literal(val, elem_size * 8)
            elif isinstance(val, float):
                member_val = _decorated_real_literal(val, in_array=False)
            else:
                member_val = val
            parts.append(
                f'<DataValueMember Name="{mname}" DataType="{mdt}" '
                f'Radix="{radix}" Value="{member_val}"/>'
            )

    return "".join(parts)

def _udt_array_to_xml(dt_base: str, values: List[dict],
                       dim_str: str,
                       data_types_map: Dict[str, 'DataType']) -> str:
    """Generate an ``<Array>`` XML fragment for a decoded UDT array tag.

    Every element up to the array's actual length is emitted -- no
    truncation, for either a 1-D or a multi-dimensional array. (A previous
    version omitted trailing all-zero elements for 1-D arrays specifically;
    that was never actually verified against real Studio 5000 output
    despite a docstring claiming otherwise, a real "Export Routine" sample
    directly contradicts it, and it was the likely trigger for a real
    Logix Designer crash during a native Import Routine attempt -- see the
    equivalent fix for primitive arrays in Tag.to_xml().) For a
    multi-dimensional array (comma-separated dim_str, e.g. "4,3,2"), every
    element is emitted with a comma-separated Index (e.g. "[3,2,1]"), and
    the flat *values* list (row-major, matching _decode_udt_initial_value's
    flat iteration and the ACD's own storage order) is mapped back to
    per-dimension indices -- verified against a real 3-D UDT array tag,
    where Studio 5000 shows all elements untruncated.
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
        # A top-level tag's own <Array> Decorated element has no Name=
        # attribute (unlike a nested ArrayMember inside a Structure, which
        # does carry one) -- already correctly handled for primitive arrays
        # in Tag.to_xml(), but this UDT-array path still included one until
        # two real project tags of the same UDT-array type showed real
        # Studio 5000 output has no Name= here either.
        return (
            f'<Array DataType="{display_name}" Dimensions="{dim_str}">'
            f'{"".join(elems)}</Array>'
        )

    elems: List[str] = []
    for i in range(len(values)):
        struct = _udt_scalar_to_xml(dt_base, values[i], data_types_map)
        elems.append(
            f'<Element Index="[{i}]">'
            f'<Structure DataType="{display_name}">{struct}</Structure>'
            f'</Element>'
        )

    return (
        f'<Array DataType="{display_name}" Dimensions="{dim_str}">'
        f'{"".join(elems)}</Array>'
    )
# Logix STRING struct: DINT LEN (4 bytes) + SINT[82] DATA (82 bytes) + 2 bytes padding.
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
    safe = _sanitize_xml_text(escaped)
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

    Raw control bytes (not just ``\\x00``) can end up inside *text* itself
    (not just the computed padding) if a nested string member's decoded
    length includes what's actually unused/uninitialized data. First found
    via a real UDT array tag's L5K rendering with an embedded NUL; a second,
    wider real-project sample (a whole-project export) hit the exact same
    class of bug with a raw ``\\x1c`` byte instead, confirming this isn't
    NUL-specific -- any control character is illegal raw XML content
    (outside CDATA-escaping) and must be handled the same way. Confirmed
    against that project's own Studio 5000 L5X export that Rockwell's own
    convention is a general ``$XX`` (uppercase hex) escape for *every*
    control character, not just NUL -- real examples found: ``$00``, ``$01``,
    ``$0B``, ``$1B`` (the last one inside a Decorated array Element's
    ``Value="..."`` attribute, confirming this same escaping applies beyond
    just the L5K literal too). Escaping every control character (0x00-0x1F,
    0x7F) this way, rather than just NUL, so this can never happen again
    regardless of which specific byte value the root-cause garbage data
    happens to produce.

    Bytes >= 0x80 (non-ASCII) are escaped the same ``$XX`` way -- Studio 5000
    rejects an L5K string literal containing a raw non-ASCII character with
    "Only ASCII characters are supported". Found via a real array tag whose
    STRING member held uninitialized/garbage data (an implausible LEN of
    millions, clamped to the type's capacity, meaning the following "text"
    bytes were never real content); the raw bytes weren't valid UTF-8, and
    utf-8-decoding them (this function's caller previously used
    errors="replace") inserted U+FFFD, an unescaped non-ASCII codepoint,
    into what must be an ASCII-only literal. Now that the caller decodes as
    latin-1 (1:1 byte<->codepoint, never fails), every original byte value
    reaches here intact and gets escaped correctly, whether it's meaningful
    extended/accented text or garbage.
    """
    escaped = text.replace("$", "$$").replace("'", "$'")
    escaped = "".join(
        f"${ord(ch):02X}" if ord(ch) < 0x20 or ord(ch) == 0x7F or ord(ch) > 0x7E else ch
        for ch in escaped
    )
    pad = "$00" * max(capacity - len(text), 0)
    return f"'{escaped}{pad}'"

def _build_comments_xml(tag_name: str, comments: List[Tuple[str, str]]) -> str:
    """Build the standalone ``<Comments>`` XML block for a tag.

    Each entry in *comments* is a ``(path, text)`` pair as stored on
    ``Tag._comments`` -- *path* is the fully-resolved address INCLUDING the
    tag name prefix (e.g. ``"MyTag.Gain"``, ``"MyTag[2,2,1].BfrLug.Z5_SawPattern.3"``,
    per ``TagBuilder``/``ControllerBuilder``'s normalization), except for the
    empty-path entry which is the tag's own whole-tag description (handled
    separately via ``<Description>``) and is skipped here. Any leftover
    unresolved ``.!HEXOID``/``!HEXOID`` reference (data-type lookup failed)
    is also skipped, since it can't be turned into a valid Operand.

    Verified against a real Studio 5000 L5X export: the ``Operand=`` attribute
    is the path suffix (tag name stripped) fully UPPERCASED (e.g.
    ``Operand=".GAIN"``, ``Operand="[2,2,1].BFRLUG.Z5_SAWPATTERN.3"``), even
    though member names keep their original casing everywhere else in the
    document (e.g. ``StructureMember Name="BfrLug"``). The comment text
    itself is NOT collapsed to a single line the way ``<Description>`` is --
    multi-line text is preserved as-is inside the CDATA block.

    Also verified: array-element / bit comments are never additionally
    embedded inline as ``<Comment>`` children of an ``<Element>`` node --
    this ``<Comments>`` block is their only representation in the L5X.

    Returns ``""`` if there are no applicable comments.
    """
    parts = []
    for ref, text in comments:
        if not ref or ref == "." or not text:
            continue
        if ref.startswith(".!") or ref.startswith("!"):
            # Unresolved hex-OID reference -- can't produce a valid Operand.
            continue
        if not ref.startswith(tag_name):
            continue
        operand = ref[len(tag_name):]
        if not operand:
            continue
        operand = operand.upper()
        safe_text = _sanitize_xml_text(text.replace("\r\n", "\n").replace("\r", "\n"))
        parts.append(f'<Comment Operand="{operand}">\n<![CDATA[{safe_text}]]>\n</Comment>\n')
    if not parts:
        return ""
    return f'<Comments>\n{"".join(parts)}</Comments>\n'
