"""Read the JSON members of an uploaded export without trusting the archive.

The zip is never extracted: members are read into memory one at a time, only if
their name ends in .json, with caps on the number of entries, on each member's and
the total uncompressed size, and on the compression ratio (zip bombs). The sizes in
the zip's directory are not trusted: the caps are enforced on the bytes actually
read. A name with an absolute path or `..` rejects the whole archive, since no real
export has one.
"""

from __future__ import annotations

import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from portal.imports.model import ExportError

MiB = 1024 * 1024
_CHUNK = MiB
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")


@dataclass(frozen=True)
class Limits:
    max_entries: int = 100_000  # ChatGPT exports hold every image: allow many
    max_member_bytes: int = 1024 * MiB
    max_total_bytes: int = 2048 * MiB
    max_ratio: float = 200.0  # JSON compresses ~10x; bombs compress ~1000x
    ratio_floor_bytes: int = MiB  # tiny members may compress oddly; judge ratio above this


DEFAULT_LIMITS = Limits()


def is_zip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(4) in _ZIP_MAGIC


def unsafe_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name:
        return True
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return True
    return ".." in PurePosixPath(name).parts


def _read_capped(
    fh: IO[bytes], *, cap: int, compressed: int | None, limits: Limits, label: str
) -> bytes:
    data = bytearray()
    while chunk := fh.read(_CHUNK):
        data += chunk
        if len(data) > cap:
            raise ExportError(f"{label} is larger than the import limit")
        if (
            compressed is not None
            and len(data) > limits.ratio_floor_bytes
            and len(data) / max(compressed, 1) > limits.max_ratio
        ):
            raise ExportError(f"{label} expands suspiciously (possible zip bomb); not imported")
    return bytes(data)


def json_members(
    path: Path, warnings: Counter[str], limits: Limits = DEFAULT_LIMITS
) -> Iterator[tuple[str, bytes]]:
    """Yield (name, raw bytes) for each JSON member, or the file itself if it is bare JSON."""
    if not is_zip(path):
        with path.open("rb") as fh:
            yield (
                path.name,
                _read_capped(
                    fh,
                    cap=limits.max_member_bytes,
                    compressed=None,
                    limits=limits,
                    label="The file",
                ),
            )
        return
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExportError("The file is not a readable zip archive.") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise ExportError(f"The zip has more than {limits.max_entries} entries.")
        if any(unsafe_name(info.filename) for info in infos):
            raise ExportError("The zip contains unsafe file names; it was not imported.")
        total = 0
        for info in infos:
            name = info.filename
            if info.is_dir() or not name.lower().endswith(".json") or "__MACOSX/" in name:
                continue
            if info.flag_bits & 0x1:
                warnings["encrypted file in the zip skipped"] += 1
                continue
            try:
                with archive.open(info) as fh:
                    data = _read_capped(
                        fh,
                        cap=min(limits.max_member_bytes, limits.max_total_bytes - total),
                        compressed=info.compress_size,
                        limits=limits,
                        label=f"'{PurePosixPath(name).name}'",
                    )
            except (zipfile.BadZipFile, zlib.error, OSError, EOFError, NotImplementedError):
                warnings["unreadable file in the zip skipped"] += 1
                continue
            total += len(data)
            yield name, data
