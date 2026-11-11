# BEP 52 for JETCARP: status

Branch `claude/kind-fermi-dx3tx8` of `jprodgers/rqbit`. Last updated 2026-11-11, after the first
session. Written with an AI model (Claude), like everything else on this branch.

## 1. Where this stands

**No BEP 52 implementation code has been written here yet, on purpose.** The handoff says to find
out first whether v2 work already exists upstream, and it does, at three levels:

1. Small v2 groundwork is already merged in rqbit `main`.
2. The maintainer has said, in writing, how he wants v2 to arrive (small slices, no dead code, a
   specific order), and has turned down a large v2 pull request for being large and AI-written.
3. On 2026-11-11, the day of this session, another contributor opened pull requests that cover
   nearly the whole handoff: v2 and hybrid download and seeding, v2 and hybrid torrent creation,
   and BEP 19 web seeds. They are large, unreviewed, and have no maintainer response yet.

A third parallel implementation from us would duplicate (3) and still face (2). Which way to go is
Jimmie's call; the options are in section 5 and my recommendation is option C then A-lite.

What this session did produce:

- This report.
- `scripts/bep52/bep52_ref.py`: an independent, standard-library-only Python reference for BEP 52
  and hybrid torrents (per-file `pieces root`, piece layers, both info hashes, magnet, `.torrent`
  bytes, and a checker for existing `.torrent` files). It is the "independent computation" the
  handoff asks for in section 5, and it is useful whichever option is chosen.
- `scripts/bep52/crosscheck_libtorrent.py`: checks the reference against libtorrent 2.1.1. All 33
  checks pass (section 4).

## 2. What already exists upstream

The fork's `main` is identical to upstream `main` (commit `f23bacd`, after release 9.1.0).

### 2.1 Merged in `main`

| What | Where it came from |
| --- | --- |
| `Magnet` parses and writes `xt=urn:btmh:1220…`, alone or with `btih` | ikatson/rqbit#72 (2024) |
| `ISha256` in `sha1w` (both backends), `Id32::truncate_for_dht()`, `Magnet::new(v1, v2, …)`, BEP 52 error variants in `librqbit_core::Error` (unused so far) | ikatson/rqbit#562 (Feb 2026) |
| Hybrid torrents can be added, as v1 torrents (duplicate `.pad/<n>` names accepted) | ikatson/rqbit#701 (Oct 2026) |
| The repository's `CLAUDE.md` rule "use v2 (BEP 52) structures" | ikatson/rqbit#562 |

What `main` cannot do: a v2-only `.torrent` fails to parse (`pieces` is a required field), a
`btmh`-only magnet cannot be added (the session needs the v1 hash), a hybrid torrent only joins the
v1 swarm, and `create_torrent` makes v1 torrents only.

### 2.2 What the maintainer has said

These are from the issue and pull-request threads. Quotes are short; the rest is paraphrase.

- **Interest** (issue 70, 2023): not opposed, not a priority. He would merge v2 "if the end result
  doesn't make things terribly complicated". A later comment there argues for v2 from exactly
  JETCARP's angle: per-file roots, cross-torrent seeding, content-addressed archives.
- **Size** (pull request 554, an 8,000-line v2 pull request, Feb 2026): "I can't accept giant
  (esp. AI-created) PRs, little reviewable pieces maybe." On issue 546 he put a number on it:
  pieces of about 500 lines that each make sense to merge alone. Partial v2 support in `main` for a
  while is fine. v1 stability and performance must not suffer.
- **No dead code** (pull requests 562 and 568): types and helpers land with their first user. The
  stand-alone Merkle module (568) was not reviewed for that reason, and its author closed it.
- **Order** (pull request 568): start with parsing v2 metainfo, then go top-down: fetch piece
  layers when missing (the magnet case), validate data on disk, then download.
- **AI** (`AI_POLICY.md`, which adopts Ghostty's): all AI use must be disclosed, naming the tool
  and the extent; the human submitting must understand the code and be able to explain it without
  the AI; AI-written issue and comment text must be reviewed and trimmed by a human first.
- **What gets merged**: the 15 most recently merged pull requests are almost all small fixes from
  outside contributors.

The earlier v2 effort (issue 546, by `meatsquirk`) stalled: after 568 was closed in July 2026 its
author said a first slice (parse and validate v2 metainfo) would follow, and none has appeared.
Issue 546 is still the open design thread.

### 2.3 The pull requests opened on 2026-11-11

All by `zerx-lab`, who has 23 pull requests on rqbit; one small fix is merged, the rest are open.
None of the ones below has a review or a maintainer comment yet. Their descriptions do not say
whether AI was used.

| Pull request | Covers | Own size | Stacked on |
| --- | --- | --- | --- |
| ikatson/rqbit#748 | v2 and hybrid: metainfo, Merkle verification, both swarms, hash request / hashes / hash reject, `btmh` magnets | +7,218 lines, 44 files | 739, 740 |
| ikatson/rqbit#750 | Creating v1, v2 and hybrid torrents; trackers, web seeds, comment; progress and cancel | +2,677 lines, 13 files | 748 |
| ikatson/rqbit#739 | BEP 19 web seeds | +4,615 lines, 30 files | none |
| ikatson/rqbit#740 | Force recheck, seed mode, move storage | +8,668 lines, 36 files | 721 |
| ikatson/rqbit#745 | Per-torrent rate and peer limits changeable at run time | about +2,500 lines | none |

Because they are stacked, the v2 pull request shows as a 20,000-line diff against `main`. Given
section 2.2, I do not expect these to be merged in this form; whether the maintainer asks for them
to be split, ignores them, or closes them is unknown today.

Related open items: issue 698 and pull request 736 (the existing `create_torrent` writes one piece
hash too many when the content ends on a piece boundary; qBittorrent rejects such torrents), issue
500 (web seeds wanted), issue 634 (SHA backend tied to the TLS feature; section 3.3).

## 3. How the open pull requests compare with what JETCARP needs

**This is from reading the code, not running it.** This session was not permitted to build or run
code from a third party's unreviewed pull request, so nothing below is a test result. Jimmie can
allow that for a later session (section 6).

### 3.1 Creation (handoff 4.1) against pull request 750

Read in full: `crates/librqbit/src/create_torrent_file.rs` at the pull request's head.

| JETCARP needs | Pull request 750 | Verdict |
| --- | --- | --- |
| Input is a list of (path in torrent, local file) | Takes one folder or file path and walks it | **Gap.** A data version that is a subset of the store would need a folder of links |
| Caller chooses the piece length | `piece_length: Option<u32>`, power of two, 16 KiB to 128 MiB | Met |
| Trackers, `url-list`, comment, name | All present; `url-list` is a string for one web seed and a list otherwise, like libtorrent | Met |
| Output: bytes, both hashes, magnet | `as_bytes()`, `info_hash()`, `info_hash_v2()`, `as_magnet()` (writes `btih` and `btmh` for hybrids) | Met |
| Output: each file's `pieces root` | In `meta.info.file_tree`, no direct accessor | Met, slightly awkward |
| No `creation date` unless asked | Written only when given | Met |
| No `created by` unless asked | **Defaults to `rqbit <version>`**; `Some("")` turns it off | Caller must pass `created_by: Some("")`. It is outside the info dictionary, so info hashes are unaffected, but `.torrent` bytes would change with every rqbit release |
| File order from the sorted file tree | Sorted by path component bytes | Met |
| No dependence on read order, no randomness | Single pass in sorted order | Met by design |
| Same info hash as other publishers | Same layout as libtorrent's default, including a padding file after the last file; its interop test asserts equal hashes with libtorrent 2.1.1 | Met, if all publishers use the same tail-padding rule (section 4) |
| Callable without an async runtime | The blocking function is private; the public `create_torrent` is `async` and needs a `BlockingSpawner` | **Gap** |
| From a small crate | Lives in `librqbit` (the whole client). It only uses `librqbit-core`, `walkdir` and a cancellation token, so it could move to `librqbit-core` | **Gap**, fixable |
| 0-byte files | No `pieces root`, no padding after them | Met by reading |
| Files under 16 KiB, over 4 GiB | Lengths are `u64` throughout; a small file's root is over its own blocks | Met by reading |
| 100,000 files | File tree limit is 2^20 files; piece layers are deduplicated with a linear scan, which is quadratic in the number of multi-piece files (about 5×10^9 comparisons at 100,000) | **Likely slow**, small fix |
| All files empty | Rejected | Fine for JETCARP |
| Throughput near disk speed | One thread, SHA-1 then SHA-256 over the same 1 MiB buffer. Not measured. From the hash speeds in section 3.3, expect roughly 600 MiB/s for hybrids on a CPU with SHA extensions, less than a fast NVMe | **Unmeasured; probably short of NVMe speed** |

### 3.2 Download and seeding (handoff 4.2) against pull request 748

The code review is in section 3.4. From the pull request's own description:

- Present: start from `.torrent` or `btmh` magnet; piece layers fetched and served with hash
  messages; hybrid torrents in both swarms; corrupt data caught by the Merkle check; fastresume;
  file selection through the existing mechanism.
- Stated limits: hash requests below the piece layer are refused (only piece layers are kept), so
  verification is per piece, not per 16 KiB block.
- Tested against libtorrent 2.1.1 through its Python bindings only. **Not tested against
  qBittorrent or Transmission**, and the interop tests are `#[ignore]`, so CI does not run them.
- "Restart must not rehash terabytes": relies on fastresume plus the `skip_initial_check` and seed
  mode from pull request 740. Needs to be run to be believed.

### 3.3 Embedding (handoff 4.3) against `main`

Measured on `main` with `cargo tree`.

| Feature set | Result |
| --- | --- |
| default (`default-tls`, `http-api-client`) | SHA through `crypto-hash`, which is **OpenSSL on Linux**; TLS as reqwest's default |
| `default-features = false` | Does not compile: exactly one SHA backend must be chosen, and both choices are tied to a TLS feature |
| `default-features = false, features = ["rust-tls"]` | **No OpenSSL.** rustls for HTTPS trackers; SHA-1 and SHA-256 through `aws-lc-rs`. No web UI, HTTP API, or npm |

So **`default-features = false, features = ["rust-tls"]` is the feature set for JETCARP today.**
It is not pure Rust: `aws-lc-rs` builds C and assembly (needs a C compiler, CMake, and NASM on some
Windows targets). There is no RustCrypto backend in `sha1w`. rustls itself has no production
pure-Rust crypto provider, so HTTPS trackers will need `aws-lc-rs` or `ring` whatever we do; the
part that can be pure Rust is hashing, which matters most for the creation-only use in v0.6.0.

Upstream context: in issue 634 the maintainer said he dropped the RustCrypto `sha1` crate long ago
for being "extremely slow", would take a switch from `aws-lc-rs` to `ring`, and would also accept
making the SHA backend selectable separately from TLS. Two pull requests tried (662, closed by its
author; 692, open). I measured the backends on this machine (16 KiB blocks, one thread, Xeon with
SHA extensions):

| Backend | SHA-1 | SHA-256 |
| --- | --- | --- |
| RustCrypto `sha1` / `sha2` 0.10 | 1,355 MiB/s | 1,245 MiB/s |
| `aws-lc-rs` | 1,175 MiB/s | 1,260 MiB/s |
| `ring` 0.17 | **355 MiB/s** | 1,220 MiB/s |
| RustCrypto with CPU SHA support disabled (`force-soft`) | 515 MiB/s | 245 MiB/s |

Two things follow. On x86-64 CPUs with SHA extensions (AMD since about 2017, Intel desktop since
about 2021) RustCrypto is now as fast as `aws-lc-rs`, so the maintainer's objection is out of date
there; on older CPUs it is slower. I have not measured ARM. And `ring`'s SHA-1 is
3 to 4 times slower than either on this CPU, so the switch to `ring` that the maintainer welcomed
would slow v1 hashing; neither open pull request mentions that. An opt-in `sha1-rustcrypto` feature
is a small, self-contained change that I think upstream would take with these numbers. It changes
the public feature list, so it is listed as a decision in section 6 rather than done.

The library API JETCARP needs, as it is on `main`:

| Need | On `main` |
| --- | --- |
| Add from bytes or magnet, output folder, files selected | `Session::add_torrent(AddTorrent::TorrentFileBytes / Url, AddTorrentOptions { output_folder, only_files, overwrite, paused, .. })` |
| Seed an existing folder | Same, with `overwrite: true` (maintainer's answer in issue 509) |
| Pause, resume | `Session::pause`, `Session::unpause` |
| Rate limits | `SessionOptions::ratelimits` (global) and `AddTorrentOptions::ratelimits` (per torrent, set when adding). Changing a torrent's limits later is pull request 745 |
| Listen port, UPnP | `SessionOptions::listen` (`ListenerOptions`) |
| Progress and peer statistics | `ManagedTorrent::stats()` |
| Remove with or without data | `Session::delete(id, delete_files)` |
| Restart without rehash | `SessionOptions::fastresume` with `persistence`; the maintainer calls it reliable but it is not the default |
| Clean stop | `Session::stop().await`, or cancel `SessionOptions::cancellation_token` |

A host that runs its own threads needs a Tokio multi-thread runtime for the session: create one
`tokio::runtime::Runtime` on a dedicated thread, `block_on` the session there, and stop by calling
`Session::stop()` and then dropping the runtime. I have not yet verified this end to end or on
Windows and macOS; that is work for the embedding step.

Minimum Rust version: upstream CI checks 1.94 and 1.97.

### 3.4 Code review of pull request 748

A read of the v2 commits (`2f58b27e..fc1147cb` in the pull request's branch), done by a delegated
review pass against the handoff's checklist. I re-read the code behind the first two concerns
myself; the rest I have not independently confirmed. File references are to that branch.

**Looks right against BEP 52:**

- Metainfo: `meta version` must be 2; file tree leaves under the `""` key; `pieces root` required
  for non-empty files and refused for empty ones; `piece layers` outside the info dictionary; piece
  length a power of two of at least 16 KiB; v2 info hash is SHA-256 of the raw info bytes, with the
  raw bytes kept, so hashing and metadata exchange are byte-exact.
- Merkle: 16 KiB leaves, zero-hash leaf padding, and the correct padding above the piece layer
  (the root of an all-zero piece, not 32 zero bytes). Each piece layer is checked against its root
  on load. Files of one piece or less verify against the root directly.
- Peer protocol: reserved bit `reserved[7] & 0x10`; message ids 21, 22, 23 with the BEP's field
  layout; hybrid connections upgrade to the v2 hash; hash messages on a non-v2 connection
  disconnect the peer; fetched layers are verified chunk by chunk against the root.
- Swarms: trackers, DHT and local discovery run per hash; a hybrid announces under both.
- Sizes: lengths and offsets are `u64` with checked arithmetic. The hard limit is about 64 TiB of
  piece space per torrent, from 32-bit chunk indices.

**Concerns, most serious first:**

1. **A hybrid piece whose SHA-1 passes and Merkle hash fails (or the reverse) appears to stall the
   download instead of failing the torrent.** `check_piece` returns an error for that case
   (`file_ops.rs:232`); the caller propagates it with `?` (`torrent_state/live/mod.rs:2019`) after
   the piece was taken out of the in-flight set, so it is neither marked failed nor queued again.
   This is exactly the inconsistent-hybrid attack the BEP warns about. Needs a test to confirm.
2. **The length of a hybrid's v1 `pieces` is never checked against the layout.** A short `pieces`
   leads into the path in point 1. `main` has the same gap for v1 torrents, so this is inherited
   rather than introduced.
3. **Verification is per piece only.** No block-level hashes are kept or served: hash requests
   below the piece layer are refused, as are requests for files of one piece. On a failed piece the
   whole piece is downloaded again and the peer that completed it is disconnected, the same as v1.
   The handoff's "16 KiB verification granularity" is not delivered. Whether libtorrent ever needs
   the refused request shapes from a seed is not confirmed; the pull request's own interop tests
   are said to pass without them.
4. **File tree key order is not validated.** An unsorted tree is accepted and its files take
   dictionary order, which can differ from clients that sort.
5. **Re-encoding drops unknown keys.** The raw info bytes are kept, so hashes are safe, but
   parse-then-serialize of the typed structure is only byte-exact for the fields it knows. Byte
   equality is tested on four libtorrent fixtures only.
6. **Resource use:** a missing piece layer is allocated up front from sizes in magnet metadata;
   for very large files the answer to a hash request rebuilds the tree per request.
7. **Web seeds:** a v2-only single-file torrent probably gets the wrong URL (the worker decides
   "single file" from the v1 `length` key, `webseed/worker.rs:485`). And a magnet-added v2 torrent
   with only web seeds cannot fetch anything larger than one piece, since web seeds cannot supply
   piece layers.

**Changes to v1 behaviour beyond what the description lists:** the read, write and check loops in
`file_ops.rs` are rewritten for all torrents; progress no longer counts BEP 47 padding; the
32-bit chunk limit is now enforced for v1 too; the stored `.torrent` is rewritten when its size
changes.

**Restart behaviour** (handoff: "must not rehash terabytes"): fastresume keeps a bitfield and on
restart checks one claimed piece per file plus up to 64 random ones; any failure triggers a full
rehash. `skip_initial_check` trusts file lengths only. Modification times are not used. Fetched
piece layers are saved at most every 30 seconds, so a crash in that window makes the next start
distrust the bitfield. For a multi-terabyte seed this is probably acceptable but is weaker than
"sizes and modification times", and a full rehash is one failed sample away.

**Tests:** four fixture torrents from libtorrent 2.1.1 with a committed generator script; unit
tests for Merkle and message geometry; loopback end-to-end tests for download, seed, restart,
corruption, magnets and hash exchange; eight interop tests against libtorrent's Python bindings,
ignored by default and so not run in CI. No qBittorrent, no Transmission, no torrents from the
wild.

**What a maintainer would likely push back on:** the size; v2 fields living inside the
`TorrentMetaV1*` types; the piece and chunk index scheme for v2-only torrents (unused index holes
after each file's short last piece); two connection attempts per hybrid peer; interop tests
outside CI; three BEP 52 error variants still unused.

## 4. The reference script, and one finding about determinism

`scripts/bep52/bep52_ref.py` builds hybrid and v2-only torrents from the BEP text with only the
Python standard library. `scripts/bep52/crosscheck_libtorrent.py` compares it with libtorrent
2.1.1 (`pip install libtorrent`) on a tree shaped like a JETCARP store, 28 files with sizes chosen
to hit the edges: 0 bytes, 1 byte, one byte either side of a block and of a piece, exact multiples,
and several pieces. For piece lengths of 16, 64 and 256 KiB, hybrid and v2-only:

- the v1 and v2 info hashes equal libtorrent's;
- libtorrent loads the reference's `.torrent` and reports the same `pieces root` for every file;
- the reference's checker accepts libtorrent's `.torrent`;
- reversing the input list gives byte-identical output;
- single-file torrents, and a folder holding one file, also match.

All 33 checks pass. Run it with:

```bash
python3 -m venv /tmp/lt && /tmp/lt/bin/pip install libtorrent
```

```bash
/tmp/lt/bin/python scripts/bep52/crosscheck_libtorrent.py
```

Not yet covered: files over 4 GiB (the script handles them but is slow, about 100 to 300 MiB/s),
and 100,000 files.

**Finding: "same data gives the same info hash" needs one more rule than the BEP gives.** In a
hybrid torrent the v1 file list, padding files included, is inside the info dictionary, so it
changes *both* info hashes. BEP 52 does not say whether a padding file follows the last file.
libtorrent pads the last file by default and has a flag (`canonical_files_no_tail_padding`) not
to; it never pads a torrent of one file. Two correct creators can therefore produce different info
hashes for the same files. JETCARP should fix the rule in its own format documentation. The
reference and pull request 750 both follow libtorrent's default (pad after every file that does
not end on a piece boundary, the last one too, unless the torrent has one file), which is what
qBittorrent produces. v2-only torrents have no such ambiguity. The per-file `pieces root` never
depends on any of this, only on the file's bytes: it does not even depend on the piece length.

## 5. Options

**A. Build on the open pull requests.** Take the head of pull request 750 into a fork branch, test
it hard against section 4 of the handoff (interop with qBittorrent and Transmission, the
JETCARP-shaped store, restart behaviour), and send fixes and review comments to those pull
requests.
Fastest route to a working client, and it helps upstream rather than competing. But the fork would
carry about 23,000 lines that nobody upstream has reviewed, from an author with no merged features
in this project, and section 2.2 says it will not be merged in this shape. If it is split and
reworked, the fork follows a moving target.

**B. Our own small slices, in the maintainer's order.** Parse and validate v2 and hybrid metainfo
first (with only the Merkle code that piece-layer validation needs), then creation, then on-disk
verification, then the peer protocol; each about 500 lines, each used by something on arrival.
This is the handoff's section 6 with the order changed to fit "no dead code".
The most likely to be merged, and the slowest: many review rounds with a maintainer who has little
time, and it duplicates work that now exists in pull request 748. It should not start without a
comment on issue 546 first, so that three people are not writing the same parser.

**C. Decouple v0.6.0 from rqbit.** JETCARP v0.6.0 only *writes* torrents. That needs no client and
little code: the reference script's logic is about 150 lines, and in Rust it needs only `sha1`,
`sha2` and a bencode writer. JETCARP can carry that itself, sync and pure Rust, taking exactly the
input it wants (a list of path and file pairs), and test it against `bep52_ref.py` and libtorrent.
Nothing upstream offers that shape today (section 3.1), and none of the open work is needed for it.
The embedded client for v0.7.0 is then decided later, when it is clearer what upstream does with
the open pull requests.

**My recommendation: C now, then a light version of A.** C removes the only time pressure. For the
client, the useful contribution at this moment is not more code but evidence: run the interop
matrix and the JETCARP-shaped tests against pull request 750's head, and report results and
concrete bugs on those pull requests and on issue 546. That is the review help a maintainer short
of time needs, it tells us whether that code is good enough to depend on, and it keeps option B
open if the open pull requests go nowhere. I would not start B today.

## 6. Decisions for Jimmie

1. **Which option** (section 5).
2. **May a later session build and run the code of pull requests 748 and 750?** Needed for option
   A and for my recommendation. It is third-party code that no maintainer has reviewed; it would
   run in the session's cloud container, not on your machine. This session declined to run it
   without your say-so.
3. **Post on issue 546?** A draft is in section 8. Per the maintainer's AI policy it should be in
   your words and state the AI involvement; treat the draft as notes.
4. **Tail padding rule** for JETCARP hybrids (section 4): I suggest libtorrent's default.
5. **A `sha1-rustcrypto` feature** in `sha1w`, selectable without a TLS feature (section 3.3). It
   adds a public feature and two dependencies (`sha1`, `sha2`), so I did not add it unasked. It is
   about 60 lines and could be offered upstream alone with the benchmark table.
6. **Where a creation function should live** if we write one in this repository rather than in
   JETCARP: `librqbit-core` is the smallest crate that has the metainfo types and bencode. It still
   depends on Tokio (as a library, no runtime needed) and on a SHA backend feature.

## 7. Interop matrix

Not run. No v2-capable rqbit was built in this session (section 3), so there is nothing to
report except the creation cross-check:

| Scenario | Peer | Result |
| --- | --- | --- |
| Reference creates hybrid and v2-only torrents; info hashes and roots compared | libtorrent 2.1.1 (Python bindings) | Pass, 33 of 33 checks |
| Handoff scenarios 1 to 7 and the JETCARP-shaped store | qBittorrent, Transmission | Not run |

In this environment `pip install libtorrent` works (2.1.1) and Docker is installed;
`qbittorrent-nox` and Transmission are not installed and I have not yet tried to get them.

## 8. Draft texts for upstream

Nothing here has been posted. Both are notes for Jimmie to rewrite, not text to paste.

### 8.1 Comment for issue 546 (the v2 design thread), instead of a new issue

> I maintain a research-data project (JETCARP) that wants to publish content-addressed archives as
> hybrid v1+v2 torrents and later embed librqbit, so I have a concrete use for BEP 52: per-file
> roots that stay the same across versions of a dataset.
>
> I see #748 and #750 now cover most of this, and I have read the earlier feedback here and on
> #554 and #568 about small pieces, no dead code, and starting from metainfo parsing. I don't want
> to add a third implementation. What I can offer instead:
>
> - a stdlib-only Python reference for v2/hybrid creation that matches libtorrent 2.1.1's info
>   hashes on edge-case file sizes, usable as a test oracle;
> - interop testing of whichever branch you prefer against qBittorrent and Transmission, with a
>   scripted harness, and bug reports from that;
> - if it helps, small (about 500 line) slices in the order you described in #568, starting with
>   v2/hybrid metainfo parsing and validation.
>
> Which of these would be useful to you? And is #748 something you would want split up, or
> reviewed as it is?
>
> Disclosure: I work with an AI model (Claude Code) on this; the reference script and the survey
> behind this comment were produced that way and checked by me.

### 8.2 Comment for issue 634 (SHA backend), if decision 5 is yes

> Some numbers that may be useful here, 16 KiB blocks, single thread, Xeon with SHA extensions:
> RustCrypto `sha1` 0.10 does 1,355 MiB/s, `aws-lc-rs` 1,175, `ring` 0.17 only 355 for SHA-1
> (SHA-256 is about 1,250 for all three). With hardware SHA disabled RustCrypto drops to 515
> (SHA-1) and 245 (SHA-256). So moving `sha1-ring` to `ring` would cost a lot of v1 hashing speed,
> and a RustCrypto backend is no longer slow on current CPUs. I can send a small PR adding an
> opt-in `sha1-rustcrypto` feature that is independent of the TLS feature, if you'd take it.

## 9. If the answer is "go ahead with B": the slice plan

Each slice is used on arrival and keeps v1 unchanged. Sizes are targets.

1. Parse v2 fields (`meta version`, `file tree`, top-level `piece layers`; `pieces` optional),
   compute the v2 info hash from the raw info bytes, validate (piece length, tree shape, piece
   layers against roots, hybrid v1/v2 agreement). First user: `rqbit` can list a v2 torrent and
   reject a bad one. The existing unused error variants get their users here.
2. Creation of v2 and hybrid torrents as a sync function in `librqbit-core`, wrapped by the
   existing async `create_torrent`. First user: `rqbit share`.
3. Verify data on disk for v2 and hybrid (initial check, piece check on download) using piece
   layers from the `.torrent`. First user: seeding and downloading a v2 `.torrent` over v1-style
   connections to v2 peers.
4. Handshake bit, truncated v2 hash for trackers, DHT and handshake, hybrid in both swarms.
5. Hash request, hashes and hash reject; `btmh`-only magnets.
6. Interop harness and documentation.

For web seeds later (handoff 4.4): the only v2 design point that matters is that a piece of a file
whose piece layer is not yet known cannot be verified, so neither peers nor web seeds may be asked
for it; and that hybrid torrents must map v1 padding files to "no bytes to fetch". Pull request 748
states it handles both.
