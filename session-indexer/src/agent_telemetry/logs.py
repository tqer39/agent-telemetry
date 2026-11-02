"""Stream compressed transcripts without writing decompressed copies."""

import gzip
import zlib
from pathlib import Path

try:
    from compression import zstd
except ImportError:  # Python < 3.14, or a build without Zstandard support.
    zstd = None


class ZstdUnavailableError(Exception):
    pass


READ_ERRORS = (OSError, EOFError, zlib.error) + ((zstd.ZstdError,) if zstd else ())
SUFFIXES = (".jsonl", ".jsonl.zst", ".jsonl.gz")


def logical_path(path):
    return path.with_suffix("") if path.suffix in (".zst", ".gz") else path


def candidates(root, diagnostics):
    """Group alternative representations, preferring an active plaintext file."""
    grouped = {}
    if not root.is_dir():
        return []
    for path in sorted(root.rglob("*.jsonl*")):
        if path.is_symlink() or not path.is_file():
            continue
        if not any(path.name.endswith(suffix) for suffix in SUFFIXES):
            diagnostics["unsupported_files"] += 1
            continue
        grouped.setdefault(logical_path(path), []).append(path)
    return [sorted(paths, key=lambda p: (p.suffix != ".jsonl", str(p))) for paths in grouped.values()]


def open_log(path):
    path = Path(path)
    if path.suffix == ".zst":
        if zstd is None:
            raise ZstdUnavailableError
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")
