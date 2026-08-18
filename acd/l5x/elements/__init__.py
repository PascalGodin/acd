# Re-export everything previously importable from the flat acd/l5x/elements.py
# module, now split into focused submodules -- see CLAUDE.md's structural-cleanup
# notes. `from acd.l5x.elements import X` (or `acd.l5x.elements.X`) keeps working
# unchanged for every name that was importable before the split.

from .base import (
    _escape_xml_attr,
    _multiline_xml_text,
    _sanitize_xml_text,
    _validate_rll_rung_syntax,
    L5xElementBuilder,
    L5xElement,
    _XML_ILLEGAL_RE,
    _LIST_SECTION_NAMES,
)  # noqa: F401

from .types import (
    _is_bit_overlay,
    _is_string_family_type,
    _string_family_capacity,
    _get_type_size,
    _STRING_SIZE,
    _PRIM,
)  # noqa: F401

from .rendering import (
    _l5k_prim_literal,
    _l5k_array_literal,
    _l5k_udt_literal,
    _l5k_real_literal,
    _shortest_float32_repr,
    _decorated_real_literal,
    _zero_value_for_member,
    _validate_tag_types_resolve,
    _validate_data_type_resolves,
    _validate_aoi_parameters_resolve,
    _member_decorated_xml,
    _array_member_xml,
    _struct_members_xml,
    _generate_decorated,
    _decorated_binary_literal,
    _udt_scalar_to_xml,
    _udt_array_to_xml,
    _string_literal_cdata,
    _l5k_string_padded,
    _build_comments_xml,
    _PRIMITIVE_DECORATED_ZERO,
    _BUILTIN_STRUCT_MEMBERS,
    _SKIP_DECORATED,
    _PRIMITIVE_RADIX,
)  # noqa: F401

from .decode import (
    _tag_value_blob_offset,
    _read_tag_initial_value,
    _decode_udt_initial_value,
    _decode_string_family_value,
    _decode_single_udt_element,
    _decode_scalar_member,
    _count_array_elements,
)  # noqa: F401

from .model import (
    Member,
    new_member,
    new_bit_member,
    new_tag,
    DataType,
    new_datatype,
    Tag,
    LocalTag,
    Parameter,
    new_aoi_parameter,
    Module,
    Routine,
    new_routine,
    AOI,
    new_aoi,
    Program,
    ScheduledProgram,
    EventInfo,
    Task,
    Controller,
    RSLogix5000Content,
    _PRIMITIVE_L5K_ZERO,
)  # noqa: F401

from .builders_common import (
    radix_enum,
    external_access_enum,
    _resolve_bit_target,
    _build_hex_oid_map,
    _resolve_tag_name_from_oid,
)  # noqa: F401

from .builders_datatype import (
    MemberBuilder,
    DataTypeBuilder,
    _apply_dead_member_byte_corrections,
)  # noqa: F401

from .builders_module import (
    ModuleBuilder,
    _CONNECTION_TYPE_BY_CODE,
)  # noqa: F401

from .builders_tag import (
    TagBuilder,
    _aoi_tag_usage_flags,
    _aoi_tag_data_type,
)  # noqa: F401

from .builders_routine import (
    ParameterBuilder,
    LocalTagBuilder,
    routine_type_enum,
    RoutineBuilder,
    _parse_fffeff,
    _st_routine_lines,
    _lookup_object_description,
    _filetime_to_iso,
    _parse_aoi_nameless,
    AoiBuilder,
    _ST_LINE_RECORD_TYPE,
)  # noqa: F401

from .builders_controller import (
    ProgramBuilder,
    TaskBuilder,
    ControllerBuilder,
    ProjectBuilder,
    DumpCompsRecords,
    _TASK_TYPE_MAP,
)  # noqa: F401

