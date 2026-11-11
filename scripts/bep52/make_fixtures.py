#!/usr/bin/env python3
"""Makes the torrent fixtures in crates/create_torrent/tests/fixtures.

The fixtures are .torrent files made by libtorrent's torrent creator (the
reference implementation of BEP 52) from small files whose bytes come from a
formula, so that tests can make the same files again without committing them.
The Rust tests in crates/create_torrent create a torrent of the same files
and expect the very same bytes.

Needs the libtorrent Python bindings: `pip install libtorrent` (2.x). The
committed fixtures were made with libtorrent 2.1.1.

    python3 scripts/bep52/make_fixtures.py

Files written, per data set and variant:
    <set>.<hybrid|v2>.<piece length>.torrent
and one list of the data sets' files, `files.txt`, with lines
    <set> <seed> <size> <path in the torrent>

Written with an AI model (Claude) for the JETCARP project; see STATUS.md.
"""

import os
import shutil
import sys
import tempfile
import warnings

import libtorrent as lt

warnings.filterwarnings("ignore", category=DeprecationWarning)

BLOCK = 16 * 1024
OUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "crates", "create_torrent", "tests", "fixtures",
)
CREATION_DATE = 1_700_000_000
CREATED_BY = "libtorrent (fixture)"


def content(seed: int, size: int) -> bytes:
    """Byte i is the top byte of (i + seed) * 2654435761 mod 2^32."""
    return bytes((((i + seed) * 2654435761) & 0xFFFFFFFF) >> 24 for i in range(size))


# set name -> (torrent name, [(path in the torrent, size)], piece lengths)
P = 2 * BLOCK  # the smallest piece length with more than one block per piece
SETS = {
    # Sizes around block and piece boundaries, empty files, folders, and names
    # whose byte order differs from their "natural" order.
    #
    # Not here: a folder next to a file or folder whose name continues it with
    # a character below "/", such as "a" next to "a.b" or "a.bin". libtorrent
    # 2.1.1's creator
    # orders those by whole path instead of by path component, and then
    # refuses to load its own hybrid torrent ("the v1 and v2 file metadata
    # does not match"). crosscheck_libtorrent.py covers that case the other
    # way round: libtorrent loads the torrent the reference makes.
    "store": (
        "store",
        [
            ("manifest.json", 15),
            ("B.bin", 1),
            ("a.bin", BLOCK - 1),
            ("ab/x.bin", BLOCK),
            ("c/b.bin", BLOCK + 1),
            ("c/empty", 0),
            ("objects/sha256/00/00aa.parquet", P - 1),
            ("objects/sha256/00/00ab.parquet", P),
            ("objects/sha256/0f/0f01.parquet", P + 1),
            ("objects/sha256/0f/0f02.parquet", 3 * P),
            ("objects/sha256/a0/a001.parquet", 3 * P + BLOCK + 7),
            ("objects/sha256/a0/a002.parquet", 9 * P + 12345),
            ("objects/sha256/a0/empty.json", 0),
            ("été/z.bin", 5 * BLOCK),
            ("zz-last.bin", 2 * P + 1),
        ],
        [P, 4 * BLOCK],
    ),
    # A single-file torrent: the file is the torrent.
    "lone": ("lone.bin", [("lone.bin", 3 * P + 100)], [P]),
    # A folder with one file: a multi-file torrent, and no padding.
    "one": ("one", [("a.bin", 3 * P + 100)], [P]),
    # Empty files first and last, around a file that needs padding.
    "empty_first": ("empty_first", [("a", 0), ("b.bin", P + 5)], [P]),
    "empty_last": ("empty_last", [("a.bin", P + 5), ("z", 0)], [P]),
    # Every file ends on a piece boundary: no padding files at all.
    "aligned": ("aligned", [("a.bin", P), ("b.bin", 2 * P)], [P]),
    # Two files with the same bytes share a pieces root and a piece layer.
    "twins": ("twins", [("a.bin", 2 * P + 9), ("b.bin", 2 * P + 9)], [P]),
}
# Files of the "twins" set get the same seed, all others their own.
SAME_SEED = {"twins"}


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    for name in os.listdir(OUT):
        if name.endswith(".torrent") or name == "files.txt":
            os.remove(os.path.join(OUT, name))
    work = tempfile.mkdtemp(prefix="bep52-fixtures-")
    lines = []
    try:
        seed = 0
        for set_name, (torrent_name, files, piece_lengths) in SETS.items():
            base = os.path.join(work, set_name)
            os.makedirs(base)
            single = len(files) == 1 and files[0][0] == torrent_name
            root = base if single else os.path.join(base, torrent_name)
            for path, size in files:
                seed = 1 if set_name in SAME_SEED else seed + 1000
                full = os.path.join(root, *path.split("/"))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as f:
                    f.write(content(seed, size))
                lines.append(f"{set_name} {seed} {size} {path}")
            for piece_length in piece_lengths:
                for label, flags in (("hybrid", 0), ("v2", lt.create_torrent.v2_only)):
                    fs = lt.file_storage()
                    lt.add_files(fs, os.path.join(base, torrent_name))
                    ct = lt.create_torrent(fs, piece_size=piece_length, flags=flags)
                    ct.set_creation_date(CREATION_DATE)
                    ct.set_creator(CREATED_BY)
                    lt.set_piece_hashes(ct, base)
                    data = lt.bencode(ct.generate())
                    lt.torrent_info(data)  # must load
                    out = f"{set_name}.{label}.{piece_length}.torrent"
                    with open(os.path.join(OUT, out), "wb") as f:
                        f.write(data)
                    print(f"{out}: {len(data)} bytes")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with open(os.path.join(OUT, "files.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"libtorrent {lt.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
