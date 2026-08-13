# Auto-split from the former acd/l5x/elements/builders.py -- see CLAUDE.md's structural-cleanup notes.
import struct
from typing import Dict, Union

from acd.generated.comps.rx_generic import RxGeneric


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

def _resolve_bit_target(
    target_key: int,
    val_60: int,
    offset60_to_name: Dict[int, str],
    fallback_target: Union[str, None],
) -> Union[str, None]:
    """Resolve a BIT-overlay member's backing-field Target name.

    Tries, in order:
      1. fallback_target -- the most-recent preceding hidden member in
         declaration order. Verified correct in every real case checked so
         far, including one where mechanisms 2/3 below return a
         wrong-but-non-None name due to a coincidental byte-offset
         collision (a real UDT: a BIT member's own 0x60 happened to equal
         an unrelated real field's 0x60 elsewhere in the same UDT, not
         either of its two genuine hidden backing fields).
      2. target_key (the member's raw 0x6c value) as an offset60_to_name
         lookup key -- works for TIMER/COUNTER-style built-in overlays
         where 0x6c genuinely holds the backing field's own 0x60 value.
      3. val_60 (the member's own 0x60 value) as an offset60_to_name lookup
         key -- works whenever the overlay shares its byte offset with a
         real backing field registered in that map.
    Mechanisms 2/3 are kept only as a fallback for when fallback_target is
    None (no hidden member precedes this one in declaration order at all);
    no real case has been found where they're needed AND correct otherwise.
    """
    if fallback_target is not None:
        return fallback_target
    if target_key != 0xFFFFFFFF:
        name = offset60_to_name.get(target_key)
        if name is not None:
            return name
    return offset60_to_name.get(val_60)

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
