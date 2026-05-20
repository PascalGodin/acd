"""Project-level binding for the FileInfo.Dat HMAC key.

The HMAC-SHA-256 key is a per-Studio-version constant that this
library does not ship. The caller extracts it once from their
legitimate Studio 5000 install, then registers it on a loaded project
via `set_fileinfo_key`. After that, the save pipeline picks it up
automatically when it needs to recompute `FileInfo.Dat`.

Two key lengths are accepted; the library dispatches to the matching
algorithm by key length:

- 32-byte key  -> selector `02 00` (modern Studio).
- 126-byte key -> selector `01 00` (older Studio).

Usage:

    from acd.api import load_acd, save_acd
    from acd.integrity import set_fileinfo_key

    project = load_acd("input.ACD")
    set_fileinfo_key(project, bytes.fromhex(os.environ["ACD_FILEINFO_KEY"]))
    # ... mutate project ...
    save_acd(project, "output.ACD")   # FileInfo.Dat recomputed using the key

The key is stored as a private attribute (`_fileinfo_key`) on the
project object. It never leaves the in-memory project; this library
does not log it, persist it, or send it anywhere.
"""
from __future__ import annotations

from typing import Optional

from .fileinfo import (
    ACCEPTABLE_KEY_LENGTHS,
    IntegrityKeyRequiredError,
    find_fileinfo_offset,
    verify_fileinfo,
)


_KEY_ATTR = "_fileinfo_key"


def set_fileinfo_key(project, key: bytes) -> None:
    """Register the FileInfo.Dat HMAC-SHA-256 key on a project.

    The key length picks the algorithm:

    - 32 bytes  -> modern Studio (`02 00` selector).
    - 126 bytes -> older Studio (`01 00` selector).

    After this call, save-side machinery (and `verify_loaded_acd`)
    can recompute or verify `FileInfo.Dat` without the caller having
    to thread the key through every API.

    Arguments:
        project: An object returned by `acd.api.load_acd` (or any
            `RSLogix5000Content` instance). The key is attached as a
            private attribute on this object only.
        key: The HMAC-SHA-256 key, 32 or 126 bytes.

    Raises:
        TypeError: if `key` is not a bytes-like object.
        ValueError: if `key` is not an accepted length.
    """
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"key must be bytes-like, got {type(key).__name__}"
        )
    key_bytes = bytes(key)
    if len(key_bytes) not in ACCEPTABLE_KEY_LENGTHS:
        raise ValueError(
            f"key must be one of {ACCEPTABLE_KEY_LENGTHS} bytes "
            f"(32 = modern Studio, 126 = older Studio); "
            f"got {len(key_bytes)}"
        )
    setattr(project, _KEY_ATTR, key_bytes)


def get_fileinfo_key(project) -> Optional[bytes]:
    """Return the key set via `set_fileinfo_key`, or None if unset.

    Returns:
        The registered key bytes, or `None` if `set_fileinfo_key`
        was never called on this project.
    """
    return getattr(project, _KEY_ATTR, None)


def clear_fileinfo_key(project) -> None:
    """Remove the FileInfo.Dat key from a project, if set.

    Use this if you want to ensure subsequent save operations fail
    with `IntegrityKeyRequiredError` rather than silently using a
    stale key.
    """
    if hasattr(project, _KEY_ATTR):
        delattr(project, _KEY_ATTR)


def verify_loaded_acd(project, acd_path) -> bool:
    """Verify the on-disk ACD's FileInfo.Dat using the project's key.

    Reads the bytes at `acd_path`, locates `FileInfo.Dat` via the
    container's record table, and recomputes the HMAC under the key
    set via `set_fileinfo_key`. The algorithm is auto-selected by
    key length.

    Arguments:
        project: A project that has had `set_fileinfo_key` called on it.
        acd_path: Path to the .ACD file to verify.

    Returns:
        True iff the file's `FileInfo.Dat` matches the computed value.

    Raises:
        IntegrityKeyRequiredError: if no key was set on the project.
        ValueError: if `acd_path` is not a valid ACD container.
    """
    key = get_fileinfo_key(project)
    if key is None:
        raise IntegrityKeyRequiredError(
            "No FileInfo.Dat HMAC key set on this project. Call "
            "set_fileinfo_key(project, key) first; see "
            "acd.integrity for details."
        )
    with open(acd_path, "rb") as fh:
        acd = fh.read()
    fi_off = find_fileinfo_offset(acd)
    return verify_fileinfo(acd, fi_off, key=key)
