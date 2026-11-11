# BEP 52 for JETCARP: status

Branch `claude/kind-fermi-dx3tx8` of `jprodgers/rqbit`. Last updated 2026-11-11, after the second
session. Everything here was written with an AI model (Claude Code), except the upstream pull
requests merged in section 3, which are by their upstream author.

## 1. Where this stands

Jimmie's decisions after the first session: start with a creation crate of our own (option C),
validate the open upstream BEP 52 work (option A) and use it if it holds up, and stop treating
upstream acceptance as a goal. This fork is its own thing, maintained for JETCARP.

Both are done:

- **`crates/create_torrent`** (package `librqbit-create-torrent`) makes hybrid and v2-only torrents
  deterministically, as a synchronous pure-Rust library. It is what JETCARP v0.6.0 needs and it is
  ready to use (section 2).
- **The upstream stack works and is now on this branch**, with two fixes of ours. It passes its
  own tests, its interop tests against libtorrent 2.1.1, and, after one fix, every scenario of a
  new 21-scenario harness against qBittorrent 4.6.3 and Transmission 4.0.5 (sections 3 and 4). The branch can
  download and seed v2 and hybrid torrents, use `btmh` magnet links, and use web seeds.

- **The client builds with no C at all** (`pure-rust` feature, section 6): hashing in pure Rust,
  no TLS library, and no C compiler or CMake needed on Linux, Windows or macOS. That build passes
  the same tests and the same interop matrix.

What is not done: nothing has been run on Windows or macOS (the pure-Rust build is type-checked
for both, not executed); the embedding API has not been exercised from a host application;
several weaknesses found by reading the upstream code are still there (section 5).

## 2. The creation crate (handoff 4.1)

`crates/create_torrent`. Its README has the API and the caveats; in short:

```toml
[dependencies]
librqbit-create-torrent = { git = "https://github.com/jprodgers/rqbit", branch = "claude/kind-fermi-dx3tx8" }
```

```rust
let files = vec![InputFile::new(["objects", "ab", "abcd.parquet"], "/data/objects/ab/abcd.parquet")];
let options = CreateOptions {
    trackers: vec![vec!["https://tracker.example/announce".into()]],
    web_seeds: vec!["https://data.example/v1/".into()],
    ..CreateOptions::new("my-data-v1", 1 << 20)
};
let torrent = create(&files, &options, &HashOptions::default())?;
// torrent.bytes, .info_hash_v1, .info_hash_v2, .magnet, .files[i].pieces_root
```

| Handoff 4.1 asks for | Result |
| --- | --- |
| List of (path in torrent, local file); caller's piece length; trackers; `url-list`; comment; name | Yes |
| `.torrent` bytes, both info hashes, magnet, each file's `pieces root` | Yes |
| Deterministic; no `creation date` or `created by` unless asked | Yes. Tested: reversed input and 1, 2 and 7 threads give identical bytes |
| Same info hash as independent publishers | The info dictionary is libtorrent's: 16 whole `.torrent` fixtures made by libtorrent 2.1.1 are reproduced byte for byte |
| No async runtime, small crate | Synchronous; depends only on RustCrypto `sha1` and `sha2`, nothing from the workspace |
| 0-byte files, files under 16 KiB | In the libtorrent fixtures |
| Files over 4 GiB | A 4.5 GiB file: same bytes as the Python reference, and libtorrent loads it |
| 100,000 files | Same bytes as the Python reference. 1.7 s at 16 KiB pieces, 16 s at 1 MiB pieces |
| Hashing near disk speed; throughput for 10 GiB | 10 GiB in 201 files, hybrid, 1 MiB pieces, 4 cores, cold cache: **5.2 s, 1,960 MiB/s**. Reading the same files with `cat` took 4.5 s. From a warm cache: 2,320 MiB/s; one thread 620 MiB/s; v2-only 4,300 MiB/s. libtorrent's creator took 20.8 s on the same data and gave the same hashes |

Beyond the handoff: `hash_files` and `build` are separate, so JETCARP can keep each file's hashes
and build the next data version's torrent without reading unchanged files again.

Four things JETCARP should know, all found while testing:

1. **The last-file padding rule** (decision 4 below). Implemented as libtorrent does it.
2. **libtorrent's own creator has a file-order bug**, and we do not copy it. When a folder sits
   next to a file or folder whose name continues it with a character below `/` (`data` next to
   `data.json` or `data-old`), libtorrent 2.1.1 writes the v1 file list in whole-path order
   instead of file-tree order, and then refuses to load its own hybrid torrent. The crate uses
   file-tree order, and libtorrent loads the result. JETCARP's `objects/sha256/ab/...` layout
   does not hit this, but a `manifest/` folder next to a `manifest.json` would.
3. **Very large file counts are a problem for other clients, not for the crate.** With 64-hex
   file names, a hybrid torrent of 100,000 files is 33 MB and libtorrent's default decoding
   limits reject it; 50,000 files (17 MB) load, and a v2-only torrent of 100,000 files (14 MB)
   loads. Magnet links are likely worse, since clients cap the size of metadata they fetch
   from peers; I have not measured that. Splitting a data version into several torrents, or
   publishing very large sets as v2-only, avoids both.
4. **Hybrid torrents pay for padding.** Each padded file costs up to one piece of SHA-1 over
   zeros, for the creator and for every client that checks the v1 half. That is the difference
   between 1.7 s and 16 s above. Prefer a small piece length when files are many and small.

## 3. Validating the upstream stack (option A)

Upstream context, briefly (the first session's survey): the maintainer accepts only small,
separately useful pull requests with no unused code and has turned down a large AI-written v2
pull request. On 2026-11-11 a contributor (`zerx-lab`) opened large stacked pull requests covering
v2 and hybrid torrents (748), creation (750), web seeds (739) and storage operations (740). They
are unreviewed upstream. Jimmie decided not to engage upstream; nothing was posted.

What was run against the head of pull request 750 (`773882d6`), which contains the others:

| Check | Result |
| --- | --- |
| Its unit tests (`librqbit-core`, peer protocol, bencode) | 121 pass |
| Its `librqbit` test suite | 197 of 198 pass. The one failure needs a directory that root cannot write to, and the tests ran as root; it passes with that privilege removed |
| Its ignored interop tests against libtorrent 2.1.1 (v2, hybrid, magnets, web seeds, creation) | 16 of 16 pass |
| Builds with `default-features = false, features = ["rust-tls"]` | Yes |
| Our harness against qBittorrent 4.6.3 and Transmission 4.0.5, unmodified | **3 of 21 scenarios fail**: see below |
| The same after our fix | **None fail** (section 4) |

Two real defects came out of this, both now fixed on this branch with tests:

1. **qBittorrent could not download a v2-only torrent from rqbit.** libtorrent 2.0, the library
   of every qBittorrent release so far, treats a file's short last piece as a whole piece padded
   with zeros and requests the whole 16 KiB block the file ends in. libtorrent 2.1 asks only for
   the bytes that exist, and upstream's code was only tested against 2.1: it called the 2.0 form
   an invalid request and dropped the peer. Any file whose length is not a multiple of 16 KiB
   stalled. Hybrids were unaffected. Fix: answer such a request with the data followed by zeros
   (`d1a5c156`, about 50 lines, with a unit test and a wire-level test that fails without it).
2. **rqbit could not start a session on a host without IPv6**, on upstream `main` too: the UDP
   tracker client binds `[::]` unconditionally. Found because the test machine has no IPv6; 92
   of 198 tests failed before starting. Fix: fall back to IPv4, for the tracker client and the
   peer listener (`5e7180bd`, `60deea66`).

**Verdict: A is good enough to build on, and this branch now does.** The merge is commits
`2c0d9b8e` and `c07639ee`; upstream's commits keep their author. After the merge and fixes:
`cargo fmt --check` clean, `cargo clippy --all-targets` no warnings, and every crate's tests pass
(389 pass; the one root-only failure as above; 23 ignored, of which 16 are the libtorrent interop
tests, run separately).

For comparison, writing the same from scratch: the upstream v2 work is about 7,200 added lines
(nearly half of them tests) touching the piece layout, verification, the peer protocol, session
identity and persistence, plus 4,600 for web seeds. I estimate eight to ten sessions like this
one for v2 and hybrid at the same level of testing, and three or four more for web seeds. The
creation part is already done in our crate.

## 4. Interop matrix

`scripts/bep52/interop/interop.py`; its README explains how to run it and what each scenario
does. One machine, a private tracker, no public swarm. Every download is compared with its source
byte for byte.

rqbit under test: this branch at `60deea66`, release build. Peers: qBittorrent 4.6.3 (libtorrent 2.0.10), Transmission 4.0.5.

| # | rqbit does | Peer | Result | Detail |
| --- | --- | --- | --- | --- |
| 1a | rqbit creates+seeds hybrid | qBittorrent 4.6.3 downloads | pass | 41 files identical |
| 1b | rqbit creates+seeds v2-only | qBittorrent 4.6.3 downloads | pass | 41 files identical |
| 1c | crate creates hybrid, rqbit seeds | qBittorrent 4.6.3 and Transmission 4.0.5 download | pass | 41 files identical in both |
| 1d | same hybrid from rqbit, crate, libtorrent | libtorrent 2.0.10 | pass | same v1 and v2 info hashes from all three |
| 2a | rqbit downloads hybrid (.torrent) | qBittorrent 4.6.3 seeds, libtorrent 2.0.10 created | pass | 41 files identical |
| 2b | rqbit downloads v2-only (.torrent) | qBittorrent 4.6.3 seeds, libtorrent 2.0.10 created | pass | 41 files identical |
| 3a | rqbit downloads v2-only from a btmh magnet | qBittorrent 4.6.3 seeds | pass | 41 files identical |
| 3b | rqbit seeds v2-only | qBittorrent 4.6.3 downloads from a btmh magnet | pass | 41 files identical |
| 3c | rqbit downloads a hybrid from a btmh-only magnet | qBittorrent 4.6.3 seeds | pass | 41 files identical |
| 4a | rqbit downloads hybrid from a v1-only seed | Transmission 4.0.5 seeds | pass | 41 files identical |
| 4b | rqbit seeds hybrid to a v1 and a v2 peer | Transmission 4.0.5 and qBittorrent 4.6.3 download | pass | 41 files identical in both; announced under both hashes |
| 4c | rqbit downloads hybrid from a v1 and a v2 seed | Transmission 4.0.5 and qBittorrent 4.6.3 seed | pass | 41 files identical; data fetched from 1 peer(s) |
| 5a | rqbit downloads hybrid from a corrupt seed, then a good one | two qBittorrent 4.6.3 seeds | pass | 41 files identical; 2 corrupt piece(s) rejected before a good seed joined |
| 5b | rqbit downloads v2-only from a corrupt seed, then a good one | two qBittorrent 4.6.3 seeds | pass | 41 files identical; 2 corrupt piece(s) rejected before a good seed joined |
| 6a | rqbit downloads some files of a hybrid | qBittorrent 4.6.3 seeds | pass | 13 chosen files identical; 0 other file(s) complete, 0 stray bytes on disk |
| 6b | rqbit downloads some files of a v2-only | qBittorrent 4.6.3 seeds | pass | 13 chosen files identical; 0 other file(s) complete, 0 stray bytes on disk |
| 7a | rqbit restarts while seeding (hybrid store) | qBittorrent 4.6.3 downloads afterwards | pass | 2.0 GiB: first check 4.1 s; restart after a clean stop 1.0 s, after kill -9 1.0 s; qBittorrent then downloaded it all |
| 7b | rqbit restarts while seeding (v2-only store) | qBittorrent 4.6.3 downloads afterwards | pass | 2.0 GiB: first check 2.6 s; restart after a clean stop 0.5 s, after kill -9 0.5 s; qBittorrent then downloaded it all |
| 8a | JETCARP store: rqbit seeds hybrid | qBittorrent 4.6.3 downloads | pass | 201 files identical; 201 pieces roots match the reference |
| 8b | JETCARP store: rqbit downloads hybrid | qBittorrent 4.6.3 seeds | pass | 201 files identical |
| 9a | v2-only torrent given to a v1-only client | Transmission 4.0.5 | unsupported by peer | Transmission: unrecognized info |

Unmodified upstream (`773882d6`) fails 1b, 3b and 7b, the three scenarios in which qBittorrent
downloads a v2-only torrent from rqbit; the rest are the same. 9a is not a failure: it records
that Transmission refuses a v2-only torrent.

Limits of this matrix:

- qBittorrent 4.6.3 is the Ubuntu 24.04 package. qBittorrent 5.x was not tested; it uses the same
  libtorrent 2.0 line.
- Transmission 4.0.5 does not support v2 (scenario 9a), so it only ever plays the v1-only peer.
- Torrents "made by qBittorrent" are made with libtorrent 2.0.10's creator through its Python
  bindings, because qbittorrent-nox 4.6 cannot create torrents without its GUI.
- In 4c rqbit took all its data from one of the two seeds; nothing forces it to use both.
- "Seeding for days" was not tested. The restart scenarios use a 2 GiB store; a 10 GiB one is
  below.
- Loopback only: no NAT, no uTP, no encryption-required peers.

Restart on a larger store, measured separately on the 10 GiB store (201 files, hybrid, 1 MiB
pieces): the first check of the existing folder took 21.8 s; coming back took 0.7 s after a clean
stop and 0.7 s after `kill -9`, process start included. The session was started with
`--fastresume` and a persistence folder.

## 5. Open problems and known deviations from BEP 52

From reading the upstream code in the first session (a delegated review; I re-read the code
behind the first two). None is confirmed by a failing test yet.

1. **An inconsistent hybrid piece (SHA-1 passes, Merkle fails, or the reverse) appears to stall
   the download** instead of failing the torrent. This is the attack BEP 52 warns about for
   hybrids. Needs a test, then a fix.
2. The length of a hybrid's v1 `pieces` is not checked against the file layout (nor is it for v1
   torrents on upstream `main`).
3. **Verification is per piece, not per 16 KiB block.** A bad piece is downloaded again whole and
   the peer that completed it is dropped, as in v1. Hash requests below the piece layer, and for
   files of one piece, are refused; qBittorrent did not need them in any scenario here.
4. The order of file tree keys is not validated when a torrent is loaded.
5. Parse-then-serialize keeps only the fields rqbit knows. The raw info dictionary is kept, so
   info hashes and metadata exchange are byte-exact, but an unknown top-level key is lost if rqbit
   rewrites a `.torrent`.
6. Restart trusts a stored bitfield after checking one piece per file plus 64 random pieces; file
   modification times are not used. A failed sample means a full rehash.
7. Web seeds: a v2-only single-file torrent probably gets the wrong URL, and a v2 torrent added
   by magnet link with web seeds but no peers cannot fetch anything larger than one piece (web
   seeds cannot supply piece layers). JETCARP's torrents carry their piece layers, so only the
   magnet-only case matters.
8. There are now two creators in the tree: upstream's `librqbit::create_torrent` (async, takes a
   folder) and our crate. Scenario 1d checks they make the same torrent. `librqbit`'s could be
   made a wrapper around the crate.
9. Transmission 4.0 treats padding files as real files: it writes a `.pad` folder next to the
   data. Harmless, but users will see it.

Not verified at all: running anything on Windows, macOS or aarch64 (the pure-Rust build is only
type-checked there); long-running seeding; behaviour behind NAT; rate limits; the HTTPS tracker
path.

## 6. Embedding (handoff 4.3), with no C

JETCARP's requirement, as of the second session: **no C in the program, no CMake.** The branch has
a feature set for that.

**Cargo features for JETCARP:**

```toml
[dependencies]
librqbit = { git = "https://github.com/jprodgers/rqbit", branch = "claude/kind-fermi-dx3tx8", default-features = false, features = ["pure-rust"] }

# Needed on macOS and the BSDs only, harmless elsewhere; see below.
[patch.crates-io]
network-interface = { git = "https://github.com/jprodgers/rqbit", branch = "claude/kind-fermi-dx3tx8" }
```

What `pure-rust` is:

- **Hashing** by the RustCrypto `sha1` and `sha2` crates, through a new `sha1-rustcrypto` backend
  in `sha1w`. The same known-answer tests run on all three backends.
- **No TLS library.** Peers, the DHT, UDP trackers, HTTP trackers and HTTP web seeds work. An
  `https://` tracker or web seed fails with an error in the log ("scheme is not http"); nothing
  panics. BitTorrent itself never needed TLS: pieces are verified by hash whoever sends them.
- No web UI, HTTP API, npm, OpenSSL, `aws-lc-rs`, `ring` or CMake.

How it was checked:

| Check | Result |
| --- | --- |
| Full build of `librqbit` and of the `rqbit` binary on Linux x86-64 with `CC` and `CXX` pointing at a program that fails and logs | Builds; the compiler was never called |
| `cc`, `cmake`, `ring`, `aws-lc`, `openssl` in the dependency tree | None |
| `librqbit` tests on this feature set | 199 pass; the one failure is the root-only test of section 3 |
| libtorrent 2.1.1 interop tests on this feature set | 16 of 16 |
| The interop matrix of section 4 with the pure-Rust `rqbit` | No failures, same as the default build |
| `cargo check` for `aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-pc-windows-msvc`, `x86_64-pc-windows-gnu`, `aarch64-unknown-linux-gnu`, compiler disabled | All pass |
| A separate project depending on this branch by git with the two entries above, compiler disabled | Builds and runs on Linux; type-checks for macOS and Windows; `network-interface` resolves to the vendored copy |

The last row is type-checking only. **Nothing has been linked or run on macOS or Windows.**

Three things to be straight about:

1. **Speed.** What I measured in the first session: on a CPU with SHA instructions, RustCrypto
   does 1,355 MiB/s for SHA-1 and 1,245 MiB/s for SHA-256, against 1,175 and 1,260 for the C
   library `aws-lc-rs`; Python's OpenSSL-backed hashing measured about the same. So on current
   x86-64 it is as fast as C. With those instructions disabled it drops to 515 and 245 MiB/s. That
   is what old CPUs get (Intel desktop before about 2021, AMD before 2017). A C library is slower
   there too, and I did not measure by how much, so I cannot say how big the gap is on such
   machines; expect checking data to be slower than with C. I have not measured ARM.
2. **The one C file that was left.** A dependency of rqbit's networking crates
   (`network-interface`) compiles one C function on macOS and the BSDs: a single pointer-offset
   macro. `vendor/network-interface` is that crate with the function written in Rust; nothing
   else differs. Cargo only applies such a substitution from the top-level project, which is why
   JETCARP's `Cargo.toml` needs the `[patch.crates-io]` lines above. Without them a Mac build
   compiles that one C file with the system compiler (no CMake). The Rust version is unrun.
3. **A C toolchain is not the same as C code.** Rust itself links with the platform linker
   (`cc`/`ld` on Linux and macOS, the MSVC linker or MinGW on Windows) and against the system C
   library. That is true of every Rust program, JETCARP included, and is not changed by any of
   this.

**If JETCARP needs HTTPS inside the client** (HTTPS trackers, or web seeds on the HTTPS static
host): use `pure-rust-tls-no-provider` instead of `pure-rust`. It adds rustls with no
cryptography in it, and the application installs a provider of its choice
(`rustls::crypto::CryptoProvider::install_default`) before creating the session; without one,
creating a session returns an error that says so.

JETCARP has no networking until v0.6.0, so nothing has to be chosen yet. Two providers with no C
exist today; I tried both outside the repository, with the C compiler disabled, against a local
HTTPS test server (a real handshake with certificate verification):

| | `rustls-graviola` 0.4.0 | `rustls-rustcrypto` 0.1.0-pre.0 |
| --- | --- | --- |
| What it is | Rust plus assembly, by the author of rustls; no build script, needs only `rustc`; reuses formally verified assembly from other projects | Rust only, from the RustCrypto project |
| Releases | Stable releases; the library was updated 2026-11-10 | Pre-release only; no stable version yet |
| Its own caution | "This project is very new, so exercise due caution" | Pre-release |
| CPUs | x86-64 with AVX2 and friends (about 2014 on) and 64-bit ARM with crypto extensions (not Raspberry Pi 4 or older); nothing else | Any |
| HTTPS request here | Worked | Worked |
| Type-checks for macOS, Windows, ARM Linux, compiler disabled | Yes | Not checked |

Neither is a dependency of this repository. My suggestion is Graviola when v0.6.0 starts, checked
again then since both are moving, and with JETCARP designed so that integrity never rests on TLS
(signed manifests and content hashes already do that), which keeps the cost of a flaw in either
down to privacy.

The API, by name (all exist on this branch; I have not yet driven them from a host program):

| Need | API |
| --- | --- |
| Add from bytes or magnet, output folder, files selected | `Session::add_torrent(AddTorrent::TorrentFileBytes / Url, AddTorrentOptions { output_folder, only_files, overwrite, paused, .. })` |
| Seed an existing folder | The same with `overwrite: true`; `skip_initial_check: true` trusts file lengths |
| Pause, resume | `Session::pause`, `Session::unpause` |
| Rate limits | `SessionOptions::ratelimits` (global), `AddTorrentOptions::ratelimits` (per torrent, at add time) |
| Listen port, UPnP | `SessionOptions::listen` |
| Progress and peer statistics | `ManagedTorrent::stats()`, `api::Api::api_peer_stats` |
| Remove with or without data | `Session::delete(id, delete_files)` |
| Restart without rehash | `SessionOptions::fastresume` with `persistence` |
| Recheck on demand | `Session::force_recheck` |
| Clean stop | `Session::stop().await`, or cancel `SessionOptions::cancellation_token` |

A host that runs its own threads should give the session its own Tokio multi-thread runtime on a
dedicated thread, `block_on` the session's work there, and stop with `Session::stop()` before
dropping the runtime. This is the next thing to write and test as a small example program.

## 7. Decisions

Taken by Jimmie: C first, validate and use A, fall back to B only if needed (it was not); do not
post upstream; work in this repository.

Still open, both with a default already in place:

4. **Last-file padding rule.** In a hybrid torrent the old-style half sees all files as one long
   stream, so zero-filled padding files are inserted to make each file start on a piece
   boundary. The specification does not say whether one follows the last file, and the choice
   changes the torrent's identity: the same data would get different info hashes under the two
   rules, and so two swarms that cannot help each other. The crate does what libtorrent and
   qBittorrent do (pad the last file too, except in a one-file torrent), so anyone who makes a
   torrent of the same files with qBittorrent gets JETCARP's hashes. **Unless you object, this
   is settled; JETCARP's format documentation should state it.**
5. **Pure-Rust hashing in the client.** Decided by Jimmie: no C at all, or no client. Done: see
   section 6. What remains open under it is whether JETCARP wants HTTPS inside the torrent
   client, and with which TLS code.

## 8. What I would do next

1. CI on this fork that builds and tests the `pure-rust` feature set on Windows and macOS, since
   it has only been type-checked there; and a small host-program example for embedding (start,
   add, select files, progress, stop).
2. Tests for the open problems in section 5, starting with the inconsistent hybrid piece, and
   fixes for those that fail.
3. Web seeds with JETCARP's layout: a torrent from the crate with `url-list`, served from a
   static host, downloaded with and without peers. Upstream's own web seed tests pass, but our
   harness does not cover web seeds yet.
4. A long-running seeding test with restarts on a store of realistic size.
5. Make `librqbit`'s creator call the crate, so there is one implementation.

## 9. What is on this branch

| Commits | What |
| --- | --- |
| `2af5d8bf` | `scripts/bep52/bep52_ref.py`, an independent Python reference, and a cross-check against libtorrent |
| `f15c4c78` | `crates/create_torrent` with libtorrent fixtures |
| `d28c6ec8`, `ca27bf9a` | The interop harness and test-store generator |
| `2c0d9b8e`, `c07639ee` | Merge of upstream pull requests 750, 748, 740, 739, 736, 721 (author: zerx-lab) |
| `d1a5c156` | Fix: zero-padded block requests from libtorrent 2.0 |
| `5e7180bd`, `60deea66` | Fix: start on hosts without IPv6 |
| `0dae4de4` | The `pure-rust` build: RustCrypto hashing backend, features, and the vendored `network-interface` without its C file |

To re-run the checks: `cargo test` (as a non-root user, or expect the one failure described in
section 3), `python3 scripts/bep52/crosscheck_libtorrent.py` with libtorrent's Python bindings,
and the interop harness as its README describes.
