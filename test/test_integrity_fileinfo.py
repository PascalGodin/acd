"""Tests for the pure-function `FileInfo.Dat` HMAC API.

These tests cover:
- Algorithm structure (deterministic output, sensitive to key changes)
- Both selectors -- 02 00 (modern, 32-byte key) and 01 00 (older,
  126-byte key) -- via parametrized tests
- Error paths (no key, wrong-length key, out-of-bounds offset,
  unknown on-disk selector)
- find_fileinfo_offset / detect_fileinfo_selector against a real ACD

Both selectors (v1 and v2) are exercised by the structural and
round-trip tests above using arbitrary test keys. A v2 real-key
end-to-end check additionally runs when the `ACD_FILEINFO_KEY` env
var is set (64-char hex = 32-byte v2 key), verifying the bundled
CuteLogix.ACD fixture; it is skipped otherwise. There is no
equivalent v1 real-key test because the repo doesn't ship a
v21-vintage sample ACD fixture.
"""
import hashlib
import hmac
import os

import pytest

from acd.integrity import (
    ACCEPTABLE_KEY_LENGTHS,
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


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")

# Parametrize over both algorithms: (key_length, expected_header).
ALGORITHMS = [
    pytest.param(HMAC_KEY_LENGTH_V2, FILEINFO_HEADER_V2, id="v2-32-byte-modern"),
    pytest.param(HMAC_KEY_LENGTH_V1, FILEINFO_HEADER_V1, id="v1-126-byte-older"),
]


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
    """A key whose length isn't in ACCEPTABLE_KEY_LENGTHS raises ValueError.

    Note that 32 and 126 are *accepted* lengths; everything else is bad."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    # Anything that is NOT in ACCEPTABLE_KEY_LENGTHS:
    bad_lengths = (0, 1, 16, 31, 33, 64, 125, 127, 128)
    for n in bad_lengths:
        assert n not in ACCEPTABLE_KEY_LENGTHS, (
            f"Test bug: {n} is in ACCEPTABLE_KEY_LENGTHS"
        )
        with pytest.raises(ValueError):
            compute_fileinfo(acd, fi_off, key=b"\x00" * n)


@pytest.mark.parametrize("key_len,header", ALGORITHMS)
def test_compute_deterministic(key_len, header):
    """Calling compute_fileinfo twice with the same inputs gives
    identical output, of length FILEINFO_LENGTH, starting with the
    selector header that matches the key length."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    key = b"\x00" * key_len
    first = compute_fileinfo(acd, fi_off, key=key)
    second = compute_fileinfo(acd, fi_off, key=key)
    assert first == second
    assert len(first) == FILEINFO_LENGTH
    assert first[:2] == header


@pytest.mark.parametrize("key_len,header", ALGORITHMS)
def test_compute_changes_with_key(key_len, header):
    """Different keys of the same length produce different HMAC outputs;
    selector header stays the same."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    fi_zero = compute_fileinfo(acd, fi_off, key=b"\x00" * key_len)
    fi_ones = compute_fileinfo(acd, fi_off, key=b"\xff" * key_len)
    assert fi_zero != fi_ones
    assert fi_zero[:2] == header
    assert fi_ones[:2] == header


@pytest.mark.parametrize("key_len,header", ALGORITHMS)
def test_verify_round_trips_with_same_key(key_len, header):
    """If we patch in the bytes compute_fileinfo emits, verify_fileinfo
    returns True under the same key."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    key = b"\x42" * key_len
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key)
    assert new_fi[:2] == header
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi
    assert verify_fileinfo(bytes(acd), fi_off, key=key)


@pytest.mark.parametrize("key_len,header", ALGORITHMS)
def test_verify_fails_with_wrong_key(key_len, header):
    """verify_fileinfo returns False under a key different from the
    one used to compute the FileInfo (same length, different bytes)."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    key_a = b"\x01" * key_len
    key_b = b"\x02" * key_len
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key_a)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi
    assert not verify_fileinfo(bytes(acd), fi_off, key=key_b)


def test_v2_uses_pre_hash_sha256():
    """The 32-byte (modern) algorithm HMACs sha256(file - FI), not the
    raw bytes. Verify by computing the expected value manually."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    key = b"\xaa" * HMAC_KEY_LENGTH_V2
    elided = acd[:fi_off] + acd[fi_off + FILEINFO_LENGTH:]
    pre = hashlib.sha256(elided).digest()
    expected_digest = hmac.new(key, pre, hashlib.sha256).digest()
    result = compute_fileinfo(acd, fi_off, key=key)
    assert result == FILEINFO_HEADER_V2 + expected_digest


def test_v1_uses_no_pre_hash():
    """The 126-byte (older) algorithm HMACs `file - FI` directly with
    no SHA-256 pre-hash. Verify by computing the expected value
    manually."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    key = b"\xbb" * HMAC_KEY_LENGTH_V1
    elided = acd[:fi_off] + acd[fi_off + FILEINFO_LENGTH:]
    expected_digest = hmac.new(key, elided, hashlib.sha256).digest()
    result = compute_fileinfo(acd, fi_off, key=key)
    assert result == FILEINFO_HEADER_V1 + expected_digest


def test_v1_and_v2_produce_different_outputs():
    """Same elided file, different key length -> different selector
    AND different digest (because the HMAC inputs differ structurally)."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    fi_v2 = compute_fileinfo(acd, fi_off, key=b"\xcc" * HMAC_KEY_LENGTH_V2)
    fi_v1 = compute_fileinfo(acd, fi_off, key=b"\xcc" * HMAC_KEY_LENGTH_V1)
    assert fi_v2[:2] == FILEINFO_HEADER_V2
    assert fi_v1[:2] == FILEINFO_HEADER_V1
    assert fi_v2[2:] != fi_v1[2:]


def test_find_fileinfo_offset_in_cutelogix():
    """The record-table scan locates FileInfo.Dat in CuteLogix.ACD.
    CuteLogix is modern (selector 02 00)."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    assert 0 < fi_off < len(acd) - FILEINFO_LENGTH
    assert acd[fi_off : fi_off + 2] == FILEINFO_HEADER_V2


def test_detect_fileinfo_selector_modern():
    """detect_fileinfo_selector returns 2 for a modern ACD's FileInfo.Dat."""
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    assert detect_fileinfo_selector(acd, fi_off) == 2


def test_detect_fileinfo_selector_older():
    """detect_fileinfo_selector returns 1 when the on-disk header is 01 00."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    # Force the header to 01 00 (the rest of the FI doesn't matter for detection).
    acd[fi_off : fi_off + 2] = FILEINFO_HEADER_V1
    assert detect_fileinfo_selector(bytes(acd), fi_off) == 1


def test_detect_fileinfo_selector_unknown_raises():
    """An on-disk header that isn't 01 00 or 02 00 raises
    UnsupportedFileInfoSelectorError."""
    acd = bytearray(_read_acd())
    fi_off = find_fileinfo_offset(bytes(acd))
    acd[fi_off : fi_off + 2] = b"\x99\x99"
    with pytest.raises(UnsupportedFileInfoSelectorError):
        detect_fileinfo_selector(bytes(acd), fi_off)


def test_detect_fileinfo_selector_out_of_bounds():
    """Out-of-bounds offset raises ValueError, NOT
    UnsupportedFileInfoSelectorError."""
    acd = _read_acd()
    with pytest.raises(ValueError):
        detect_fileinfo_selector(acd, -1)
    with pytest.raises(ValueError):
        detect_fileinfo_selector(acd, len(acd) - 1)


def test_expected_key_length_for_selector():
    """Lookup helper maps selector -> required key length."""
    assert expected_key_length_for_selector(2) == HMAC_KEY_LENGTH_V2
    assert expected_key_length_for_selector(1) == HMAC_KEY_LENGTH_V1
    with pytest.raises(UnsupportedFileInfoSelectorError):
        expected_key_length_for_selector(3)


@pytest.mark.parametrize("key_len,header", ALGORITHMS)
def test_out_of_bounds_offset_raises(key_len, header):
    """A bogus fi_offset raises ValueError."""
    acd = _read_acd()
    key = b"\x00" * key_len
    with pytest.raises(ValueError):
        compute_fileinfo(acd, -1, key=key)
    with pytest.raises(ValueError):
        compute_fileinfo(acd, len(acd) - 10, key=key)


def test_back_compat_aliases():
    """Back-compat aliases still resolve (HMAC_KEY_LENGTH = v2 length,
    FILEINFO_HEADER = v2 header)."""
    from acd.integrity import FILEINFO_HEADER
    assert HMAC_KEY_LENGTH == HMAC_KEY_LENGTH_V2 == 32
    assert FILEINFO_HEADER == FILEINFO_HEADER_V2 == b"\x02\x00"


# Real-key v2 end-to-end check; gated on the ACD_FILEINFO_KEY env var.
def test_real_key_verifies_cutelogix():
    """If `ACD_FILEINFO_KEY` is set (64-char hex = 32-byte v2 key),
    the bundled CuteLogix.ACD reference verifies under it. Skipped
    when the env var is unset.

    No v1 equivalent: the repo doesn't ship a v21-vintage sample
    ACD. The v1 algorithm is covered by the structural / round-trip
    cases above with arbitrary 126-byte keys."""
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
    acd = _read_acd()
    fi_off = find_fileinfo_offset(acd)
    assert verify_fileinfo(acd, fi_off, key=key), (
        "ACD_FILEINFO_KEY does not verify CuteLogix.ACD -- wrong key, "
        "or CuteLogix.ACD was modified post-extraction."
    )
