# Auto-split from the former acd/l5x/elements/builders.py -- see CLAUDE.md's structural-cleanup notes.
import math
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Union

from loguru import logger as log

from acd.generated.comps.rx_generic import RxGeneric

from .base import L5xElementBuilder
from .builders_common import _resolve_bit_target, external_access_enum, radix_enum
from .model import DataType, Member


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

        # BIT members: data_type resolves to BOOL but only a "plain field" signature
        # (0x6c == 0xFFFFFFFF and 0x68 == 0x800) means it's a real, independent BOOL --
        # everything else is a BIT-overlay pseudo-member aliasing a bit of some other
        # (usually hidden) backing field. 0x68's exact value was originally treated as a
        # small enumerated "pattern selector" (0/1/0x800) whose other branches resolved
        # Target by matching 0x6c or the member's own 0x60 against offset60_to_name (a
        # map of every "plain field" member's own 0x60 -> name). Real UDTs have since
        # turned up several more 0x68 values (0x2, 0x9, 0x14, ...) that don't fit that
        # enum, AND -- more importantly -- turned up real cases where a BIT member's own
        # 0x60 coincidentally equals a totally unrelated real field's 0x60 elsewhere in
        # the same UDT (e.g. a real UDT where 16 BIT-overlay members' own 0x60 all read
        # 4, which matches an unrelated plain field also at 0x60=4, not either of the
        # UDT's two real hidden backing fields at 0x60=2 and 0x60=3). An offset60_to_name
        # lookup on a coincidental match returns a wrong-but-non-None name, which is
        # worse than the lookup failing outright. The one mechanism that has resolved
        # every real case checked so far correctly -- including this collision case --
        # is declaration order: a BIT-overlay member always immediately follows its own
        # backing field, so _fallback_target (the most-recent preceding hidden member)
        # is tried FIRST; the offset60_to_name lookups (via 0x6c, then via this member's
        # own 0x60) are kept only as a fallback for the case they were originally added
        # for (no hidden member precedes at all, e.g. a BIT member as a UDT's very first
        # member) since no real case has been found where they're needed AND correct.
        # Cross-checked against a real Studio 5000 whole-project L5X export: every BIT
        # member across all 99 DataTypes in one real project now resolves an exact-match
        # Target (previously 4 DataTypes had wrong targets from the offset60 collision,
        # and 2 more had entirely unresolved targets from the original bug this
        # comment block replaced).
        target: Union[str, None] = None
        bit_number: Union[int, None] = None
        if data_type == "BOOL":
            target_key = struct.unpack_from("<I", self.record, 0x6C)[0]
            val_68 = struct.unpack_from("<I", self.record, 0x68)[0]
            if target_key == 0xFFFFFFFF and val_68 == 0x800:
                # Plain BOOL (not a BIT sub-element) — bit_number is at 0x64 for data
                # table bit-packing extraction.
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]
            else:
                data_type = "BIT"
                # offset 0x5C holds a bit-offset into the host register, not an array
                # size — force dimension to 0 so _member_decorated_xml treats this as
                # a scalar rather than emitting thousands of <Element> entries.
                dimension = 0
                bit_number = struct.unpack_from("<I", self.record, 0x64)[0]
                val_60 = struct.unpack_from("<I", self.record, 0x60)[0]
                target = _resolve_bit_target(
                    target_key, val_60, self._offset60_to_name, self._fallback_target
                )

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
            _byte_offset=byte_offset,
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
        dead_member_bytes = 0
        if len(member_results) == 1:
            member_collection_id = member_results[0][1]

            self._cur.execute(
                f"SELECT comp_name, object_id, parent_id, seq_number, record, record_type "
                f"FROM comps WHERE parent_id={member_collection_id} ORDER BY seq_number"
            )
            children_results = self._cur.fetchall()

            # Build a name→child map so we pair members by name instead of
            # by seq_number position (seq_number can be stale when members
            # are deleted and re-added over a project's lifetime).
            #
            # A member-collection child's own record_type is 256 for a live
            # member, 512 for a deleted one -- the same live/deleted marker
            # already used elsewhere for Program/Module/Tag/Routine phantom
            # filtering, but never applied here until now. Deleting a member
            # marks its own child row 512 but does NOT necessarily remove its
            # extended-record descriptor from the *type's* own comps row (see
            # the member_keys loop below) -- found via a real project (a UDT
            # whose 15 scalar DINT members were consolidated into a single
            # array member and re-saved): the type's own declared
            # member_count (attribute 0x64) correctly read back as 1, but 15
            # stale extended-record descriptors for the deleted scalars were
            # still present and, before this fix, got matched against their
            # equally-stale-but-still-present record_type=512 child rows,
            # silently resurrecting them as 15 phantom live members. Excluding
            # record_type=512 rows from name_to_child means a stale extended
            # record with no *live* child match falls through to the same
            # "no descriptor found" path already used for a fully-purged
            # deletion (see below), rather than being treated as live.
            name_to_child: Dict[str, Tuple] = {}
            deleted_child_names: List[str] = []
            for child in children_results:
                if child[5] == 256:
                    name_to_child[child[0]] = child
                else:
                    deleted_child_names.append(child[0])

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

            # Whatever's left in name_to_child (after excluding record_type=512
            # rows up front, see above) is a *live-marked* member-collection
            # child with NO matching extended-record descriptor -- a deleted
            # UDT member whose child row was never marked 512 at all, just
            # left with no type-level descriptor (data_type/dimension) --
            # verified against a real project (a UDT with a deleted member,
            # confirmed via an older Studio 5000 export of the same UDT to
            # have been DataType="INT"). This used to be
            # treated as needing a byte-offset correction for every
            # subsequent sibling member, but that theory was disproven by a
            # real, populated tag (see _apply_dead_member_byte_corrections's
            # docstring) -- Rockwell's own stored member offsets already
            # account for this correctly with no adjustment needed.
            # `_dead_member_bytes` is kept purely as a diagnostic (logged
            # below) that this type has an orphaned/deleted member; it is no
            # longer used in any byte-offset or size computation.
            # Both of these are self-healing confirmations, not problems --
            # the deleted/orphaned member data was already correctly excluded
            # by the filtering above. Deliberately INFO, not WARNING: they
            # fire deterministically, once per affected UDT, on every single
            # load of a project with any historically-deleted UDT member,
            # which is common enough in real long-lived projects that a
            # downstream caller piping WARNING-and-above through a filter to
            # find *actionable* signal (e.g. "Unrecognized connection type
            # code..." below) had to grep these out by hand every time --
            # flagged as pure noise by a real downstream agent session.
            if name_to_child:
                log.info(
                    f"DataType {name!r}: {len(name_to_child)} deleted member(s) "
                    f"with no type descriptor found ({sorted(name_to_child)}) -- "
                    "diagnostic only, no byte-offset correction is applied for this."
                )
            if deleted_child_names:
                log.info(
                    f"DataType {name!r}: {len(deleted_child_names)} member-collection "
                    f"child row(s) marked deleted (record_type=512) filtered out "
                    f"({sorted(deleted_child_names)}) -- a stale extended-record "
                    "descriptor for one of these may still be present on the type's "
                    "own comps row (Rockwell doesn't always purge it on deletion), "
                    "but is now correctly ignored rather than resurrecting a phantom "
                    "member."
                )
            dead_member_bytes = (len(name_to_child) + len(deleted_child_names)) * 2

            # Cross-check against the type's own declared member_count
            # (attribute 0x64) -- diagnostic only, not used to include/exclude
            # anything, since its exact counting convention (does it include
            # hidden BIT-backing members?) isn't independently verified yet.
            # Still a useful free signal: it correctly read back as 1 for the
            # real case above while this function was about to return 16
            # members, which would have been an immediate, obvious red flag
            # had this check existed already.
            if member_count and len(children) != member_count:
                log.warning(
                    f"DataType {name!r}: declared member_count={member_count} but "
                    f"{len(children)} member(s) were actually built -- possible "
                    "remaining stale/phantom member data; investigate if this "
                    "type's members look wrong."
                )

        # --- Description ---
        description: Union[str, None] = None
        self._cur.execute(
            "SELECT record_string FROM comments WHERE parent=? AND member_ref=0 LIMIT 1",
            ((r.comment_id * 0x10000) + r.cip_type,),
        )
        desc_row = self._cur.fetchone()
        if desc_row and desc_row[0]:
            description = desc_row[0]

        return DataType(
            name, name, string_family, class_type, children, description,
            _dead_member_bytes=dead_member_bytes,
        )

def _apply_dead_member_byte_corrections(all_data_types_map: Dict[str, "DataType"]) -> None:
    """No-op, kept only so existing callers don't need to change.

    A previous version of this function shifted `_byte_offset` for every
    member declared after a scalar struct-typed member whose own nested
    DataType has `_dead_member_bytes > 0`, on the theory that a deleted
    member's old byte range keeps occupying space in an already-allocated
    tag's data table. That theory was disproven by a real Studio 5000
    screenshot of a populated tag (a UDT-typed tag whose nested struct
    member has one dead/deleted member): the tag's real values only decode
    correctly using each member's own *raw*, uncorrected stored byte_offset
    -- applying this pass's shift on top double-counts a correction that
    turned out to belong entirely to `_tag_value_blob_offset()` (the tag's
    own data-table blob start position, which already accounts for the
    record's real layout on a per-tag basis and needs no per-type
    adjustment on top). The earlier "verified exact" claim for this same
    tag was made against an all-zero/unpopulated instance, which cannot
    distinguish a correct
    offset from an off-by-2 one -- a real, populated instance was needed to
    catch this. `_dead_member_bytes` is still computed and logged (see
    DataTypeBuilder.build()) as a useful diagnostic that a type has an
    orphaned member, but no longer feeds into any byte-offset math.
    """
    return


# Connection Type CIP enum, read at absolute offset 90 (u16le) of a
# RxMapConnectionCollection child's raw record. Verified against two real
# Studio 5000 projects (matching every connection's raw bytes, keyed by
# (comp_name, RPI) read from offset 92, against its actual L5X Type=
# attribute): a clean, zero-exception discriminator, most recently across
# every one of 205 real connection records in a second project (134 of
# them code 48/StandardDataDriven alone) -- that same cross-check also
# reconfirmed codes 5/6/7/23 hold with zero exceptions in this second,
# independent project. Code 48 is a strong confirmation of the general
# "don't trust the connection name" warning below: it appears on
# connections literally named both "InputData" and "OutputData" in the
# same project, so the old name-based fallback silently guessed opposite
# answers for functionally-identical connections depending on which one
# happened to be sitting in front of it. Other CIP connection types (e.g.
# Safety Input/Output, Listen-Only, Config) may use additional codes not
# yet observed -- see the name-based fallback below.