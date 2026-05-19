"""ACD `FileInfo.Dat` integrity check (HMAC-SHA-256).

Studio 5000's `.ACD` container embeds a 34-byte `FileInfo.Dat` stream
whose contents validate the rest of the container on open. If you
modify any HMAC-covered stream and write the container back without
recomputing `FileInfo.Dat`, the Logix Designer SDK rejects the file
with::

    OperationFailedError: RxDbE_FILE_NOT_VALID
    (HRESULT 0x80043D09)

This module implements the algorithm. The 32-byte HMAC **key is not
distributed with this library** — the caller must supply it as a
keyword argument to `compute_fileinfo` / `verify_fileinfo`. The key
is a per-Studio-version constant; users with a legitimate Studio
5000 installation can extract it from their local copy.

## Algorithm

```text
FileInfo.Dat = "02 00" || HMAC-SHA-256(KEY, sha256(file − FileInfo.Dat))
```

where `file − FileInfo.Dat` means "the entire ACD container with the
34-byte FileInfo.Dat range elided." Both halves of the digest are
straight SHA-256:

1. `pre = sha256(acd[:fi_off] + acd[fi_off + 34:])`
2. `post = hmac_sha256(KEY, pre)`
3. `FileInfo.Dat = b"\\x02\\x00" + post`

The leading two bytes `02 00` are an algorithm-selector header
indicating SHA-256; they precede the 32-byte HMAC digest in the
on-disk stream.

## Verification

Pure SHA-256 + HMAC-SHA-256; uses only `hashlib` + `hmac` stdlib.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Union


# 34-byte FileInfo.Dat = 2-byte algorithm selector + 32-byte HMAC-SHA-256 digest.
FILEINFO_LENGTH = 34
FILEINFO_HEADER = b"\x02\x00"  # algorithm-selector header for SHA-256
HMAC_KEY_LENGTH = 32

# ACD container record-table constants (matches acd/zip/write_acd.py format).
_RECORD_SIZE = 528
_FILENAME_FIELD_SIZE = 520
_FOOTER_SIZE = 8


class IntegrityKeyRequiredError(RuntimeError):
    """Raised when `compute_fileinfo` is called without an HMAC key.

    The 32-byte FileInfo.Dat HMAC key is not distributed with this
    library. Pass it as a keyword argument (`key=...`) extracted from
    a legitimately-installed Studio 5000.
    """


def compute_fileinfo(
    acd_bytes: bytes,
    fi_offset: int,
    *,
    key: Union[bytes, None],
) -> bytes:
    """Compute the 34-byte `FileInfo.Dat` payload for an ACD file.

    Arguments:
        acd_bytes: The full ACD container bytes, with the existing
            `FileInfo.Dat` payload present at `fi_offset` (its content
            doesn't matter — those 34 bytes are elided from the hash
            input).
        fi_offset: Byte offset of `FileInfo.Dat` within `acd_bytes`
            (look it up via the container's record table — see
            `find_fileinfo_offset`).
        key: The 32-byte HMAC-SHA-256 key (keyword-only). Required.
            Extract once from your locally-installed Studio 5000;
            it's a per-version constant that you can stash in an
            env var or local config file.

    Returns:
        34 bytes: `b"\\x02\\x00"` + 32-byte HMAC-SHA-256(key, sha256(file − FI)).

    Raises:
        IntegrityKeyRequiredError: if `key` is None.
        ValueError: if `key` is the wrong length, or if `fi_offset` is
            out of bounds.
    """
    if key is None:
        raise IntegrityKeyRequiredError(
            "FileInfo.Dat HMAC key required. The key is not distributed "
            "with this library; extract it once from your locally-"
            "installed Studio 5000 and pass it as the `key=` argument."
        )
    if len(key) != HMAC_KEY_LENGTH:
        raise ValueError(
            f"FileInfo HMAC key must be {HMAC_KEY_LENGTH} bytes; got {len(key)}"
        )
    if fi_offset < 0 or fi_offset + FILEINFO_LENGTH > len(acd_bytes):
        raise ValueError(
            f"fi_offset={fi_offset} out of bounds for "
            f"{len(acd_bytes)}-byte ACD"
        )

    elided = acd_bytes[:fi_offset] + acd_bytes[fi_offset + FILEINFO_LENGTH:]
    pre = hashlib.sha256(elided).digest()
    post = hmac.new(key, pre, hashlib.sha256).digest()
    return FILEINFO_HEADER + post


def verify_fileinfo(
    acd_bytes: bytes,
    fi_offset: int,
    *,
    key: Union[bytes, None],
) -> bool:
    """Return True iff the 34-byte `FileInfo.Dat` at `fi_offset`
    matches the bytes that `compute_fileinfo` would produce.

    Same key requirement as `compute_fileinfo`.
    """
    expected = compute_fileinfo(acd_bytes, fi_offset, key=key)
    actual = acd_bytes[fi_offset : fi_offset + FILEINFO_LENGTH]
    return actual == expected


def find_fileinfo_offset(acd_bytes: bytes) -> int:
    """Scan the ACD container's record table to find FileInfo.Dat's
    offset within the container.

    The ACD container layout (see `acd/zip/write_acd.py`):

    ```text
    [file data blocks ...]
    [file record table: 528 B × num_files]
        [filename: UTF-16LE null-terminated, padded to 520 B]
        [file_length: u32_le]
        [file_offset: u32_le  -- absolute offset]
    [footer: num_files u32_le + footer_unknown u32_le]
    ```
    """
    if len(acd_bytes) < _FOOTER_SIZE:
        raise ValueError("ACD too short for footer")
    num_files, _ = struct.unpack_from("<II", acd_bytes, len(acd_bytes) - _FOOTER_SIZE)
    rec_start = len(acd_bytes) - _FOOTER_SIZE - num_files * _RECORD_SIZE
    if rec_start < 0:
        raise ValueError("ACD record table out of bounds")
    for i in range(num_files):
        off = rec_start + i * _RECORD_SIZE
        name_bytes = acd_bytes[off : off + _FILENAME_FIELD_SIZE]
        name = name_bytes.decode("utf-16-le", errors="ignore").rstrip("\x00")
        if name == "FileInfo.Dat":
            _, foff = struct.unpack_from(
                "<II", acd_bytes, off + _FILENAME_FIELD_SIZE
            )
            return foff
    raise ValueError("FileInfo.Dat not found in record table")
