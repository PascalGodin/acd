"""ACD `FileInfo.Dat` integrity check (HMAC-SHA-256).

Studio 5000's `.ACD` container embeds a 34-byte `FileInfo.Dat` stream
whose contents validate the rest of the container on open. If you
modify any HMAC-covered stream and write the container back without
recomputing `FileInfo.Dat`, the Logix Designer SDK rejects the file
with::

    OperationFailedError: RxDbE_FILE_NOT_VALID
    (HRESULT 0x80043D09)

This module supports both algorithm variants observed across Studio
5000 versions. Each `FileInfo.Dat` carries a 2-byte selector header
that identifies the construction; the remaining 32 bytes are an
HMAC-SHA-256 digest.

| Selector | HMAC input            | Key length |
|----------|-----------------------|-----------|
| `01 00`  | `file - FileInfo.Dat` | 126 bytes |
| `02 00`  | `sha256(file - FI)`   | 32 bytes  |

The HMAC key is **not distributed with this library** -- the caller
must supply it as a keyword argument. The key is a per-Studio-version
constant; users with a legitimate Studio installation can extract it
from their local copy.

## Algorithm dispatch

This module picks the algorithm based on the **key length** supplied:

- A 32-byte key produces a selector `02 00` FileInfo.Dat.
- A 126-byte key produces a selector `01 00` FileInfo.Dat.
- Any other key length raises ValueError.

Callers don't choose the selector explicitly; the key length
unambiguously identifies which Studio version's algorithm to use.
Use `detect_fileinfo_selector` to read the selector from an existing
file when you need to choose between multiple keys you hold.

## Verification

Pure SHA-256 + HMAC-SHA-256; uses only `hashlib` + `hmac` stdlib.
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from typing import Tuple, Union


# FileInfo.Dat layout: 2-byte selector + 32-byte HMAC-SHA-256 digest.
FILEINFO_LENGTH = 34

# Per-version constants.
# Selector 1: older Studio versions.
FILEINFO_HEADER_V1 = b"\x01\x00"
HMAC_KEY_LENGTH_V1 = 126
# Selector 2: modern Studio versions.
FILEINFO_HEADER_V2 = b"\x02\x00"
HMAC_KEY_LENGTH_V2 = 32

# Back-compat aliases (selector 2 was the only one historically named here).
FILEINFO_HEADER = FILEINFO_HEADER_V2
HMAC_KEY_LENGTH = HMAC_KEY_LENGTH_V2

# Tuple of all key lengths this library accepts.
ACCEPTABLE_KEY_LENGTHS = (HMAC_KEY_LENGTH_V2, HMAC_KEY_LENGTH_V1)

# ACD container record-table constants (matches acd/zip/write_acd.py format).
_RECORD_SIZE = 528
_FILENAME_FIELD_SIZE = 520
_FOOTER_SIZE = 8


class IntegrityKeyRequiredError(RuntimeError):
    """Raised when `compute_fileinfo` is called without an HMAC key.

    The FileInfo.Dat HMAC key is not distributed with this library.
    Pass it as a keyword argument (`key=...`) extracted from a
    legitimately-installed Studio 5000.
    """


class UnsupportedFileInfoSelectorError(ValueError):
    """Raised when FileInfo.Dat uses a selector this library doesn't
    implement.

    Two selectors are recognised: `01 00` (older Studio) and `02 00`
    (modern Studio). Any other selector indicates either a corrupt
    container or an algorithm not implemented here.
    """


def _algorithm_for_key(key: bytes) -> Tuple[bytes, "callable"]:
    """Return (selector_header, prep_fn) for an HMAC key.

    `prep_fn(elided_bytes)` returns the value HMAC will sign:

    - Selector 1 (126-byte key): identity -- HMAC over `file - FI`.
    - Selector 2 (32-byte key): SHA-256 -- HMAC over `sha256(file - FI)`.
    """
    if len(key) == HMAC_KEY_LENGTH_V2:
        return FILEINFO_HEADER_V2, lambda elided: hashlib.sha256(elided).digest()
    if len(key) == HMAC_KEY_LENGTH_V1:
        return FILEINFO_HEADER_V1, lambda elided: elided
    raise ValueError(
        f"FileInfo HMAC key must be {HMAC_KEY_LENGTH_V2} bytes "
        f"(modern Studio) or {HMAC_KEY_LENGTH_V1} bytes (older Studio); "
        f"got {len(key)}"
    )


def compute_fileinfo(
    acd_bytes: bytes,
    fi_offset: int,
    *,
    key: Union[bytes, None],
) -> bytes:
    """Compute the 34-byte `FileInfo.Dat` payload for an ACD file.

    The algorithm is selected by `key` length:

    - A 32-byte key produces a selector `02 00` FileInfo.Dat
      (modern Studio): `HMAC-SHA-256(key, sha256(file - FI))`.
    - A 126-byte key produces a selector `01 00` FileInfo.Dat
      (older Studio): `HMAC-SHA-256(key, file - FI)`.

    Arguments:
        acd_bytes: The full ACD container bytes, with the existing
            `FileInfo.Dat` payload present at `fi_offset` (its content
            doesn't matter -- those 34 bytes are elided from the hash
            input).
        fi_offset: Byte offset of `FileInfo.Dat` within `acd_bytes`
            (look it up via `find_fileinfo_offset`).
        key: The HMAC-SHA-256 key (keyword-only). Required. Must be
            either 32 bytes (modern) or 126 bytes (older).

    Returns:
        34 bytes: 2-byte algorithm-selector header + 32-byte digest.

    Raises:
        IntegrityKeyRequiredError: if `key` is None.
        ValueError: if `key` is not an accepted length, or if
            `fi_offset` is out of bounds.
    """
    if key is None:
        raise IntegrityKeyRequiredError(
            "FileInfo.Dat HMAC key required. The key is not distributed "
            "with this library; extract it once from your locally-"
            "installed Studio 5000 and pass it as the `key=` argument."
        )
    header, prep = _algorithm_for_key(key)
    if fi_offset < 0 or fi_offset + FILEINFO_LENGTH > len(acd_bytes):
        raise ValueError(
            f"fi_offset={fi_offset} out of bounds for "
            f"{len(acd_bytes)}-byte ACD"
        )

    elided = acd_bytes[:fi_offset] + acd_bytes[fi_offset + FILEINFO_LENGTH:]
    hmac_input = prep(elided)
    digest = hmac.new(key, hmac_input, hashlib.sha256).digest()
    return header + digest


def verify_fileinfo(
    acd_bytes: bytes,
    fi_offset: int,
    *,
    key: Union[bytes, None],
) -> bool:
    """Return True iff the 34-byte `FileInfo.Dat` at `fi_offset`
    matches the bytes that `compute_fileinfo` would produce.

    The algorithm is auto-selected by key length. Same key requirement
    as `compute_fileinfo`.
    """
    expected = compute_fileinfo(acd_bytes, fi_offset, key=key)
    actual = acd_bytes[fi_offset : fi_offset + FILEINFO_LENGTH]
    return actual == expected


def detect_fileinfo_selector(acd_bytes: bytes, fi_offset: int) -> int:
    """Return the algorithm-selector value (1 or 2) read from
    `FileInfo.Dat` at `fi_offset`.

    Reads the 2-byte little-endian selector header. Use this when
    you don't know which Studio version produced a given ACD and
    want to look up the right key to register.

    Raises:
        ValueError: if `fi_offset` is out of bounds.
        UnsupportedFileInfoSelectorError: if the selector isn't a
            known value.
    """
    if fi_offset < 0 or fi_offset + 2 > len(acd_bytes):
        raise ValueError(
            f"fi_offset={fi_offset} out of bounds for "
            f"{len(acd_bytes)}-byte ACD"
        )
    selector = acd_bytes[fi_offset] | (acd_bytes[fi_offset + 1] << 8)
    if selector not in (1, 2):
        raise UnsupportedFileInfoSelectorError(
            f"FileInfo.Dat selector {selector:#06x} not supported; "
            f"this library implements selectors 0x0001 and 0x0002."
        )
    return selector


def expected_key_length_for_selector(selector: int) -> int:
    """Return the key length (in bytes) required for a given selector.

    Raises:
        UnsupportedFileInfoSelectorError: if the selector isn't known.
    """
    if selector == 2:
        return HMAC_KEY_LENGTH_V2
    if selector == 1:
        return HMAC_KEY_LENGTH_V1
    raise UnsupportedFileInfoSelectorError(
        f"FileInfo.Dat selector {selector:#06x} not supported; "
        f"this library implements selectors 0x0001 and 0x0002."
    )


def find_fileinfo_offset(acd_bytes: bytes) -> int:
    """Scan the ACD container's record table to find FileInfo.Dat's
    offset within the container.

    The ACD container layout (see `acd/zip/write_acd.py`):

    ```text
    [file data blocks ...]
    [file record table: 528 B * num_files]
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
