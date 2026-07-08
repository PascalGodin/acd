"""Patch embedded .Dat files inside a loaded ACD project.

Currently supports SbRegion.Dat (rung text write-back).

SbRegion.Dat layout (after decompression):
  Header (24 bytes + variable header_buffer):
    [0:4]   format_type          u32_le
    [4:8]   blank_2              u32_le  (0)
    [8:12]  file_length          u32_le  (bounds records region; see note below)
    [12:16] first_record_position u32_le (offset to first record = 24 + len(header_buffer))
    [16:20] blank_3              u32_le
    [20:24] number_records_fafa  u32_le  (count of 0xFAFA records — updated on rebuild)
    [24:]   header_buffer        bytes   (variable padding, copy verbatim)

  Records (from first_record_position to file_length+1):
    [0:2]  identifier  u16_le  (0xFAFA=64250, 0xFDFD=65021, etc.)
    [2:6]  len_record  u32_le  (total record bytes including this 6-byte header)
    [6:]   payload     bytes   (len_record - 6 bytes)

  Trailer (from file_length+1 to EOF):
    FEFE pointer/index regions — copy verbatim, not parsed by record iterator.

  The Kaitai Dat parser reads exactly (file_length - first_record_position + 1) bytes
  for the records sub-stream (i.e., records region = dat_bytes[first_record_pos:file_length+1]).
  file_length is therefore NOT the total file size.

FAFA rung payload layout (when language_type is "Rung NT" or "REGION NT"):
    [0:4]   record_length    u32_le  (= payload_len - 4 = 51 + len_record_buffer)
    [4:6]   sb_regions       u16_le
    [6:10]  object_id        u32_le  ← rung identifier, matched for patching
    [10:51] language_type    bytes[41]  (null-terminated, zero-padded)
    [51:55] len_record_buffer u32_le
    [55:]   record_buffer    bytes   (UTF-16LE encoded rung text, null-terminated)
"""

import gzip
import re
import struct
from typing import Dict

_FAFA = 0xFAFA  # 64250

# Byte offsets within a FAFA rung payload
_OFF_OBJ_ID = 6          # u32_le: rung object_id
_OFF_LANG_TYPE_END = 51  # exclusive end of fixed fields before len_record_buffer
_OFF_LEN_BUF = 51        # u32_le: byte length of record_buffer
_OFF_BUF = 55            # start of UTF-16LE rung text

# language_type values that indicate rung text records
_RUNG_LANG_TYPES = {b"Rung NT", b"REGION NT"}


def _is_rung_record(payload: bytes) -> bool:
    """Return True if this FAFA payload contains rung text."""
    if len(payload) < _OFF_BUF:
        return False
    lang_null = payload[10:51].split(b"\x00")[0]
    return lang_null in _RUNG_LANG_TYPES


def _get_rung_object_id(payload: bytes) -> int:
    return struct.unpack_from("<I", payload, _OFF_OBJ_ID)[0]


def _get_original_rung_text(payload: bytes) -> str:
    """Decode the original rung text (with @HEX@ placeholders) from a payload."""
    len_buf = struct.unpack_from("<I", payload, _OFF_LEN_BUF)[0]
    raw = payload[_OFF_BUF : _OFF_BUF + len_buf]
    return raw.decode("utf-16-le").rstrip("\x00")


def _encode_rung_text(text: str) -> bytes:
    """Encode rung text to UTF-16LE with null terminator."""
    return text.encode("utf-16-le") + b"\x00\x00"


def _restore_tag_refs(new_text: str, orig_text_with_refs: str, id_to_name: Dict[int, str]) -> str:
    """Replace tag names in new_text with @HEX_OBJECT_ID@ placeholders.

    Only substitutes tags that appear as @HEX@ references in orig_text_with_refs
    (the original raw rung text before name resolution).  This prevents false-positive
    substitution of short names into instruction opcodes (e.g. 'X' inside 'XIC').

    Substitutions within the scoped set are done longest-name-first to handle
    cases where one tag name is a prefix of another.
    """
    # Identify which object IDs were referenced in the original rung
    rung_ids = {int(m, 16) for m in re.findall(r"@([A-Za-z0-9]+)@", orig_text_with_refs)}

    # Build rung-scoped name → id map
    scoped = {}
    for oid in rung_ids:
        name = id_to_name.get(oid)
        if name:
            scoped[name] = oid

    # Apply longest-first replacement.
    # Format: 8 hex digits, zero-padded, lowercase -- verified against a real
    # project's SbRegion.Dat (20,710 @HEX@ refs sampled, all exactly this
    # shape, 0 with uppercase letters). Using ":X" (uppercase, no padding)
    # instead produced numerically-equivalent but textually different
    # references, causing an otherwise-identical rung to re-encode as a
    # different byte sequence (and therefore compress to a different size).
    for name in sorted(scoped, key=len, reverse=True):
        if name in new_text:
            new_text = new_text.replace(name, f"@{scoped[name]:08x}@")

    return new_text


def _build_fafa_record(orig_payload: bytes, new_text_bytes: bytes) -> bytes:
    """Reconstruct a FAFA rung record with replaced text, preserving all other fields."""
    new_len = len(new_text_bytes)
    new_payload = (
        struct.pack("<I", 51 + new_len)          # record_length = payload_len - 4
        + orig_payload[4:_OFF_LANG_TYPE_END]     # sb_regions + object_id + language_type
        + struct.pack("<I", new_len)             # len_record_buffer
        + new_text_bytes                         # record_buffer
    )
    return struct.pack("<HI", _FAFA, 6 + len(new_payload)) + new_payload


def patch_sbregion_dat(
    dat_bytes: bytes,
    changes: Dict[int, str],
    id_to_name: Dict[int, str],
) -> bytes:
    """Return new SbRegion.Dat bytes with modified rung text.

    Args:
        dat_bytes: Raw bytes of SbRegion.Dat from _raw_files (may be gzip-compressed).
        changes: Mapping of {rung_object_id: new_rung_text}.  Tag names in new_rung_text
            are plain strings (as they appear in the Python object model / L5X); this
            function re-inserts the @HEX_OBJECT_ID@ placeholders before encoding.
        id_to_name: Mapping of {object_id: comp_name} used to resolve which @HEX@ tokens
            to substitute when writing back.  Available as project._id_to_name after load_acd().

    Returns:
        New SbRegion.Dat bytes, gzip-compressed to match the original container
        convention (every internal .Dat/.Idx file is stored gzip-compressed in a
        real ACD).  Swap into project._raw_files["SbRegion.Dat"] before calling
        save_acd().
    """
    if not changes:
        return dat_bytes

    # Decompress if needed
    if dat_bytes[:2] == b"\x1f\x8b":
        dat_bytes = gzip.decompress(dat_bytes)

    # Parse fixed 24-byte header fields we need
    (format_type, blank_2, _file_length, first_record_pos,
     blank_3, num_fafa) = struct.unpack_from("<IIIIII", dat_bytes, 0)

    header_verbatim = dat_bytes[:first_record_pos]  # preserve everything verbatim

    # The Dat file has FEFE pointer regions appended AFTER the FAFA/FDFD records.
    # file_length bounds the records region:
    #   records region = dat_bytes[first_record_pos : file_length + 1]
    #   trailer        = dat_bytes[file_length + 1 :]  (FEFE pointer data — preserve verbatim)
    records_end = _file_length + 1   # exclusive end of records region
    trailer = dat_bytes[records_end:]

    # Walk records, replacing modified FAFA rung records
    pos = first_record_pos
    new_records = bytearray()
    new_num_fafa = 0

    while pos < records_end:
        identifier = struct.unpack_from("<H", dat_bytes, pos)[0]
        len_record = struct.unpack_from("<I", dat_bytes, pos + 2)[0]
        payload = dat_bytes[pos + 6 : pos + len_record]

        if identifier == _FAFA:
            new_num_fafa += 1
            if _is_rung_record(payload):
                oid = _get_rung_object_id(payload)
                if oid in changes:
                    # Get original @HEX@ text for scoped reference resolution
                    orig_text_with_refs = _get_original_rung_text(payload)
                    new_text = _restore_tag_refs(changes[oid], orig_text_with_refs, id_to_name)
                    new_records.extend(_build_fafa_record(payload, _encode_rung_text(new_text)))
                    pos += len_record
                    continue

        # Copy record verbatim
        new_records.extend(dat_bytes[pos : pos + len_record])
        pos += len_record

    # Rebuild header with updated file_length and number_records_fafa.
    # file_length = first_record_pos + len(new_records) - 1  (matches Kaitai formula).
    new_file_length = first_record_pos + len(new_records) - 1
    new_header = bytearray(header_verbatim)
    struct.pack_into("<I", new_header, 8, new_file_length)   # file_length
    struct.pack_into("<I", new_header, 20, new_num_fafa)     # number_records_fafa

    new_dat = bytes(new_header) + bytes(new_records) + trailer

    # Re-compress to match the original container convention: every internal
    # .Dat/.Idx file (SbRegion.Dat included) is stored gzip-compressed in a
    # real ACD. build_acd_bytes() writes _raw_files verbatim with no
    # compression step of its own, so if we returned the raw/decompressed
    # bytes here, the saved container would store SbRegion.Dat ~12x larger
    # than the original AND -- more importantly -- as a plain (non-gzip)
    # stream, which the real Studio 5000 SDK may not tolerate even though
    # this library's own reader (Unzip) is lenient and sniffs the gzip
    # magic bytes before deciding whether to decompress.
    #
    # Verified against a real project: Rockwell's own encoder uses gzip
    # compresslevel=1 (fastest) with mtime=0 (no embedded timestamp) --
    # with those two params, Python's gzip.compress() output is byte-for-
    # byte identical to the original from offset 10 onward (the entire
    # DEFLATE payload + CRC32 + ISIZE trailer). The only remaining
    # difference is the header's XFL/OS bytes (offsets 8-9), which are
    # purely informational per RFC 1952 and have zero effect on
    # decompression -- patched here anyway for a fully byte-identical
    # round-trip when the content itself is unchanged (Rockwell's encoder
    # writes XFL=0x00, OS=0x0b/NTFS; Python defaults to XFL=0x04, OS=0xff).
    compressed = bytearray(gzip.compress(new_dat, compresslevel=1, mtime=0))
    compressed[8] = 0x00   # XFL
    compressed[9] = 0x0B   # OS = NTFS
    return bytes(compressed)
