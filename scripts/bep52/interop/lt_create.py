#!/usr/bin/env python3
"""Makes a torrent with libtorrent's creator, for the interop harness.

Run it with a Python that has the libtorrent bindings of the version under
test. On Ubuntu 24.04, `/usr/bin/python3.12` with the `python3-libtorrent`
package is libtorrent 2.0.10, the library qBittorrent 4.6.3 is built on, so
this makes what that qBittorrent's torrent creator makes (qbittorrent-nox 4.6
has no way to create a torrent without its GUI).

    lt_create.py PATH --piece-length N --mode hybrid|v2|v1 --tracker URL -o OUT

Prints the libtorrent version.
"""

import argparse
import os
import warnings

import libtorrent as lt

warnings.filterwarnings("ignore", category=DeprecationWarning)

p = argparse.ArgumentParser()
p.add_argument("path")
p.add_argument("--piece-length", type=int, required=True)
p.add_argument("--mode", choices=["hybrid", "v2", "v1"], default="hybrid")
p.add_argument("--tracker", action="append", default=[])
p.add_argument("-o", "--output", required=True)
args = p.parse_args()

flags = {
    "hybrid": 0,
    "v2": lt.create_torrent.v2_only,
    "v1": lt.create_torrent.v1_only,
}[args.mode]
path = os.path.abspath(args.path)
fs = lt.file_storage()
lt.add_files(fs, path)
ct = lt.create_torrent(fs, piece_size=args.piece_length, flags=flags)
for tier, url in enumerate(args.tracker):
    ct.add_tracker(url, tier)
ct.set_creator(f"libtorrent {lt.version}")
lt.set_piece_hashes(ct, os.path.dirname(path))
data = lt.bencode(ct.generate())
lt.torrent_info(data)  # libtorrent must accept its own torrent
with open(args.output, "wb") as f:
    f.write(data)
print(lt.version)
