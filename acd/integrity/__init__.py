"""ACD `FileInfo.Dat` integrity check.

Public API:
    pure functions (low-level):
        compute_fileinfo(acd_bytes, fi_offset, *, key) -> 34-byte payload
        verify_fileinfo(acd_bytes, fi_offset, *, key) -> bool
        find_fileinfo_offset(acd_bytes) -> int
        detect_fileinfo_selector(acd_bytes, fi_offset) -> int
        expected_key_length_for_selector(selector) -> int

    project-level binding (high-level):
        set_fileinfo_key(project, key) -> None
        get_fileinfo_key(project) -> bytes | None
        clear_fileinfo_key(project) -> None
        verify_loaded_acd(project, acd_path) -> bool

    exceptions:
        IntegrityKeyRequiredError
        UnsupportedFileInfoSelectorError

The algorithm is selected by key length:

- 32-byte key  -> selector 02 00 (modern Studio).
- 126-byte key -> selector 01 00 (older Studio).

The HMAC key is NOT shipped with this library. Callers must supply
it; the key is a per-Studio-version constant that users can extract
from their legitimately-installed Studio 5000.
"""
from .fileinfo import (
    ACCEPTABLE_KEY_LENGTHS,
    FILEINFO_HEADER,
    FILEINFO_HEADER_V1,
    FILEINFO_HEADER_V2,
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH,
    HMAC_KEY_LENGTH_V1,
    HMAC_KEY_LENGTH_V2,
    IntegrityKeyRequiredError,
    UnsupportedFileInfoSelectorError,
    compute_fileinfo,
    detect_fileinfo_selector,
    expected_key_length_for_selector,
    find_fileinfo_offset,
    verify_fileinfo,
)
from .project_key import (
    clear_fileinfo_key,
    get_fileinfo_key,
    set_fileinfo_key,
    verify_loaded_acd,
)

__all__ = [
    "ACCEPTABLE_KEY_LENGTHS",
    "FILEINFO_HEADER",
    "FILEINFO_HEADER_V1",
    "FILEINFO_HEADER_V2",
    "FILEINFO_LENGTH",
    "HMAC_KEY_LENGTH",
    "HMAC_KEY_LENGTH_V1",
    "HMAC_KEY_LENGTH_V2",
    "IntegrityKeyRequiredError",
    "UnsupportedFileInfoSelectorError",
    "compute_fileinfo",
    "verify_fileinfo",
    "find_fileinfo_offset",
    "detect_fileinfo_selector",
    "expected_key_length_for_selector",
    "set_fileinfo_key",
    "get_fileinfo_key",
    "clear_fileinfo_key",
    "verify_loaded_acd",
]
