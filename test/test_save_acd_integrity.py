"""Integration tests for save_acd's FileInfo.Dat regeneration.

These tests exercise the full load_acd -> mutate -> save_acd path
with a project-bound key, asserting that the written file's
FileInfo.Dat verifies under that key.

No real Studio key is used -- the HMAC keys are test constants. We
patch FileInfo.Dat in the source ACD before loading so that load
sees a self-consistent file under the test key; then we save under
the same key and verify the output.

Both algorithms are covered:
- 32-byte key + 02 00 selector (modern Studio)
- 126-byte key + 01 00 selector (older Studio)

The test_api.py side of save_acd (no-key passthrough) is also
covered here so the no-regression contract is explicit.
"""
import os
import shutil

import pytest

from acd.api import load_acd, save_acd
from acd.integrity import (
    FILEINFO_HEADER_V1,
    FILEINFO_HEADER_V2,
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH_V1,
    HMAC_KEY_LENGTH_V2,
    compute_fileinfo,
    detect_fileinfo_selector,
    find_fileinfo_offset,
    set_fileinfo_key,
    verify_fileinfo,
)


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")
TEST_KEY_V2 = b"\x77" * HMAC_KEY_LENGTH_V2
TEST_KEY_V1 = b"\x88" * HMAC_KEY_LENGTH_V1


# Parametrize across both algorithms: (key_length, test_key, expected_header).
ALGORITHMS = [
    pytest.param(HMAC_KEY_LENGTH_V2, TEST_KEY_V2, FILEINFO_HEADER_V2, id="v2-32-byte-modern"),
    pytest.param(HMAC_KEY_LENGTH_V1, TEST_KEY_V1, FILEINFO_HEADER_V1, id="v1-126-byte-older"),
]


def _make_acd_with_key(src_path, dst_path, key):
    """Copy `src_path` to `dst_path`, patching FileInfo.Dat to verify under `key`.

    Algorithm is auto-selected by key length: 32 -> v2 (02 00),
    126 -> v1 (01 00)."""
    with open(src_path, "rb") as fh:
        acd = bytearray(fh.read())
    fi_off = find_fileinfo_offset(bytes(acd))
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi
    with open(dst_path, "wb") as fh:
        fh.write(bytes(acd))


def test_save_acd_no_key_byte_equal_round_trip(tmp_path):
    """save_acd without a key recovers the original container byte-for-byte.

    This is the back-compat contract: callers that never set a key
    continue to get the prior write-as-is behavior."""
    src = tmp_path / "src.ACD"
    dst = tmp_path / "dst.ACD"
    shutil.copy(CUTELOGIX, src)

    project = load_acd(str(src))
    save_acd(project, str(dst))

    src_bytes = open(src, "rb").read()
    dst_bytes = open(dst, "rb").read()
    assert src_bytes == dst_bytes


@pytest.mark.parametrize("key_len,test_key,expected_header", ALGORITHMS)
def test_save_acd_with_key_recomputes_fileinfo(tmp_path, key_len, test_key, expected_header):
    """When a key is registered, save_acd writes a FileInfo.Dat that
    verifies under that key -- even after the project is loaded and
    re-saved with no in-memory mutations. The on-disk selector matches
    the key length (32 -> 02 00, 126 -> 01 00)."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, test_key)

    dst = tmp_path / "dst.ACD"
    project = load_acd(str(src))
    set_fileinfo_key(project, test_key)
    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert out[fi_off : fi_off + 2] == expected_header
    assert verify_fileinfo(out, fi_off, key=test_key)


@pytest.mark.parametrize("key_len,test_key,expected_header", ALGORITHMS)
def test_save_acd_with_key_after_mutation_verifies(tmp_path, key_len, test_key, expected_header):
    """If we mutate a non-FI stream in _raw_files between load and save,
    the recomputed FileInfo.Dat still verifies under the registered key.

    Picks an unrelated file (Version.Log) so this stays a unit test of
    the integrity-recompute path, not of higher-level mutations."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, test_key)

    dst = tmp_path / "dst.ACD"
    project = load_acd(str(src))
    set_fileinfo_key(project, test_key)

    # Tweak a single byte in a small stream to force a different
    # pre-image hash (so save MUST recompute or the file is broken).
    target = "Version.Log"
    assert target in project._raw_files
    original = project._raw_files[target]
    project._raw_files[target] = original + b"\x00"  # one-byte append

    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert verify_fileinfo(out, fi_off, key=test_key), (
        "After mutating Version.Log, save_acd must produce a FileInfo.Dat "
        "that verifies under the registered key."
    )


def test_save_acd_no_key_after_mutation_fi_stale(tmp_path):
    """Without a registered key, save_acd writes the in-memory
    FileInfo.Dat unchanged -- even if other streams were mutated. The
    output's FileInfo.Dat will therefore NOT verify under any key.

    This documents the (intentionally backward-compatible) behaviour:
    no key set -> no automatic recompute -> caller is responsible."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, TEST_KEY_V2)

    dst = tmp_path / "dst.ACD"
    project = load_acd(str(src))
    # No set_fileinfo_key.
    project._raw_files["Version.Log"] = (
        project._raw_files["Version.Log"] + b"\x00"
    )
    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert not verify_fileinfo(out, fi_off, key=TEST_KEY_V2)


def test_save_acd_key_length_must_match_source_selector(tmp_path):
    """Registering a v2 key against an ACD whose FileInfo.Dat uses the
    v1 selector (and vice versa) raises ValueError on save -- the
    library refuses to silently write a file Studio would reject.

    This is the cross-version safety check."""
    src = tmp_path / "src.ACD"
    # Make a v1-flavoured source (01 00 selector under TEST_KEY_V1).
    _make_acd_with_key(CUTELOGIX, src, TEST_KEY_V1)

    project = load_acd(str(src))
    # Register a v2 key (32 bytes) against a v1 source -- mismatch.
    set_fileinfo_key(project, TEST_KEY_V2)

    dst = tmp_path / "dst.ACD"
    with pytest.raises(ValueError, match=r"32-byte|0x0001"):
        save_acd(project, str(dst))


# Real-key v2 end-to-end check; gated on the ACD_FILEINFO_KEY env var.
def test_save_acd_with_real_key_verifies_cutelogix(tmp_path):
    """If ACD_FILEINFO_KEY is set, load_acd + save_acd on CuteLogix.ACD
    produces a file whose FileInfo.Dat verifies under that key.
    Skipped when the env var is unset.

    Confirms a modern-Studio key flows end-to-end through save. No
    v1 equivalent: the repo doesn't ship a v21-vintage sample ACD."""
    raw_hex = os.environ.get("ACD_FILEINFO_KEY", "").strip()
    if not raw_hex:
        pytest.skip("ACD_FILEINFO_KEY env var not set; skipping real-key check")
    try:
        key = bytes.fromhex(raw_hex)
    except ValueError:
        pytest.fail(f"ACD_FILEINFO_KEY is not valid hex: {raw_hex[:8]}...")
    if len(key) != HMAC_KEY_LENGTH_V2:
        pytest.fail(
            f"ACD_FILEINFO_KEY decoded to {len(key)} bytes; "
            f"expected {HMAC_KEY_LENGTH_V2}"
        )

    dst = tmp_path / "out.ACD"
    project = load_acd(CUTELOGIX)
    set_fileinfo_key(project, key)
    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert verify_fileinfo(out, fi_off, key=key)
