# librqbit-create-torrent

Makes BitTorrent v2 ([BEP 52]) and hybrid v1+v2 `.torrent` files from files on disk, without a
client. Synchronous, pure Rust, and deterministic: the same files, paths and options give the same
bytes on every machine, so independent publishers of the same data get the same info hashes.

It lives in the rqbit workspace but uses nothing from it. Its only dependencies are the RustCrypto
`sha1` and `sha2` crates.

```toml
[dependencies]
librqbit-create-torrent = { git = "https://github.com/jprodgers/rqbit" }
```

```rust
use librqbit_create_torrent::{create, CreateOptions, HashOptions, InputFile};

let files = vec![
    InputFile::new(["objects", "ab", "abcd.parquet"], "/data/objects/ab/abcd.parquet"),
    InputFile::new(["manifest.json"], "/data/manifest.json"),
];
let options = CreateOptions {
    trackers: vec![vec!["https://tracker.example/announce".into()]],
    web_seeds: vec!["https://data.example/v1/".into()],
    ..CreateOptions::new("my-data-v1", 1 << 20) // name, piece length
};
let torrent = create(&files, &options, &HashOptions::default())?;
std::fs::write("my-data-v1.torrent", &torrent.bytes)?;
// torrent.info_hash_v1, torrent.info_hash_v2, torrent.magnet,
// torrent.files[i].pieces_root
```

There is a command-line example, mostly for testing:

```bash
cargo run --release -p librqbit-create-torrent --example mktorrent -- FOLDER --piece-length 1048576 -o out.torrent
```

## What it promises

- **Input**: a list of (path in the torrent, local file), in any order; a name; a piece length (a
  power of two from 16 KiB to 128 MiB); optionally tracker tiers, web seeds (`url-list`), a
  comment, the private flag, `created by` and `creation date`.
- **Output**: the `.torrent` bytes, the v1 and v2 info hashes, a magnet link (`btih` and `btmh`),
  and every file's `pieces root`.
- **Deterministic**: files are put in the order of the bencoded `file tree`; nothing depends on
  the order of the input, the order files are read in, or the number of threads; `created by` and
  `creation date` are written only when given.
- **Same torrent as libtorrent**: for the same files, name and piece length, the info dictionary,
  and so both info hashes, are the ones libtorrent 2.x (qBittorrent) produces. The tests compare
  whole `.torrent` files byte for byte with fixtures made by libtorrent 2.1.1.
- **Hash once, build many**: `hash_files` gives each file's `FileHashes`, which depend only on the
  file's bytes and the piece length; `build` makes a torrent from them without touching the disk.
  When most files are unchanged from one data version to the next, keep their hashes.

## Things to know

- **Padding files.** The v1 half of a hybrid torrent gets a [BEP 47] padding file after every
  non-empty file that does not end on a piece boundary, the last one included, unless the torrent
  has exactly one file. That is libtorrent's default. BEP 52 does not settle whether the last file
  is padded, and the choice changes both info hashes, so anyone who must reproduce these hashes
  with another tool has to use the same rule.
- **File order.** Files are ordered by path component, which is what the file tree requires:
  `a/b` comes before `a.b/x` and `a.bin`. libtorrent 2.1.1's own creator orders the v1 list by
  whole path, gets those cases the other way round, and then refuses to load its own hybrid
  torrent; libtorrent loads the ones made here.
- **Many small files with a large piece length are slow and big in hybrid torrents.** Every
  padded file costs one piece of SHA-1 over zeros and every client that checks the v1 half pays
  the same. 100,000 small files take 1.7 s at a 16 KiB piece length and 16 s at 1 MiB (4 cores).
  A hybrid torrent of 100,000 files is about 20 MB and is more than libtorrent's default decoding
  limits accept. v2-only torrents have none of this.
- **A `pieces root` identifies a file's bytes** whatever the piece length and whatever torrent
  the file is in. It is not the SHA-256 of the file.
- **Speed.** Hashing uses one thread per CPU by default (`HashOptions::threads`). On the 4-core
  machine it was developed on: 10 GiB in 201 files as a hybrid torrent in 5.2 s from a cold cache
  (about 1,960 MiB/s, where reading the same files with `cat` took 4.5 s), 620 MiB/s on one
  thread, and about 4,300 MiB/s for v2-only. Use one thread on a spinning disk.
- A file that changes size while it is read is an error. A file whose bytes change without its
  size changing is not detected.

## Testing

```bash
cargo test -p librqbit-create-torrent
```

`tests/fixtures` holds the libtorrent-made torrents; `scripts/bep52/make_fixtures.py` regenerates
them. `scripts/bep52/bep52_ref.py` is an independent Python implementation of the same format, and
`scripts/bep52/crosscheck_libtorrent.py` checks that one against libtorrent.

Written with an AI model (Claude Code) for the JETCARP project.

[BEP 52]: https://www.bittorrent.org/beps/bep_0052.html
[BEP 47]: https://www.bittorrent.org/beps/bep_0047.html
