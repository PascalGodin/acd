# Auto-split from the former acd/l5x/elements/builders.py -- see CLAUDE.md's structural-cleanup notes.
import re
import struct
from dataclasses import dataclass
from typing import Dict, Union

from acd.generated.comps.rx_generic import RxGeneric

from .base import L5xElementBuilder
from .builders_common import (
    _build_hex_oid_map,
    _resolve_tag_name_from_oid,
    external_access_enum,
    radix_enum,
)
from .decode import _count_array_elements, _read_tag_initial_value
from .model import Tag
from .rendering import _SKIP_DECORATED


@dataclass
class TagBuilder(L5xElementBuilder):
    # Project-wide, identical for every tag (see _build_hex_oid_map) -- pass
    # it in from the caller's own single project-wide build instead of
    # rebuilding it here per tag. A real project (2725 controller tags +
    # program tags) took 90s to load_acd() before this fix, ~82s of which
    # was this same global query re-run once per tag (3872 times); passing
    # a pre-built map in cut that to a fraction of a second. Falls back to
    # building it locally (the old, slow behavior) only if a caller
    # constructs TagBuilder directly without providing one.
    _hex_oid_map: Union[Dict[int, str], None] = None

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
            self._cur, dti, data_type, _count_array_elements(dimensions),
            is_array=dimensions is not None,
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
            hex_oid_map = (
                self._hex_oid_map if self._hex_oid_map is not None
                else _build_hex_oid_map(self._cur)
            )
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
