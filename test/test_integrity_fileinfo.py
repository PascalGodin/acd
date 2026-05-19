"""Tests for the pure-function `FileInfo.Dat` HMAC API.

These tests cover:
- Algorithm structure (deterministic output, sensitive to key changes)
- Error paths (no key, wrong-length key, out-of-bounds offset)
- find_fileinfo_offset against a real ACD's record table

A real-key verification test runs only when the `ACD_FILEINFO_KEY`
env var is set (64-char hex string). Users with a legitimate Studio
5000 install can extract their key and set the env var to run it;
CI / strangers without the key skip it.
"""
import os

import pytest

from acd.integrity import (
    FILEINFO_HEADER,
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH,
    IntegrityKeyRequiredError,
    compute_fileinfo,
    find_fileinfo_offset,
    verify_fileinfo,
)


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")


def _read_acd():
    with open(CUTELOGIX, "rb") as fh:
        return fh.read()


def test_no_key_raises():
    """Passing key=None raises IntegrityKeyRequiredError with a
    helpful message that does NOT leak the key value or its source."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    with pytest.raises(IntegrityKeyRequiredError) as exc:
        compute_fileinfo(acd, fi_off, key=None)
    assert "key required" in str(exc.value).lower()


def test_wrong_length_key_raises():
    """A key that isn't exactly 32 bytes raises ValueError."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    for bad in (b"", b"x" * 16, b"y" * 31, b"z" * 33, b"w" * 64):
        with pytest.raises(ValueError):
            compute_fileinfo(acd, fi_off, key=bad)


def test_compute_deterministic():
    """Calling compute_fileinfo twice with the same inputs gives
    identical output."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    key = b"\x00" * HMAC_KEY_LENGTH
    first = compute_fileinfo(acd, fi_off, key=key)
    second = compute_fileinfo(acd, fi_off, key=key)
    assert first == second
    assert len(first) == FILEINFO_LENGTH
    assert first.startswith(FILEINFO_HEADER)


def test_compute_changes_with_key():
    """Different keys produce different HMAC outputs."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    fi_zero = compute_fileinfo(acd, fi_off, key=b"\x00" * HMAC_KEY_LENGTH)
    fi_ones = compute_fileinfo(acd, fi_off, key=b"\xff" * HMAC_KEY_LENGTH)
    assert fi_zero != fi_ones
    # Both still start with the algorithm-selector header.
    assert fi_zero[:2] == FILEINFO_HEADER
    assert fi_ones[:2] == FILEINFO_HEADER


def test_verify_round_trips_with_same_key():
    """If we patch in the bytes compute_fileinfo emits, verify_fileinfo
    returns True under the same key."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    key = b"\x42" * HMAC_KEY_LENGTH
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi
    assert verify_fileinfo(bytes(acd), fi_off, key=key)


def test_verify_fails_with_wrong_key():
    """verify_fileinfo returns False under a key different from the
    one used to compute the FileInfo."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    key_a = b"\x01" * HMAC_KEY_LENGTH
    key_b = b"\x02" * HMAC_KEY_LENGTH
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key_a)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi
    assert not verify_fileinfo(bytes(acd), fi_off, key=key_b)


def test_find_fileinfo_offset_in_cutelogix():
    """The record-table scan locates FileInfo.Dat in CuteLogix.ACD."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    # Verify the byte range is plausible: should be within the file,
    # and the stream content should start with the 02 00 algorithm
    # selector header.
    assert 0 < fi_off < len(acd) - FILEINFO_LENGTH
    assert acd[fi_off : fi_off + 2] == FILEINFO_HEADER


def test_out_of_bounds_offset_raises():
    """A bogus fi_offset raises ValueError."""
    acd = _read_acd()
    key = b"\x00" * HMAC_KEY_LENGTH
    with pytest.raises(ValueError):
        compute_fileinfo(acd, -1, key=key)
    with pytest.raises(ValueError):
        compute_fileinfo(acd, len(acd) - 10, key=key)


# Real-key test — only runs if user supplies the key via env var.
def test_real_key_verifies_cutelogix():
    """If `ACD_FILEINFO_KEY` is set (64-char hex), the CuteLogix.ACD
    reference verifies under that key. This is the test that confirms
    a user's extracted key is correct."""
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
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    assert verify_fileinfo(acd, fi_off, key=key), (
        "ACD_FILEINFO_KEY does not verify CuteLogix.ACD — wrong key, "
        "or CuteLogix.ACD was modified post-extraction."
    )
