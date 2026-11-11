#!/usr/bin/env python3
"""Cross-checks bep52_ref.py against libtorrent's torrent creator.

Needs the libtorrent Python bindings (`pip install libtorrent`, 2.x). Builds a
small tree shaped like a JETCARP object store (`objects/sha256/ab/<64 hex>.ext`)
with awkward sizes (0 bytes, under a block, exactly a block, exactly a piece,
one byte either side of both, several pieces), then for several piece lengths
checks that:

  * the hybrid and the v2-only torrents made by bep52_ref.py have the same info
    hashes as libtorrent's for the same folder;
  * libtorrent loads the reference's .torrent and reports the same per-file
    `pieces root`s;
  * bep52_ref.py `check` accepts libtorrent's .torrent (piece layers hash to
    the roots, v1 and v2 views agree);
  * the reference's output is byte-identical when the input list is reversed.

Usage: crosscheck_libtorrent.py [--keep DIR]

Written with an AI model (Claude) for the JETCARP project; see STATUS.md.
"""

import argparse
import hashlib
import os
import random
import shutil
import sys
import tempfile

import warnings

import libtorrent as lt

# The file_storage creation API is deprecated in 2.1 but is the one that
# exists in every 2.x release.
warnings.filterwarnings("ignore", category=DeprecationWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bep52_ref  # noqa: E402

BLOCK = 16 * 1024


def make_tree(root: str, piece_lengths) -> int:
    rng = random.Random(52)
    sizes = [0, 1, 100, BLOCK - 1, BLOCK, BLOCK + 1, 3 * BLOCK + 5]
    for pl in piece_lengths:
        sizes += [pl - 1, pl, pl + 1, 2 * pl, 2 * pl + BLOCK, 5 * pl + 12345]
    sizes += [0, 7 * 1024 * 1024 + 3]
    total = 0
    for i, size in enumerate(sizes):
        data = rng.randbytes(size)
        digest = hashlib.sha256(data + i.to_bytes(4, "big")).hexdigest()
        ext = ".json" if size < BLOCK else ".parquet"
        folder = os.path.join(root, "objects", "sha256", digest[:2])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, digest + ext), "wb") as f:
            f.write(data)
        total += size
    with open(os.path.join(root, "manifest.json"), "wb") as f:
        f.write(b'{"version": 1}\n')
    return len(sizes) + 1


def lt_create(folder: str, piece_length: int, flags) -> bytes:
    fs = lt.file_storage()
    lt.add_files(fs, folder)
    ct = lt.create_torrent(fs, piece_size=piece_length, flags=flags)
    lt.set_piece_hashes(ct, os.path.dirname(folder))
    return lt.bencode(ct.generate())


def lt_roots(info: "lt.torrent_info"):
    fs = info.files()
    out = {}
    for i in range(fs.num_files()):
        if fs.file_flags(i) & lt.file_storage.flag_pad_file:
            continue
        path = fs.file_path(i).replace(os.sep, "/").split("/", 1)[1]
        root = fs.root(i).to_bytes()
        out[path] = root if fs.file_size(i) else None
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep", help="build the tree here and keep it")
    args = p.parse_args()
    base = args.keep or tempfile.mkdtemp(prefix="bep52-crosscheck-")
    folder = os.path.join(base, "store")
    piece_lengths = [BLOCK, 4 * BLOCK, 16 * BLOCK]
    failures = 0

    def expect(what: str, ok: bool, detail: str = ""):
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what + (f": {detail}" if detail and not ok else ""))
        failures += not ok

    try:
        if os.path.exists(folder):
            shutil.rmtree(folder)
        n = make_tree(folder, piece_lengths)
        print(f"libtorrent {lt.version}, {n} files in {folder}")
        files = bep52_ref.walk(folder)
        for pl in piece_lengths:
            for label, hybrid, flags in (
                ("hybrid", True, 0),
                ("v2-only", False, lt.create_torrent.v2_only),
            ):
                tag = f"piece length {pl:>7}, {label}"
                ref = bep52_ref.create(files, "store", pl, hybrid=hybrid)
                again = bep52_ref.create(list(reversed(files)), "store", pl, hybrid=hybrid)
                expect(f"{tag}: same bytes for another input order", ref["torrent"] == again["torrent"])

                theirs = lt_create(folder, pl, flags)
                their_info = lt.torrent_info(theirs)
                their_hashes = their_info.info_hashes()
                expect(
                    f"{tag}: v2 info hash equals libtorrent's",
                    their_hashes.v2.to_bytes() == ref["info_hash_v2"],
                    f"{their_hashes.v2} != {ref['info_hash_v2'].hex()}",
                )
                if hybrid:
                    expect(
                        f"{tag}: v1 info hash equals libtorrent's",
                        their_hashes.v1.to_bytes() == ref["info_hash_v1"],
                        f"{their_hashes.v1} != {ref['info_hash_v1'].hex()}",
                    )
                problems = bep52_ref.check(theirs)
                expect(f"{tag}: reference check accepts libtorrent's torrent", not problems, "; ".join(problems))

                try:
                    loaded = lt.torrent_info(ref["torrent"])
                    roots = lt_roots(loaded)
                    mine = {"/".join(path): root for path, _len, root in ref["roots"]}
                    expect(f"{tag}: libtorrent loads the reference torrent, same roots", roots == mine)
                except RuntimeError as e:
                    expect(f"{tag}: libtorrent loads the reference torrent", False, str(e))

        # One file: alone (a single-file torrent) and alone in a folder (a
        # multi-file torrent of one file; libtorrent does not pad it).
        lone = os.path.join(base, "lone.bin")
        with open(lone, "wb") as f:
            f.write(random.Random(1).randbytes(100_000))
        one = os.path.join(base, "one")
        os.makedirs(one, exist_ok=True)
        shutil.copy(lone, os.path.join(one, "a.bin"))
        for pl in piece_lengths:
            for label, path, name, listed in (
                ("single file", lone, "lone.bin", [(("lone.bin",), lone)]),
                ("folder of one file", one, "one", bep52_ref.walk(one)),
            ):
                ref = bep52_ref.create(listed, name, pl)
                their_hashes = lt.torrent_info(lt_create(path, pl, 0)).info_hashes()
                expect(
                    f"piece length {pl:>7}, hybrid, {label}: both info hashes equal libtorrent's",
                    their_hashes.v1.to_bytes() == ref["info_hash_v1"]
                    and their_hashes.v2.to_bytes() == ref["info_hash_v2"],
                )

        # A folder "a" next to "a.b" or "a.bin": by path component "a" sorts
        # first (the order of the file tree); by whole path "a.b/x" and "a.bin"
        # sort before "a/b". The torrent must use the first, and libtorrent
        # must accept it. (libtorrent 2.1.1's own creator uses the second for
        # the v1 file list and then rejects its own hybrid torrent.)
        tricky = os.path.join(base, "tricky")
        for i, rel in enumerate(("a.b/x.bin", "a/b.bin", "a-b/y.bin", "a b/z.bin", "a.bin")):
            full = os.path.join(tricky, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(random.Random(i).randbytes(40_000))
        ref = bep52_ref.create(bep52_ref.walk(tricky), "tricky", 2 * BLOCK)
        order = ["/".join(path) for path, _len, _root in ref["roots"]]
        expect(
            "folder names that continue another: file tree order",
            order == ["a/b.bin", "a b/z.bin", "a-b/y.bin", "a.b/x.bin", "a.bin"],
            str(order),
        )
        try:
            loaded = lt.torrent_info(ref["torrent"])
            hashes = loaded.info_hashes()
            expect(
                "folder names that continue another: libtorrent loads it, same hashes",
                hashes.v1.to_bytes() == ref["info_hash_v1"]
                and hashes.v2.to_bytes() == ref["info_hash_v2"],
            )
        except RuntimeError as e:
            expect("folder names that continue another: libtorrent loads it", False, str(e))
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    print("all checks passed" if not failures else f"{failures} check(s) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
