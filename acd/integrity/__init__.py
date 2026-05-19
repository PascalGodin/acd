"""ACD `FileInfo.Dat` integrity check.

Public API:
    pure functions (low-level):
        compute_fileinfo(acd_bytes, fi_offset, *, key) -> 34-byte payload
        verify_fileinfo(acd_bytes, fi_offset, *, key) -> bool
        find_fileinfo_offset(acd_bytes) -> int

    exceptions:
        IntegrityKeyRequiredError

The 32-byte HMAC key is NOT shipped with this library. Callers must
supply it; the key is a per-Studio-version constant that users can
extract from their legitimately-installed Studio 5000.
"""
from .fileinfo import (
    FILEINFO_HEADER,
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH,
    IntegrityKeyRequiredError,
    compute_fileinfo,
    find_fileinfo_offset,
    verify_fileinfo,
)

__all__ = [
    "FILEINFO_HEADER",
    "FILEINFO_LENGTH",
    "HMAC_KEY_LENGTH",
    "IntegrityKeyRequiredError",
    "compute_fileinfo",
    "verify_fileinfo",
    "find_fileinfo_offset",
]
