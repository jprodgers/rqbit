#!/usr/bin/env python3
"""Makes a test folder shaped like a JETCARP object store.

    make_store.py DIR --files 200 --total 10G [--max-file 300M] [--also 300M] [--seed 1]

Writes `DIR/objects/sha256/<ab>/<64 hex>.parquet` (and `.json` for the small
ones) plus `DIR/manifest.json`. Sizes run from 0 bytes up to --max-file and add
up to about --total; the first few are fixed awkward sizes (0, 1, one byte
either side of 16 KiB and 1 MiB). File names are the SHA-256 of the content,
as in a content-addressed store. The same arguments always make the same
folder.

Written with an AI model (Claude) for the JETCARP project; see STATUS.md.
"""

import argparse
import hashlib
import os
import random
import sys


def size(text: str) -> int:
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    return int(float(text[:-1]) * units[text[-1]]) if text[-1] in units else int(text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dir")
    p.add_argument("--files", type=int, default=200)
    p.add_argument("--total", type=size, default=size("1G"))
    p.add_argument("--max-file", type=size, default=size("300M"))
    p.add_argument("--also", type=size, action="append", default=[],
                   help="a file of exactly this size as well (repeatable)")
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()

    rng = random.Random(args.seed)
    fixed = [0, 1, 16383, 16384, 16385, (1 << 20) - 1, 1 << 20, (1 << 20) + 1, 0]
    fixed = (args.also + fixed)[: args.files]
    rest = args.files - len(fixed)
    # Random weights, skewed so that a few files are large and many are small.
    weights = [rng.random() ** 3 for _ in range(rest)]
    budget = max(args.total - sum(fixed), 0)
    scale = budget / sum(weights) if weights else 0
    sizes = fixed + [min(int(w * scale), args.max_file) for w in weights]

    written = 0
    for index, n in enumerate(sizes):
        folder = os.path.join(args.dir, "objects", "sha256", "tmp")
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, f"{index}.tmp")
        digest = hashlib.sha256(index.to_bytes(8, "big"))
        with open(tmp, "wb") as f:
            left = n
            while left:
                chunk = rng.randbytes(min(left, 1 << 22))
                digest.update(chunk)
                f.write(chunk)
                left -= len(chunk)
        name = digest.hexdigest() + (".json" if n < 16384 else ".parquet")
        final = os.path.join(args.dir, "objects", "sha256", name[:2])
        os.makedirs(final, exist_ok=True)
        os.replace(tmp, os.path.join(final, name))
        written += n
    os.rmdir(os.path.join(args.dir, "objects", "sha256", "tmp"))
    with open(os.path.join(args.dir, "manifest.json"), "w") as f:
        f.write('{"files": %d, "bytes": %d}\n' % (len(sizes), written))
    print(f"{len(sizes) + 1} files, {written / (1 << 30):.2f} GiB in {args.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
