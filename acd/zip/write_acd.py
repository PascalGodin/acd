"""Write a Rockwell ACD container file from a dict of raw file bytes.

The ACD container format is footer-based:

    [file data blocks, packed sequentially starting at offset 0]
    [file record table: 528 bytes × num_files]
      [filename: UTF-16LE null-terminated, zero-padded to 520 bytes]
      [file_length: u32_le]
      [file_offset: u32_le  -- absolute offset from start of ACD]
    [footer: num_files u32_le + unknown u32_le]

The first bytes of the ACD are the first bytes of the first embedded file
(typically Version.Log, which starts with 0x0D 0x0A — this is what the
reader checks as its "magic number").
"""

import struct
from pathlib import Path
from typing import Dict, List


def build_acd_bytes(
    files: Dict[str, bytes],
    file_order: List[str] = None,
    footer_unknown: int = 2,
) -> bytes:
    """Pack files into a Rockwell ACD container, returning the bytes.

    Args:
        files: Mapping of filename → raw bytes.
        file_order: Filenames in desired write order.  Defaults to files.keys().
        footer_unknown: The second u32 in the ACD footer.  Preserve the value
            from the original file (accessible via Unzip.header._unknown_two).

    Returns:
        The full ACD container as a bytes object.
    """
    if file_order is None:
        file_order = list(files.keys())

    output = bytearray()
    file_records = []

    # Write file data blocks sequentially, recording each file's offset
    for filename in file_order:
        offset = len(output)
        data = files[filename]
        output.extend(data)
        file_records.append((filename, offset, len(data)))

    # File record table — 528 bytes per entry
    for filename, offset, length in file_records:
        # Filename section: UTF-16LE + null terminator, zero-padded to 520 bytes
        encoded = filename.encode("utf-16-le") + b"\x00\x00"
        name_section = encoded.ljust(520, b"\x00")
        output.extend(name_section)
        output.extend(struct.pack("<II", length, offset))

    # Footer
    output.extend(struct.pack("<II", len(file_records), footer_unknown))

    return bytes(output)


def write_acd(
    files: Dict[str, bytes],
    output_path,
    file_order: List[str] = None,
    footer_unknown: int = 2,
) -> None:
    """Pack files into a Rockwell ACD container and write to disk.

    Thin wrapper around `build_acd_bytes`; preserved as the public API.

    Args:
        files: Mapping of filename → raw bytes.
        output_path: Destination .ACD path.
        file_order: Filenames in desired write order.  Defaults to files.keys().
        footer_unknown: The second u32 in the ACD footer.  Preserve the value
            from the original file (accessible via Unzip.header._unknown_two).
    """
    Path(output_path).write_bytes(
        build_acd_bytes(files, file_order=file_order, footer_unknown=footer_unknown)
    )
