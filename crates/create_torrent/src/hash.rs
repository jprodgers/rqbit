//! Hashing files: SHA-256 merkle trees (BEP 52) and, for hybrid torrents,
//! SHA-1 piece hashes (BEP 3) of the same bytes, in one pass.
//!
//! In a hybrid torrent every file starts on a piece boundary, so a file's
//! hashes depend only on its own bytes and the piece length. That is what lets
//! files, and ranges of pieces within a file, be hashed on different threads,
//! and lets a caller keep [`FileHashes`] for files that did not change.

use std::fs::File;
use std::io::{ErrorKind, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

use sha1::{Digest, Sha1};
use sha2::Sha256;

use crate::{BLOCK_SIZE, Error, Version};

pub type Sha256Hash = [u8; 32];
pub type Sha1Hash = [u8; 20];

/// How much of a file is read at once. A multiple of the block size.
const READ_BUF_LEN: usize = 1024 * 1024;
/// Files are split into ranges of about this many bytes (a whole number of
/// pieces), each hashed by one thread.
const SEGMENT_LEN: u64 = 32 * 1024 * 1024;

static ZEROS: [u8; 64 * 1024] = [0; 64 * 1024];

/// The hashes of one file at one piece length.
///
/// They depend only on the file's bytes and `piece_length`, not on the
/// torrent the file ends up in, so they can be kept and reused for a file
/// that has not changed. `pieces_root` does not even depend on the piece
/// length.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileHashes {
    pub length: u64,
    pub piece_length: u32,
    /// The root of the file's merkle tree. `None` for an empty file.
    pub pieces_root: Option<Sha256Hash>,
    /// The tree's hashes at the piece level, one per piece. Empty when the
    /// file fits in one piece (such a file has no entry in `piece layers`).
    pub piece_layer: Vec<Sha256Hash>,
    /// Hybrid only: the SHA-1 of each piece, the last one hashed as if the
    /// file were padded with zeros to a whole piece (which is what the piece
    /// holds when a BEP 47 padding file follows).
    pub v1_pieces: Vec<Sha1Hash>,
    /// Hybrid only: the SHA-1 of the last piece without padding, for a file
    /// that nothing follows. `None` for an empty file.
    pub v1_last_unpadded: Option<Sha1Hash>,
}

impl FileHashes {
    pub(crate) fn num_pieces(length: u64, piece_length: u32) -> u64 {
        length.div_ceil(u64::from(piece_length))
    }

    pub(crate) fn has_v1(&self) -> bool {
        self.v1_pieces.len() as u64 == Self::num_pieces(self.length, self.piece_length)
            && self.v1_last_unpadded.is_some() == (self.length > 0)
    }
}

/// The root of a tree of `2^height` zero leaves: the padding hash at that
/// height. "The remaining leaf hashes beyond the end of the file required to
/// construct upper layers of the merkle tree are set to zero" (BEP 52).
fn pad_hash(height: u32) -> Sha256Hash {
    static PADS: OnceLock<[Sha256Hash; 64]> = OnceLock::new();
    PADS.get_or_init(|| {
        let mut pads = [[0u8; 32]; 64];
        for h in 1..64 {
            pads[h] = hash_pair(&pads[h - 1], &pads[h - 1]);
        }
        pads
    })[height as usize]
}

fn hash_pair(left: &Sha256Hash, right: &Sha256Hash) -> Sha256Hash {
    let mut h = Sha256::new();
    h.update(left);
    h.update(right);
    h.finalize().into()
}

/// The root `levels` above `layer`, whose hashes are `base_height` above the
/// leaves; missing hashes on the right are padding. `layer` is used as scratch
/// space and must not be empty or wider than `2^levels`.
fn reduce(layer: &mut Vec<Sha256Hash>, base_height: u32, levels: u32) -> Sha256Hash {
    debug_assert!(!layer.is_empty() && layer.len() as u64 <= 1u64 << levels);
    for level in 0..levels {
        let pad = pad_hash(base_height + level);
        let pairs = layer.len().div_ceil(2);
        for i in 0..pairs {
            let right = layer.get(2 * i + 1).copied().unwrap_or(pad);
            layer[i] = hash_pair(&layer[2 * i], &right);
        }
        layer.truncate(pairs);
    }
    layer[0]
}

/// log2 of the smallest power of two that is at least `n` (and at least 1).
fn ceil_log2(n: u64) -> u32 {
    n.max(1).next_power_of_two().trailing_zeros()
}

/// How a file is laid out as a tree.
#[derive(Debug, Clone, Copy)]
struct Shape {
    piece_length: u32,
    /// The height of the tree under one piece-layer hash. For a file that
    /// fits in one piece this is the height of the whole tree, which is only
    /// as tall as the file's own blocks need.
    piece_height: u32,
    /// Whether the file has a piece layer (is larger than one piece).
    has_layer: bool,
}

impl Shape {
    fn new(length: u64, piece_length: u32) -> Self {
        let has_layer = length > u64::from(piece_length);
        let piece_height = if has_layer {
            (piece_length / BLOCK_SIZE).trailing_zeros()
        } else {
            ceil_log2(length.div_ceil(u64::from(BLOCK_SIZE)))
        };
        Self {
            piece_length,
            piece_height,
            has_layer,
        }
    }
}

/// The hashes of a run of whole pieces of a file (the last piece of the file
/// may be short).
#[derive(Debug, Default)]
struct SegmentHashes {
    v2: Vec<Sha256Hash>,
    v1: Vec<Sha1Hash>,
    v1_last_unpadded: Option<Sha1Hash>,
}

fn read_full(file: &mut File, mut buf: &mut [u8]) -> std::io::Result<usize> {
    let mut total = 0;
    while !buf.is_empty() {
        match file.read(buf) {
            Ok(0) => break,
            Ok(n) => {
                total += n;
                buf = &mut buf[n..];
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(total)
}

/// What the hashing threads share.
struct Shared<'a> {
    version: Version,
    hashed: AtomicU64,
    total: u64,
    progress: Option<&'a (dyn Fn(Progress) + Sync)>,
    cancel: Option<&'a AtomicBool>,
}

impl Shared<'_> {
    fn advance(&self, bytes: u64) -> Result<(), Error> {
        if self.cancel.is_some_and(|c| c.load(Ordering::Relaxed)) {
            return Err(Error::Cancelled);
        }
        let hashed = self.hashed.fetch_add(bytes, Ordering::Relaxed) + bytes;
        if let Some(progress) = self.progress {
            progress(Progress {
                hashed_bytes: hashed,
                total_bytes: self.total,
            });
        }
        Ok(())
    }
}

/// Hashes `len` bytes of `file` from `offset`, which is on a piece boundary.
/// `end_of_file` says the range ends where the file is expected to end.
#[allow(clippy::too_many_arguments)]
fn hash_segment(
    file: &mut File,
    path: &Path,
    offset: u64,
    len: u64,
    end_of_file: bool,
    shape: Shape,
    buf: &mut [u8],
    shared: &Shared<'_>,
) -> Result<SegmentHashes, Error> {
    let io = |source| Error::Io {
        path: path.to_owned(),
        source,
    };
    let changed = || Error::FileChanged {
        path: path.to_owned(),
    };
    let want_v1 = shared.version == Version::Hybrid;
    let piece_length = u64::from(shape.piece_length);
    let block = BLOCK_SIZE as usize;

    file.seek(SeekFrom::Start(offset)).map_err(io)?;
    let mut out = SegmentHashes::default();
    let mut leaves: Vec<Sha256Hash> = Vec::with_capacity(1 << shape.piece_height.min(16));
    let mut remaining = len;
    while remaining > 0 {
        let piece_len = remaining.min(piece_length);
        let mut sha1 = want_v1.then(Sha1::new);
        leaves.clear();
        let mut left = piece_len;
        while left > 0 {
            let want = usize::try_from(left).map_or(buf.len(), |l| l.min(buf.len()));
            let chunk = &mut buf[..want];
            if read_full(file, chunk).map_err(io)? != want {
                return Err(changed());
            }
            for b in chunk.chunks(block) {
                leaves.push(Sha256::digest(b).into());
            }
            if let Some(sha1) = sha1.as_mut() {
                sha1.update(&*chunk);
            }
            left -= want as u64;
            shared.advance(want as u64)?;
        }
        out.v2.push(reduce(&mut leaves, 0, shape.piece_height));
        if let Some(mut sha1) = sha1 {
            if piece_len < piece_length {
                out.v1_last_unpadded = Some(sha1.clone().finalize().into());
                let mut pad = piece_length - piece_len;
                while pad > 0 {
                    let take = usize::try_from(pad).map_or(ZEROS.len(), |p| p.min(ZEROS.len()));
                    sha1.update(&ZEROS[..take]);
                    pad -= take as u64;
                }
                out.v1.push(sha1.finalize().into());
            } else {
                let hash: Sha1Hash = sha1.finalize().into();
                out.v1.push(hash);
                if end_of_file && remaining == piece_len {
                    out.v1_last_unpadded = Some(hash);
                }
            }
        }
        remaining -= piece_len;
    }
    if end_of_file && read_full(file, &mut buf[..1]).map_err(io)? != 0 {
        return Err(changed());
    }
    Ok(out)
}

/// One range of one file for a thread to hash.
struct Job {
    file: usize,
    offset: u64,
    len: u64,
    end_of_file: bool,
}

/// How far hashing has come.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Progress {
    /// Bytes of file content hashed so far.
    pub hashed_bytes: u64,
    /// Bytes of file content in total.
    pub total_bytes: u64,
}

/// How [`hash_files`] and [`crate::create`] do the work. None of this changes
/// the result.
#[derive(Clone, Copy, Default)]
pub struct HashOptions<'a> {
    /// How many threads hash. 0 uses one per CPU. Use 1 for a spinning disk.
    pub threads: usize,
    /// Called from the hashing threads after every read (at most 1 MiB), so it
    /// must be quick and may be called from several threads at once.
    pub progress: Option<&'a (dyn Fn(Progress) + Sync)>,
    /// Set it to stop: the call then returns [`Error::Cancelled`].
    pub cancel: Option<&'a AtomicBool>,
}

/// Hashes the files at `sources`, in parallel. The result has one entry per
/// source, in the same order.
///
/// A file that changes size while it is read is an error; a file whose bytes
/// change without its size changing cannot be detected.
pub fn hash_files(
    sources: &[PathBuf],
    piece_length: u32,
    version: Version,
    options: &HashOptions<'_>,
) -> Result<Vec<FileHashes>, Error> {
    crate::check_piece_length(piece_length)?;
    let pl = u64::from(piece_length);
    let mut lengths = Vec::with_capacity(sources.len());
    let mut total = 0u64;
    for path in sources {
        let metadata = std::fs::metadata(path).map_err(|source| Error::Io {
            path: path.clone(),
            source,
        })?;
        if !metadata.is_file() {
            return Err(Error::NotAFile { path: path.clone() });
        }
        lengths.push(metadata.len());
        total = total
            .checked_add(metadata.len())
            .ok_or(Error::TooMuchData)?;
    }

    // Ranges of whole pieces, in file order, so that with one thread the
    // files are read front to back.
    let segment = SEGMENT_LEN.div_ceil(pl) * pl;
    let mut jobs = Vec::new();
    let mut first_job = Vec::with_capacity(sources.len() + 1);
    for (file, &length) in lengths.iter().enumerate() {
        first_job.push(jobs.len());
        let mut offset = 0;
        while offset < length {
            let len = segment.min(length - offset);
            jobs.push(Job {
                file,
                offset,
                len,
                end_of_file: offset + len == length,
            });
            offset += len;
        }
    }
    first_job.push(jobs.len());

    let shared = Shared {
        version,
        hashed: AtomicU64::new(0),
        total,
        progress: options.progress,
        cancel: options.cancel,
    };
    let threads = match options.threads {
        0 => std::thread::available_parallelism().map_or(1, |n| n.get()),
        n => n,
    }
    .min(jobs.len().max(1));
    let next = AtomicUsize::new(0);
    let failed = AtomicBool::new(false);
    let results: Mutex<Vec<Option<SegmentHashes>>> =
        Mutex::new((0..jobs.len()).map(|_| None).collect());
    let first_error: Mutex<Option<Error>> = Mutex::new(None);

    let worker = || {
        let mut buf = vec![0u8; READ_BUF_LEN];
        let mut open: Option<(usize, File)> = None;
        loop {
            let index = next.fetch_add(1, Ordering::Relaxed);
            let Some(job) = jobs.get(index) else { break };
            if failed.load(Ordering::Relaxed) {
                break;
            }
            let path = &sources[job.file];
            let result = (|| {
                if open.as_ref().is_none_or(|(file, _)| *file != job.file) {
                    let file = File::open(path).map_err(|source| Error::Io {
                        path: path.clone(),
                        source,
                    })?;
                    open = Some((job.file, file));
                }
                let (_, file) = open.as_mut().expect("just opened");
                hash_segment(
                    file,
                    path,
                    job.offset,
                    job.len,
                    job.end_of_file,
                    Shape::new(lengths[job.file], piece_length),
                    &mut buf,
                    &shared,
                )
            })();
            match result {
                Ok(hashes) => results.lock().unwrap()[index] = Some(hashes),
                Err(e) => {
                    failed.store(true, Ordering::Relaxed);
                    first_error.lock().unwrap().get_or_insert(e);
                    break;
                }
            }
        }
    };
    if threads <= 1 {
        worker();
    } else {
        std::thread::scope(|scope| {
            for _ in 0..threads {
                scope.spawn(worker);
            }
        });
    }
    if let Some(e) = first_error.into_inner().unwrap() {
        return Err(e);
    }
    // A file that was empty when it was listed is not opened at all.
    if shared.cancel.is_some_and(|c| c.load(Ordering::Relaxed)) {
        return Err(Error::Cancelled);
    }

    let mut results = results.into_inner().unwrap();
    let mut out = Vec::with_capacity(sources.len());
    for (file, &length) in lengths.iter().enumerate() {
        let shape = Shape::new(length, piece_length);
        let mut layer = Vec::new();
        let mut v1_pieces = Vec::new();
        let mut v1_last_unpadded = None;
        for result in &mut results[first_job[file]..first_job[file + 1]] {
            let segment = result.take().expect("every job ran");
            layer.extend(segment.v2);
            v1_pieces.extend(segment.v1);
            v1_last_unpadded = segment.v1_last_unpadded;
        }
        let (pieces_root, piece_layer) = match (length, shape.has_layer) {
            (0, _) => (None, Vec::new()),
            (_, false) => (Some(layer[0]), Vec::new()),
            (_, true) => {
                let mut scratch = layer.clone();
                let levels = ceil_log2(layer.len() as u64);
                (
                    Some(reduce(&mut scratch, shape.piece_height, levels)),
                    layer,
                )
            }
        };
        out.push(FileHashes {
            length,
            piece_length,
            pieces_root,
            piece_layer,
            v1_pieces,
            v1_last_unpadded,
        });
    }
    Ok(out)
}

/// Hashes one file on the calling thread. See [`hash_files`].
pub fn hash_file(source: &Path, piece_length: u32, version: Version) -> Result<FileHashes, Error> {
    let options = HashOptions {
        threads: 1,
        ..Default::default()
    };
    let mut hashes = hash_files(&[source.to_owned()], piece_length, version, &options)?;
    Ok(hashes.remove(0))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(h: &[u8]) -> String {
        h.iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn padding_hashes() {
        assert_eq!(pad_hash(0), [0u8; 32]);
        // SHA-256 of 64 zero bytes.
        assert_eq!(
            hex(&pad_hash(1)),
            "f5a5fd42d16a20302798ef6ed309979b43003d2320d9f0e8ea9831a92759fb4b"
        );
        assert_eq!(pad_hash(2), hash_pair(&pad_hash(1), &pad_hash(1)));
    }

    #[test]
    fn reduce_pads_on_the_right() {
        let a = [1u8; 32];
        let b = [2u8; 32];
        let c = [3u8; 32];
        // One hash and no levels: the hash itself.
        assert_eq!(reduce(&mut vec![a], 0, 0), a);
        assert_eq!(reduce(&mut vec![a, b], 0, 1), hash_pair(&a, &b));
        // Three hashes in a tree of four: the fourth is the padding hash.
        let expected = hash_pair(&hash_pair(&a, &b), &hash_pair(&c, &[0u8; 32]));
        assert_eq!(reduce(&mut vec![a, b, c], 0, 2), expected);
        // The same one level up: the padding is the root of two zero leaves.
        let expected = hash_pair(&hash_pair(&a, &b), &hash_pair(&c, &pad_hash(1)));
        assert_eq!(reduce(&mut vec![a, b, c], 1, 2), expected);
        // One hash in a taller tree is paired with padding all the way up.
        let expected = hash_pair(&hash_pair(&a, &pad_hash(0)), &pad_hash(1));
        assert_eq!(reduce(&mut vec![a], 0, 2), expected);
    }

    #[test]
    fn tree_shapes() {
        let pl = 4 * BLOCK_SIZE;
        // A file in one piece: only as tall as its own blocks need.
        assert_eq!(Shape::new(1, pl).piece_height, 0);
        assert_eq!(Shape::new(u64::from(BLOCK_SIZE), pl).piece_height, 0);
        assert_eq!(Shape::new(u64::from(BLOCK_SIZE) + 1, pl).piece_height, 1);
        assert_eq!(Shape::new(3 * u64::from(BLOCK_SIZE), pl).piece_height, 2);
        assert!(!Shape::new(u64::from(pl), pl).has_layer);
        // Larger than a piece: pieces are whole subtrees.
        let s = Shape::new(u64::from(pl) + 1, pl);
        assert!(s.has_layer);
        assert_eq!(s.piece_height, 2);
    }
}
