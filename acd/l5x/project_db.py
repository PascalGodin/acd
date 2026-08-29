"""Persistent, directly-editable project database.

See CLAUDE.md's "Persistent project DB" section for the full design
rationale -- in short: `acd.db` (the SQLite database `ExportL5x` already
builds next to a loaded ACD, see `export_l5x.py`) also holds a normalized,
directly-editable layer (tags, UDT members, routines/rungs, tag comments)
on top of the raw binary-decode tables. An edit (`new_tag`, `new_member`,
`insert_rung`, ...) writes directly into that layer -- no separate
"pending edits" concept, no journal, no replay step -- so it persists
across separate Python process invocations without needing a live server
or an in-memory/pickle cache. This is meant to feel like editing Studio
5000's own offline project state: the DB *is* the current project, until
it's explicitly rebuilt from the real `.ACD` (a new Studio save, detected
via the source file's mtime, or an explicit `rebuild=True`).

All new tables here are named with a `proj_` prefix -- `ExportL5x` already
owns an unprefixed `rungs` table (raw `object_id, rung, seq_number`
records) in the SAME `acd.db` file; without the prefix, this module's own
schema would silently collide with (and DROP) that raw table on rebuild.

`.modules`/`.aois`/`.tasks` and every Controller-level scalar field are
NOT part of the new editable tables at all -- nothing edits these yet
(v1 scope is tags, UDT members, rungs, tag comments only). `to_controller()`
re-derives them fresh from the raw tables via the existing, already-verified
`ControllerBuilder` every call -- zero new decode logic for these, at the
cost of redoing that decode work each time `to_controller()` runs (a known,
accepted v1 tradeoff -- see its own docstring).

PREFER THE `db_*` FUNCTIONS BELOW (`db_new_tag`, `db_insert_rung`, ...) over
`open_project_db()`/`ProjectDB` directly for a typical one-off edit -- each
`db_*` call opens, does exactly one thing, and closes again before
returning, so there is no connection for a caller to forget to close. Every
call (through either surface) also acquires a project-wide lock file
(`project_dir/.lock`, see `_ProjectLock`) for its own duration, including a
rebuild -- this is what actually prevents the failure mode `_ProjectLock`'s
own docstring describes, not just caller discipline: a rebuild's raw
`os.remove()` of `acd.db` can never race a still-open connection, because by
construction nothing else can be mid-operation while the lock is held.
`open_project_db()`/`ProjectDB` are still there for a script doing many
edits in one session that wants to hold the lock across all of them rather
than pay one acquire/release cycle per edit.

ATOMICITY ACROSS SEVERAL EDITS -- `db_transaction()`/`ProjectDB.transaction()`.
Each `db_*` call commits the instant it returns -- unlike the old in-memory
workflow (`load_acd()` + edit + `export_routine()`), where a script that
raised partway through left zero durable side effects simply because
nothing was ever written until a final export call succeeded. With `db_*`,
a script that adds a UDT member, creates 3 tags, then raises on tag 4 has
already durably committed the member and the first 3 tags -- nothing marks
that as an incomplete attempt. Found via a real downstream report after a
session's worth of scripts that used to fail "cleanly" (collision asserts,
typos, ...) started leaving partial state behind instead. Use
`db_transaction(acd_path)` (a context manager) to batch several edits into
one atomic unit -- see its own docstring for the "don't call db_* functions
inside the block" caveat (they'd deadlock against the transaction's own
held lock).
"""
import contextlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Union

from loguru import logger as log

from acd.api import (
    _AOI_RESERVED_ROUTINE_NAMES,
    _sync_data_types_map,
    diff_io_addresses as _diff_io_addresses,
    diff_project as _diff_project,
    diff_routine as _diff_routine,
    export_aoi as _export_aoi,
    export_datatype as _export_datatype,
    export_program as _export_program,
    export_routine as _export_routine,
    find_tag_references as _find_tag_references,
    get_project_summary as _get_project_summary,
    get_routine as _get_routine,
    get_tag_value as _get_tag_value,
    io_addresses_by_routine as _io_addresses_by_routine,
    list_routines as _list_routines,
    list_tags as _list_tags,
    tag_exists as _tag_exists,
)
from acd.l5x.elements import (
    AOI,
    Controller,
    ControllerBuilder,
    DataType,
    LocalTag,
    Member,
    Parameter,
    Program,
    ProjectBuilder,
    RSLogix5000Content,
    Routine,
    Tag,
    _BUILTIN_STRUCT_MEMBERS,
    _validate_rll_rung_syntax,
    _zero_value_for_member,
    new_aoi as _new_aoi,
    new_aoi_local_tag as _new_aoi_local_tag,
    new_aoi_parameter as _new_aoi_parameter,
    new_bit_member as _new_bit_member,
    new_datatype as _new_datatype,
    new_member as _new_member,
    new_routine as _new_routine,
    new_tag as _new_tag,
)
from acd.l5x.export_l5x import _log_once, configure_logging, ExportL5x

_DB_FILENAME = "acd.db"

# A known Rockwell-internal placeholder name for an in-flight/incomplete UDT
# import -- Comps.Dat can genuinely contain MULTIPLE distinct DataType
# records that all render to this exact name (confirmed via a real project,
# left behind by a run of back-to-back partial-L5X imports), always empty
# (0 members). `proj_data_types.name` has a global UNIQUE index (see
# _SCHEMA above) -- inserting a second one crashed EVERY rebuild, which
# blocks every db_* call (reads included) against that project until the
# next successful rebuild. Filtered out entirely in _materialize() below,
# same "not a real object a caller would ever want to reference" reasoning
# already applied elsewhere in this codebase to other Comps-level artifacts
# (hex-named connections, "__Map:" prefixes, phantom Program/Module/Tag/
# Routine records -- see CLAUDE.md). Matched by prefix, not exact string,
# in case Rockwell appends a different counter suffix for a different
# in-flight import than the "_000" observed in the one real case seen so far.
_TEMPORARY_IMPORT_DATATYPE_PREFIX = "ZZZZZ_TEMPORARY_IMPORT_DATATYPE_NAME"

# Reserved `proj_programs.id` representing controller scope -- always
# present (inserted at rebuild time) so `proj_tags.program_id`/
# `proj_routines.program_id` can use a real FK-style value instead of
# relying on SQLite's NULL-is-never-equal-to-NULL uniqueness semantics for
# the controller-scope case.
_CONTROLLER_SCOPE = 0

_LOCK_FILENAME = ".lock"
_LOCK_POLL_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 30
# A lock file older than this is assumed to be left over from a process
# that died while holding it (crash, kill -9, ...) rather than genuinely
# still in use, and is stolen rather than waited out. Every operation this
# module does is local SQL against a small-to-moderate SQLite file -- there
# is no legitimate reason for one to hold the lock anywhere near this long;
# a real rebuild of a large project is the slowest case and still nowhere
# close. If a future caller legitimately needs to hold the lock longer than
# this, that's a sign the lock granularity itself needs revisiting, not a
# reason to raise this constant blindly.
_LOCK_STALE_SECONDS = 120


class _ProjectLock:
    """A simple cross-process mutex over one project's `acd.db`, backed by
    an exclusively-created lock file (`project_dir/.lock`) -- not SQLite's
    own locking, which only covers row-level access to an already-open
    database and has no say over `_rebuild_project_db()`'s raw filesystem
    `os.remove()`/recreate of the whole file. Without this, a rebuild
    racing a still-open connection (from this or another process) raises
    `PermissionError` on Windows (which refuses to delete an open file --
    POSIX would silently allow it, hence this not surfacing there). Every
    `db_*` function and every `open_project_db()` call acquires this for
    its own duration (see this module's own top-level docstring for why
    that -- not caller discipline -- is what actually closes the gap).

    `os.open(..., O_CREAT | O_EXCL)` is the acquire primitive: it's an
    atomic "create only if it doesn't already exist" available identically
    on Windows and POSIX, so two processes racing to acquire can never both
    succeed. A lock file older than `_LOCK_STALE_SECONDS` is assumed
    abandoned by a crashed holder and is stolen rather than waited out
    (an mtime-age heuristic, not real liveness detection -- simpler and
    dependency-free, and a false "still alive" read only costs a caller an
    extra `_LOCK_TIMEOUT_SECONDS`-long wait, never incorrect behavior).
    """

    def __init__(self, project_dir):
        self._path = os.path.join(str(project_dir), _LOCK_FILENAME)

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return
            except (FileExistsError, PermissionError):
                # PermissionError (not just FileExistsError): observed for real
                # on Windows -- immediately re-creating a filename right after
                # another thread/process deletes it can transiently raise
                # PermissionError instead of either succeeding or reporting
                # FileExistsError, a real NTFS delete/create race, not a
                # hypothetical. Treated identically to a genuinely-still-held
                # lock (retry/stale-check/timeout below) since a create that
                # fails for either reason means "can't acquire right now."
                try:
                    age = time.time() - os.path.getmtime(self._path)
                except OSError:
                    # Lock file vanished (or isn't statable yet) between the
                    # failed create and this stat -- just retry.
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"Timed out after {_LOCK_TIMEOUT_SECONDS}s waiting for "
                            f"project DB lock: {self._path} (held by another "
                            "process/connection)"
                        )
                    time.sleep(_LOCK_POLL_SECONDS)
                    continue
                if age > _LOCK_STALE_SECONDS:
                    log.warning(
                        f"_ProjectLock: stealing stale lock {self._path} "
                        f"(unheld for over {_LOCK_STALE_SECONDS}s -- presumed "
                        "abandoned by a crashed process)"
                    )
                    try:
                        os.remove(self._path)
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timed out after {_LOCK_TIMEOUT_SECONDS}s waiting for "
                        f"project DB lock: {self._path} (held by another "
                        "process/connection)"
                    )
                time.sleep(_LOCK_POLL_SECONDS)

    def release(self) -> None:
        try:
            os.remove(self._path)
        except OSError:
            pass

    def __enter__(self) -> "_ProjectLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


_SCHEMA = """
DROP TABLE IF EXISTS proj_meta;
DROP TABLE IF EXISTS proj_members;
DROP TABLE IF EXISTS proj_data_types;
DROP TABLE IF EXISTS proj_tag_comments;
DROP TABLE IF EXISTS proj_tags;
DROP TABLE IF EXISTS proj_rungs;
DROP TABLE IF EXISTS proj_st_lines;
DROP TABLE IF EXISTS proj_routines;
DROP TABLE IF EXISTS proj_programs;
DROP TABLE IF EXISTS proj_aoi_local_tags;
DROP TABLE IF EXISTS proj_aoi_parameters;
DROP TABLE IF EXISTS proj_aois;

CREATE TABLE proj_meta (
    source_acd_path TEXT NOT NULL,
    source_acd_mtime REAL NOT NULL,
    -- Flipped to 1 by every edit method (new_tag, insert_rung, ...) in the
    -- same commit as the edit itself; checked (and warned on) by
    -- open_project_db() before a rebuild would discard it. A fresh
    -- materialize() always inserts 0 here (nothing to lose yet).
    dirty INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE proj_data_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    cls TEXT NOT NULL,
    description TEXT
);
CREATE UNIQUE INDEX idx_proj_data_types_name ON proj_data_types(name COLLATE NOCASE);

CREATE TABLE proj_members (
    id INTEGER PRIMARY KEY,
    data_type_id INTEGER NOT NULL REFERENCES proj_data_types(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_type_name TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    radix TEXT,
    hidden INTEGER NOT NULL,
    target TEXT,
    bit_number INTEGER,
    external_access TEXT NOT NULL,
    description TEXT
);
CREATE UNIQUE INDEX idx_proj_members_seq ON proj_members(data_type_id, seq);
CREATE UNIQUE INDEX idx_proj_members_name ON proj_members(data_type_id, name COLLATE NOCASE);

CREATE TABLE proj_programs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    main_routine_name TEXT,
    fault_routine_name TEXT,
    disabled TEXT NOT NULL,
    test_edits TEXT NOT NULL,
    description TEXT
);
CREATE UNIQUE INDEX idx_proj_programs_name ON proj_programs(name COLLATE NOCASE);

-- v1 scope: proj_aois holds ONLY brand-new AOIs authored through new_aoi()/
-- db_new_aoi() -- NEVER populated from a real project's own pre-existing
-- AOIs at rebuild time (unlike every other proj_* table, which materializes
-- the WHOLE real project). This is deliberate, not an oversight: a real
-- AOI's own LocalTags aren't persisted here at all (out of scope, see
-- new_aoi()'s own docstring), so materializing a real AOI into this table
-- would silently drop its real LocalTags on every rehydration. to_controller()
-- APPENDS these onto the real, freshly-decoded `.aois` list instead of
-- replacing it -- see its own docstring.
CREATE TABLE proj_aois (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    revision TEXT NOT NULL,
    revision_extension TEXT,
    vendor TEXT,
    execute_prescan TEXT NOT NULL,
    execute_postscan TEXT NOT NULL,
    execute_enable_in_false TEXT NOT NULL,
    created_date TEXT NOT NULL,
    created_by TEXT NOT NULL,
    edited_date TEXT NOT NULL,
    edited_by TEXT NOT NULL,
    software_revision TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_proj_aois_name ON proj_aois(name COLLATE NOCASE);

CREATE TABLE proj_aoi_parameters (
    id INTEGER PRIMARY KEY,
    aoi_id INTEGER NOT NULL REFERENCES proj_aois(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_type_name TEXT NOT NULL,
    dimensions TEXT,
    radix TEXT,
    usage TEXT NOT NULL,
    required TEXT NOT NULL,
    visible TEXT NOT NULL,
    external_access TEXT,
    constant TEXT,
    description TEXT
);
CREATE UNIQUE INDEX idx_proj_aoi_params_seq ON proj_aoi_parameters(aoi_id, seq);
CREATE UNIQUE INDEX idx_proj_aoi_params_name ON proj_aoi_parameters(aoi_id, name COLLATE NOCASE);

CREATE TABLE proj_aoi_local_tags (
    id INTEGER PRIMARY KEY,
    aoi_id INTEGER NOT NULL REFERENCES proj_aois(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    data_type_name TEXT NOT NULL,
    dimensions TEXT,
    radix TEXT,
    external_access TEXT NOT NULL,
    description TEXT
);
CREATE UNIQUE INDEX idx_proj_aoi_local_tags_seq ON proj_aoi_local_tags(aoi_id, seq);
CREATE UNIQUE INDEX idx_proj_aoi_local_tags_name ON proj_aoi_local_tags(aoi_id, name COLLATE NOCASE);

CREATE TABLE proj_tags (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES proj_programs(id),
    name TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    data_type_name TEXT NOT NULL,
    radix TEXT,
    external_access TEXT NOT NULL,
    constant TEXT,
    dimensions TEXT,
    target TEXT,
    initial_value TEXT,
    source_object_id INTEGER
);
CREATE UNIQUE INDEX idx_proj_tags_scope_name ON proj_tags(program_id, name COLLATE NOCASE);

CREATE TABLE proj_tag_comments (
    id INTEGER PRIMARY KEY,
    tag_id INTEGER NOT NULL REFERENCES proj_tags(id),
    path TEXT NOT NULL,
    text TEXT NOT NULL
);

-- Exactly one of program_id/aoi_id is set -- a routine belongs to a Program
-- (the original, still-primary case) OR an AOI's own logic (new_aoi(),
-- new_routine(..., aoi_name=...)/db_new_routine(..., aoi_name=...)), never
-- both and never neither. Two separate PARTIAL unique indexes (not one
-- combined "UNIQUE(program_id, aoi_id, name)") -- SQLite treats every NULL
-- as distinct from every other NULL, so a combined index would never
-- actually catch a same-name collision within the same aoi_id (the
-- always-NULL program_id column would make every row look unique
-- regardless) -- the exact same pitfall proj_programs' own id=0 sentinel
-- row was designed to avoid for controller-scope tags; a partial index
-- (WHERE program_id/aoi_id IS NOT NULL) sidesteps it without needing an
-- equivalent sentinel AOI row here.
CREATE TABLE proj_routines (
    id INTEGER PRIMARY KEY,
    program_id INTEGER REFERENCES proj_programs(id),
    aoi_id INTEGER REFERENCES proj_aois(id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    CHECK ((program_id IS NULL) != (aoi_id IS NULL))
);
CREATE UNIQUE INDEX idx_proj_routines_scope_name ON proj_routines(program_id, name COLLATE NOCASE)
    WHERE program_id IS NOT NULL;
CREATE UNIQUE INDEX idx_proj_routines_aoi_name ON proj_routines(aoi_id, name COLLATE NOCASE)
    WHERE aoi_id IS NOT NULL;

CREATE TABLE proj_rungs (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER NOT NULL REFERENCES proj_routines(id),
    rung_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    comment TEXT,
    source_object_id INTEGER
);
CREATE UNIQUE INDEX idx_proj_rungs_pos ON proj_rungs(routine_id, rung_index);

CREATE TABLE proj_st_lines (
    id INTEGER PRIMARY KEY,
    routine_id INTEGER NOT NULL REFERENCES proj_routines(id),
    line_index INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_proj_st_lines_pos ON proj_st_lines(routine_id, line_index);
"""


def _materialize(db: sqlite3.Connection, project: RSLogix5000Content, acd_path) -> None:
    """Walk the freshly-built (unedited) object graph `project` exactly
    once and INSERT it into the normalized `proj_*` tables -- the only
    place this module ever reads the raw comps/rungs/etc. tables' *decoded*
    shape; every other read in this module queries the `proj_*` tables
    written here.

    Raises `sqlite3.IntegrityError` (with the offending name/scope, not
    just SQLite's own opaque "UNIQUE constraint failed") if the source
    project genuinely has two DataTypes/tags/routines sharing a name within
    the same uniqueness scope -- these tables assume unique names (a real,
    documented v1 limitation, see CLAUDE.md's "A real .ACD can contain two
    DataTypes with the same name"), and a raw ACD can legitimately violate
    that in ways this codebase already knows to filter for other object
    kinds. Re-raising with a clear message at least makes a FUTURE instance
    of this (a tag/routine collision, or a differently-named DataType
    duplicate that isn't the known placeholder pattern below) fast to
    diagnose instead of every db_* call -- reads included -- failing on an
    opaque crash with no indication of which object or scope is responsible.
    """
    db.executescript(_SCHEMA)
    cur = db.cursor()
    cur.execute(
        "INSERT INTO proj_meta (source_acd_path, source_acd_mtime) VALUES (?, ?)",
        (str(acd_path), os.path.getmtime(acd_path)),
    )
    cur.execute(
        "INSERT INTO proj_programs (id, name, main_routine_name, fault_routine_name, "
        "disabled, test_edits, description) VALUES (?, '', NULL, NULL, 'false', 'false', NULL)",
        (_CONTROLLER_SCOPE,),
    )

    ctrl = project.controller

    skipped_placeholder_dts = 0
    for dt in ctrl.data_types:
        if dt.name.startswith(_TEMPORARY_IMPORT_DATATYPE_PREFIX):
            skipped_placeholder_dts += 1
            continue
        try:
            cur.execute(
                "INSERT INTO proj_data_types (name, family, cls, description) VALUES (?, ?, ?, ?)",
                (dt.name, dt.family, dt.cls, dt._description),
            )
        except sqlite3.IntegrityError as e:
            raise sqlite3.IntegrityError(
                f"proj_data_types: DataType name {dt.name!r} collides with another "
                f"DataType already inserted -- Comps.Dat genuinely contains two distinct "
                f"DataType records rendering to the same name, and this schema currently "
                f"requires DataType names to be globally unique (see CLAUDE.md). "
                f"Original error: {e}"
            ) from e
        dt_id = cur.lastrowid
        for seq, m in enumerate(dt.members):
            cur.execute(
                "INSERT INTO proj_members (data_type_id, seq, name, data_type_name, dimension, "
                "radix, hidden, target, bit_number, external_access, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dt_id, seq, m.name, m.data_type, m.dimension, m.radix, int(m.hidden),
                 m.target, m.bit_number, m.external_access, m._description),
            )
    if skipped_placeholder_dts:
        log.info(
            f"_materialize(): skipped {skipped_placeholder_dts} "
            f"'{_TEMPORARY_IMPORT_DATATYPE_PREFIX}*' placeholder DataType(s) -- a "
            f"Rockwell-internal in-flight-import marker, not a real type"
        )

    def _insert_tag(program_id: int, program_label: str, tag: Tag) -> None:
        try:
            cur.execute(
                "INSERT INTO proj_tags (program_id, name, tag_type, data_type_name, radix, "
                "external_access, constant, dimensions, target, initial_value, source_object_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (program_id, tag.name, tag.tag_type, tag.data_type, tag.radix,
                 tag.external_access, tag.constant, tag.dimensions, tag.target,
                 json.dumps(tag._initial_value) if tag._initial_value is not None else None,
                 tag._data_table_instance or None),
            )
        except sqlite3.IntegrityError as e:
            raise sqlite3.IntegrityError(
                f"proj_tags: tag name {tag.name!r} collides with another tag already "
                f"inserted in scope {program_label!r} -- Comps.Dat genuinely contains two "
                f"distinct tag records sharing this (scope, name), and this schema "
                f"currently requires tag names to be unique per scope. "
                f"Original error: {e}"
            ) from e
        tag_id = cur.lastrowid
        for path, text in tag._comments:
            cur.execute(
                "INSERT INTO proj_tag_comments (tag_id, path, text) VALUES (?, ?, ?)",
                (tag_id, path, text),
            )

    for tag in ctrl.tags:
        _insert_tag(_CONTROLLER_SCOPE, "<controller>", tag)

    for program in ctrl.programs:
        cur.execute(
            "INSERT INTO proj_programs (name, main_routine_name, fault_routine_name, disabled, "
            "test_edits, description) VALUES (?, ?, ?, ?, ?, ?)",
            (program.name, program.main_routine_name, program.fault_routine_name,
             program.disabled, program.test_edits, program._description),
        )
        program_id = cur.lastrowid
        for tag in program.tags:
            _insert_tag(program_id, program.name, tag)
        for routine in program.routines:
            try:
                cur.execute(
                    "INSERT INTO proj_routines (program_id, name, type, description) "
                    "VALUES (?, ?, ?, ?)",
                    (program_id, routine.name, routine.type, routine._description),
                )
            except sqlite3.IntegrityError as e:
                raise sqlite3.IntegrityError(
                    f"proj_routines: routine name {routine.name!r} collides with another "
                    f"routine already inserted in program {program.name!r} -- Comps.Dat "
                    f"genuinely contains two distinct routine records sharing this "
                    f"(program, name), and this schema currently requires routine names "
                    f"to be unique per program. Original error: {e}"
                ) from e
            routine_id = cur.lastrowid
            for i, text in enumerate(routine.rungs):
                source_object_id = routine._rung_ids[i] if i < len(routine._rung_ids) else None
                cur.execute(
                    "INSERT INTO proj_rungs (routine_id, rung_index, text, comment, source_object_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (routine_id, i, text, routine._rung_comments.get(i), source_object_id),
                )
            for i, text in enumerate(routine._st_lines):
                cur.execute(
                    "INSERT INTO proj_st_lines (routine_id, line_index, text) VALUES (?, ?, ?)",
                    (routine_id, i, text),
                )
    db.commit()


def _rebuild_project_db(acd_path, project_dir, verbose: bool = False) -> None:
    """Run the existing, unmodified `ExportL5x` pipeline (unzip + raw
    binary-record parse + full object-graph build -- exactly what a plain
    `load_acd()` does) and materialize its result into the normalized
    `proj_*` tables, in the SAME `acd.db` file `ExportL5x` already creates.
    `ExportL5x.__post_init__` unconditionally deletes any existing `acd.db`
    in `project_dir` before recreating it, so this is always a full wipe
    of both the raw and normalized layers -- no merge/reconciliation logic
    needed, by design (see CLAUDE.md).
    """
    exporter = ExportL5x(str(acd_path), str(project_dir), verbose=verbose)
    try:
        project = exporter.project
        _materialize(exporter._db, project, acd_path)
    finally:
        exporter.close()


def open_project_db(acd_path, project_dir=None, rebuild: bool = False,
                     verbose: bool = False) -> "ProjectDB":
    """Open (or build) the persistent, directly-editable project DB for
    `acd_path`.

    `project_dir` defaults to the same convention `ExportL5x` itself uses
    when given no explicit temp_dir (`<dir>/<stem>/`, e.g. `MyController.ACD`
    -> `MyController/`) -- unlike `load_acd()`, which always uses a
    throwaway OS temp directory, this is deliberately a stable, predictable
    location next to the source file so a later call (in the same or a
    different process) finds the same DB.

    Rebuilds from the real `.ACD` (via `_rebuild_project_db()`) when
    `rebuild=True`, when no DB exists yet, or when the source file's mtime
    doesn't match what's recorded in `proj_meta` (e.g. a new Studio 5000
    save since the last rebuild) -- otherwise just opens the existing file
    directly, with no re-parsing at all.

    Acquires this project's `_ProjectLock` (see its own docstring) BEFORE
    even checking staleness, and holds it for the lifetime of the returned
    `ProjectDB` -- released by `.close()`. This is what actually prevents a
    rebuild from racing a connection some other caller still has open, not
    just a documentation convention; prefer the `db_*` functions below for
    a single edit so there's no long-lived handle for a caller to forget to
    release.

    If a rebuild is about to happen (mtime changed, or `rebuild=True`) AND
    the existing DB has at least one edit since its own last rebuild that
    was never exported (`proj_meta.dirty`, see the edit methods below),
    logs a WARNING before discarding it rather than doing so silently at
    INFO level -- found via a real report: the source `.ACD` here got
    re-synced from Studio mid-session more than once, and a `db_*` edit
    made just before that happened vanished with no visible signal at all.

    Also applies `configure_logging(verbose)` (see `export_l5x.py`)
    unconditionally, even when no rebuild happens -- `to_controller()`
    calls `ControllerBuilder` directly against an already-open connection,
    which never goes through `ExportL5x.__post_init__` (the only other
    place this was previously applied), so a process that never triggers a
    rebuild would otherwise see this library's INFO/DEBUG progress output
    unfiltered regardless of `verbose=False`.
    """
    configure_logging(verbose)
    acd_path = Path(acd_path)
    project_dir = Path(project_dir) if project_dir is not None else acd_path.parent / acd_path.stem
    db_file = project_dir / _DB_FILENAME

    lock = _ProjectLock(project_dir)
    lock.acquire()
    try:
        needs_rebuild = rebuild or not db_file.exists()
        was_dirty = False
        if db_file.exists():
            probe = sqlite3.connect(str(db_file))
            try:
                row = probe.execute("SELECT source_acd_mtime, dirty FROM proj_meta").fetchone()
            except sqlite3.OperationalError:
                # acd.db exists but has no proj_meta table -- either a plain
                # load_acd()/ExportL5x call left raw-only tables here, or a
                # previous rebuild was interrupted before materialization.
                row = None
            finally:
                probe.close()
            if row is None:
                needs_rebuild = True
            else:
                was_dirty = bool(row[1])
                if not needs_rebuild:
                    needs_rebuild = row[0] != os.path.getmtime(acd_path)

        if needs_rebuild:
            if was_dirty:
                # _log_once(): the first occurrence in this process logs at
                # WARNING (as before); an identical repeat (e.g. a retry
                # loop, or many open_project_db() calls in one long-running
                # script) logs at DEBUG instead -- same real content, just
                # not repeated in full every single time. See its own
                # docstring (export_l5x.py) for why this is per-process,
                # not persisted.
                _log_once(
                    f"open_project_db(): rebuilding {db_file} from {acd_path} -- this DISCARDS "
                    "one or more edits made since the last rebuild that were never exported via "
                    "db_export_routine()/db_export_datatype() (the source .ACD changed, or "
                    "rebuild=True was passed). Export first if you don't want those edits lost."
                )
            else:
                log.info(f"open_project_db(): rebuilding {db_file} from {acd_path}")
            os.makedirs(project_dir, exist_ok=True)
            _rebuild_project_db(acd_path, project_dir, verbose=verbose)
        else:
            log.info(f"open_project_db(): reusing {db_file}, source ACD unchanged")

        conn = sqlite3.connect(str(db_file))
    except Exception:
        lock.release()
        raise
    return ProjectDB(acd_path, project_dir, conn, lock)


_ELEMENT_PATH_INDEX_RE = re.compile(r"^\[(\d+)\]")
_ELEMENT_PATH_MEMBER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_tag_element_path(path: str):
    """Parse a `set_tag_element_value()` path into `(index_or_None,
    [member_name, ...])`.

    Deliberately narrow, matching a single-dimension array element plus a
    dotted member chain -- the exact shape the motivating real report
    described (`"[3].PRE"` for `StartFaultTimer[3].PRE`, one motor out of
    an `[11]`-element array of structs): an OPTIONAL leading `[N]` (a
    literal top-level array index -- required if and only if the tag
    itself is declared as a 1-D array), followed by zero or more
    `.MemberName` segments (a leading `.` is optional and stripped either
    way). NOT supported, and raises `ValueError` naming the gap rather than
    guessing: a multi-dimensional index (`[2,2]`), an index on a MEMBER
    rather than the top-level tag (`.Times[2]`), or a completely empty path
    (use `edit_tag(value=...)` to set a tag's whole value instead).
    """
    index = None
    rest = path
    m = _ELEMENT_PATH_INDEX_RE.match(rest)
    if m:
        index = int(m.group(1))
        rest = rest[m.end():]
    if "[" in rest or "]" in rest:
        raise ValueError(
            f"Tag element path {path!r}: only a single, single-dimension leading "
            f"'[N]' index is supported -- a multi-dimensional or member-level array "
            f"index is not."
        )
    rest = rest.lstrip(".")
    members = [seg for seg in rest.split(".") if seg] if rest else []
    for seg in members:
        if not _ELEMENT_PATH_MEMBER_RE.match(seg):
            raise ValueError(f"Tag element path {path!r}: invalid member segment {seg!r}")
    if index is None and not members:
        raise ValueError(
            f"Tag element path {path!r} is empty -- must name at least a leading array "
            f"index ('[N]') or a member ('MemberName' / '[N].MemberName'); use "
            f"edit_tag(value=...) to set a tag's WHOLE value instead."
        )
    return index, members


class ProjectDB:
    """A handle onto one project's persistent, directly-editable DB --
    returned by `open_project_db()`, not constructed directly.

    Edits (`new_tag`/`edit_tag`/`new_member`/`insert_rung`/`delete_rung`/
    `replace_rung_safe`/`set_tag_comment`) write straight into the
    underlying SQLite tables and commit immediately -- there is no
    in-memory staging step, no separate "save" call -- UNLESS called inside
    a `.transaction()` block (see its own docstring), which defers every
    edit's commit to the block's own exit instead, rolling all of them back
    together if anything inside the block raises. Every method that needs
    to resolve a name against this project's current live state
    (`edit_tag`, `insert_rung`, `to_controller`, ...) reads the DB fresh
    each call (including its own uncommitted writes from earlier in the
    same `.transaction()` block, if any -- a single SQLite connection
    always sees its own in-flight changes), so edits made by a different
    process against the same `acd_path`/`project_dir` (opened via its own
    `open_project_db()` call) are visible immediately once committed, no
    polling/refresh step needed.

    Holds this project's `_ProjectLock` for its entire lifetime (acquired
    by `open_project_db()` before this object is constructed, released by
    `.close()`) -- another process's `open_project_db()`/`db_*` call
    against the same project will wait for this one to close rather than
    racing it.
    """

    def __init__(self, acd_path: Path, project_dir: Path, conn: sqlite3.Connection,
                 lock: "_ProjectLock"):
        self.acd_path = acd_path
        self.project_dir = project_dir
        self._conn = conn
        self._lock = lock
        self._in_transaction = False

    def close(self) -> None:
        self._conn.close()
        self._lock.release()

    def __enter__(self) -> "ProjectDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self):
        """Batch several edit calls into ONE atomic unit: nothing commits
        until the `with` block exits cleanly; if anything inside it raises,
        every edit made so far in the block is rolled back together, not
        left as whatever partial state happened to exist at the moment of
        the exception. See this module's own top-level docstring
        ("ATOMICITY ACROSS SEVERAL EDITS") for the real report that
        prompted this -- without it, each edit method commits independently
        the instant it returns, so a script that raises partway through a
        multi-step edit leaves everything up to that point durably sitting
        in the DB with nothing marking it as an incomplete attempt.

            with db.transaction():
                db.new_member(dt_name, "Foo", "DINT")
                db.new_tag("Tag1", "DINT")
                db.new_tag("Tag2", "DINT")
                db.insert_rung(routine_name, 0, "...")
            # all four committed together, or none of them did

        Do NOT call the stateless `db_*` functions (`db_new_tag`, ...) from
        inside this block -- each of those opens its OWN connection and
        tries to acquire the SAME project lock this transaction is already
        holding, and will hang until that call's own lock-acquire timeout
        rather than deadlock instantly. Use this `ProjectDB` instance's own
        methods instead (`db.new_tag(...)`, not `acd.db_new_tag(...)`).

        Cannot be nested (raises `RuntimeError`) -- this is one flat
        transaction, not SAVEPOINT-based partial rollback.
        """
        if self._in_transaction:
            raise RuntimeError("ProjectDB.transaction() cannot be nested")
        self._in_transaction = True
        try:
            yield self
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        finally:
            self._in_transaction = False

    # ---- scope resolution helpers ----

    def _program_id(self, program_name: Union[str, None]) -> int:
        if program_name is None:
            return _CONTROLLER_SCOPE
        row = self._conn.execute(
            "SELECT id FROM proj_programs WHERE id != ? AND name=? COLLATE NOCASE",
            (_CONTROLLER_SCOPE, program_name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No program named {program_name!r}")
        return row[0]

    def _aoi_id(self, aoi_name: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM proj_aois WHERE name=? COLLATE NOCASE", (aoi_name,)
        ).fetchone()
        if row is None:
            raise KeyError(
                f"No AOI named {aoi_name!r} -- only AOIs created via new_aoi()/db_new_aoi() "
                f"in THIS project DB are addressable here, not a real project's pre-existing "
                f"AOIs (v1 scope limit, see new_aoi()'s own docstring)."
            )
        return row[0]

    def _routine_id(self, routine_name: str, program_name: Union[str, None] = None,
                     aoi_name: Union[str, None] = None) -> int:
        """Resolve a routine name to its `proj_routines.id`.

        `program_name` given: looks ONLY in that program. `aoi_name` given:
        looks ONLY in that AOI (created via `new_aoi()`/`db_new_aoi()` in
        THIS project DB -- same v1 scope limit as `_aoi_id()`). Passing both
        raises `ValueError`. Neither given (both `None`, the default):
        searches every program AND every AOI -- raises `ValueError` naming
        every scope a match was found in if genuinely ambiguous (this is the
        case an AOI whose routine uses Rockwell's own conventional name,
        e.g. `"Logic"`, hits immediately -- pass `aoi_name=`/`program_name=`
        to disambiguate).

        A `program_name` starting with `"AOI:"` (e.g. `"AOI:MyAOI"`) is
        treated as shorthand for `aoi_name="MyAOI"` -- this is the exact
        convention `list_routines()`'s own `"program"` field already uses
        for an AOI-owned routine (matching `acd.api._all_routines()`'s own
        keying), so a caller can feed a `list_routines()` entry straight
        into `get_routine(entry["routine"], program_name=entry["program"])`
        without special-casing AOI entries themselves.
        """
        if program_name is not None and program_name.startswith("AOI:") and aoi_name is None:
            aoi_name = program_name[len("AOI:"):]
            program_name = None
        if program_name is not None and aoi_name is not None:
            raise ValueError(
                "pass at most one of program_name/aoi_name to disambiguate a routine, not both"
            )
        if program_name is not None:
            program_id = self._program_id(program_name)
            row = self._conn.execute(
                "SELECT id FROM proj_routines WHERE program_id=? AND name=? COLLATE NOCASE",
                (program_id, routine_name),
            ).fetchone()
            if row is None:
                raise KeyError(f"No routine {routine_name!r} in program {program_name!r}")
            return row[0]
        if aoi_name is not None:
            aoi_id = self._aoi_id(aoi_name)
            row = self._conn.execute(
                "SELECT id FROM proj_routines WHERE aoi_id=? AND name=? COLLATE NOCASE",
                (aoi_id, routine_name),
            ).fetchone()
            if row is None:
                raise KeyError(f"No routine {routine_name!r} in AOI {aoi_name!r}")
            return row[0]
        rows = self._conn.execute(
            "SELECT r.id, COALESCE(p.name, '(AOI:' || a.name || ')') FROM proj_routines r "
            "LEFT JOIN proj_programs p ON p.id = r.program_id "
            "LEFT JOIN proj_aois a ON a.id = r.aoi_id "
            "WHERE r.name=? COLLATE NOCASE",
            (routine_name,),
        ).fetchall()
        if not rows:
            raise KeyError(f"No routine named {routine_name!r} in any program or AOI")
        if len(rows) > 1:
            scopes = [s for _, s in rows]
            raise ValueError(
                f"Routine name {routine_name!r} is ambiguous -- found in "
                f"{scopes}; pass program_name= or aoi_name= to disambiguate"
            )
        return rows[0][0]

    def _routine_type(self, routine_id: int) -> str:
        return self._conn.execute(
            "SELECT type FROM proj_routines WHERE id=?", (routine_id,)
        ).fetchone()[0]

    # ---- edits ----

    def new_tag(self, name: str, data_type: str, program_name: Union[str, None] = None,
                dimensions: Union[str, None] = None, description: Union[str, None] = None,
                value=None, external_access: str = "Read/Write") -> int:
        """Create a new tag directly in this project's DB -- persists for
        every future `open_project_db()` call against this same project
        until the next rebuild, unlike constructing a plain `new_tag()`
        object that only lives in one process's memory. Reuses `new_tag()`
        (`acd/l5x/elements/model.py`) for its radix-default/field-shape
        logic, then persists the resulting object's fields as a row.

        Raises `sqlite3.IntegrityError` if the name already exists in this
        scope, `KeyError` if `program_name` doesn't match any program.
        """
        program_id = self._program_id(program_name)
        tag = _new_tag(name, data_type, dimensions=dimensions, description=description,
                        value=value, external_access=external_access)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO proj_tags (program_id, name, tag_type, data_type_name, radix, "
            "external_access, constant, dimensions, target, initial_value, source_object_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (program_id, tag.name, tag.tag_type, tag.data_type, tag.radix,
             tag.external_access, tag.constant, tag.dimensions, tag.target,
             json.dumps(tag._initial_value) if tag._initial_value is not None else None),
        )
        tag_id = cur.lastrowid
        for path, text in tag._comments:
            cur.execute(
                "INSERT INTO proj_tag_comments (tag_id, path, text) VALUES (?, ?, ?)",
                (tag_id, path, text),
            )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return tag_id

    def edit_tag(self, name: str, program_name: Union[str, None] = None,
                 description: Union[str, None] = None, value=None) -> None:
        """Update an existing tag's description and/or value in place.
        Only fields actually passed are changed -- omitting `value`
        (leaving it `None`) leaves the tag's current value untouched;
        there's no way to explicitly clear a tag's value back to unknown
        through this method, matching `new_tag()`'s own convention that
        `None` means "no value information" rather than a real settable
        state. Raises `KeyError` if no tag named `name` exists in scope.
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_tags WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {name!r} in this scope")
        tag_id = row[0]
        if value is not None:
            cur.execute("UPDATE proj_tags SET initial_value=? WHERE id=?",
                        (json.dumps(value), tag_id))
        if description is not None:
            cur.execute("DELETE FROM proj_tag_comments WHERE tag_id=? AND path=''", (tag_id,))
            cur.execute("INSERT INTO proj_tag_comments (tag_id, path, text) VALUES (?, '', ?)",
                        (tag_id, description))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def set_tag_element_value(self, tag_name: str, path: str, value,
                               program_name: Union[str, None] = None) -> None:
        """Set ONE leaf value inside a tag's (possibly nested, possibly
        array-of-struct) `_initial_value`, without requiring the caller to
        reconstruct the whole nested value by hand.

        Added after a real report: a UDT array tag (e.g. an `[11]`-element
        array of a `Motor`-shaped struct) needed one member set per element
        (`StartFaultTimer.PRE` per motor) at creation time -- the only
        existing option, `edit_tag(value=...)`, replaces the tag's ENTIRE
        value, so setting one member for one element meant hand-building
        the full nested list-of-dicts structure (all 11 motors, every
        member of each) just to change one field of one of them. The
        reporting agent's own workaround was to leave it at the type
        default and flag it for manual tuning instead.

        `path` addresses the target leaf: an OPTIONAL leading `[N]` (a
        literal top-level array index -- required if and only if `tag_name`
        is itself declared as a 1-D array; `N` must be in range for its
        declared `Dimensions`), followed by zero or more `.MemberName`
        segments (dot-separated, arbitrarily deep through nested structs).
        Examples: `"[3].PRE"` (element 3's `PRE` member),
        `"[3].Sub.PRE"` (a nested struct member), `"PRE"` (a scalar struct
        tag, no array), `"[3]"` (the whole element of a primitive array,
        e.g. a plain `DINT[N]` tag -- no member chain at all). See
        `_parse_tag_element_path()` for the exact grammar and its NOT-YET-
        supported cases (multi-dimensional index, an index on a member
        rather than the top-level tag) -- both raise `ValueError` naming
        the gap rather than guessing.

        If the tag has no stored value at all yet (`initial_value IS NULL`
        -- e.g. a tag just created via `new_tag()`/`db_new_tag()` with no
        `value=`), the WHOLE value is zero-filled first (via the same
        `_zero_value_for_member()` Studio-consistent zero-fill this
        codebase already uses at export-render time for a stale/incomplete
        decoded value -- see CLAUDE.md's "Mutating a UDT with live tag
        instances" section), THEN the one leaf this call targets is set --
        so a caller never has to separately seed a default value before
        patching one field of it. The same zero-fill is applied to any
        individual intermediate member found MISSING while navigating an
        already-existing value (the identical "member added to a type with
        existing instances" gap that section documents), so a partially-
        populated value from an earlier `set_tag_element_value()` call, or
        a real decoded value that's one member short, is patched rather
        than rejected.

        Navigates into a member typed as a REAL PROJECT AOI the same way
        as a plain UDT (e.g. a UDT member typed as an AOI like
        `"VAB_MCC_IO"`) -- via the same synthetic instance-shape
        `DataType`/`_sync_data_types_map()` machinery `export_routine()`/
        `export_aoi()`/etc. already rely on for this (see CLAUDE.md's AOI
        instance-value sections). This is why this method pays for a full
        `to_controller()` rehydration rather than the cheaper
        `proj_data_types`-only map every OTHER read method in this class
        prefers (`get_datatype()`, `get_routine()`'s fast path, ...) --
        that synthetic-type machinery only exists on a real `Controller`
        object, and there's no cheaper way to get it. Acceptable here
        since, unlike `get_routine()`, this is a write typically called
        once (or a handful of times) per tag, not looped over hundreds of
        routines.

        Raises `KeyError` if `tag_name` doesn't exist in scope, or if a
        member named in `path` doesn't exist on the resolved type at that
        point in the chain. Raises `ValueError` for an out-of-range index,
        an index given for a non-array tag (or omitted for an array one),
        or a `path` this function doesn't support (see above).
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id, data_type_name, dimensions, initial_value FROM proj_tags "
            "WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, tag_name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {tag_name!r} in this scope")
        tag_id, data_type_name, dims, iv_json = row

        index, members = _parse_tag_element_path(path)

        dim = None
        if dims is not None:
            if "," in dims:
                raise ValueError(
                    f"set_tag_element_value() only supports a single-dimension array tag "
                    f"(tag {tag_name!r} has Dimensions={dims!r}, which is multi-dimensional)"
                )
            dim = int(dims)
            if index is None:
                raise ValueError(
                    f"Tag {tag_name!r} is an array (Dimensions={dims!r}) -- path {path!r} "
                    f"must start with a literal index, e.g. '[0].{members[0] if members else '...'}'"
                )
            if not (0 <= index < dim):
                raise ValueError(
                    f"Index {index} out of bounds for tag {tag_name!r} "
                    f"(Dimensions={dims!r}, valid range 0..{dim - 1})"
                )
        elif index is not None:
            raise ValueError(
                f"Tag {tag_name!r} is not an array (no Dimensions) -- path {path!r} must "
                f"not start with an index"
            )

        # Full data_types_map -- project UDTs AND real/DB-created AOI
        # synthetic instance-shape types (see docstring above for why a
        # full to_controller() rehydration is needed here, unlike every
        # other read method in this class).
        project = self.to_controller()
        _sync_data_types_map(project)
        data_types_map = project.controller._data_types_map

        if iv_json is None:
            root_member = Member(tag_name, tag_name, data_type_name, dim or 0, None, False,
                                  None, None, "Read/Write")
            value_tree = _zero_value_for_member(root_member, data_types_map)
        else:
            value_tree = json.loads(iv_json)

        container = value_tree
        if index is not None:
            if not isinstance(container, list) or not (0 <= index < len(container)):
                got = len(container) if isinstance(container, list) else "no"
                raise ValueError(
                    f"Tag {tag_name!r}'s current stored value doesn't have a real element "
                    f"at index {index} (has {got} elements) -- this shouldn't happen for a "
                    f"tag whose Dimensions matches its own stored value; investigate before "
                    f"trusting this edit."
                )
            if not members:
                container[index] = value
            else:
                container = container[index]

        current_type_name = data_type_name
        for i, member_name in enumerate(members):
            # Built-in Logix struct types (TIMER, COUNTER, CONTROL, ...) are
            # never project UDTs -- proj_data_types has no row for them at
            # all -- but every real project uses them constantly as UDT
            # members (StartFaultTimer, Delay_1..9, ...). Checked FIRST,
            # before the proj_data_types lookup, since it's a cheap dict
            # lookup and a project can never legally have a UDT literally
            # named "TIMER"/"COUNTER"/"CONTROL" (Studio rejects the name
            # collision). Found via a real report: navigating
            # "[1].StartFaultTimer.PRE" raised "Type 'TIMER' does not
            # resolve to a known DataType" -- this project DB genuinely had
            # no way to know TIMER's own member shape at all.
            builtin_members = _BUILTIN_STRUCT_MEMBERS.get(current_type_name.upper())
            if builtin_members is not None:
                real_name = next(
                    (bn for bn, _ in builtin_members if bn.upper() == member_name.upper()), None
                )
                if real_name is None:
                    raise KeyError(
                        f"No member named {member_name!r} on built-in type "
                        f"{current_type_name!r} (navigating path {path!r} for tag "
                        f"{tag_name!r})"
                    )
                m_dtype = dict(builtin_members)[real_name]
            else:
                # data_types_map covers a real project UDT, a real
                # (already-imported) AOI's own synthetic instance-shape
                # type, AND a db_new_aoi()-created (not-yet-real) AOI's
                # synthetic type (via _sync_data_types_map() above) --
                # uniformly, with no separate AOI-specific handling needed
                # here. Found via a real report: a UDT member typed as a
                # real project AOI (e.g. "VAB_MCC_IO") raised the exact
                # same "does not resolve" error TIMER/COUNTER/CONTROL used
                # to, because this used to query proj_data_types directly
                # instead of the richer map that already knows about AOIs.
                dt_obj = data_types_map.get(current_type_name.upper())
                if dt_obj is None:
                    raise ValueError(
                        f"Type {current_type_name!r} does not resolve to a known DataType, "
                        f"a project AOI, or a built-in Logix struct (TIMER/COUNTER/"
                        f"CONTROL) -- cannot navigate to member {member_name!r} of path "
                        f"{path!r} for tag {tag_name!r}"
                    )
                member_obj = next(
                    (m for m in dt_obj.members if m.name.upper() == member_name.upper()), None
                )
                if member_obj is None:
                    raise KeyError(
                        f"No member named {member_name!r} on type {current_type_name!r} "
                        f"(navigating path {path!r} for tag {tag_name!r})"
                    )
                real_name = member_obj.name
                m_dtype = member_obj.data_type
                m_dim = member_obj.dimension
                m_radix = member_obj.radix
                m_hidden = member_obj.hidden
                m_target = member_obj.target
                m_bit_number = member_obj.bit_number
                m_ext_access = member_obj.external_access
                m_desc = member_obj._description

            if not isinstance(container, dict):
                raise ValueError(
                    f"Cannot navigate to member {member_name!r} of path {path!r} for tag "
                    f"{tag_name!r} -- the value at this point is a {type(container).__name__}, "
                    f"not a struct"
                )
            if i == len(members) - 1:
                container[real_name] = value
            else:
                if builtin_members is not None:
                    # Every currently-known built-in struct member (TIMER/
                    # COUNTER/CONTROL's PRE/ACC/EN/...) is itself a plain
                    # DINT/BOOL, never a nested struct -- there's nothing to
                    # zero-fill/navigate further into.
                    raise ValueError(
                        f"Member {real_name!r} of built-in type {current_type_name!r} is "
                        f"a plain {m_dtype}, not a struct -- path {path!r} for tag "
                        f"{tag_name!r} cannot navigate any further through it"
                    )
                if real_name not in container:
                    m_wrapper = Member(real_name, real_name, m_dtype, m_dim, m_radix,
                                        bool(m_hidden), m_target, m_bit_number, m_ext_access,
                                        _description=m_desc)
                    container[real_name] = _zero_value_for_member(m_wrapper, data_types_map)
                container = container[real_name]
                current_type_name = m_dtype

        cur.execute("UPDATE proj_tags SET initial_value=? WHERE id=?",
                    (json.dumps(value_tree), tag_id))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def set_tag_comment(self, name: str, path: str, text: str,
                         program_name: Union[str, None] = None) -> None:
        """Set (replacing any existing) the comment at `path` on tag
        `name` -- `path=""` is the tag's own whole-tag description, same
        convention as `Tag._comments`: `path` is the FULL tag-qualified
        address, tag name included, e.g. `set_tag_comment("MyTag",
        "MyTag.Member[4].5", "...")`, not just `"Member[4].5"` -- matches
        exactly what `find_tag_references()`/`Tag._comments` themselves use.
        `text=""` clears the comment at that path (an empty-text entry is
        filtered out at export time by `_build_comments_xml`, so it never
        renders); it does not raise or store a visible empty comment.
        Raises `KeyError` if no tag named `name` exists in scope.
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_tags WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {name!r} in this scope")
        tag_id = row[0]
        cur.execute("DELETE FROM proj_tag_comments WHERE tag_id=? AND path=?", (tag_id, path))
        cur.execute("INSERT INTO proj_tag_comments (tag_id, path, text) VALUES (?, ?, ?)",
                    (tag_id, path, text))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def get_tag_comment(self, name: str, path: Union[str, None] = None,
                         program_name: Union[str, None] = None) -> Union[str, None]:
        """The comment at `path` on tag `name` -- the read counterpart to
        `set_tag_comment()` (same `path` convention: `path=None`/`""` is
        the tag's own whole-tag description; otherwise the FULL
        tag-qualified address, tag name included, e.g.
        `"HTV_BStatus_Status[0].2"`, not just `"[0].2"`).

        Added after a real report: there was no read path anywhere for a
        comment `db_set_tag_comment()` had a write path for -- a real
        project's per-bit comments (e.g. `HTV_BStatus_Status[0].2`
        commented `"Tray 1 Full"` in Studio) resolve real ambiguity in
        legacy ladder logic that rung text and tag names alone don't, and
        the only way to get them before this was asking a human to relay
        each one by hand from Studio.

        Returns the RAW stored text as-is (multi-line preserved, NOT
        collapsed to one line the way the convenience `Tag.description`
        Python property does for the whole-tag case -- see CLAUDE.md's
        "Whole-project L5X fidelity" section for why that collapsing exists
        and is deliberately NOT applied here). Returns `None` if no comment
        is stored at `path` -- this is a normal, common state (most
        addresses have no comment at all), not an error. Raises `KeyError`
        only if tag `name` itself doesn't exist in this scope.
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_tags WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {name!r} in this scope")
        tag_id = row[0]
        comment_row = cur.execute(
            "SELECT text FROM proj_tag_comments WHERE tag_id=? AND path=?",
            (tag_id, path or ""),
        ).fetchone()
        return comment_row[0] if comment_row is not None else None

    def list_tag_comments(self, name: str, program_name: Union[str, None] = None) -> Dict[str, str]:
        """Every comment stored on tag `name`, as `{path: text}` -- the
        bulk counterpart to `get_tag_comment()`, for reading every
        element/bit comment on one tag in a single call instead of one
        round trip per address (added for the exact case that motivated
        this: tracing a routine that references a few dozen distinct bits
        of a handful of tags, where fetching each comment individually
        would mean a few dozen separate `get_tag_comment()` calls).

        The whole-tag description, if set, is included under the key `""`
        (matching `get_tag_comment()`'s own `path=None`/`""` convention) --
        filter it out yourself (`{p: t for p, t in comments.items() if p}`)
        if you only want per-element comments. Returns `{}` if the tag has
        no comments at all (not an error). Raises `KeyError` if tag `name`
        doesn't exist in this scope.
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_tags WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {name!r} in this scope")
        tag_id = row[0]
        rows = cur.execute(
            "SELECT path, text FROM proj_tag_comments WHERE tag_id=? ORDER BY id", (tag_id,)
        ).fetchall()
        return {p: t for p, t in rows}

    def new_datatype(self, name: str, description: Union[str, None] = None) -> int:
        """Create a new, empty UDT in this project's DB -- the SQL
        equivalent of appending `new_datatype(...)`
        (`acd/l5x/elements/model.py`) to `project.controller.data_types`.
        Use `new_member()`/`db_new_member()` afterward to populate it, the
        same way you already would for an existing UDT.

        Raises `sqlite3.IntegrityError` if a DataType with this name
        already exists -- name uniqueness is GLOBAL/project-wide here,
        unlike tags/routines (which are scoped per program) -- see
        CLAUDE.md's "A real .ACD can contain two DataTypes with the same
        name".
        """
        dt = _new_datatype(name, description=description)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO proj_data_types (name, family, cls, description) VALUES (?, ?, ?, ?)",
            (dt.name, dt.family, dt.cls, dt._description),
        )
        dt_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return dt_id

    def _load_member_view(self, dt_id: int) -> List[Member]:
        """A lightweight, in-memory `Member` list for `dt_id` (name, data_type,
        hidden, target, bit_number only -- enough for `new_bit_member()`'s own
        allocation logic, not a full rehydration). Used by `new_member()`
        below to let the pure `new_bit_member()` allocator (which only knows
        how to work against real `DataType`/`Member` objects) see this UDT's
        already-existing members without going through a full
        `to_controller()` rehydration for one field lookup.
        """
        rows = self._conn.execute(
            "SELECT name, data_type_name, hidden, target, bit_number "
            "FROM proj_members WHERE data_type_id=? ORDER BY seq",
            (dt_id,),
        ).fetchall()
        return [
            Member(n, n, dtn, 0, "Decimal", bool(h), t, bn, "Read/Write")
            for (n, dtn, h, t, bn) in rows
        ]

    def new_member(self, data_type_name: str, name: str, member_data_type: str,
                    dimension: int = 0, radix: Union[str, None] = None,
                    description: Union[str, None] = None,
                    index: Union[int, None] = None) -> int:
        """Add a member to an existing UDT, at position `index` (default:
        appended at the end) -- the SQL equivalent of
        `dt.members.insert(i, new_member(...))`. Reuses `new_member()` for
        its radix-default/field-shape logic. Raises `KeyError` if
        `data_type_name` doesn't exist.

        For `member_data_type="BIT"`, allocates a real bit position the same
        way `new_bit_member()` does (see its own docstring) -- reusing a free
        bit in an existing hidden backing member if one has room, or
        inserting a brand-new hidden backing member first (appended at the
        current end of this UDT's member list) if none does. Fixes a real
        reported bug: this method used to hardcode `hidden=0, target=NULL,
        bit_number=NULL` in its own INSERT regardless of what kind of member
        was actually being added -- so even a caller that separately worked
        around `new_member()`'s old silent BIT/target=None gap got the
        allocation silently discarded again at THIS layer, with the same
        symptom (commits cleanly, no exception, only a real Studio 5000
        import rejects it on `Target`).
        """
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_data_types WHERE name=? COLLATE NOCASE", (data_type_name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No DataType named {data_type_name!r}")
        dt_id = row[0]

        if member_data_type.upper() == "BIT":
            existing_members = self._load_member_view(dt_id)
            # DataType(..., existing_members) does NOT copy the list -- dt_view.members
            # IS existing_members, so "len(dt_view.members) > len(existing_members)"
            # would always be False (comparing a list to itself). Capture the count as
            # a plain int BEFORE the call instead.
            count_before = len(existing_members)
            dt_view = DataType(data_type_name, data_type_name, "", "", existing_members)
            member = _new_bit_member(dt_view, name, description=description)
            new_backing = dt_view.members[-1] if len(dt_view.members) > count_before else None
            if new_backing is not None:
                backing_seq = cur.execute(
                    "SELECT COUNT(*) FROM proj_members WHERE data_type_id=?", (dt_id,)
                ).fetchone()[0]
                cur.execute(
                    "INSERT INTO proj_members (data_type_id, seq, name, data_type_name, "
                    "dimension, radix, hidden, target, bit_number, external_access, description) "
                    "VALUES (?, ?, ?, ?, 0, 'Decimal', 1, NULL, NULL, 'Read/Write', NULL)",
                    (dt_id, backing_seq, new_backing.name, new_backing.data_type),
                )
        else:
            member = _new_member(name, member_data_type, dimension=dimension,
                                  radix=radix, description=description)

        count = cur.execute(
            "SELECT COUNT(*) FROM proj_members WHERE data_type_id=?", (dt_id,)
        ).fetchone()[0]
        insert_at = count if index is None else index
        # Negative-intermediate shift -- same UNIQUE-collision reasoning as
        # insert_rung()/delete_rung() above, applied to (data_type_id, seq).
        cur.execute(
            "UPDATE proj_members SET seq = -(seq + 1) WHERE data_type_id=? AND seq >= ?",
            (dt_id, insert_at),
        )
        cur.execute(
            "UPDATE proj_members SET seq = -seq WHERE data_type_id=? AND seq < 0",
            (dt_id,),
        )
        cur.execute(
            "INSERT INTO proj_members (data_type_id, seq, name, data_type_name, dimension, "
            "radix, hidden, target, bit_number, external_access, description) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (dt_id, insert_at, member.name, member.data_type, member.dimension,
             member.radix, member.target, member.bit_number,
             member.external_access, member._description),
        )
        member_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return member_id

    def edit_member(self, data_type_name: str, name: str,
                     member_data_type: Union[str, None] = None,
                     dimension: Union[int, None] = None,
                     radix: Union[str, None] = None,
                     description: Union[str, None] = None) -> None:
        """Update an existing UDT member's fields in place -- only the
        fields actually passed (non-`None`) are changed, same "only what
        you pass" convention as `edit_tag()`/`edit_aoi_parameter()`. Added
        after a real report, the same shape as the earlier AOI-parameter
        gap: a member -- newly added via `new_member()`, or already present
        on a real, pre-existing UDT (the reporting case: a plain field on
        `Bin_Sequence` needing its description set to match a sibling
        field's, done by hand in Studio's own UDT editor as the only
        available fix) -- had no way to change its `description` (or any
        other field) through this API at all.

        Unlike `dimension` on `edit_aoi_parameter()`, `dimension=None` here
        has NO ambiguity with "explicitly set to scalar" -- `new_member()`'s
        own convention already treats `dimension=0` (not `None`) as the
        real scalar value, so `dimension=None` unambiguously means "leave
        the current dimension unchanged."

        For a normal (non-BIT, non-hidden-backing) member, reuses
        `new_member()`'s own radix-default derivation by re-running it
        against the MERGED (existing + overridden) field set -- one place
        the defaulting logic lives, same reasoning as `edit_aoi_parameter()`.
        A DELIBERATE exception to the general "only what you pass changes"
        rule: if `member_data_type` is changed and `radix` is NOT also
        explicitly passed, the CURRENT stored radix is discarded (not
        carried over) in favor of `new_member()`'s own fresh default for
        the NEW type -- e.g. changing `DINT` to `REAL` re-derives `"Float"`
        rather than keeping the stale `"Decimal"` a REAL member should
        never actually have. Pass `radix=` explicitly alongside
        `member_data_type=` if you need a specific non-default radix on
        the changed member.

        NOT supported, raising `ValueError` rather than guessing:
        - `member_data_type="BIT"` -- converting a member TO a BIT-overlay
          member needs a real backing-field allocation (reusing a free bit
          in an existing hidden backing member, or creating a new one) that
          this method has no way to perform; use `new_bit_member()`/
          `db_new_member(..., member_data_type="BIT")` instead.
        - Changing `member_data_type`/`dimension` on an member that's
          ALREADY a BIT-overlay member OR a hidden BIT-backing field (only
          its `description`/`radix` may be edited through this method) --
          delete and recreate it via `new_bit_member()`/`new_member()` if a
          genuinely different type/dimension allocation is needed.

        Raises `KeyError` if `data_type_name`/`name` doesn't resolve to an
        existing member.
        """
        cur = self._conn.cursor()
        dt_row = cur.execute(
            "SELECT id FROM proj_data_types WHERE name=? COLLATE NOCASE", (data_type_name,)
        ).fetchone()
        if dt_row is None:
            raise KeyError(f"No DataType named {data_type_name!r}")
        dt_id = dt_row[0]
        row = cur.execute(
            "SELECT id, data_type_name, dimension, radix, hidden, target, description "
            "FROM proj_members WHERE data_type_id=? AND name=? COLLATE NOCASE",
            (dt_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No member named {name!r} in DataType {data_type_name!r}")
        (member_id, cur_dtype, cur_dimension, cur_radix, cur_hidden, cur_target,
         cur_description) = row
        is_bit_related = bool(cur_hidden) or cur_target is not None

        if member_data_type is not None and member_data_type.upper() == "BIT":
            raise ValueError(
                "edit_member() cannot convert a member to a BIT-overlay member -- use "
                "new_bit_member()/db_new_member(..., member_data_type='BIT') instead, "
                "which allocates a real backing field."
            )
        if is_bit_related and (member_data_type is not None or dimension is not None):
            raise ValueError(
                f"Member {name!r} of {data_type_name!r} is a BIT-overlay member or a "
                f"hidden BIT-backing field -- only its description/radix may be edited "
                f"through edit_member(); delete and recreate it via "
                f"new_bit_member()/new_member() if a different type/dimension "
                f"allocation is genuinely needed."
            )

        effective_description = description if description is not None else cur_description
        # If the data_type is changing and no explicit radix was given,
        # DON'T carry over the old type's radix -- let _new_member() derive
        # a fresh default for the NEW type instead (see docstring above).
        if radix is not None:
            effective_radix = radix
        elif member_data_type is not None:
            effective_radix = None
        else:
            effective_radix = cur_radix

        if is_bit_related:
            # member_data_type is guaranteed None here (raised above
            # otherwise), so effective_radix already falls back to
            # cur_radix when radix wasn't explicitly passed.
            cur.execute(
                "UPDATE proj_members SET radix=?, description=? WHERE id=?",
                (effective_radix, effective_description, member_id),
            )
        else:
            effective_dtype = member_data_type if member_data_type is not None else cur_dtype
            effective_dimension = dimension if dimension is not None else cur_dimension
            member = _new_member(name, effective_dtype, dimension=effective_dimension,
                                  radix=effective_radix, description=effective_description)
            cur.execute(
                "UPDATE proj_members SET data_type_name=?, dimension=?, radix=?, "
                "description=? WHERE id=?",
                (member.data_type, member.dimension, member.radix, member._description,
                 member_id),
            )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def new_aoi(self, name: str, description: Union[str, None] = None) -> int:
        """Create a new, empty Add-On Instruction directly in this project's
        DB -- the SQL equivalent of appending `new_aoi(...)`
        (`acd/l5x/elements/model.py`) to `project.controller.aois`. Use
        `new_aoi_parameter()`/`db_new_aoi_parameter()` to add parameters and
        `new_routine(..., aoi_name=name)`/`db_new_routine(..., aoi_name=name)`
        to add its logic routine, the same way `new_member()` populates a
        UDT created via `new_datatype()`.

        v1 scope limit, see `new_aoi()`'s own docstring: this ONLY creates
        brand-new AOIs -- a real project's own pre-existing AOIs are never
        readable/editable through this table (`to_controller()` leaves them
        completely untouched, sourced fresh from the raw ACD data every
        call, and appends anything from here alongside them). Raises
        `sqlite3.IntegrityError` if an AOI with this name already exists
        in THIS table (does not check against a real project's own
        pre-existing AOI names, which this table has no visibility into).
        """
        aoi = _new_aoi(name, description=description)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO proj_aois (name, description, revision, revision_extension, vendor, "
            "execute_prescan, execute_postscan, execute_enable_in_false, created_date, "
            "created_by, edited_date, edited_by, software_revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aoi.name, aoi._description, aoi.revision, aoi.revision_extension, aoi.vendor,
             aoi.execute_prescan, aoi.execute_postscan, aoi.execute_enable_in_false,
             aoi.created_date, aoi.created_by, aoi.edited_date, aoi.edited_by,
             aoi.software_revision),
        )
        aoi_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return aoi_id

    def new_aoi_parameter(self, aoi_name: str, name: str, data_type: str,
                           usage: str = "Input", dimension: Union[int, None] = None,
                           description: Union[str, None] = None, index: Union[int, None] = None,
                           required: Union[str, None] = None, visible: Union[str, None] = None,
                           external_access: Union[str, None] = None,
                           ) -> int:
        """Add a public parameter to an AOI created via `new_aoi()`/
        `db_new_aoi()`, at position `index` (default: appended) -- the SQL
        equivalent of appending `new_aoi_parameter(...)`
        (`acd/l5x/elements/model.py`) to `AOI.parameters`. See that
        function's own docstring for `usage`/`radix`/`external_access`/
        `constant`/`required`/`visible` conventions, including the
        `required`/`visible`/`external_access` override args here (`None` =
        this constructor's own default; pass an explicit string to override
        -- e.g. to build a real `EnableIn`/`EnableOut` parameter by hand, or
        use `new_aoi_parameter()`/`new_aoi_enable_parameters()` directly and
        insert the two returned `Parameter` objects yourself if you need the
        ready-made pair without duplicating this call twice).

        Raises `KeyError` if `aoi_name` doesn't resolve to an AOI created in
        THIS project DB (see `new_aoi()`'s own v1 scope limit), `ValueError`
        if `usage` isn't `"Input"`/`"Output"`/`"InOut"`, if `dimension` is
        given with a `usage` other than `"InOut"` (Studio 5000 rejects an
        array `Input`/`Output` parameter outright), or if `data_type` isn't
        an elementary/atomic type with a `usage` other than `"InOut"`
        (Studio 5000 rejects a `STRING`/UDT/AOI-typed `Input`/`Output`
        parameter outright -- see `new_aoi_parameter()`'s own docstring for
        both), `sqlite3.IntegrityError` if a parameter with this name
        already exists on that AOI.
        """
        param = _new_aoi_parameter(name, data_type, usage=usage, dimension=dimension,
                                    description=description, required=required,
                                    visible=visible, external_access=external_access)
        aoi_id = self._aoi_id(aoi_name)
        cur = self._conn.cursor()
        count = cur.execute(
            "SELECT COUNT(*) FROM proj_aoi_parameters WHERE aoi_id=?", (aoi_id,)
        ).fetchone()[0]
        insert_at = count if index is None else index
        # Negative-intermediate shift -- same UNIQUE-collision reasoning as
        # new_member()'s own (data_type_id, seq) shift above.
        cur.execute(
            "UPDATE proj_aoi_parameters SET seq = -(seq + 1) WHERE aoi_id=? AND seq >= ?",
            (aoi_id, insert_at),
        )
        cur.execute(
            "UPDATE proj_aoi_parameters SET seq = -seq WHERE aoi_id=? AND seq < 0",
            (aoi_id,),
        )
        cur.execute(
            "INSERT INTO proj_aoi_parameters (aoi_id, seq, name, data_type_name, dimensions, "
            "radix, usage, required, visible, external_access, constant, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aoi_id, insert_at, param.name, param.data_type, param.dimensions, param.radix,
             param.usage, param.required, param.visible, param.external_access,
             param.constant, param._description),
        )
        param_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return param_id

    def new_aoi_local_tag(self, aoi_name: str, name: str, data_type: str,
                           dimension: Union[int, None] = None,
                           description: Union[str, None] = None,
                           index: Union[int, None] = None) -> int:
        """Add a private/scratch LocalTag to an AOI created via `new_aoi()`/
        `db_new_aoi()`, at position `index` (default: appended) -- the SQL
        equivalent of appending `new_aoi_local_tag(...)`
        (`acd/l5x/elements/model.py`) to `AOI.local_tags`. Unlike a
        `Parameter`, a LocalTag has no `Usage`/`Required`/`Visible` concept
        -- it's never a public Input/Output/InOut pin, just internal state
        for the AOI's own logic.

        Raises `KeyError` if `aoi_name` doesn't resolve to an AOI created in
        THIS project DB (see `new_aoi()`'s own v1 scope limit),
        `sqlite3.IntegrityError` if a local tag with this name already
        exists on that AOI.
        """
        local_tag = _new_aoi_local_tag(name, data_type, dimension=dimension,
                                        description=description)
        aoi_id = self._aoi_id(aoi_name)
        cur = self._conn.cursor()
        count = cur.execute(
            "SELECT COUNT(*) FROM proj_aoi_local_tags WHERE aoi_id=?", (aoi_id,)
        ).fetchone()[0]
        insert_at = count if index is None else index
        # Negative-intermediate shift -- same UNIQUE-collision reasoning as
        # new_member()'s own (data_type_id, seq) shift above.
        cur.execute(
            "UPDATE proj_aoi_local_tags SET seq = -(seq + 1) WHERE aoi_id=? AND seq >= ?",
            (aoi_id, insert_at),
        )
        cur.execute(
            "UPDATE proj_aoi_local_tags SET seq = -seq WHERE aoi_id=? AND seq < 0",
            (aoi_id,),
        )
        cur.execute(
            "INSERT INTO proj_aoi_local_tags (aoi_id, seq, name, data_type_name, dimensions, "
            "radix, external_access, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (aoi_id, insert_at, local_tag.name, local_tag.data_type, local_tag.dimensions,
             local_tag.radix, local_tag.external_access, local_tag._description),
        )
        local_tag_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return local_tag_id

    def edit_aoi_parameter(self, aoi_name: str, name: str,
                            data_type: Union[str, None] = None,
                            usage: Union[str, None] = None,
                            dimension: Union[int, None] = None,
                            description: Union[str, None] = None,
                            required: Union[str, None] = None,
                            visible: Union[str, None] = None,
                            external_access: Union[str, None] = None) -> None:
        """Update an existing AOI parameter's fields in place -- only the
        fields actually passed (non-`None`) are changed, same "only what
        you pass" convention as `edit_tag()`. Added after a real report: a
        parameter added with the wrong shape previously had no fix short of
        Studio's own AOI editor after import (see `delete_aoi_parameter()`
        below for the sibling gap this closes).

        Re-runs `new_aoi_parameter()`'s own validation/default-derivation
        logic against the MERGED (existing + overridden) field set -- an
        edit can't be used to sneak a parameter into a shape Studio 5000
        would reject that creating one directly never could (an array on
        `usage="Input"`/`"Output"`, or a `STRING`/UDT/AOI `data_type` with
        that same usage -- see `new_aoi_parameter()`'s own docstring for
        both real Studio import rejections these guard against).

        CAVEAT, same shape as `edit_tag()`'s own documented one:
        `dimension=None` means "leave the current dimension unchanged," NOT
        "clear it back to scalar" -- there's no way to explicitly un-array a
        parameter through this method (delete and recreate it instead if
        you genuinely need that). The same applies to `required`/`visible`/
        `external_access`: passing `None` keeps the CURRENT stored value,
        not the usage-derived default `new_aoi_parameter()` would compute
        for a brand-new parameter.

        Raises `KeyError` if `aoi_name`/`name` doesn't resolve to an
        existing AOI parameter created in THIS project DB (same v1 scope
        limit as `new_aoi_parameter()`), or `ValueError` if the merged
        field set violates a constraint `new_aoi_parameter()` itself would
        also reject.
        """
        aoi_id = self._aoi_id(aoi_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id, data_type_name, dimensions, usage, required, visible, "
            "external_access, description FROM proj_aoi_parameters "
            "WHERE aoi_id=? AND name=? COLLATE NOCASE",
            (aoi_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No parameter named {name!r} on AOI {aoi_name!r}")
        (param_id, cur_dtype, cur_dims, cur_usage, cur_required, cur_visible,
         cur_ext_access, cur_description) = row

        effective_dtype = data_type if data_type is not None else cur_dtype
        effective_usage = usage if usage is not None else cur_usage
        effective_dimension = (
            dimension if dimension is not None
            else (int(cur_dims) if cur_dims is not None else None)
        )
        effective_description = description if description is not None else cur_description
        effective_required = required if required is not None else cur_required
        effective_visible = visible if visible is not None else cur_visible
        effective_external_access = (
            external_access if external_access is not None else cur_ext_access
        )

        param = _new_aoi_parameter(
            name, effective_dtype, usage=effective_usage, dimension=effective_dimension,
            description=effective_description, required=effective_required,
            visible=effective_visible, external_access=effective_external_access,
        )
        cur.execute(
            "UPDATE proj_aoi_parameters SET data_type_name=?, dimensions=?, radix=?, usage=?, "
            "required=?, visible=?, external_access=?, constant=?, description=? WHERE id=?",
            (param.data_type, param.dimensions, param.radix, param.usage, param.required,
             param.visible, param.external_access, param.constant, param._description,
             param_id),
        )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def new_routine(self, routine_name: str, routine_type: str,
                     program_name: Union[str, None] = None,
                     description: Union[str, None] = None,
                     aoi_name: Union[str, None] = None) -> int:
        """Create a new, empty routine ("RLL" or "ST") in an existing
        program OR AOI -- the SQL equivalent of appending `new_routine(...)`
        (`acd/l5x/elements/model.py`) to `Program.routines`/`AOI.routines`.
        Use `insert_rung()` (RLL) or `insert_st_line()` (ST) afterward to
        populate it -- both raise a clear error if used against the wrong
        routine type, so no separate guard is needed here.

        EXACTLY ONE of `program_name`/`aoi_name` must be given (unlike
        `new_tag()`, where `program_name=None` means controller scope, a
        routine has no scope-less default -- it always belongs to exactly
        one Program or one AOI). `aoi_name` must resolve to an AOI created
        via `new_aoi()`/`db_new_aoi()` in THIS project DB (v1 scope limit,
        same as `new_aoi_parameter()`). Raises `ValueError` if both or
        neither are given, or if `routine_type` isn't `"RLL"`/`"ST"`,
        `KeyError` if the given scope name doesn't resolve,
        `sqlite3.IntegrityError` if a routine with this name already exists
        in that scope.

        When `aoi_name` is given, `routine_name` must be one of
        `"Logic"`/`"Prescan"`/`"Postscan"`/`"EnableInFalse"` -- unlike a
        Program's routine, an AOI's own routine name is a fixed, Rockwell-
        reserved set, confirmed via a real Studio 5000 import rejection
        (`"Invalid name for Add-On Instruction routine."`) of a routine
        named after the AOI itself (a natural, but wrong, first guess for
        avoiding a name collision with every other AOI's own `"Logic"`
        routine -- see `_routine_id()`'s `aoi_name=` support below for the
        actual way to disambiguate that without renaming anything).
        """
        if (program_name is None) == (aoi_name is None):
            raise ValueError(
                "new_routine(): exactly one of program_name/aoi_name is required -- a "
                "routine always belongs to exactly one Program or one AOI, never both "
                "and never neither."
            )
        if aoi_name is not None and routine_name not in _AOI_RESERVED_ROUTINE_NAMES:
            raise ValueError(
                f"new_routine(): {routine_name!r} is not a valid AOI routine name -- must "
                f"be one of {sorted(_AOI_RESERVED_ROUTINE_NAMES)}. Use aoi_name= on "
                f"insert_rung()/insert_st_line()/get_routine()/export_routine()/etc. to "
                f"address this routine unambiguously without needing a distinctive name."
            )
        routine = _new_routine(routine_name, routine_type, description=description)
        cur = self._conn.cursor()
        if program_name is not None:
            program_id = self._program_id(program_name)
            cur.execute(
                "INSERT INTO proj_routines (program_id, aoi_id, name, type, description) "
                "VALUES (?, NULL, ?, ?, ?)",
                (program_id, routine.name, routine.type, routine._description),
            )
        else:
            aoi_id = self._aoi_id(aoi_name)
            cur.execute(
                "INSERT INTO proj_routines (program_id, aoi_id, name, type, description) "
                "VALUES (NULL, ?, ?, ?, ?)",
                (aoi_id, routine.name, routine.type, routine._description),
            )
        routine_id = cur.lastrowid
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()
        return routine_id

    def insert_rung(self, routine_name: str, index: int, text: str,
                     comment: Union[str, None] = None,
                     program_name: Union[str, None] = None,
                     aoi_name: Union[str, None] = None) -> None:
        """SQL equivalent of `Routine.insert_rung()` -- shifts every rung
        at or after `index` up by one, same shape as the in-memory
        version's list-splice. The shift goes through a temporary negative
        `rung_index` in between (two UPDATEs, not one) -- SQLite doesn't
        guarantee the row processing order of a single UPDATE, and shifting
        directly (index 5 -> 6) can momentarily collide with an existing
        row still at 6 before ITS OWN shift runs, tripping the
        `(routine_id, rung_index)` UNIQUE index even though the end state
        is perfectly valid; negative values can never collide with a real
        (always >= 0) rung_index, so the intermediate state is always safe.

        `text` is checked with `_validate_rll_rung_syntax()` (unbalanced
        brackets, a one-member `"[...]"` branch group) before being
        inserted -- raises `ValueError` instead of silently accepting rung
        text that would only be caught later by a real Studio 5000 import
        (see its own docstring for the real failure this catches).

        Raises `ValueError` if the routine's own type isn't `"RLL"` -- an
        ST routine's real source lives in `proj_st_lines` (see
        `insert_st_line()`), not `proj_rungs`; a prior version of this
        method silently accepted the call anyway, writing into a slot
        `export_routine()` never reads for an ST routine (found via a real
        report: no exception, transaction commits clean, `get_routine()`
        afterward shows the new text sitting in `"rungs"` while the
        routine's real `"st_lines"` -- what actually gets exported/
        imported -- stayed untouched, looking exactly like a successful
        edit that silently never took effect).
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "RLL":
            raise ValueError(
                f"insert_rung() only applies to an RLL routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an ST routine's "
                f"source lives in proj_st_lines, not proj_rungs; use "
                f"insert_st_line() instead."
            )
        _validate_rll_rung_syntax(text)
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE proj_rungs SET rung_index = -(rung_index + 1) "
            "WHERE routine_id=? AND rung_index >= ?",
            (routine_id, index),
        )
        cur.execute(
            "UPDATE proj_rungs SET rung_index = -rung_index "
            "WHERE routine_id=? AND rung_index < 0",
            (routine_id,),
        )
        cur.execute(
            "INSERT INTO proj_rungs (routine_id, rung_index, text, comment, source_object_id) "
            "VALUES (?, ?, ?, ?, NULL)",
            (routine_id, index, text, comment),
        )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_rung(self, routine_name: str, index: int,
                     program_name: Union[str, None] = None,
                     aoi_name: Union[str, None] = None) -> None:
        """SQL equivalent of `Routine.delete_rung()` -- same negative-
        intermediate shift technique as `insert_rung()`, for the same
        reason (shifting down can just as easily collide mid-UPDATE).

        Raises `ValueError` if the routine's own type isn't `"RLL"` -- see
        `insert_rung()`'s docstring for why this guard exists.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "RLL":
            raise ValueError(
                f"delete_rung() only applies to an RLL routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an ST routine's "
                f"source lives in proj_st_lines, not proj_rungs; use "
                f"delete_st_line() instead."
            )
        cur = self._conn.cursor()
        cur.execute("DELETE FROM proj_rungs WHERE routine_id=? AND rung_index=?", (routine_id, index))
        cur.execute(
            "UPDATE proj_rungs SET rung_index = -(rung_index - 1) "
            "WHERE routine_id=? AND rung_index > ?",
            (routine_id, index),
        )
        cur.execute(
            "UPDATE proj_rungs SET rung_index = -rung_index "
            "WHERE routine_id=? AND rung_index < 0",
            (routine_id,),
        )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def replace_rung_safe(self, routine_name: str, index: int, expected_old: str,
                           new_text: str, program_name: Union[str, None] = None,
                           aoi_name: Union[str, None] = None) -> None:
        """SQL equivalent of `replace_rung_safe()` -- optimistic-concurrency
        guard: raises `ValueError` (showing expected vs. actual) if the
        rung at `index` no longer matches `expected_old`.

        For an RLL routine, `new_text` is also checked with
        `_validate_rll_rung_syntax()` (unbalanced brackets, a one-member
        `"[...]"` branch group) before being applied -- runs AFTER the
        expected-text match check, so a mismatch is always reported as a
        mismatch, never masked by a syntax error in `new_text`. See its
        own docstring for the real failure this catches: this guard only
        protects against editing the WRONG rung, not against writing
        syntactically-malformed rung text.

        Raises `ValueError` if the routine's own type isn't `"RLL"` -- see
        `insert_rung()`'s docstring for why this guard exists; use
        `replace_st_line_safe()` instead for an ST routine.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "RLL":
            raise ValueError(
                f"replace_rung_safe() only applies to an RLL routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an ST routine's "
                f"source lives in proj_st_lines, not proj_rungs; use "
                f"replace_st_line_safe() instead."
            )
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT text FROM proj_rungs WHERE routine_id=? AND rung_index=?", (routine_id, index)
        ).fetchone()
        if row is None:
            raise KeyError(f"No rung at index {index} in this routine")
        if row[0] != expected_old:
            raise ValueError(
                f"Rung {index} has changed since last read.\n"
                f"Expected: {expected_old!r}\nActual:   {row[0]!r}"
            )
        _validate_rll_rung_syntax(new_text)
        cur.execute("UPDATE proj_rungs SET text=? WHERE routine_id=? AND rung_index=?",
                    (new_text, routine_id, index))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def set_rung_comment(self, routine_name: str, index: int, comment: Union[str, None],
                          program_name: Union[str, None] = None,
                          aoi_name: Union[str, None] = None) -> None:
        """Set or clear a rung's comment WITHOUT touching its text --
        `comment=None` clears it. Added because the only prior way to
        rename a rung's comment was `delete_rung()` + `insert_rung()` with
        the same text retyped by hand, which also throws away
        `replace_rung_safe()`'s optimistic-concurrency guard. Raises
        `KeyError` if no rung exists at `index` in this routine.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_rungs WHERE routine_id=? AND rung_index=?", (routine_id, index)
        ).fetchone()
        if row is None:
            raise KeyError(f"No rung at index {index} in this routine")
        cur.execute("UPDATE proj_rungs SET comment=? WHERE routine_id=? AND rung_index=?",
                    (comment, routine_id, index))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def insert_st_line(self, routine_name: str, index: int, text: str,
                        program_name: Union[str, None] = None,
                        aoi_name: Union[str, None] = None) -> None:
        """The ST counterpart to `insert_rung()` -- shifts every line at or
        after `index` down by one, via the same negative-intermediate-value
        technique (`(routine_id, line_index)` has the same `UNIQUE`-
        collision-during-shift risk as rungs' `(routine_id, rung_index)`).
        No syntax check is applied (unlike `insert_rung()`'s
        `_validate_rll_rung_syntax()`) -- ST syntax validation doesn't
        exist yet.

        Raises `ValueError` if the routine's own type isn't `"ST"` -- an
        RLL routine's real source lives in `proj_rungs` (see
        `insert_rung()`), not `proj_st_lines`.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "ST":
            raise ValueError(
                f"insert_st_line() only applies to an ST routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an RLL routine's "
                f"source lives in proj_rungs, not proj_st_lines; use "
                f"insert_rung() instead."
            )
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE proj_st_lines SET line_index = -(line_index + 1) "
            "WHERE routine_id=? AND line_index >= ?",
            (routine_id, index),
        )
        cur.execute(
            "UPDATE proj_st_lines SET line_index = -line_index "
            "WHERE routine_id=? AND line_index < 0",
            (routine_id,),
        )
        cur.execute(
            "INSERT INTO proj_st_lines (routine_id, line_index, text) VALUES (?, ?, ?)",
            (routine_id, index, text),
        )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_st_line(self, routine_name: str, index: int,
                        program_name: Union[str, None] = None,
                        aoi_name: Union[str, None] = None) -> None:
        """The ST counterpart to `delete_rung()` -- same negative-
        intermediate shift technique as `insert_st_line()`.

        Raises `ValueError` if the routine's own type isn't `"ST"` -- see
        `insert_st_line()`'s docstring for why this guard exists.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "ST":
            raise ValueError(
                f"delete_st_line() only applies to an ST routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an RLL routine's "
                f"source lives in proj_rungs, not proj_st_lines; use "
                f"delete_rung() instead."
            )
        cur = self._conn.cursor()
        cur.execute("DELETE FROM proj_st_lines WHERE routine_id=? AND line_index=?",
                    (routine_id, index))
        cur.execute(
            "UPDATE proj_st_lines SET line_index = -(line_index - 1) "
            "WHERE routine_id=? AND line_index > ?",
            (routine_id, index),
        )
        cur.execute(
            "UPDATE proj_st_lines SET line_index = -line_index "
            "WHERE routine_id=? AND line_index < 0",
            (routine_id,),
        )
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def replace_st_line_safe(self, routine_name: str, index: int, expected_old: str,
                              new_text: str, program_name: Union[str, None] = None,
                              aoi_name: Union[str, None] = None) -> None:
        """SQL equivalent of `replace_st_line_safe()` -- the ST counterpart
        to `replace_rung_safe()`, same optimistic-concurrency guard: raises
        `ValueError` (showing expected vs. actual) if the line at `index`
        no longer matches `expected_old`.

        Raises `ValueError` if the routine's own type isn't `"ST"` -- use
        `replace_rung_safe()` instead for an RLL routine.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        routine_type = self._routine_type(routine_id)
        if routine_type != "ST":
            raise ValueError(
                f"replace_st_line_safe() only applies to an ST routine (routine "
                f"{routine_name!r} has type {routine_type!r}) -- an RLL routine's "
                f"source lives in proj_rungs, not proj_st_lines; use "
                f"replace_rung_safe() instead."
            )
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT text FROM proj_st_lines WHERE routine_id=? AND line_index=?",
            (routine_id, index),
        ).fetchone()
        if row is None:
            raise KeyError(f"No line at index {index} in this routine")
        if row[0] != expected_old:
            raise ValueError(
                f"Line {index} has changed since last read.\n"
                f"Expected: {expected_old!r}\nActual:   {row[0]!r}"
            )
        cur.execute("UPDATE proj_st_lines SET text=? WHERE routine_id=? AND line_index=?",
                    (new_text, routine_id, index))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_tag(self, name: str, program_name: Union[str, None] = None) -> None:
        """Remove a tag from this project's DB -- e.g. cleaning up dead
        code after a redesign moved its logic elsewhere.

        Does NOT delete anything in the real `.ACD`/Studio project --
        Studio's native "Import Routine"/"Import Data Type..." mechanism
        has no delete semantics (it can only add/update entities present
        in the partial L5X, never remove ones that simply aren't
        mentioned), so removing something from the real project still
        needs a manual Studio action regardless of what this library does.
        This only prevents an abandoned tag from cluttering
        `db_list_tags()`/`db_get_project_summary()` forever with nothing
        distinguishing "still relevant" from "dead, forgot to clean up."

        Raises `KeyError` if no tag named `name` exists in scope.
        """
        program_id = self._program_id(program_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_tags WHERE program_id=? AND name=? COLLATE NOCASE",
            (program_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No tag named {name!r} in this scope")
        tag_id = row[0]
        cur.execute("DELETE FROM proj_tag_comments WHERE tag_id=?", (tag_id,))
        cur.execute("DELETE FROM proj_tags WHERE id=?", (tag_id,))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_routine(self, routine_name: str, program_name: Union[str, None] = None,
                        aoi_name: Union[str, None] = None) -> None:
        """Remove a routine (and all its rungs/ST lines) from this
        project's DB. Same real-`.ACD` caveat as `delete_tag()` -- this
        only cleans up the persistent DB's own bookkeeping, not the real
        Studio project. Raises `KeyError`/`ValueError` the same way
        `get_routine()` does for a missing/ambiguous name.
        """
        routine_id = self._routine_id(routine_name, program_name, aoi_name)
        cur = self._conn.cursor()
        cur.execute("DELETE FROM proj_rungs WHERE routine_id=?", (routine_id,))
        cur.execute("DELETE FROM proj_st_lines WHERE routine_id=?", (routine_id,))
        cur.execute("DELETE FROM proj_routines WHERE id=?", (routine_id,))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_member(self, data_type_name: str, member_name: str) -> None:
        """Remove a member from an existing UDT. Same real-`.ACD` caveat
        as `delete_tag()`. Raises `KeyError` if the DataType or the member
        doesn't exist.
        """
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_data_types WHERE name=? COLLATE NOCASE", (data_type_name,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No DataType named {data_type_name!r}")
        dt_id = row[0]
        row = cur.execute(
            "SELECT id FROM proj_members WHERE data_type_id=? AND name=? COLLATE NOCASE",
            (dt_id, member_name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No member named {member_name!r} in DataType {data_type_name!r}")
        cur.execute("DELETE FROM proj_members WHERE id=?", (row[0],))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    def delete_aoi_parameter(self, aoi_name: str, name: str) -> None:
        """Remove a parameter from an AOI created via `new_aoi()`/
        `db_new_aoi()` in THIS project DB. Same real-`.ACD` caveat as
        `delete_tag()` -- this only cleans up this project DB's own
        bookkeeping, not a real Studio project (there's no "un-import" for
        an AOI parameter Studio has already accepted). Pairs with
        `edit_aoi_parameter()` above for the "added with the wrong shape,
        no fix short of Studio's own AOI editor" gap this closes -- a
        parameter that should never have been added at all can now just be
        deleted and recreated correctly, rather than left as permanent
        clutter.

        Raises `KeyError` if `aoi_name`/`name` doesn't resolve to an
        existing AOI parameter created in THIS project DB (same v1 scope
        limit as `new_aoi_parameter()`).
        """
        aoi_id = self._aoi_id(aoi_name)
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id FROM proj_aoi_parameters WHERE aoi_id=? AND name=? COLLATE NOCASE",
            (aoi_id, name),
        ).fetchone()
        if row is None:
            raise KeyError(f"No parameter named {name!r} on AOI {aoi_name!r}")
        cur.execute("DELETE FROM proj_aoi_parameters WHERE id=?", (row[0],))
        cur.execute("UPDATE proj_meta SET dirty=1")
        if not self._in_transaction:
            self._conn.commit()

    # ---- rehydration ----

    def _load_data_types(self, data_types_map: Dict[str, DataType]) -> List[DataType]:
        cur = self._conn.cursor()
        dt_rows = cur.execute(
            "SELECT id, name, family, cls, description FROM proj_data_types ORDER BY id"
        ).fetchall()
        data_types: List[DataType] = []
        for (dt_id, name, family, cls, description) in dt_rows:
            member_rows = cur.execute(
                "SELECT name, data_type_name, dimension, radix, hidden, target, "
                "bit_number, external_access, description FROM proj_members "
                "WHERE data_type_id=? ORDER BY seq",
                (dt_id,),
            ).fetchall()
            members = [
                Member(mname, mname, mdtype, dim, radix, bool(hidden), target,
                       bit_number, ext_access, _description=mdesc)
                for (mname, mdtype, dim, radix, hidden, target, bit_number, ext_access, mdesc)
                in member_rows
            ]
            dt = DataType(name, name, family, cls, members, _description=description)
            data_types.append(dt)
            data_types_map[name.upper()] = dt
        return data_types

    def _load_tags(self, program_id: int, data_types_map: Dict[str, DataType]) -> List[Tag]:
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT id, name, tag_type, data_type_name, radix, external_access, "
            "constant, dimensions, target, initial_value FROM proj_tags WHERE program_id=? "
            "ORDER BY id",
            (program_id,),
        ).fetchall()
        tags: List[Tag] = []
        for (tag_id, name, tag_type, dtype, radix, ext_access, constant, dims, target, iv_json) in rows:
            comments = cur.execute(
                "SELECT path, text FROM proj_tag_comments WHERE tag_id=?", (tag_id,)
            ).fetchall()
            tag = Tag(
                name, name, tag_type, dtype, radix, ext_access, constant, dims,
                target=target,
                _comments=[(p, t) for (p, t) in comments],
                _initial_value=json.loads(iv_json) if iv_json is not None else None,
            )
            tag._data_types_map = data_types_map
            tags.append(tag)
        return tags

    def _load_routines_where(self, cur, owner_column: str, owner_id: int) -> List[Routine]:
        """Shared routine+rung+ST-line loader for both `program_id`- and
        `aoi_id`-owned routines -- `owner_column` is always one of those two
        hardcoded literals from this module's own code, never external
        input, so the f-string column name here is safe.
        """
        rows = cur.execute(
            f"SELECT id, name, type, description FROM proj_routines WHERE {owner_column}=? "
            "ORDER BY id",
            (owner_id,),
        ).fetchall()
        routines: List[Routine] = []
        for (rid, name, rtype, description) in rows:
            rung_rows = cur.execute(
                "SELECT text, comment, source_object_id FROM proj_rungs WHERE routine_id=? "
                "ORDER BY rung_index",
                (rid,),
            ).fetchall()
            st_rows = cur.execute(
                "SELECT text FROM proj_st_lines WHERE routine_id=? ORDER BY line_index", (rid,)
            ).fetchall()
            routine = Routine(
                name, name, rtype,
                [r[0] for r in rung_rows],
                _rung_ids=[r[2] for r in rung_rows],
                _rung_comments={i: r[1] for i, r in enumerate(rung_rows) if r[1] is not None},
                _description=description,
                _st_lines=[r[0] for r in st_rows],
            )
            routines.append(routine)
        return routines

    def _load_routines(self, program_id: int) -> List[Routine]:
        return self._load_routines_where(self._conn.cursor(), "program_id", program_id)

    def _load_aois(self) -> List[AOI]:
        """Every AOI created via `new_aoi()`/`db_new_aoi()` in THIS project
        DB, fully populated (parameters + logic routine, if any) -- NEVER a
        real project's own pre-existing AOIs (v1 scope limit, see
        `new_aoi()`'s own docstring). `to_controller()` merges this list
        onto the real, freshly-decoded `.aois`, with a name collision
        resolved in favor of the AOI loaded HERE (see `to_controller()`'s
        own comment for why: once a `db_new_aoi()`-authored AOI is actually
        imported into Studio and re-saved, the real ACD gains an AOI under
        the same name too, and the one loaded here is always the more
        current of the two for anyone still editing that name through
        `db_*`).
        """
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT id, name, description, revision, revision_extension, vendor, "
            "execute_prescan, execute_postscan, execute_enable_in_false, "
            "created_date, created_by, edited_date, edited_by, software_revision "
            "FROM proj_aois ORDER BY id"
        ).fetchall()
        aois: List[AOI] = []
        for (aid, name, description, revision, rev_ext, vendor, exec_prescan, exec_postscan,
             exec_enable_in_false, created_date, created_by, edited_date, edited_by,
             sw_rev) in rows:
            param_rows = cur.execute(
                "SELECT name, data_type_name, dimensions, radix, usage, required, visible, "
                "external_access, constant, description FROM proj_aoi_parameters "
                "WHERE aoi_id=? ORDER BY seq",
                (aid,),
            ).fetchall()
            parameters = [
                Parameter(pname, pname, "Base", dtype, usage, radix, required, visible,
                          ext_access, constant, dims, _description=pdesc)
                for (pname, dtype, dims, radix, usage, required, visible, ext_access, constant,
                     pdesc) in param_rows
            ]
            lt_rows = cur.execute(
                "SELECT name, data_type_name, dimensions, radix, external_access, description "
                "FROM proj_aoi_local_tags WHERE aoi_id=? ORDER BY seq",
                (aid,),
            ).fetchall()
            local_tags = [
                LocalTag(ltname, ltname, dtype, dims, radix, ext_access, _description=ltdesc)
                for (ltname, dtype, dims, radix, ext_access, ltdesc) in lt_rows
            ]
            routines = self._load_routines_where(cur, "aoi_id", aid)
            aois.append(AOI(
                name, name, revision, rev_ext, vendor, exec_prescan, exec_postscan,
                exec_enable_in_false, created_date, created_by, edited_date, edited_by,
                sw_rev, parameters, local_tags, routines, _description=description,
            ))
        return aois

    def to_controller(self) -> RSLogix5000Content:
        """Rehydrate a full, fresh `Controller`/`RSLogix5000Content` object
        graph from this DB. `.tags`/`.data_types`/`.programs` (and their
        routines/rungs) reflect the CURRENT state of the normalized
        `proj_*` tables, including any edits made through this `ProjectDB`
        -- cheap (plain SELECTs into dataclasses, no binary decoding).
        `.modules`/`.aois`/`.tasks` and every Controller-level scalar field
        are re-derived fresh from the raw tables via the same
        `ControllerBuilder` a plain `load_acd()` call uses (not edited
        through this class in v1 -- see this module's own docstring) --
        this does real, non-trivial decode work each call (though never
        re-parses/re-unzips the source `.ACD` itself), a known v1
        cost/simplicity tradeoff, not a bug.

        Call this ONCE per export and get both the returned project and
        the routine/data_type you're about to pass to
        `export_routine()`/`export_datatype()` from the SAME call --
        `export_routine()` locates a routine's owning `Program` by
        identity-scanning `project.controller.programs[*].routines`, so
        objects from two different `to_controller()` calls won't resolve
        (the `export_routine()`/`export_datatype()` convenience methods
        below already do this correctly).
        """
        controller: Controller = ControllerBuilder(self._conn.cursor()).build()

        data_types_map: Dict[str, DataType] = dict(controller._data_types_map)
        controller.data_types = self._load_data_types(data_types_map)
        controller._data_types_map = data_types_map

        prog_rows = self._conn.execute(
            "SELECT id, name, main_routine_name, fault_routine_name, disabled, "
            "test_edits, description FROM proj_programs WHERE id != ? ORDER BY id",
            (_CONTROLLER_SCOPE,),
        ).fetchall()
        programs: List[Program] = []
        for (pid, pname, main_rn, fault_rn, disabled, test_edits, description) in prog_rows:
            programs.append(Program(
                pname, pname, test_edits, main_rn, fault_rn, disabled, None, "false",
                self._load_tags(pid, data_types_map), self._load_routines(pid),
                _description=description,
            ))
        controller.programs = programs
        controller.tags = self._load_tags(_CONTROLLER_SCOPE, data_types_map)
        controller.__post_init__()  # recompute io_tags/alias_tags from the new .tags

        # AOIs are handled differently from every other collection here:
        # APPENDED, never fully replaced -- see _load_aois()'s own docstring
        # for why (a real project's own pre-existing AOIs, including
        # LocalTags this DB never persists, must never be touched or
        # dropped). BUT a real AOI here CAN share a name with a proj_aois
        # one: once a db_new_aoi()-authored AOI is actually imported into
        # Studio and the project re-saved, the next rebuild's fresh
        # ControllerBuilder decode picks up that AOI for real, while the
        # user keeps editing the SAME name through db_new_aoi_parameter()/
        # db_new_aoi_local_tag() against the still-separate proj_aois row --
        # producing two same-named AOI objects in this one list. Any
        # name-keyed lookup downstream (this class's own export_aoi(), or a
        # caller's own next(a for a in aois if a.name == ...)) then risks
        # silently resolving to whichever happens to come first, which used
        # to be the real one -- stale relative to every edit made since that
        # import (a real report: Parameters/LocalTags came back from a
        # 3-recreate-cycles-ago version while db_get_routine()'s routine
        # content, sourced independently via proj_routines.aoi_id, was
        # already correctly fresh). The proj_aois-sourced object is always
        # the more current one for a name the user is actively authoring
        # through db_*, so it wins any collision -- exclude the
        # ControllerBuilder-decoded AOI(s) sharing a name with one loaded
        # here before appending.
        new_aois = self._load_aois()
        new_aoi_names = {a.name.upper() for a in new_aois}
        controller.aois = [
            a for a in controller.aois if a.name.upper() not in new_aoi_names
        ] + new_aois

        project = ProjectBuilder(str(self.project_dir / "QuickInfo.XML")).build()
        project.controller = controller
        return project

    # ---- export bridge ----

    def export_routine(self, routine_name: str, output_path, program_name: Union[str, None] = None,
                        aoi_name: Union[str, None] = None,
                        owner: Union[str, None] = None, validate: bool = True) -> None:
        """`to_controller()` + `get_routine()` + the existing, unmodified
        `export_routine()` in one call.

        `validate` defaults to `True` here (opposite of the underlying
        `export_routine()`'s own `validate=False` default) -- both checks
        it runs (declared-type-vs-rendered-value resolution, and for an
        RLL routine, `_validate_rll_rung_syntax()` on every rung) are cheap
        relative to a full edit -> export -> Studio-import round trip, and
        the failure modes they prevent are silent/late, not loud errors up
        front, which is the wrong kind of thing to leave opt-in. Found via
        real reports: two separate rounds of live-Studio-rejection
        debugging traced back to the type-resolution gap, and a third
        traced back to malformed rung text (a one-branch `"[...]"` group
        left over after an edit) that neither this function's old
        `validate=True` nor `replace_rung_safe()`'s own guard caught before
        Studio's own import parser did. Pass `validate=False` explicitly to
        skip both checks if you're confident they're unnecessary.

        `aoi_name` is accepted for signature symmetry with the other routine
        methods (`insert_rung()`, `get_routine()`, ...), but ALWAYS raises
        `ValueError` if given -- Studio 5000 has no "Import Routine"
        mechanism for a routine living inside an AddOnInstructionDefinition
        (unlike a Program's routine); the whole-AOI `export_aoi()` (via
        Studio's separate "Import Add-On Instruction..." feature) is the
        only export path for an AOI's own routine, including its content.
        Found the hard way: passing `aoi_name` through to the underlying,
        Program-only `export_routine()` (acd.api) surfaced as a confusing
        "routine not found in any program of this project" instead of
        pointing at the actual fix.
        """
        if aoi_name is not None:
            raise ValueError(
                "export_routine() cannot export an AOI-owned routine -- Studio 5000 has "
                "no 'Import Routine' mechanism for a routine inside an "
                "AddOnInstructionDefinition. Use export_aoi()/db_export_aoi() instead, "
                "which exports the whole AOI (including this routine's content) via "
                "Studio's 'Import Add-On Instruction...' feature."
            )
        project = self.to_controller()
        routine = _get_routine(project, routine_name, program_name)
        _export_routine(project, routine, output_path, owner=owner, validate=validate)

    def export_datatype(self, data_type_name: str, output_path,
                         owner: Union[str, None] = None, validate: bool = True) -> None:
        """`to_controller()` + DataType lookup + the existing, unmodified
        `export_datatype()` in one call. `validate` defaults to `True` here
        for the same reason as `export_routine()` above -- see its
        docstring.
        """
        project = self.to_controller()
        dt = next(
            (d for d in project.controller.data_types if d.name.upper() == data_type_name.upper()),
            None,
        )
        if dt is None:
            raise KeyError(f"No DataType named {data_type_name!r}")
        _export_datatype(project, dt, output_path, owner=owner, validate=validate)

    def export_aoi(self, aoi_name: str, output_path,
                    owner: Union[str, None] = None, validate: bool = True) -> None:
        """`to_controller()` + AOI lookup + the existing, unmodified
        `export_aoi()` in one call. `validate` defaults to `True` here for
        the same reason as `export_routine()`/`export_datatype()` above --
        see either's own docstring. `aoi_name` must resolve to an AOI
        created via `new_aoi()`/`db_new_aoi()` in THIS project DB, OR a real
        project's own pre-existing AOI name (unlike routine/tag lookups, an
        AOI export doesn't need `new_aoi()` first -- `to_controller()`
        already includes every real AOI, untouched, alongside anything
        created here; see `_load_aois()`'s own docstring).

        See `acd.api.export_aoi()`'s own docstring for the CAUTION about
        this wrapper shape being unverified against a real Studio 5000
        import.
        """
        project = self.to_controller()
        aoi = next(
            (a for a in (project.controller.aois or []) if a.name.upper() == aoi_name.upper()),
            None,
        )
        if aoi is None:
            raise KeyError(f"No AOI named {aoi_name!r}")
        _export_aoi(project, aoi, output_path, owner=owner, validate=validate)

    def export_program(self, program_name: str, output_path,
                        owner: Union[str, None] = None, validate: bool = True) -> None:
        """`to_controller()` + Program lookup + the existing, unmodified
        `acd.api.export_program()` in one call. `validate` defaults to
        `True` here for the same reason as `export_routine()`/
        `export_datatype()`/`export_aoi()` above.

        See `acd.api.export_program()`'s own docstring for the wrapper
        shape (calibrated against a real Studio 5000 "Export Program"
        output) and its one known, deliberate gap (`<ChildPrograms>` /
        Program-folder nesting is not detected or emitted -- a real
        mechanism disproven by real data when first hypothesized, not
        guessed at further).
        """
        project = self.to_controller()
        program = next(
            (p for p in project.controller.programs if p.name.upper() == program_name.upper()),
            None,
        )
        if program is None:
            raise KeyError(f"No program named {program_name!r}")
        _export_program(project, program, output_path, owner=owner, validate=validate)

    # ---- read-only convenience (thin wrappers over acd.api, against a fresh rehydration) ----

    def list_tags(self, program_name: Union[str, None] = None) -> List[dict]:
        return _list_tags(self.to_controller(), program_name)

    def list_routines(self, program_name: Union[str, None] = None) -> List[dict]:
        """Name/type/line-count for every routine, WITHOUT rung/line
        content -- same shape as `acd.api.list_routines()`. Still sourced
        via a full `to_controller()` rehydration (NOT the SQL-direct
        shortcut `get_routine()` now uses -- see its own docstring for why):
        `proj_routines` never contains a REAL, pre-existing AOI's own
        routines at all (`_materialize()` only ever walks `ctrl.programs`,
        never `ctrl.aois` -- deliberate, see `_load_aois()`'s own docstring
        on why `proj_aois` never holds a real AOI either), so a SQL-only
        listing here would silently omit every real AOI's routine from the
        result -- a real, dangerous gap for exactly the "find every routine
        project-wide" use case this function exists for. Unlike
        `get_routine()` (one name, cheap to fall back to a full decode only
        when genuinely needed), there's no equivalently cheap way to list
        *some* routines fast and merge in the rest, so this method keeps
        paying the one-time full-decode cost on every call -- acceptable
        since a caller normally calls this ONCE to see what exists, not in
        a loop (loop `get_routine()`, not this, if you must; better yet see
        `db_find_tag_references()` for "who references X" instead of
        looping either one, per `acd.__init__`'s own guidance).
        """
        return _list_routines(self.to_controller(), program_name)

    def get_datatype(self, name: str) -> dict:
        """The current shape of one UDT -- name/family/cls/description plus
        every member (name/data_type/dimension/radix/hidden/target/
        bit_number/external_access/description), in declaration order.
        Returns a plain dict, not a `DataType` object, matching
        `get_routine()`'s own "self-contained, no rehydration needed by the
        caller" convention.

        Sourced DIRECTLY from `proj_data_types`/`proj_members` via SQL, NOT
        via `to_controller()` -- unlike an AOI (see `get_aoi()`),
        `proj_data_types` always holds EVERY real DataType in the project
        (`_materialize()` walks `ctrl.data_types` in full, no v1 scope
        limit the way `proj_aois` has), so there's no real-vs-DB-only split
        to fall back for here. Added after a real report: inspecting a
        single UDT's members (to disambiguate near-identical sibling array
        types, or just check one member's shape) previously required a full
        `to_controller()` rehydration -- every tag's value, every routine,
        every AOI -- decoded just to answer "what members does this ONE
        type have," repeated 10+ times in one real session.

        Raises `KeyError` if no DataType named `name` exists.
        """
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id, name, family, cls, description FROM proj_data_types "
            "WHERE name=? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No DataType named {name!r}")
        dt_id, real_name, family, cls, description = row
        member_rows = cur.execute(
            "SELECT name, data_type_name, dimension, radix, hidden, target, bit_number, "
            "external_access, description FROM proj_members WHERE data_type_id=? ORDER BY seq",
            (dt_id,),
        ).fetchall()
        return {
            "name": real_name,
            "family": family,
            "cls": cls,
            "description": description,
            "members": [
                {
                    "name": mname,
                    "data_type": mdtype,
                    "dimension": dim,
                    "radix": radix,
                    "hidden": bool(hidden),
                    "target": target,
                    "bit_number": bit_number,
                    "external_access": ext_access,
                    "description": mdesc,
                }
                for (mname, mdtype, dim, radix, hidden, target, bit_number, ext_access, mdesc)
                in member_rows
            ],
        }

    def list_datatypes(self) -> List[dict]:
        """Name/family/cls/description/member_count for EVERY DataType in
        the project, WITHOUT each one's own member list -- the
        `get_datatype()` counterpart to `list_tags()`/`list_routines()`, for
        a quick "does this exist, what's its shape at a glance" pass (e.g.
        disambiguating several similarly-named UDTs) before fetching one in
        full. SQL-direct, same reasoning as `get_datatype()` -- no
        `to_controller()` rehydration needed.
        """
        rows = self._conn.execute(
            "SELECT dt.name, dt.family, dt.cls, dt.description, COUNT(m.id) "
            "FROM proj_data_types dt LEFT JOIN proj_members m ON m.data_type_id = dt.id "
            "GROUP BY dt.id ORDER BY dt.name COLLATE NOCASE"
        ).fetchall()
        return [
            {"name": name, "family": family, "cls": cls, "description": description,
             "member_count": count}
            for (name, family, cls, description, count) in rows
        ]

    def get_aoi(self, name: str) -> dict:
        """The current shape of one AOI -- name/description/revision plus
        every parameter (name/data_type/dimensions/usage/radix/required/
        visible/external_access/description) and every local tag, in
        declaration order. Returns a plain dict, not an `AOI` object, same
        convention as `get_datatype()`/`get_routine()`.

        Tries `proj_aois` first (an AOI created via `new_aoi()`/
        `db_new_aoi()` in THIS project DB) -- cheap, SQL-direct, no
        rehydration. FALLS BACK to a full `to_controller()` rehydration ONLY
        when not found there, which covers a REAL, pre-existing project
        AOI (never stored in `proj_aois` at all, see `_load_aois()`'s own
        docstring for why) -- the same fast-path/fallback shape
        `get_routine()` already uses for the equivalent Program-routine vs.
        AOI-routine split.

        Raises `KeyError` if no AOI named `name` exists either way.
        """
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT id, name, description, revision FROM proj_aois WHERE name=? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if row is None:
            project = self.to_controller()
            aoi = next(
                (a for a in (project.controller.aois or []) if a.name.upper() == name.upper()),
                None,
            )
            if aoi is None:
                raise KeyError(f"No AOI named {name!r}")
            return {
                "name": aoi.name,
                "description": aoi._description,
                "revision": aoi.revision,
                "parameters": [
                    {
                        "name": p.name, "data_type": p.data_type, "dimensions": p.dimensions,
                        "usage": p.usage, "radix": p.radix, "required": p.required,
                        "visible": p.visible, "external_access": p.external_access,
                        "description": p._description,
                    }
                    for p in aoi.parameters
                ],
                "local_tags": [
                    {
                        "name": lt.name, "data_type": lt.data_type, "dimensions": lt.dimensions,
                        "radix": lt.radix, "external_access": lt.external_access,
                        "description": lt._description,
                    }
                    for lt in aoi.local_tags
                ],
            }
        aoi_id, real_name, description, revision = row
        param_rows = cur.execute(
            "SELECT name, data_type_name, dimensions, radix, usage, required, visible, "
            "external_access, constant, description FROM proj_aoi_parameters "
            "WHERE aoi_id=? ORDER BY seq",
            (aoi_id,),
        ).fetchall()
        lt_rows = cur.execute(
            "SELECT name, data_type_name, dimensions, radix, external_access, description "
            "FROM proj_aoi_local_tags WHERE aoi_id=? ORDER BY seq",
            (aoi_id,),
        ).fetchall()
        return {
            "name": real_name,
            "description": description,
            "revision": revision,
            "parameters": [
                {
                    "name": pname, "data_type": dtype, "dimensions": dims, "usage": usage,
                    "radix": radix, "required": required, "visible": visible,
                    "external_access": ext_access, "description": pdesc,
                }
                for (pname, dtype, dims, radix, usage, required, visible, ext_access, _constant,
                     pdesc) in param_rows
            ],
            "local_tags": [
                {
                    "name": ltname, "data_type": dtype, "dimensions": dims, "radix": radix,
                    "external_access": ext_access, "description": ltdesc,
                }
                for (ltname, dtype, dims, radix, ext_access, ltdesc) in lt_rows
            ],
        }

    def list_aois(self) -> List[dict]:
        """Name/description/revision/parameter_count for EVERY AOI in the
        project (both real, pre-existing ones AND any created via
        `new_aoi()`/`db_new_aoi()` in this project DB) -- the `get_aoi()`
        counterpart to `list_datatypes()`.

        Unlike `list_datatypes()`, this DOES pay for a full
        `to_controller()` rehydration -- same reasoning as `list_routines()`
        (see its own docstring): `proj_aois` never holds a real project's
        own pre-existing AOIs (v1 scope limit), so a SQL-only listing here
        would silently omit every real AOI, which is worse for a "what AOIs
        exist" call than the one-time decode cost. Prefer `get_aoi()`
        directly (or `db_get_project_summary()`'s own AOI name list) over
        looping this.
        """
        project = self.to_controller()
        return [
            {
                "name": a.name,
                "description": a._description,
                "revision": a.revision,
                "parameter_count": len(a.parameters),
            }
            for a in (project.controller.aois or [])
        ]

    def tag_exists(self, name: str, program_name: Union[str, None] = None) -> bool:
        return _tag_exists(self.to_controller(), name, program_name)

    def get_project_summary(self) -> dict:
        return _get_project_summary(self.to_controller())

    def get_routine(self, routine_name: str, program_name: Union[str, None] = None,
                     aoi_name: Union[str, None] = None) -> dict:
        """The current content of one routine -- name/type/description,
        RLL rungs + their comments, or ST lines. Returns a plain dict
        (not a `Routine` object) to keep this surface self-contained --
        pass the rung text you get back to `replace_rung_safe()` if you're
        about to edit it. Raises `KeyError`/`ValueError` the same way
        `_routine_id()` does for a missing/ambiguous name.

        `"rung_comments"` is `Dict[int, str]` -- keyed by the INTEGER rung
        index (same index space as `"rungs"`), not a stringified index and
        not JSON-style string keys. A rung with no comment has no entry at
        all (use `.get(i)`, which returns `None`, rather than assuming
        every index 0..len(rungs) is present). `comments.get(str(i))` will
        silently return `None` for every rung -- no exception, just quietly
        wrong/empty data.

        Pass `aoi_name=` (not `program_name=`) to read an AOI-owned logic
        routine -- exactly one of `program_name`/`aoi_name` may be given.
        `program_name` starting with `"AOI:"` (e.g. `"AOI:MyAOI"`, matching
        `list_routines()`'s own `"program"` field for an AOI-owned routine)
        is treated as shorthand for `aoi_name="MyAOI"` -- see `_routine_id()`
        for why, so a `list_routines()` entry can be fed straight in here.

        Sourced DIRECTLY from `proj_routines`/`proj_rungs`/`proj_st_lines`
        via SQL, NOT via `to_controller()`, for the common case (a Program's
        routine, or an AOI created via `new_aoi()`/`db_new_aoi()` in THIS
        project DB) -- a real report found looping this call (or the
        stateless `db_get_routine()`) over every routine in a ~180-routine
        project (a normal "find every reference to X" scan, exactly the
        pattern `db_find_tag_references()`/`find_tag_references()` already
        do in ONE call, see their own docstrings -- prefer those over
        hand-looping `get_routine()` for that specific use case) took 10+
        minutes of real CPU time with the stateless wrapper, and still
        multiple minutes with a single `open_project_db()` connection reused
        across the loop. Root cause: this method used to build a FULL
        project rehydration (`to_controller()` -- every tag's initial value
        decoded, every UDT/Module/AOI/Task rebuilt via `ControllerBuilder`)
        just to answer "what's in this one routine," discarding everything
        else it just built. Fixed by reading only the `proj_routines`/
        `proj_rungs`/`proj_st_lines` rows this routine actually needs -- no
        Controller rehydration at all for the common case, so a
        full-project routine-by-routine scan (looping this method, NOT the
        stateless wrapper -- see below) is a cheap SQL query per routine,
        not a full decode per routine.

        FALLS BACK to a full `to_controller()` rehydration (the old, slow
        behavior) ONLY when the fast path finds no match at all -- this
        covers a REAL, pre-existing AOI's own routine, which `proj_routines`
        never contains (`_materialize()` only ever walks a project's
        Programs, never its AOIs -- deliberate, see `_load_aois()`'s own
        docstring). Rare enough in practice (one routine, not 180) that
        paying the slow path once here is fine; `list_routines()` still
        always does this (see its own docstring for why a partial/merged
        fast path isn't safe there the way it is here).
        """
        try:
            routine_id = self._routine_id(routine_name, program_name, aoi_name)
        except KeyError:
            scope = f"AOI:{aoi_name}" if aoi_name is not None else program_name
            routine = _get_routine(self.to_controller(), routine_name, scope)
            return {
                "name": routine.name,
                "type": routine.type,
                "description": routine._description,
                "rungs": list(routine.rungs),
                "rung_comments": dict(routine._rung_comments),
                "st_lines": list(routine._st_lines),
            }
        cur = self._conn.cursor()
        rtype, description = cur.execute(
            "SELECT type, description FROM proj_routines WHERE id=?", (routine_id,)
        ).fetchone()
        rung_rows = cur.execute(
            "SELECT text, comment FROM proj_rungs WHERE routine_id=? ORDER BY rung_index",
            (routine_id,),
        ).fetchall()
        st_rows = cur.execute(
            "SELECT text FROM proj_st_lines WHERE routine_id=? ORDER BY line_index",
            (routine_id,),
        ).fetchall()
        return {
            "name": routine_name,
            "type": rtype,
            "description": description,
            "rungs": [r[0] for r in rung_rows],
            "rung_comments": {i: r[1] for i, r in enumerate(rung_rows) if r[1] is not None},
            "st_lines": [r[0] for r in st_rows],
        }

    def get_tag_value(self, tag_name: str, program_name: Union[str, None] = None,
                       offset: int = 0, limit: int = 50) -> dict:
        return _get_tag_value(self.to_controller(), tag_name, program_name, offset, limit)

    def find_tag_references(self, name: str, regex: bool = False,
                             include_text: bool = True) -> List[tuple]:
        return _find_tag_references(self.to_controller(), name, regex, include_text)

    def io_addresses_by_routine(self) -> dict:
        return _io_addresses_by_routine(self.to_controller())


# ---- stateless db_* functions -- the recommended surface for a single edit ----
#
# Each of these opens the project DB, does exactly one thing, and closes
# again before returning -- see this module's own top-level docstring for
# why that (not caller discipline) is what actually prevents a rebuild from
# racing a still-open connection. Prefer these over open_project_db()/
# ProjectDB directly unless a script is doing many edits in one session and
# wants to hold the lock across all of them rather than pay one
# acquire/release cycle per edit.

def _run(acd_path, project_dir, verbose, fn):
    db = open_project_db(acd_path, project_dir=project_dir, verbose=verbose)
    try:
        return fn(db)
    finally:
        db.close()


@contextlib.contextmanager
def db_transaction(acd_path, project_dir=None, verbose: bool = False):
    """Context manager: batch several edits into ONE atomic unit, using the
    yielded `ProjectDB`'s own methods -- commits everything together on a
    clean exit, rolls back everything together if an exception propagates
    out of the block. See `ProjectDB.transaction()`'s own docstring for the
    full rationale and the "don't call db_* functions inside this block"
    warning (each of those opens a separate connection and would deadlock
    against this one's still-held lock).

        with db_transaction(acd_path) as db:
            db.new_member(dt_name, "Foo", "DINT")
            db.new_tag("Tag1", "DINT")
            db.insert_rung(routine_name, 0, "...")
        # all three committed together, or none of them did

    Use this whenever a script needs several edits to succeed or fail as
    one unit -- without it, each db_* call commits independently the
    instant it returns, so a script that raises partway through a
    multi-step edit leaves everything up to that point durably sitting in
    the DB, with nothing marking it as an incomplete attempt (a real
    behavioral difference from the old in-memory workflow, where a crashed
    script left zero durable side effects for free).
    """
    db = open_project_db(acd_path, project_dir=project_dir, verbose=verbose)
    try:
        with db.transaction():
            yield db
    finally:
        db.close()


def db_new_tag(acd_path, name: str, data_type: str, program_name: Union[str, None] = None,
                dimensions: Union[str, None] = None, description: Union[str, None] = None,
                value=None, external_access: str = "Read/Write",
                project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_tag()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_tag(
        name, data_type, program_name=program_name, dimensions=dimensions,
        description=description, value=value, external_access=external_access,
    ))


def db_edit_tag(acd_path, name: str, program_name: Union[str, None] = None,
                 description: Union[str, None] = None, value=None,
                 project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.edit_tag()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.edit_tag(
        name, program_name=program_name, description=description, value=value,
    ))


def db_set_tag_comment(acd_path, name: str, path: str, text: str,
                        program_name: Union[str, None] = None,
                        project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.set_tag_comment()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.set_tag_comment(
        name, path, text, program_name=program_name,
    ))


def db_get_tag_comment(acd_path, name: str, path: Union[str, None] = None,
                        program_name: Union[str, None] = None,
                        project_dir=None, verbose: bool = False) -> Union[str, None]:
    """Stateless equivalent of `ProjectDB.get_tag_comment()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose,
                lambda db: db.get_tag_comment(name, path, program_name=program_name))


def db_list_tag_comments(acd_path, name: str, program_name: Union[str, None] = None,
                          project_dir=None, verbose: bool = False) -> Dict[str, str]:
    """Stateless equivalent of `ProjectDB.list_tag_comments()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose,
                lambda db: db.list_tag_comments(name, program_name=program_name))


def db_set_tag_element_value(acd_path, tag_name: str, path: str, value,
                              program_name: Union[str, None] = None,
                              project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.set_tag_element_value()` -- see
    its docstring for the `path` grammar and the zero-fill-on-first-use
    behavior."""
    _run(acd_path, project_dir, verbose, lambda db: db.set_tag_element_value(
        tag_name, path, value, program_name=program_name,
    ))


def db_new_datatype(acd_path, name: str, description: Union[str, None] = None,
                     project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_datatype()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_datatype(
        name, description=description,
    ))


def db_new_member(acd_path, data_type_name: str, name: str, member_data_type: str,
                   dimension: int = 0, radix: Union[str, None] = None,
                   description: Union[str, None] = None, index: Union[int, None] = None,
                   project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_member()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_member(
        data_type_name, name, member_data_type, dimension=dimension, radix=radix,
        description=description, index=index,
    ))


def db_edit_member(acd_path, data_type_name: str, name: str,
                    member_data_type: Union[str, None] = None,
                    dimension: Union[int, None] = None,
                    radix: Union[str, None] = None,
                    description: Union[str, None] = None,
                    project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.edit_member()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.edit_member(
        data_type_name, name, member_data_type=member_data_type, dimension=dimension,
        radix=radix, description=description,
    ))


def db_new_aoi(acd_path, name: str, description: Union[str, None] = None,
               project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_aoi()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_aoi(
        name, description=description,
    ))


def db_new_aoi_parameter(acd_path, aoi_name: str, name: str, data_type: str,
                          usage: str = "Input", dimension: Union[int, None] = None,
                          description: Union[str, None] = None, index: Union[int, None] = None,
                          required: Union[str, None] = None, visible: Union[str, None] = None,
                          external_access: Union[str, None] = None,
                          project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_aoi_parameter()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_aoi_parameter(
        aoi_name, name, data_type, usage=usage, dimension=dimension,
        description=description, index=index, required=required, visible=visible,
        external_access=external_access,
    ))


def db_edit_aoi_parameter(acd_path, aoi_name: str, name: str,
                           data_type: Union[str, None] = None,
                           usage: Union[str, None] = None,
                           dimension: Union[int, None] = None,
                           description: Union[str, None] = None,
                           required: Union[str, None] = None, visible: Union[str, None] = None,
                           external_access: Union[str, None] = None,
                           project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.edit_aoi_parameter()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.edit_aoi_parameter(
        aoi_name, name, data_type=data_type, usage=usage, dimension=dimension,
        description=description, required=required, visible=visible,
        external_access=external_access,
    ))


def db_delete_aoi_parameter(acd_path, aoi_name: str, name: str,
                             project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_aoi_parameter()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.delete_aoi_parameter(aoi_name, name))


def db_new_aoi_local_tag(acd_path, aoi_name: str, name: str, data_type: str,
                          dimension: Union[int, None] = None,
                          description: Union[str, None] = None, index: Union[int, None] = None,
                          project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_aoi_local_tag()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_aoi_local_tag(
        aoi_name, name, data_type, dimension=dimension, description=description, index=index,
    ))


def db_new_routine(acd_path, routine_name: str, routine_type: str,
                    program_name: Union[str, None] = None,
                    description: Union[str, None] = None,
                    aoi_name: Union[str, None] = None,
                    project_dir=None, verbose: bool = False) -> int:
    """Stateless equivalent of `ProjectDB.new_routine()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.new_routine(
        routine_name, routine_type, program_name=program_name,
        description=description, aoi_name=aoi_name,
    ))


def db_insert_rung(acd_path, routine_name: str, index: int, text: str,
                    comment: Union[str, None] = None, program_name: Union[str, None] = None,
                    aoi_name: Union[str, None] = None,
                    project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.insert_rung()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.insert_rung(
        routine_name, index, text, comment=comment, program_name=program_name,
        aoi_name=aoi_name,
    ))


def db_delete_rung(acd_path, routine_name: str, index: int,
                    program_name: Union[str, None] = None,
                    aoi_name: Union[str, None] = None,
                    project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_rung()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.delete_rung(
        routine_name, index, program_name=program_name, aoi_name=aoi_name,
    ))


def db_replace_rung_safe(acd_path, routine_name: str, index: int, expected_old: str,
                          new_text: str, program_name: Union[str, None] = None,
                          aoi_name: Union[str, None] = None,
                          project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.replace_rung_safe()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.replace_rung_safe(
        routine_name, index, expected_old, new_text, program_name=program_name,
        aoi_name=aoi_name,
    ))


def db_set_rung_comment(acd_path, routine_name: str, index: int,
                         comment: Union[str, None], program_name: Union[str, None] = None,
                         aoi_name: Union[str, None] = None,
                         project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.set_rung_comment()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.set_rung_comment(
        routine_name, index, comment, program_name=program_name, aoi_name=aoi_name,
    ))


def db_insert_st_line(acd_path, routine_name: str, index: int, text: str,
                       program_name: Union[str, None] = None,
                       aoi_name: Union[str, None] = None,
                       project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.insert_st_line()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.insert_st_line(
        routine_name, index, text, program_name=program_name, aoi_name=aoi_name,
    ))


def db_delete_st_line(acd_path, routine_name: str, index: int,
                       program_name: Union[str, None] = None,
                       aoi_name: Union[str, None] = None,
                       project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_st_line()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.delete_st_line(
        routine_name, index, program_name=program_name, aoi_name=aoi_name,
    ))


def db_replace_st_line_safe(acd_path, routine_name: str, index: int, expected_old: str,
                             new_text: str, program_name: Union[str, None] = None,
                             aoi_name: Union[str, None] = None,
                             project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.replace_st_line_safe()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.replace_st_line_safe(
        routine_name, index, expected_old, new_text, program_name=program_name,
        aoi_name=aoi_name,
    ))


def db_delete_tag(acd_path, name: str, program_name: Union[str, None] = None,
                   project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_tag()` -- see its docstring."""
    _run(acd_path, project_dir, verbose, lambda db: db.delete_tag(name, program_name))


def db_delete_routine(acd_path, routine_name: str, program_name: Union[str, None] = None,
                       aoi_name: Union[str, None] = None,
                       project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_routine()` -- see its docstring."""
    _run(acd_path, project_dir, verbose,
         lambda db: db.delete_routine(routine_name, program_name, aoi_name))


def db_delete_member(acd_path, data_type_name: str, member_name: str,
                      project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.delete_member()` -- see its docstring."""
    _run(acd_path, project_dir, verbose,
         lambda db: db.delete_member(data_type_name, member_name))


def db_export_routine(acd_path, routine_name: str, output_path,
                       program_name: Union[str, None] = None,
                       aoi_name: Union[str, None] = None, owner: Union[str, None] = None,
                       validate: bool = True, project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.export_routine()` -- see its docstring
    (including why `validate` defaults to `True` here)."""
    _run(acd_path, project_dir, verbose, lambda db: db.export_routine(
        routine_name, output_path, program_name=program_name, aoi_name=aoi_name,
        owner=owner, validate=validate,
    ))


def db_export_datatype(acd_path, data_type_name: str, output_path,
                        owner: Union[str, None] = None, validate: bool = True,
                        project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.export_datatype()` -- see its docstring
    (including why `validate` defaults to `True` here)."""
    _run(acd_path, project_dir, verbose, lambda db: db.export_datatype(
        data_type_name, output_path, owner=owner, validate=validate,
    ))


def db_export_aoi(acd_path, aoi_name: str, output_path,
                   owner: Union[str, None] = None, validate: bool = True,
                   project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.export_aoi()` -- see its docstring
    (including the CAUTION about this wrapper being unverified against a
    real Studio 5000 import)."""
    _run(acd_path, project_dir, verbose, lambda db: db.export_aoi(
        aoi_name, output_path, owner=owner, validate=validate,
    ))


def db_export_program(acd_path, program_name: str, output_path,
                       owner: Union[str, None] = None, validate: bool = True,
                       project_dir=None, verbose: bool = False) -> None:
    """Stateless equivalent of `ProjectDB.export_program()` -- see its
    docstring (including the known `<ChildPrograms>` gap and the NOT YET
    VERIFIED AGAINST A REAL STUDIO 5000 IMPORT caveat in
    `acd.api.export_program()`)."""
    _run(acd_path, project_dir, verbose, lambda db: db.export_program(
        program_name, output_path, owner=owner, validate=validate,
    ))


def db_list_tags(acd_path, program_name: Union[str, None] = None, project_dir=None,
                  verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_tags()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_tags(program_name))


def db_list_routines(acd_path, program_name: Union[str, None] = None, project_dir=None,
                      verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_routines()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_routines(program_name))


def db_get_datatype(acd_path, name: str, project_dir=None, verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.get_datatype()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.get_datatype(name))


def db_list_datatypes(acd_path, project_dir=None, verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_datatypes()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_datatypes())


def db_get_aoi(acd_path, name: str, project_dir=None, verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.get_aoi()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.get_aoi(name))


def db_list_aois(acd_path, project_dir=None, verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_aois()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_aois())


def db_tag_exists(acd_path, name: str, program_name: Union[str, None] = None,
                   project_dir=None, verbose: bool = False) -> bool:
    """Stateless equivalent of `ProjectDB.tag_exists()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.tag_exists(name, program_name))


def db_get_project_summary(acd_path, project_dir=None, verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.get_project_summary()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.get_project_summary())


def db_to_controller(acd_path, project_dir=None, verbose: bool = False) -> RSLogix5000Content:
    """Stateless equivalent of `ProjectDB.to_controller()` -- see its docstring. For
    anything needing more than one read from the same rehydration in a
    single call (e.g. locating a routine AND its program), use
    `open_project_db()` directly instead of calling this repeatedly."""
    return _run(acd_path, project_dir, verbose, lambda db: db.to_controller())


def db_get_routine(acd_path, routine_name: str, program_name: Union[str, None] = None,
                    aoi_name: Union[str, None] = None,
                    project_dir=None, verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.get_routine()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose,
                lambda db: db.get_routine(routine_name, program_name, aoi_name))


def db_get_tag_value(acd_path, tag_name: str, program_name: Union[str, None] = None,
                      offset: int = 0, limit: int = 50, project_dir=None,
                      verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.get_tag_value()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose,
                lambda db: db.get_tag_value(tag_name, program_name, offset, limit))


def db_find_tag_references(acd_path, name: str, regex: bool = False, include_text: bool = True,
                            project_dir=None, verbose: bool = False) -> List[tuple]:
    """Stateless equivalent of `ProjectDB.find_tag_references()` -- see its
    docstring, and `acd.api.find_tag_references()`'s own docstring for the
    `include_text=False` shape (returns 3-tuples instead of 4-tuples)."""
    return _run(acd_path, project_dir, verbose,
                lambda db: db.find_tag_references(name, regex, include_text))


def db_io_addresses_by_routine(acd_path, project_dir=None, verbose: bool = False) -> dict:
    """Stateless equivalent of `ProjectDB.io_addresses_by_routine()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.io_addresses_by_routine())


# ---- two-project comparisons -----------------------------------------------
#
# These don't fit as ProjectDB instance methods (an instance is scoped to one
# project) -- each side is opened/rehydrated/closed independently, through the
# exact same locking (_ProjectLock) every other db_* function goes through, so
# comparing a project against itself while something else edits it is just as
# safe as any other concurrent access here.

def db_diff_project(acd_path_a, acd_path_b, project_dir_a=None, project_dir_b=None,
                     verbose: bool = False) -> dict:
    """Stateless, acd_path-based equivalent of `diff_project()` (acd.api) --
    the general-purpose "what changed between these two projects" comparison.
    See `diff_project()`'s own docstring for the returned shape.
    """
    project_a = _run(acd_path_a, project_dir_a, verbose, lambda db: db.to_controller())
    project_b = _run(acd_path_b, project_dir_b, verbose, lambda db: db.to_controller())
    return _diff_project(project_a, project_b)


def db_diff_routine(acd_path_a, routine_name_a: str, acd_path_b, routine_name_b: str,
                     program_name_a: Union[str, None] = None,
                     program_name_b: Union[str, None] = None,
                     project_dir_a=None, project_dir_b=None, verbose: bool = False) -> dict:
    """Stateless, acd_path-based equivalent of `diff_routine()` (acd.api) --
    already have two specific routines (by name, possibly in two different
    projects/saves) and just want that one routine's diff. `acd_path_b`/
    `routine_name_b`/`program_name_b` may be the same project as `a` (e.g.
    comparing a routine against itself after an edit) or a different one.
    """
    project_a = _run(acd_path_a, project_dir_a, verbose, lambda db: db.to_controller())
    routine_a = _get_routine(project_a, routine_name_a, program_name_a)
    project_b = _run(acd_path_b, project_dir_b, verbose, lambda db: db.to_controller())
    routine_b = _get_routine(project_b, routine_name_b, program_name_b)
    return _diff_routine(routine_a, routine_b)


def db_diff_io_addresses(acd_path_a, acd_path_b, project_dir_a=None, project_dir_b=None,
                          verbose: bool = False) -> dict:
    """Stateless, acd_path-based equivalent of `diff_io_addresses()` (acd.api)
    -- ONLY for a request specifically about I/O address wiring; use
    `db_diff_project()` for a general comparison (see its own docstring for
    why one narrowly-scoped function with "diff" in the name isn't a safe
    default to reach for).
    """
    project_a = _run(acd_path_a, project_dir_a, verbose, lambda db: db.to_controller())
    project_b = _run(acd_path_b, project_dir_b, verbose, lambda db: db.to_controller())
    return _diff_io_addresses(project_a, project_b)
