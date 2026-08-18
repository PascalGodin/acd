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
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Union

from loguru import logger as log

from acd.api import (
    _AOI_RESERVED_ROUTINE_NAMES,
    diff_io_addresses as _diff_io_addresses,
    diff_project as _diff_project,
    diff_routine as _diff_routine,
    export_aoi as _export_aoi,
    export_datatype as _export_datatype,
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
    _validate_rll_rung_syntax,
    new_aoi as _new_aoi,
    new_aoi_local_tag as _new_aoi_local_tag,
    new_aoi_parameter as _new_aoi_parameter,
    new_bit_member as _new_bit_member,
    new_datatype as _new_datatype,
    new_member as _new_member,
    new_routine as _new_routine,
    new_tag as _new_tag,
)
from acd.l5x.export_l5x import configure_logging, ExportL5x

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
                log.warning(
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

    @staticmethod
    def _get_routine_scope(program_name: Union[str, None], aoi_name: Union[str, None]) -> Union[str, None]:
        """Translate this class's own `program_name=`/`aoi_name=` pair into
        the single `program_name=` string `acd.api.get_routine()` expects --
        that function already supports AOI-owned routines via its own
        pre-existing `program_name=f"AOI:{aoi_name}"` convention (see its
        docstring), so `export_routine()`/`get_routine()` below translate
        into that rather than needing a second resolution mechanism.
        """
        if program_name is not None and aoi_name is not None:
            raise ValueError(
                "pass at most one of program_name/aoi_name to disambiguate a routine, not both"
            )
        if aoi_name is not None:
            return f"AOI:{aoi_name}"
        return program_name

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
        """
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
        if `usage` isn't `"Input"`/`"Output"`/`"InOut"`,
        `sqlite3.IntegrityError` if a parameter with this name already
        exists on that AOI.
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
        `new_aoi()`'s own docstring). `to_controller()` APPENDS this list
        onto the real, freshly-decoded `.aois` rather than replacing it.
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
        # APPENDED, never replaced -- see _load_aois()'s own docstring for
        # why (a real project's own pre-existing AOIs, including LocalTags
        # this DB never persists, must never be touched).
        controller.aois = controller.aois + self._load_aois()

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

    # ---- read-only convenience (thin wrappers over acd.api, against a fresh rehydration) ----

    def list_tags(self, program_name: Union[str, None] = None) -> List[dict]:
        return _list_tags(self.to_controller(), program_name)

    def list_routines(self, program_name: Union[str, None] = None) -> List[dict]:
        return _list_routines(self.to_controller(), program_name)

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
        `get_routine()` (acd.api) does for a missing/ambiguous name.

        `"rung_comments"` is `Dict[int, str]` -- keyed by the INTEGER rung
        index (same index space as `"rungs"`), not a stringified index and
        not JSON-style string keys. A rung with no comment has no entry at
        all (use `.get(i)`, which returns `None`, rather than assuming
        every index 0..len(rungs) is present). `comments.get(str(i))` will
        silently return `None` for every rung -- no exception, just quietly
        wrong/empty data.

        Pass `aoi_name=` (not `program_name=`) to read an AOI-owned logic
        routine -- exactly one of `program_name`/`aoi_name` may be given.
        """
        scope = self._get_routine_scope(program_name, aoi_name)
        routine = _get_routine(self.to_controller(), routine_name, scope)
        return {
            "name": routine.name,
            "type": routine.type,
            "description": routine._description,
            "rungs": list(routine.rungs),
            "rung_comments": dict(routine._rung_comments),
            "st_lines": list(routine._st_lines),
        }

    def get_tag_value(self, tag_name: str, program_name: Union[str, None] = None,
                       offset: int = 0, limit: int = 50) -> dict:
        return _get_tag_value(self.to_controller(), tag_name, program_name, offset, limit)

    def find_tag_references(self, name: str, regex: bool = False) -> List[tuple]:
        return _find_tag_references(self.to_controller(), name, regex)

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


def db_list_tags(acd_path, program_name: Union[str, None] = None, project_dir=None,
                  verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_tags()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_tags(program_name))


def db_list_routines(acd_path, program_name: Union[str, None] = None, project_dir=None,
                      verbose: bool = False) -> List[dict]:
    """Stateless equivalent of `ProjectDB.list_routines()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.list_routines(program_name))


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


def db_find_tag_references(acd_path, name: str, regex: bool = False, project_dir=None,
                            verbose: bool = False) -> List[tuple]:
    """Stateless equivalent of `ProjectDB.find_tag_references()` -- see its docstring."""
    return _run(acd_path, project_dir, verbose, lambda db: db.find_tag_references(name, regex))


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
