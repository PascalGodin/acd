"""Integration tests for save_acd's FileInfo.Dat regeneration.

These tests exercise the full load_acd -> mutate -> save_acd path
with a project-bound key, asserting that the written file's
FileInfo.Dat verifies under that key.

No real Studio key is used — the HMAC key is a test constant. We
patch FileInfo.Dat in the source ACD before loading so that load
sees a self-consistent file under the test key; then we save under
the same key and verify the output.

The test_api.py side of save_acd (no-key passthrough) is also
covered here so the no-regression contract is explicit.
"""
import os
import shutil

import pytest

from acd.api import load_acd, save_acd
from acd.integrity import (
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH,
    compute_fileinfo,
    find_fileinfo_offset,
    set_fileinfo_key,
    verify_fileinfo,
)


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")
TEST_KEY = b"\x77" * HMAC_KEY_LENGTH


def _make_acd_with_key(src_path, dst_path, key):
    """Copy `src_path` to `dst_path`, patching FileInfo.Dat to verify under `key`."""
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


def test_save_acd_with_key_recomputes_fileinfo(tmp_path):
    """When a key is registered, save_acd writes a FileInfo.Dat that
    verifies under that key — even after the project is loaded and
    re-saved with no in-memory mutations."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, TEST_KEY)

    dst = tmp_path / "dst.ACD"
    project = load_acd(str(src))
    set_fileinfo_key(project, TEST_KEY)
    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert verify_fileinfo(out, fi_off, key=TEST_KEY)


def test_save_acd_with_key_after_mutation_verifies(tmp_path):
    """If we mutate a non-FI stream in _raw_files between load and save,
    the recomputed FileInfo.Dat still verifies under the registered key.

    Picks an unrelated file (Version.Log) so this stays a unit test of
    the integrity-recompute path, not of higher-level mutations."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, TEST_KEY)

    dst = tmp_path / "dst.ACD"
    project = load_acd(str(src))
    set_fileinfo_key(project, TEST_KEY)

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
    assert verify_fileinfo(out, fi_off, key=TEST_KEY), (
        "After mutating Version.Log, save_acd must produce a FileInfo.Dat "
        "that verifies under the registered key."
    )


def test_save_acd_no_key_after_mutation_fi_stale(tmp_path):
    """Without a registered key, save_acd writes the in-memory
    FileInfo.Dat unchanged — even if other streams were mutated. The
    output's FileInfo.Dat will therefore NOT verify under any key.

    This documents the (intentionally backward-compatible) behaviour:
    no key set → no automatic recompute → caller is responsible."""
    src = tmp_path / "src.ACD"
    _make_acd_with_key(CUTELOGIX, src, TEST_KEY)

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
    assert not verify_fileinfo(out, fi_off, key=TEST_KEY)


# Real-key end-to-end — only runs if user supplies a Studio key.
def test_save_acd_with_real_key_verifies_cutelogix(tmp_path):
    """If ACD_FILEINFO_KEY is set, load_acd + save_acd on CuteLogix.ACD
    produces a file whose FileInfo.Dat verifies under that key.

    This is the test a Studio-equipped user runs to confirm the
    extracted key flows end-to-end through save."""
    raw_hex = os.environ.get("ACD_FILEINFO_KEY", "").strip()
    if not raw_hex:
        pytest.skip("ACD_FILEINFO_KEY env var not set; skipping real-key check")
    try:
        key = bytes.fromhex(raw_hex)
    except ValueError:
        pytest.fail(f"ACD_FILEINFO_KEY is not valid hex: {raw_hex[:8]}...")
    if len(key) != HMAC_KEY_LENGTH:
        pytest.fail(
            f"ACD_FILEINFO_KEY decoded to {len(key)} bytes; "
            f"expected {HMAC_KEY_LENGTH}"
        )

    dst = tmp_path / "out.ACD"
    project = load_acd(CUTELOGIX)
    set_fileinfo_key(project, key)
    save_acd(project, str(dst))

    with open(dst, "rb") as fh:
        out = fh.read()
    fi_off = find_fileinfo_offset(out)
    assert verify_fileinfo(out, fi_off, key=key)
