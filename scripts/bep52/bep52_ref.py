#!/usr/bin/env python3
"""Independent reference for BitTorrent v2 (BEP 52) and hybrid torrents.

Standard library only. It exists to cross-check other implementations (rqbit,
libtorrent): it computes each file's `pieces root`, the `piece layers`, the
info dictionary and both info hashes straight from the text of BEP 52 and
BEP 47, sharing no code with any client.

It is written to be read next to the BEP rather than to be fast: expect about
100 to 300 MiB/s.

Deterministic by construction: the output depends only on the (torrent path,
file content) pairs, the name, the piece length and the options given. Files
are put in the order of the bencoded `file tree` (raw bytes of each path
component), so the order of the input list does not matter. No `creation date`
and no `created by` are written.

Usage:
    bep52_ref.py roots  FOLDER --piece-length N
        print "<pieces root hex>  <length>  <path>" per file (empty files: "-")
    bep52_ref.py create FOLDER --piece-length N [--name NAME] [--v2-only]
                        [--tracker URL]... [--web-seed URL]... [--comment TEXT]
                        [-o OUT.torrent]
        write a hybrid (or v2-only) torrent, print the info hashes and magnet
    bep52_ref.py check  FILE.torrent
        recompute every piece layer's root and compare it with the file tree

Written with an AI model (Claude) for the JETCARP project; see STATUS.md.
"""

import argparse
import hashlib
import os
import sys

BLOCK = 16 * 1024
ZERO = bytes(32)


# ---------------------------------------------------------------- bencode


def bencode(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, str):
        return bencode(x.encode("utf-8"))
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        items = sorted(
            (k.encode("utf-8") if isinstance(k, str) else k, v) for k, v in x.items()
        )
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(type(x))


def bdecode(data: bytes):
    """Returns (value, raw bytes of the top-level `info` value or None)."""
    info_span = [None]

    def parse(i, depth, key=None):
        c = data[i : i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1 : j]), j + 1
        if c == b"l":
            i += 1
            out = []
            while data[i : i + 1] != b"e":
                v, i = parse(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i : i + 1] != b"e":
                k, i = parse(i, depth + 1)
                start = i
                v, i = parse(i, depth + 1, k)
                if depth == 0 and k == b"info":
                    info_span[0] = (start, i)
                out[k] = v
            return out, i + 1
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1 : j + 1 + n], j + 1 + n

    value, end = parse(0, 0)
    if end != len(data):
        raise ValueError("trailing bytes after the torrent")
    raw = data[info_span[0][0] : info_span[0][1]] if info_span[0] else None
    return value, raw


# ---------------------------------------------------------------- merkle


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def merkle_root(hashes, pad: bytes) -> bytes:
    """Root of a balanced tree over `hashes`, padded with `pad` to a power of two."""
    layer = list(hashes)
    width = 1
    while width < len(layer):
        width *= 2
    layer += [pad] * (width - len(layer))
    while len(layer) > 1:
        layer = [sha256(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def file_hashes(path: str, length: int, piece_length: int):
    """(pieces root, piece layer or None) of a non-empty file, per BEP 52.

    Leaves are SHA-256 hashes of 16 KiB blocks. "The remaining leaf hashes
    beyond the end of the file required to construct upper layers of the
    merkle tree are set to zero." A file no larger than a piece has no piece
    layer: its root is the root over its own blocks.
    """
    blocks_per_piece = piece_length // BLOCK
    if length <= piece_length:
        leaves = []
        with open(path, "rb") as f:
            while chunk := f.read(BLOCK):
                leaves.append(sha256(chunk))
        if len(leaves) != -(-length // BLOCK):
            raise ValueError(f"{path} changed size while being read")
        return merkle_root(leaves, ZERO), None

    layer = []
    with open(path, "rb") as f:
        while piece := f.read(piece_length):
            leaves = [sha256(piece[i : i + BLOCK]) for i in range(0, len(piece), BLOCK)]
            # A short last piece is padded with zero leaves to a whole piece.
            leaves += [ZERO] * (blocks_per_piece - len(leaves))
            layer.append(merkle_root(leaves, ZERO))
    if len(layer) != -(-length // piece_length):
        raise ValueError(f"{path} changed size while being read")
    # Above the piece layer, the padding is the root of a piece of zero leaves.
    pad = merkle_root([ZERO] * blocks_per_piece, ZERO)
    return merkle_root(layer, pad), layer


# ---------------------------------------------------------------- creation


def check_piece_length(piece_length: int):
    if piece_length < BLOCK or piece_length & (piece_length - 1):
        raise ValueError("the piece length must be a power of two of at least 16 KiB")


def create(
    files,
    name: str,
    piece_length: int,
    *,
    hybrid: bool = True,
    trackers=(),
    web_seeds=(),
    comment=None,
):
    """`files` is an iterable of (path in the torrent as a tuple of str, local path).

    Returns a dict with `torrent` (bytes), `info_hash_v1` (bytes or None),
    `info_hash_v2` (bytes), `magnet` (str) and `roots`: a list of
    (path tuple, length, pieces root or None) in torrent order.
    """
    check_piece_length(piece_length)
    entries = []
    for tpath, local in files:
        parts = tuple(p.encode("utf-8") for p in tpath)
        if not parts or any(p in (b"", b".", b"..") or b"/" in p for p in parts):
            raise ValueError(f"invalid path in the torrent: {tpath!r}")
        entries.append((parts, local, os.path.getsize(local)))
    # Torrent order: the order of the keys of the bencoded file tree.
    entries.sort(key=lambda e: e[0])
    if len({e[0] for e in entries}) != len(entries):
        raise ValueError("two files have the same path in the torrent")

    tree = {}
    layers = {}
    roots = []
    v1_files = []
    sha1_pieces = []
    piece = hashlib.sha1()
    in_piece = 0

    def v1_feed(data: bytes):
        nonlocal piece, in_piece
        while data:
            take = min(len(data), piece_length - in_piece)
            piece.update(data[:take])
            in_piece += take
            data = data[take:]
            if in_piece == piece_length:
                sha1_pieces.append(piece.digest())
                piece, in_piece = hashlib.sha1(), 0

    # A torrent of one file at the top level is a single-file torrent: its v1
    # view is `length`, and nothing follows the file, so it has no padding.
    single = len(entries) == 1 and len(entries[0][0]) == 1 and entries[0][0][0] == name.encode()

    for index, (parts, local, length) in enumerate(entries):
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if b"" in node:
                raise ValueError(f"{parts!r}: a file is used as a folder")
        if parts[-1] in node:
            raise ValueError(f"{parts!r}: a folder is used as a file")
        leaf = {b"length": length}
        root = None
        if length:
            root, layer = file_hashes(local, length, piece_length)
            leaf[b"pieces root"] = root
            if layer is not None:
                layers[root] = b"".join(layer)
        node[parts[-1]] = {b"": leaf}
        roots.append((tuple(p.decode() for p in parts), length, root))

        if hybrid:
            v1_files.append({b"length": length, b"path": list(parts)})
            with open(local, "rb") as f:
                while chunk := f.read(1 << 20):
                    v1_feed(chunk)
            # BEP 47 padding so the next file starts on a piece boundary. BEP 52
            # does not say whether the last file is padded; this follows
            # libtorrent's default (no `canonical_files_no_tail_padding`): the
            # last file is padded too, unless the torrent has only one file.
            tail = length % piece_length
            if len(entries) > 1 and tail:
                pad = piece_length - tail
                v1_files.append(
                    {b"attr": b"p", b"length": pad, b"path": [b".pad", b"%d" % pad]}
                )
                v1_feed(bytes(pad))

    info = {
        b"file tree": tree,
        b"meta version": 2,
        b"name": name.encode("utf-8"),
        b"piece length": piece_length,
    }
    if hybrid:
        if in_piece:
            sha1_pieces.append(piece.digest())
        info[b"pieces"] = b"".join(sha1_pieces)
        if single:
            info[b"length"] = entries[0][2]
        else:
            info[b"files"] = v1_files

    raw_info = bencode(info)
    v1 = hashlib.sha1(raw_info).digest() if hybrid else None
    v2 = sha256(raw_info)

    trackers = [t for t in trackers if t]
    torrent = {b"info": info, b"piece layers": layers}
    if trackers:
        torrent[b"announce"] = trackers[0]
    if len(trackers) > 1:
        torrent[b"announce-list"] = [[t] for t in trackers]
    web_seeds = list(web_seeds)
    if len(web_seeds) == 1:
        torrent[b"url-list"] = web_seeds[0]
    elif web_seeds:
        torrent[b"url-list"] = web_seeds
    if comment:
        torrent[b"comment"] = comment

    from urllib.parse import quote

    xt = []
    if v1:
        xt.append("xt=urn:btih:" + v1.hex())
    xt.append("xt=urn:btmh:1220" + v2.hex())
    magnet = "magnet:?" + "&".join(
        xt
        + ["dn=" + quote(name)]
        + ["tr=" + quote(t, safe="") for t in trackers]
        + ["ws=" + quote(w, safe="") for w in web_seeds]
    )
    return {
        "torrent": bencode(torrent),
        "info_hash_v1": v1,
        "info_hash_v2": v2,
        "magnet": magnet,
        "roots": roots,
    }


def walk(folder: str):
    """(path tuple, local path) of every regular file under `folder`."""
    out = []
    for dirpath, _dirs, names in os.walk(folder):
        rel = os.path.relpath(dirpath, folder)
        prefix = () if rel == "." else tuple(rel.split(os.sep))
        for n in names:
            out.append((prefix + (n,), os.path.join(dirpath, n)))
    return out


# ---------------------------------------------------------------- checking


def iter_tree(tree, prefix=()):
    for key in sorted(tree):
        node = tree[key]
        if set(node) == {b""}:
            yield prefix + (key,), node[b""]
        else:
            yield from iter_tree(node, prefix + (key,))


def check(torrent_bytes: bytes):
    """Checks the v2 part of a .torrent. Returns a list of problems (empty: good)."""
    meta, raw_info = bdecode(torrent_bytes)
    info = meta[b"info"]
    problems = []
    if info.get(b"meta version") != 2:
        return ["not a v2 torrent (meta version is not 2)"]
    if bencode(info) != raw_info:
        problems.append("the info dictionary is not in canonical bencoding")
    piece_length = info[b"piece length"]
    try:
        check_piece_length(piece_length)
    except ValueError as e:
        problems.append(str(e))
        return problems
    blocks_per_piece = piece_length // BLOCK
    pad = merkle_root([ZERO] * blocks_per_piece, ZERO)
    layers = meta.get(b"piece layers", {})
    v2_files = []
    for path, leaf in iter_tree(info[b"file tree"]):
        length = leaf[b"length"]
        root = leaf.get(b"pieces root")
        shown = "/".join(p.decode("utf-8", "replace") for p in path)
        v2_files.append((path, length))
        if length == 0:
            if root is not None:
                problems.append(f"{shown}: an empty file has a pieces root")
            continue
        if root is None or len(root) != 32:
            problems.append(f"{shown}: missing or malformed pieces root")
            continue
        if length <= piece_length:
            if root in layers:
                problems.append(f"{shown}: has a piece layer but fits in one piece")
            continue
        layer = layers.get(root)
        if layer is None:
            problems.append(f"{shown}: no piece layer")
            continue
        want = -(-length // piece_length)
        if len(layer) != 32 * want:
            problems.append(f"{shown}: piece layer has {len(layer) // 32} hashes, expected {want}")
            continue
        hashes = [layer[i : i + 32] for i in range(0, len(layer), 32)]
        if merkle_root(hashes, pad) != root:
            problems.append(f"{shown}: piece layer does not hash to the pieces root")

    if b"pieces" in info:  # hybrid: the v1 view must describe the same files
        if b"files" in info:
            v1_files, offset = [], 0
            for f in info[b"files"]:
                is_pad = b"p" in f.get(b"attr", b"")
                if not is_pad:
                    if offset % piece_length:
                        problems.append(
                            f"v1 file {b'/'.join(f[b'path'])!r} does not start on a piece boundary"
                        )
                    v1_files.append((tuple(f[b"path"]), f[b"length"]))
                offset += f[b"length"]
        else:
            v1_files, offset = [((info[b"name"],), info[b"length"])], info[b"length"]
        if v1_files != v2_files:
            problems.append("the v1 file list and the v2 file tree differ")
        if len(info[b"pieces"]) != 20 * -(-offset // piece_length):
            problems.append("wrong number of v1 piece hashes")
    return problems


# ---------------------------------------------------------------- cli


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("roots", "create"):
        s = sub.add_parser(cmd)
        s.add_argument("folder")
        s.add_argument("--piece-length", type=int, required=True)
        if cmd == "create":
            s.add_argument("--name")
            s.add_argument("--v2-only", action="store_true")
            s.add_argument("--tracker", action="append", default=[])
            s.add_argument("--web-seed", action="append", default=[])
            s.add_argument("--comment")
            s.add_argument("-o", "--output")
    s = sub.add_parser("check")
    s.add_argument("torrent")
    args = p.parse_args(argv)

    if args.cmd == "check":
        with open(args.torrent, "rb") as f:
            problems = check(f.read())
        for problem in problems:
            print(problem)
        print("OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    folder = args.folder.rstrip("/")
    name = getattr(args, "name", None) or os.path.basename(os.path.abspath(folder))
    # A file instead of a folder makes a single-file torrent.
    files = [((name,), folder)] if os.path.isfile(folder) else walk(folder)
    result = create(
        files,
        name,
        args.piece_length,
        hybrid=not getattr(args, "v2_only", False),
        trackers=getattr(args, "tracker", ()),
        web_seeds=getattr(args, "web_seed", ()),
        comment=getattr(args, "comment", None),
    )
    if args.cmd == "roots":
        for path, length, root in result["roots"]:
            print(f"{root.hex() if root else '-'}  {length}  {'/'.join(path)}")
        return 0
    if result["info_hash_v1"]:
        print("v1 info hash:", result["info_hash_v1"].hex())
    print("v2 info hash:", result["info_hash_v2"].hex())
    print("magnet:", result["magnet"])
    if args.output:
        with open(args.output, "wb") as f:
            f.write(result["torrent"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
