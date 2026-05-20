"""Tests for the project-level FileInfo.Dat key binding.

These tests use a `types.SimpleNamespace` as a stand-in for the
project object -- `set_fileinfo_key` doesn't care what type its first
argument is, only that it can hold an attribute. That keeps these
tests fast and free of the heavyweight `ExportL5x` import path used
in the API integration test.

The `verify_loaded_acd` round-trip test patches a fake FileInfo.Dat
into a temp file using the same test key, then asserts verification
passes -- exercised across both algorithms (32-byte modern, 126-byte
older).
"""
import os
import shutil
import types

import pytest

from acd.integrity import (
    ACCEPTABLE_KEY_LENGTHS,
    FILEINFO_LENGTH,
    HMAC_KEY_LENGTH_V1,
    HMAC_KEY_LENGTH_V2,
    IntegrityKeyRequiredError,
    clear_fileinfo_key,
    compute_fileinfo,
    find_fileinfo_offset,
    get_fileinfo_key,
    set_fileinfo_key,
    verify_loaded_acd,
)


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")

# Parametrize across both accepted key lengths.
KEY_LENGTHS = [
    pytest.param(HMAC_KEY_LENGTH_V2, id="v2-32-byte-modern"),
    pytest.param(HMAC_KEY_LENGTH_V1, id="v1-126-byte-older"),
]


def _project_stub():
    return types.SimpleNamespace()


@pytest.mark.parametrize("key_len", KEY_LENGTHS)
def test_set_get_round_trip(key_len):
    """A key stored via set_fileinfo_key comes back via get_fileinfo_key."""
    project = _project_stub()
    assert get_fileinfo_key(project) is None
    key = b"\x11" * key_len
    set_fileinfo_key(project, key)
    assert get_fileinfo_key(project) == key


@pytest.mark.parametrize("key_len", KEY_LENGTHS)
def test_set_accepts_bytearray_and_memoryview(key_len):
    """bytes-like inputs are normalised to bytes."""
    project = _project_stub()
    key = bytearray(b"\x22" * key_len)
    set_fileinfo_key(project, key)
    stored = get_fileinfo_key(project)
    assert isinstance(stored, bytes)
    assert stored == bytes(key)

    project2 = _project_stub()
    set_fileinfo_key(project2, memoryview(b"\x33" * key_len))
    assert get_fileinfo_key(project2) == b"\x33" * key_len


def test_set_accepts_both_lengths():
    """Sanity: both 32 and 126 are accepted lengths."""
    assert ACCEPTABLE_KEY_LENGTHS == (HMAC_KEY_LENGTH_V2, HMAC_KEY_LENGTH_V1)
    project = _project_stub()
    set_fileinfo_key(project, b"\xaa" * HMAC_KEY_LENGTH_V2)
    assert len(get_fileinfo_key(project)) == HMAC_KEY_LENGTH_V2
    set_fileinfo_key(project, b"\xbb" * HMAC_KEY_LENGTH_V1)
    assert len(get_fileinfo_key(project)) == HMAC_KEY_LENGTH_V1


def test_set_rejects_wrong_length():
    """Keys whose length isn't in ACCEPTABLE_KEY_LENGTHS raise ValueError."""
    project = _project_stub()
    bad_lengths = (0, 1, 16, 31, 33, 64, 125, 127, 128)
    for n in bad_lengths:
        assert n not in ACCEPTABLE_KEY_LENGTHS, (
            f"Test bug: {n} is in ACCEPTABLE_KEY_LENGTHS"
        )
        with pytest.raises(ValueError):
            set_fileinfo_key(project, b"\x00" * n)
    assert get_fileinfo_key(project) is None


def test_set_rejects_non_bytes():
    """Non-bytes inputs raise TypeError, regardless of length."""
    project = _project_stub()
    for bad in ("a" * 32, 12345, [0] * 32, None):
        with pytest.raises(TypeError):
            set_fileinfo_key(project, bad)


@pytest.mark.parametrize("key_len", KEY_LENGTHS)
def test_clear_removes_key(key_len):
    """clear_fileinfo_key removes the binding."""
    project = _project_stub()
    set_fileinfo_key(project, b"\x44" * key_len)
    assert get_fileinfo_key(project) is not None
    clear_fileinfo_key(project)
    assert get_fileinfo_key(project) is None
    # Clearing again is a no-op (no error).
    clear_fileinfo_key(project)


def test_verify_loaded_acd_no_key_raises(tmp_path):
    """verify_loaded_acd without set_fileinfo_key raises a clear error."""
    project = _project_stub()
    dst = tmp_path / "cute.ACD"
    shutil.copy(CUTELOGIX, dst)
    with pytest.raises(IntegrityKeyRequiredError):
        verify_loaded_acd(project, str(dst))


@pytest.mark.parametrize("key_len", KEY_LENGTHS)
def test_verify_loaded_acd_round_trips(tmp_path, key_len):
    """If we patch FileInfo.Dat into the file using key K and call
    verify_loaded_acd with key K registered on the project, verify
    passes. With a different key (same length), it fails. Exercised
    across both algorithms (32-byte modern, 126-byte older)."""
    project = _project_stub()
    key_a = b"\x55" * key_len

    # Copy the reference ACD, patch in a FileInfo.Dat computed under key_a.
    with open(CUTELOGIX, "rb") as fh:
        acd = bytearray(fh.read())
    fi_off = find_fileinfo_offset(bytes(acd))
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key_a)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi

    dst = tmp_path / f"cute_{key_len}.ACD"
    with open(dst, "wb") as fh:
        fh.write(bytes(acd))

    set_fileinfo_key(project, key_a)
    assert verify_loaded_acd(project, str(dst))

    # Re-bind with a different key of the same length -- verification must fail.
    set_fileinfo_key(project, b"\x66" * key_len)
    assert not verify_loaded_acd(project, str(dst))
