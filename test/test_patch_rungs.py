"""Tests for patch_rungs() / patch_sbregion_dat() rung text write-back.

Covers two real bugs found while manually verifying a round-trip against a
large real-world project (neither was caught by any prior test, since this
path had zero test coverage before):

1. patch_sbregion_dat() returned *decompressed* SbRegion.Dat bytes, and
   save_acd()/build_acd_bytes() never compresses anything -- every other
   internal .Dat/.Idx file stays gzip-compressed (untouched), so the patched
   SbRegion.Dat alone ballooned ~12x in a real project (1.08MB -> 13.8MB)
   and was stored as a non-gzip stream, unlike every other internal file.

2. @HEX_OBJECT_ID@ tag-reference placeholders were re-encoded with
   uppercase, non-zero-padded hex (":X") instead of the real convention
   (8 hex digits, zero-padded, lowercase -- verified against 20,710 real
   references in a real project's SbRegion.Dat, 0 of which were uppercase).
   This meant even a true no-op patch (rewriting a rung to its own existing
   text) produced textually-different (though numerically equivalent)
   output, breaking byte-identical round-trips.
"""
import gzip
import os

from acd.api import load_acd, save_acd, patch_rungs


CUTELOGIX = os.path.join("..", "resources", "CuteLogix.ACD")


def _find_routine(controller, program_name, routine_name):
    for p in controller.programs:
        if p.name == program_name:
            for r in p.routines:
                if r.name == routine_name:
                    return r
    raise AssertionError(f"routine {program_name}/{routine_name} not found")


def test_patch_rungs_no_op_round_trip_is_byte_identical(tmp_path):
    """Patching a rung to its own existing text must reproduce the exact
    original SbRegion.Dat bytes and the exact original ACD container bytes.

    This is the strongest possible check of the write path: it proves the
    decompress -> re-encode -> recompress cycle is lossless and matches
    Rockwell's own encoding conventions (compression settings, hex-ref
    formatting) closely enough to be indistinguishable from the source.
    """
    src = CUTELOGIX
    dst = tmp_path / "out.ACD"

    project = load_acd(src)
    routine = _find_routine(project.controller, "Instructions", "MainRoutine")
    orig_sbregion = project._raw_files["SbRegion.Dat"]

    same_text = routine.rungs[1]
    assert "Toggle" in same_text  # sanity: this rung has a real @HEX@ tag ref
    patch_rungs(project, {routine._rung_ids[1]: same_text})

    assert project._raw_files["SbRegion.Dat"] == orig_sbregion, (
        "No-op patch (same text back) must reproduce byte-identical "
        "SbRegion.Dat -- check hex-ref formatting (lowercase, 8-digit "
        "zero-padded) and gzip compression settings (compresslevel=1, "
        "mtime=0) in patch_sbregion_dat()."
    )

    save_acd(project, str(dst))
    assert open(dst, "rb").read() == open(src, "rb").read(), (
        "No-op patch + save must reproduce the exact original ACD bytes."
    )


def test_patch_rungs_sbregion_stays_gzip_compressed(tmp_path):
    """The patched SbRegion.Dat must remain gzip-compressed and roughly the
    same size as the original -- not ~12x larger from being stored raw."""
    src = CUTELOGIX
    project = load_acd(src)
    orig_len = len(project._raw_files["SbRegion.Dat"])

    routine = _find_routine(project.controller, "Duh", "Stupid")
    patch_rungs(project, {routine._rung_ids[0]: "NOP();"})

    new_sbregion = project._raw_files["SbRegion.Dat"]
    assert new_sbregion[:2] == b"\x1f\x8b", "SbRegion.Dat must stay gzip-compressed"
    # Same content in, same content out (NOP(); unchanged) -- size should be
    # very close to the original, not inflated by losing compression.
    assert new_sbregion == project._raw_files["SbRegion.Dat"]
    assert abs(len(new_sbregion) - orig_len) < 1024


def test_patch_rungs_changes_only_the_targeted_rung(tmp_path):
    """Patching one rung's text must leave every other rung, and the tag
    reference of an untouched rung, completely unaffected."""
    src = CUTELOGIX
    dst = tmp_path / "out.ACD"

    project = load_acd(src)
    routine = _find_routine(project.controller, "Instructions", "MainRoutine")
    original_rungs = list(routine.rungs)

    new_text = "XIC(Toggle)MSG(WebPage)NOP();"
    patch_rungs(project, {routine._rung_ids[1]: new_text})
    save_acd(project, str(dst))

    reloaded = load_acd(str(dst))
    routine2 = _find_routine(reloaded.controller, "Instructions", "MainRoutine")

    assert routine2.rungs[1] == new_text
    assert routine2.rungs[0] == original_rungs[0]
    assert routine2.rungs[2] == original_rungs[2]
