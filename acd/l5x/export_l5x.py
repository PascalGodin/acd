import argparse
import os
import re
import sqlite3
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Cursor
from typing import Dict, List, Union

from acd.database.dbextract import DbExtract
from acd.zip.unzip import Unzip
from loguru import logger as log

from acd.l5x.elements import (
    Controller,
    ControllerBuilder,
    ProjectBuilder,
    RSLogix5000Content,
)
from acd.record.comments import CommentsRecord
from acd.record.comps import CompsRecord
from acd.record.nameless import NamelessRecord
from acd.record.sbregion import SbRegionRecord


def _dedupe_comps_records(tuples: List[tuple]) -> Dict[tuple, tuple]:
    """Deduplicate parsed Comps.Dat record tuples (object_id, parent_id,
    comp_name, seq_number, record_type, record) by (object_id, parent_id),
    not object_id alone.

    object_id is usually unique, but a real, heavily-edited production
    project turned up a genuine 3-way collision: one object_id shared by
    the real "RxDataTypeCollection" (parent = the controller) and two
    entirely unrelated objects with different parents
    ("B_Manual_Solution", "ZZZ_TEMPORARY_IMPORT_DATATYPE_NAME_000" -- the
    latter's name suggests a leftover artifact of some prior import/edit
    operation). Deduping by bare object_id (the original logic, kept for
    the scenario it was actually designed for: a routine appearing twice
    in Comps.Dat with different record_type values, e.g. 271 vs 259, where
    the smaller one is a truncated/partial parse of the SAME object under
    the SAME parent) picked whichever of the three had the largest record
    -- silently discarding the tiny (78-byte) RxDataTypeCollection in
    favor of an unrelated, larger object, which then crashed
    ControllerBuilder.build() with an IndexError (no data-type collection
    found under the controller at all). Keying by (object_id, parent_id)
    instead still correctly collapses the original truncated-vs-full
    same-object case (same parent both times) while keeping genuinely
    different objects that happen to share a raw object_id apart.
    """
    comps_by_id: Dict[tuple, tuple] = {}
    for t in tuples:
        key = (t[0], t[1])
        if key not in comps_by_id or len(t[5]) > len(comps_by_id[key][5]):
            comps_by_id[key] = t
    return comps_by_id


def _parse_records(dat_path: str, parse_one, label: str) -> List[tuple]:
    """Parse every record of a .Dat file, skipping records that fail.

    Real-world ACDs (notably newer firmware like V33+) routinely contain a
    handful of records the parsers don't understand yet; aborting the whole
    import over one bad record makes the library unusable on those files.
    Failures are counted and reported as a single warning instead. A missing
    or wholly unreadable .Dat file degrades to an empty table the same way.
    Returns the list of successfully parsed non-None tuples."""
    if not os.path.exists(dat_path):
        log.warning(f"{label}: file not found at {dat_path} - skipping")
        return []
    try:
        records = DbExtract(dat_path).read().records.record
    except Exception as e:
        log.warning(f"{label}: unreadable database file ({e!r}) - skipping")
        return []
    out: List[tuple] = []
    failed = 0
    for record in records:
        try:
            t = parse_one(record)
        except Exception:
            failed += 1
            continue
        if t is not None:
            out.append(t)
    if failed:
        log.warning(f"{label}: skipped {failed} unparseable record(s) of {len(records)}")
    return out


def _iter_region_map_entries_v_pre38(record: bytes, start: int, end: int):
    """Region Map entry layout for every project seen before Studio 5000 V38:
    16-byte tuples of (parent_id, unknown, seq_no, object_id), densely packed
    from `start` to `end` (both computed by the caller from the record's own
    78-byte header)."""
    offset = start
    while offset <= (end - 16):
        parent_id, unknown, seq_no, object_id = struct.unpack_from("<IIII", record, offset)
        yield (object_id, parent_id, unknown, seq_no, record[offset : offset + 16])
        offset += 16


def _iter_region_map_entries_v38(record: bytes):
    """Region Map entry layout found on a real Studio 5000 V38.02 (schema
    revision 1.0) project: the EXACT SAME 16-byte (parent_id, unknown,
    seq_no, object_id) tuple, same field order, as the pre-V38 layout --
    just relocated to start right after a 7-byte header (rather than the
    old 78-byte one), with no length field anywhere to bound it, so entries
    simply run to the exact end of the record.

    A first attempt at this (see git history / CLAUDE.md "Region Map format
    change") got the header offset wrong by exactly one field (4 bytes: used
    3, not 7) AND wrongly concluded the field order had changed. Both
    mistakes were self-consistent with each other in a way that passed a
    shallow validation check (searching for a known routine's own object_id
    correctly located it in the parent_id slot every time; the seq/"unknown"
    field even came back as a clean, contiguous 0..15 run), which is what let
    a genuinely wrong fix look right: reading 4 bytes early means the
    "object_id" field silently comes from the END of the PRECEDING 16-byte
    entry, not the start of the current one -- still a real, valid rung
    object_id (so it passes an "is this a real rung?" check), just the
    wrong routine's rung. Every routine's rungs came back populated, in the
    right relative order, and completely misattributed to the wrong
    routines. Caught only by checking actual rung TEXT content against a
    real Studio 5000 whole-project L5X export (ground truth) for specific,
    known routines -- a structurally-valid-looking result is not the same
    as a correct one; see the "don't trust a 'looks plausible' fix" lesson
    repeated elsewhere in CLAUDE.md. Re-verified this time by resolving
    ground-truth rung TEXT (not just object_id existence) to real object_ids
    via the independently-decoded `rungs` table, then confirming those
    specific object_ids land in the *current* entry's last field, not the
    preceding entry's."""
    start = 7
    offset = start
    end = len(record)
    while offset <= (end - 16):
        parent_id, unknown, seq_no, object_id = struct.unpack_from("<IIII", record, offset)
        yield (object_id, parent_id, unknown, seq_no, record[offset : offset + 16])
        offset += 16


@dataclass
class ExportL5x:
    input_filename: os.PathLike
    _temp_dir: str = ""
    _controller: Union[Controller, None] = None
    _project: Union[RSLogix5000Content, None] = None
    verbose: bool = True

    def __post_init__(self):
        if not self.verbose:
            # loguru's logger is a process-wide singleton -- there is no
            # per-instance/per-call log level in this library, so this is a
            # one-time, persistent reconfiguration of the default sink, not
            # scoped to just this load. This is exactly what a caller doing
            # `from loguru import logger; logger.remove()` before calling
            # this library already had to do themselves (the only previously
            # *possible*, but undocumented, way to quiet the ~15-20 lines of
            # INFO/DEBUG progress output every load_acd() call produces) --
            # exposed here as a documented parameter instead. WARNING and
            # above are always kept: this library relies on log.warning() to
            # surface real data-quality concerns (stale/deleted records,
            # unrecognized codes falling back to a guess, recovered data,
            # etc. -- see CLAUDE.md), which should never be silent even in
            # quiet mode.
            log.remove()
            log.add(sys.stderr, level="WARNING")
        if not self._temp_dir:
            acd_path = Path(self.input_filename)
            self._temp_dir = str(acd_path.parent / acd_path.stem)
        log.info(
            "Creating temporary directory (if it doesn't exist to store ACD database files - "
            + self._temp_dir
        )
        _DEFAULT_SQL_DATABASE_NAME = "acd.db"
        if os.path.exists(os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME)):
            os.remove(os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME))
        if not os.path.exists(os.path.join(self._temp_dir)):
            os.makedirs(self._temp_dir)
        log.info("Creating sqllite database to store ACD database records")
        self._db = sqlite3.connect(
            os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME)
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=OFF")
        self._cur: Cursor = self._db.cursor()

        log.debug("Create Comps table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create pointers table in sqllite db")
        self._cur.execute(
            "CREATE TABLE pointers(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create Rungs table in sqllite db")
        self._cur.execute(
            "CREATE TABLE rungs(object_id int, rung text, seq_number int)"
        )
        log.debug("Create Region_map table in sqllite db")
        self._cur.execute(
            "CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB NOT NULL)"
        )
        log.debug("Create Comments table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text, record_type int, parent int, tag_reference text, rung_content int, member_ref int, scope_id int)"
        )

        log.debug("Create Nameless table in sqllite db")
        self._cur.execute(
            "CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)"
        )

        log.debug("Create Regnlink table in sqllite db")
        self._cur.execute(
            "CREATE TABLE regnlink(routine_id int, fragment int, rung_object_id int)"
        )
        log.debug("Create Regnlink_idx table in sqllite db")
        self._cur.execute(
            "CREATE TABLE regnlink_idx(routine_id int, fragment int, rung_object_id int)"
        )
        log.debug("Create Regnlink_chain table in sqllite db")
        self._cur.execute(
            "CREATE TABLE regnlink_chain(routine_id int, own_rung int, next_rung int)"
        )

        log.info("Extracting ACD database file")
        unzip = Unzip(self.input_filename)
        unzip.write_files(self._temp_dir)

        # Preserve all embedded files in original order for round-trip writing.
        # Read directly from the ACD archive (pre-decompression) so that
        # compressed files are carried as-is and write-back is byte-identical.
        self._file_order: List[str] = [r.filename for r in unzip.records]
        self._footer_unknown: int = unzip.header._unknown_two
        self._raw_files: Dict[str, bytes] = {}
        with open(self.input_filename, "rb") as acd_fh:
            for record in unzip.records:
                acd_fh.seek(record.file_offset)
                self._raw_files[record.filename] = acd_fh.read(record.file_length)

        log.info("Getting records from ACD Comps file and storing in sqllite database")
        comps_by_id = _dedupe_comps_records(
            _parse_records(
                os.path.join(self._temp_dir, "Comps.Dat"), CompsRecord.parse, "Comps"
            )
        )
        self._cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", comps_by_id.values())
        self._db.commit()

        # Build name lookup for SbRegion tag reference resolution (object_id → comp_name).
        # Store on self for use during write-back (patch_sbregion_dat needs id_to_name).
        # A colliding object_id (see above) resolves to whichever of its entries was
        # inserted last here -- an accepted, pre-existing ambiguity for the rare real
        # case an object_id genuinely isn't unique, not something introduced by this fix.
        #
        # A "__Map:" prefix (e.g. "__Map:VAB_SERVER_Bridge") marks an internal shadow
        # comps object distinct from the real Module/device object of the same base
        # name (confirmed: two different object_ids, different parents) -- some raw
        # @HEX@ tag references in SbRegion.Dat point at the shadow object rather than
        # the real one (seen on every GSV(Module, ...) instruction sampled), but real
        # Studio 5000 never emits the "__Map:" prefix itself in rendered ladder text.
        # Stripping it here, at the single shared source for both rung-text resolution
        # (SbRegionRecord.parse) and tag-ref write-back (_restore_tag_refs), keeps both
        # consistent. Found via a real V38.02 project's whole-project L5X ground truth:
        # GSV(Module,__Map:FencePositioner1,...) where real Studio shows
        # GSV(Module,FencePositioner1,...) -- verified against every one of 15 affected
        # routines project-wide, zero remaining differences after stripping.
        name_lookup = {
            t[0]: (t[2][len("__Map:") :] if t[2].startswith("__Map:") else t[2])
            for t in comps_by_id.values()
        }
        self._id_to_name: Dict[int, str] = name_lookup

        log.info(
            "Getting records from ACD Region Map file and storing in sqllite database"
        )
        self.populate_region_map()

        log.info(
            "Getting records from ACD RegnLink file and storing in sqllite database"
        )
        self.populate_regnlink({t[0] for t in comps_by_id.values()})

        log.info(
            "Getting records from ACD SbRegion file and storing in sqllite database"
        )
        rung_tuples = _parse_records(
            os.path.join(self._temp_dir, "SbRegion.Dat"),
            lambda record: SbRegionRecord.parse(record, name_lookup),
            "SbRegion",
        )
        self._cur.executemany("INSERT INTO rungs VALUES (?,?,?)", rung_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Comments file and storing in sqllite database"
        )
        comment_tuples = _parse_records(
            os.path.join(self._temp_dir, "Comments.Dat"), CommentsRecord.parse, "Comments"
        )
        # Fix garbled "N]" -> "[N]" in tag_references (missing opening bracket).
        comment_tuples = [self._normalize_comment(t) for t in comment_tuples]
        # Deduplicate: for same (parent, tag_reference, scope_id), keep the one with the
        # longest description (preferring more descriptive type-6/7 records over shorter
        # ones). scope_id is included because multiple unrelated tags can share the same
        # (parent) container key while having identical-looking tag_reference suffixes
        # (e.g. two different array tags both having a "[0].DN" element) — scope_id is
        # what actually distinguishes them (see TagBuilder / _build_hex_oid_map usage).
        #
        # rung_content (t[7]) is also included: a routine's own whole-routine Description
        # and one of its rung comments can share the exact same (parent, tag_reference="",
        # scope_id, object_id) -- verified against a real project where a "Get_Bin"
        # routine's real Description ("Find bin for current set") and an unrelated rung
        # comment ("****Get_BIN\nSearch for available bin...") both had object_id=1 under
        # the same parent/scope_id, distinguished *only* by rung_content (0 for the
        # description, nonzero for the rung comment -- the same field
        # RoutineBuilder.build() already uses to tell them apart). Without rung_content in
        # this key, the dedup step silently discarded whichever of the two had the shorter
        # text -- not just a missing Description, but a risk of *also* dropping a real rung
        # comment in the reverse case.
        seen: Dict[tuple, tuple] = {}
        for t in comment_tuples:
            key = (t[5], t[6], t[9], t[7])
            if key not in seen or len(t[3]) > len(seen[key][3]):
                seen[key] = t
        self._cur.executemany("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?,?)", seen.values())
        self._db.commit()

        log.info(
            "Getting records from ACD Nameless file and storing in sqllite database"
        )
        nameless_tuples = _parse_records(
            os.path.join(self._temp_dir, "Nameless.Dat"), NamelessRecord.parse, "Nameless"
        )
        self._cur.executemany("INSERT INTO nameless VALUES (?,?,?)", nameless_tuples)
        self._db.commit()

        log.info("Creating indexes for fast object graph queries")
        self._cur.execute("CREATE INDEX idx_comps_object_id ON comps(object_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_id ON comps(parent_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_name ON comps(parent_id, comp_name)")
        self._cur.execute("CREATE INDEX idx_rungs_object_id ON rungs(object_id)")
        self._cur.execute("CREATE INDEX idx_region_map_parent_id ON region_map(parent_id)")
        self._cur.execute("CREATE INDEX idx_comments_parent ON comments(parent, scope_id)")
        self._cur.execute("CREATE INDEX idx_nameless_parent_id ON nameless(parent_id)")
        self._db.commit()

    @staticmethod
    def _normalize_comment(t: tuple) -> tuple:
        """Normalize comment tag_reference: fix garbled \"N]\" -> \"[N]\".
        Hex OID resolution is handled by TagBuilder (non-I/O tags).
        """
        seq, sub_len, obj_id, text, rec_type, parent, tag_ref, rung, member, scope_id = t
        if not tag_ref:
            return t

        # The lookbehind excludes "[", a digit, AND "," so this only matches a
        # digit-run at the true start of a bare/garbled index (e.g. "10]" with
        # no opening bracket at all) -- not the last component of an
        # already-bracketed multi-dimensional index like "[2,2,1]", where the
        # "1]" segment is preceded by a comma, not a missing bracket.
        new_ref = re.sub(r'(?<![\[\d,])(\d+])', r'[\1', tag_ref)

        if new_ref != tag_ref:
            return (seq, sub_len, obj_id, text, rec_type, parent, new_ref, rung, member, scope_id)
        return t

    @property
    def controller(self):
        if self._controller is None:
            self._controller = ControllerBuilder(self._cur).build()
        return self._controller

    @property
    def project(self):
        if self._project is None:
            self._project = ProjectBuilder(
                Path(os.path.join(self._temp_dir, "QuickInfo.XML"))
            ).build()
            self._project.controller = self.controller
            self._project._raw_files = self._raw_files
            self._project._file_order = self._file_order
            self._project._footer_unknown = self._footer_unknown
            self._project._id_to_name = self._id_to_name
        return self._project

    def close(self):
        self._db.close()

    def populate_region_map(self):
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id=0 AND comp_name='Region Map'"
        )
        results = self._cur.fetchall()

        if len(results) == 0:
            return
        record = results[0][3]

        identifier_offset = 70

        if len(record) < (identifier_offset + 8):
            return

        region_length = struct.unpack(
            "I", record[identifier_offset + 4 : identifier_offset + 8]
        )[0]

        identifier_offset = 78
        # region_length is always an exact multiple of 16 (one 16-byte entry
        # per rung/region), verified across every local fixture and a real
        # project -- it is not a "- 4" short of the true payload size, the
        # original "- 4" here silently dropped exactly the single last entry
        # in the whole table every time (confirmed against a real project: a
        # routine's very last rung was missing from region_map, and the
        # dropped 16-byte entry sat exactly at the true end of the buffer,
        # one entry beyond what "- 4" allowed the read loop to reach).
        record_length_absolute = identifier_offset + region_length

        # Schema-version check: on every local fixture and every real project
        # checked before Studio 5000 V38, `78 + region_length` lands EXACTLY
        # on len(record) -- the 78-byte header + region_length'd entry table
        # consumes the whole record with nothing left over. A real V38.02
        # project (schema revision 1.0) broke this: region_length read back
        # as a small, non-multiple-of-16 value miles short of the record's
        # real size (e.g. 8985 vs an 86151-byte true payload), causing this
        # loop to silently stop after ~10% of the table -- and worse, every
        # entry it DID emit was itself misaligned garbage (this file's real
        # entries live on a different 16-byte grid than offset 78 sits on),
        # so not one routine's rungs/rung_ids ever resolved (see CLAUDE.md
        # "Region Map format change" for the full investigation). Detected
        # by reverse-searching a real routine's own object_id through the
        # raw record: entries in the new layout are still a dense, gapless
        # array of the same 16-byte (object_id, parent_id, unknown, seq_no)
        # tuples (just reordered within the entry, and the field at the old
        # "region_length" offset no longer means anything) -- they simply
        # start at a fixed, tiny 3-byte header instead of the old 78-byte
        # one, and run to the exact end of the record with no length field
        # to trust at all.
        if record_length_absolute == len(record):
            entries = _iter_region_map_entries_v_pre38(record, identifier_offset, record_length_absolute)
        else:
            log.warning(
                "Region Map: header-declared length (78 + "
                f"{region_length}={record_length_absolute}) doesn't match the "
                f"record's real size ({len(record)}) -- falling back to the "
                "V38+ dense-array layout (see CLAUDE.md 'Region Map format "
                "change')"
            )
            entries = _iter_region_map_entries_v38(record)

        for object_id_identifier, parent_id_identifier, unknown_identifier, seq_identifier, blob in entries:
            self._cur.execute(
                "INSERT INTO region_map VALUES (?, ?, ?, ?, ?)",
                (object_id_identifier, parent_id_identifier, unknown_identifier, seq_identifier, blob),
            )

        self._db.commit()

    def populate_regnlink(self, known_object_ids: set) -> None:
        """Parse RegnLink.Dat *and RegnLink.Idx* and build two
        (routine_id, fragment) -> rung_object_id lookups used to resolve which
        rung a rung-level comment belongs to.

        IMPORTANT -- which lookup is authoritative: **RegnLink.Idx** (the
        `regnlink_idx` table built at the bottom of this function) stores an
        explicit fragment -> rung_object_id mapping and is what Studio 5000
        itself agrees with (verified 582/582 rung comments exact against a real
        project's own full L5X export, plus every staged edit-history test).
        The RegnLink.Dat chain reading below (fragment belongs to the rung
        identified by the link record's next_id) is only correct for routines
        whose rungs were never reordered/relinked -- in a real long-lived
        project it was wrong for ~75% of comments (usually off by exactly +2),
        because a link record keeps its fragment when its next pointer is
        redirected (verified: inserting a rung rewrites next_id but not the
        fragment). Kept as a fallback for files with a missing/corrupt Idx.
        See RoutineBuilder.build() for the lookup order and CLAUDE.md ("Rung
        comments") for the full investigation.

        RegnLink.Dat stores, per routine, a linked list of that routine's rungs.
        Each 22-byte link record has the shape (all little-endian):

          [0:4]   owner_id   -- the *routine's own* comps object_id (constant
                                 for every link record belonging to that routine)
          [4:8]   own_id     -- this link's own rung object_id (or the routine's
                                 own object_id, for the list head)
          [8:12]  next_id    -- the *next* rung's object_id in rung order
                                 (0xFFFFFFFF terminates the list)
          [12:16] type       -- 0x00020000 for a normal live link; 0xFFFF0000
                                 marks a stale/deleted link that must be
                                 ignored (found via a real project: a routine's
                                 first rung had two link records, an old
                                 dead-ended stale one and the real current one --
                                 without filtering by type, the stale one could
                                 win and truncate/misdirect the whole chain)
          [16:18] flags      -- unknown, constant "0001" in every sample seen
          [18:20] fragment   -- a 16-bit value that is *specific to the rung
                                 identified by next_id*, not to this record's
                                 own owner/own_id
          [20:22] unknown    -- unexplained, not needed for this lookup

        The critical discovery (verified byte-exact against a real Studio 5000
        "Export Routine" ground truth, see CLAUDE.md "Rung comments" section):
        a comment's rung_content field in Comments.Dat (see RoutineBuilder.build())
        has this exact same fragment value as its **upper 16 bits** when that
        comment is attached to the rung identified by next_id above. Resolving
        a comment's target rung is then: fragment = rung_content >> 16; look up
        (routine_id, fragment) here to get the rung's object_id; find that
        object_id's position in the routine's own (region_map-ordered) rung list.

        Records are not reliably contiguous in the file (real, long-lived
        projects fragment this data across edits) -- found by scanning the
        entire file for every occurrence of a *known* comps object_id as the
        4-byte owner field, which is small enough (file sizes seen so far:
        tens of KB to a few hundred KB) for a linear scan to be fast. Since
        routine object_ids are unknown at this point in ingestion (Comps has
        just been parsed, RoutineBuilder hasn't run yet), the check is
        conservatively widened to *any* known comps object_id rather than
        routines specifically -- a false-positive owner match is harmless,
        since RoutineBuilder only ever queries the fragments for its own
        specific routine_id, and a record whose "next_id" doesn't correspond
        to any real rung in that routine's own rungs list is simply never
        matched during lookup.
        """
        path = os.path.join(self._temp_dir, "RegnLink.Dat")
        if not os.path.exists(path):
            return
        with open(path, "rb") as f:
            data = f.read()

        rows: List[tuple] = []
        chain_rows: List[tuple] = []
        limit = len(data) - 22
        i = 0
        while i <= limit:
            owner_id = struct.unpack_from("<I", data, i)[0]
            if owner_id in known_object_ids:
                own_id, next_id, typ = struct.unpack_from("<III", data, i + 4)
                if typ != 0xFFFF0000:
                    fragment = struct.unpack_from("<H", data, i + 18)[0]
                    rows.append((owner_id, fragment, next_id))
                    # own_id/next_id (a routine-scoped rung linked list, see the
                    # docstring above) is a SEPARATE, independent source of
                    # routine<->rung ownership from Region Map -- used as a
                    # fallback in RoutineBuilder.build() for a rung whose Region
                    # Map entry has gone missing (a real, observed data-loss case,
                    # not a parsing gap -- see CLAUDE.md "Region Map entries can
                    # go missing independently of the format").
                    chain_rows.append((owner_id, own_id, next_id))
            i += 1

        if rows:
            self._cur.executemany("INSERT INTO regnlink VALUES (?,?,?)", rows)
            self._cur.execute(
                "CREATE INDEX idx_regnlink_routine_fragment ON regnlink(routine_id, fragment)"
            )
            self._db.commit()

        if chain_rows:
            self._cur.executemany("INSERT INTO regnlink_chain VALUES (?,?,?)", chain_rows)
            self._cur.execute(
                "CREATE INDEX idx_regnlink_chain_routine ON regnlink_chain(routine_id)"
            )
            self._cur.execute(
                "CREATE INDEX idx_regnlink_chain_own ON regnlink_chain(own_rung)"
            )
            self._db.commit()

        # RegnLink.Idx: the authoritative fragment -> rung map. B-tree-style
        # index pages contain dense 16-byte entries of the shape (all LE):
        #
        #   [0:2]   fragment   -- same 16-bit value as the .Dat record's [18:20]
        #   [2:3]   unk        -- same 7-bit value as the .Dat record's [20:22]
        #   [3:4]   0x00       -- always zero (used as a validation byte here)
        #   [4:8]   routine_id -- the owning routine's comps object_id
        #   [8:12]  rung_object_id -- THE rung this fragment belongs to (this is
        #                             the field the .Dat chain reading only
        #                             approximates)
        #   [12:16] ptr        -- file offset + 12 of the paired RegnLink.Dat
        #                          record carrying the same fragment (used as a
        #                          validation bound here: must be <= dat size)
        #
        # Like the .Dat scan above, entries are found by scanning the whole
        # file for any known comps object_id in the routine_id slot rather than
        # by walking the page structure -- stale entries from old/free pages do
        # survive this scan (a fragment can appear twice with different
        # rung_object_ids), so RoutineBuilder prefers the entry whose
        # rung_object_id is one of the routine's own live rungs.
        idx_path = os.path.join(self._temp_dir, "RegnLink.Idx")
        if not os.path.exists(idx_path):
            return
        with open(idx_path, "rb") as f:
            idx_data = f.read()

        dat_len = len(data)
        idx_rows: List[tuple] = []
        limit = len(idx_data) - 16
        i = 0
        while i <= limit:
            routine_id = struct.unpack_from("<I", idx_data, i + 4)[0]
            if routine_id in known_object_ids and idx_data[i + 3] == 0:
                fragment = struct.unpack_from("<H", idx_data, i)[0]
                rung_object_id, ptr = struct.unpack_from("<II", idx_data, i + 8)
                if fragment != 0xFFFF and ptr <= dat_len:
                    idx_rows.append((routine_id, fragment, rung_object_id))
            i += 1

        if idx_rows:
            self._cur.executemany("INSERT INTO regnlink_idx VALUES (?,?,?)", idx_rows)
            self._cur.execute(
                "CREATE INDEX idx_regnlink_idx_routine_fragment ON regnlink_idx(routine_id, fragment)"
            )
            self._db.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read an ACD file and export the database as an L5X file"
    )
    parser.add_argument(
        "input", metavar="input", type=str, nargs="+", help="The file to be converted"
    )
    parser.add_argument(
        "output",
        metavar="output",
        type=str,
        nargs="+",
        help="Filename of the exported file",
    )

    args = parser.parse_args()
    ExportL5x(args.input[0], args.output[0])
