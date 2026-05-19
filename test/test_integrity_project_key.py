"""Tests for the project-level FileInfo.Dat key binding.

These tests use a `types.SimpleNamespace` as a stand-in for the
project object — `set_fileinfo_key` doesn't care what type its first
argument is, only that it can hold an attribute. That keeps these
tests fast and free of the heavyweight `ExportL5x` import path used
in the API integration test (Commit 5).

The `verify_loaded_acd` round-trip test patches a fake FileInfo.Dat
into a temp file using the same test key, then asserts verification
passes.
"""
import os
import shutil
import types

import pytest

from acd.integrity import (
    HMAC_KEY_LENGTH,
    FILEINFO_LENGTH,
    IntegrityKeyRequiredError,
    clear_fileinfo_key,
    compute_fileinfo,
    find_fileinfo_offset,
    get_fileinfo_key,
    set_fileinfo_key,
    verify_loaded_acd,
)


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")


def _project_stub():
    return types.SimpleNamespace()


def test_set_get_round_trip():
    """A key stored via set_fileinfo_key comes back via get_fileinfo_key."""
    project = _project_stub()
    assert get_fileinfo_key(project) is None
    key = b"\x11" * HMAC_KEY_LENGTH
    set_fileinfo_key(project, key)
    assert get_fileinfo_key(project) == key


def test_set_accepts_bytearray_and_memoryview():
    """bytes-like inputs are normalised to bytes."""
    project = _project_stub()
    key = bytearray(b"\x22" * HMAC_KEY_LENGTH)
    set_fileinfo_key(project, key)
    stored = get_fileinfo_key(project)
    assert isinstance(stored, bytes)
    assert stored == bytes(key)

    project2 = _project_stub()
    set_fileinfo_key(project2, memoryview(b"\x33" * HMAC_KEY_LENGTH))
    assert get_fileinfo_key(project2) == b"\x33" * HMAC_KEY_LENGTH


def test_set_rejects_wrong_length():
    """Keys that aren't exactly 32 bytes raise ValueError."""
    project = _project_stub()
    for bad in (b"", b"x" * 16, b"y" * 31, b"z" * 33, b"w" * 64):
        with pytest.raises(ValueError):
            set_fileinfo_key(project, bad)
    assert get_fileinfo_key(project) is None


def test_set_rejects_non_bytes():
    """Non-bytes inputs raise TypeError."""
    project = _project_stub()
    for bad in ("a" * HMAC_KEY_LENGTH, 12345, [0] * HMAC_KEY_LENGTH, None):
        with pytest.raises(TypeError):
            set_fileinfo_key(project, bad)


def test_clear_removes_key():
    """clear_fileinfo_key removes the binding."""
    project = _project_stub()
    set_fileinfo_key(project, b"\x44" * HMAC_KEY_LENGTH)
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


def test_verify_loaded_acd_round_trips(tmp_path):
    """If we patch FileInfo.Dat into the file using key K and call
    verify_loaded_acd with key K registered on the project, verify
    passes. With a different key, it fails."""
    project = _project_stub()
    key_a = b"\x55" * HMAC_KEY_LENGTH

    # Copy the reference ACD, patch in a FileInfo.Dat computed under key_a.
    with open(CUTELOGIX, "rb") as fh:
        acd = bytearray(fh.read())
    fi_off = find_fileinfo_offset(bytes(acd))
    new_fi = compute_fileinfo(bytes(acd), fi_off, key=key_a)
    acd[fi_off : fi_off + FILEINFO_LENGTH] = new_fi

    dst = tmp_path / "cute_keyA.ACD"
    with open(dst, "wb") as fh:
        fh.write(bytes(acd))

    set_fileinfo_key(project, key_a)
    assert verify_loaded_acd(project, str(dst))

    # Re-bind with a different key — verification must fail.
    set_fileinfo_key(project, b"\x66" * HMAC_KEY_LENGTH)
    assert not verify_loaded_acd(project, str(dst))
