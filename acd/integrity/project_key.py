"""Project-level binding for the FileInfo.Dat HMAC key.

The 32-byte HMAC-SHA-256 key is a per-Studio-version constant that
this library does not ship. The caller extracts it once from their
legitimate Studio 5000 install, then registers it on a loaded
project via `set_fileinfo_key`. After that, the save pipeline picks
it up automatically when it needs to recompute `FileInfo.Dat`.

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
    HMAC_KEY_LENGTH,
    IntegrityKeyRequiredError,
    find_fileinfo_offset,
    verify_fileinfo,
)


_KEY_ATTR = "_fileinfo_key"


def set_fileinfo_key(project, key: bytes) -> None:
    """Register the 32-byte FileInfo.Dat HMAC-SHA-256 key on a project.

    After this call, save-side machinery (and `verify_loaded_acd`)
    can recompute or verify `FileInfo.Dat` without the caller having
    to thread the key through every API.

    Arguments:
        project: An object returned by `acd.api.load_acd` (or any
            `RSLogix5000Content` instance). The key is attached as a
            private attribute on this object only.
        key: The 32-byte HMAC-SHA-256 key.

    Raises:
        TypeError: if `key` is not a bytes-like object.
        ValueError: if `key` is not exactly 32 bytes.
    """
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"key must be bytes-like, got {type(key).__name__}"
        )
    key_bytes = bytes(key)
    if len(key_bytes) != HMAC_KEY_LENGTH:
        raise ValueError(
            f"key must be {HMAC_KEY_LENGTH} bytes; got {len(key_bytes)}"
        )
    setattr(project, _KEY_ATTR, key_bytes)


def get_fileinfo_key(project) -> Optional[bytes]:
    """Return the key set via `set_fileinfo_key`, or None if unset.

    Returns:
        The 32-byte key, or `None` if `set_fileinfo_key` was never
        called on this project.
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
    set via `set_fileinfo_key`.

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
