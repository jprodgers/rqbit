//! Deterministic creation of BitTorrent v2 ([BEP 52]) and hybrid v1+v2
//! torrents from files on disk.
//!
//! - **No client, no async runtime, pure Rust.** The only dependencies are the
//!   RustCrypto `sha1` and `sha2` crates.
//! - **Deterministic.** The output is a function of the files' bytes, their
//!   paths in the torrent, and the options. Files are put in the order of the
//!   bencoded `file tree` whatever order they are given in, nothing depends on
//!   the order files are read in or on the number of threads, and no
//!   `creation date` or `created by` is written unless asked for. Two people
//!   who make a torrent of the same data with the same options get the same
//!   bytes and so the same info hashes.
//! - **Each file's `pieces root`** is returned, to record next to the file.
//!
//! ```no_run
//! use librqbit_create_torrent::{create, CreateOptions, HashOptions, InputFile};
//!
//! let files = vec![
//!     InputFile::new(["objects", "ab", "abcd.parquet"], "/data/objects/ab/abcd.parquet"),
//!     InputFile::new(["manifest.json"], "/data/manifest.json"),
//! ];
//! let options = CreateOptions {
//!     trackers: vec![vec!["https://tracker.example/announce".into()]],
//!     web_seeds: vec!["https://data.example/v1/".into()],
//!     ..CreateOptions::new("my-data-v1", 1 << 20)
//! };
//! let torrent = create(&files, &options, &HashOptions::default())?;
//! std::fs::write("my-data-v1.torrent", &torrent.bytes)?;
//! println!("{}", torrent.magnet);
//! for file in &torrent.files {
//!     println!("{:?} {:?}", file.path, file.pieces_root);
//! }
//! # Ok::<(), Box<dyn std::error::Error>>(())
//! ```
//!
//! # Hashing once, building many times
//!
//! [`create`] is [`hash_files`] followed by [`build`]. A file's
//! [`FileHashes`] depend only on its bytes and the piece length, so when most
//! files are the same from one version of a data set to the next, keep their
//! hashes and call [`build`] with them: only new files need reading.
//!
//! # The layout of a hybrid torrent
//!
//! The v1 half of a hybrid torrent needs [BEP 47] padding files so that every
//! file starts on a piece boundary. BEP 52 leaves one thing open: whether a
//! padding file follows the *last* file. The choice is part of the info
//! dictionary, so it changes both info hashes. This crate does what
//! libtorrent (and so qBittorrent) does by default: a padding file follows
//! every non-empty file that does not end on a piece boundary, the last one
//! included, except in a torrent of exactly one file, which has none. Padding
//! files are named `.pad/<length>`.
//!
//! [BEP 52]: https://www.bittorrent.org/beps/bep_0052.html
//! [BEP 47]: https://www.bittorrent.org/beps/bep_0047.html

mod bencode;
mod hash;

use std::fmt;
use std::path::{Path, PathBuf};

use sha1::{Digest, Sha1};
use sha2::Sha256;

use crate::bencode::{Dict, Value};
pub use crate::hash::{
    FileHashes, HashOptions, Progress, Sha1Hash, Sha256Hash, hash_file, hash_files,
};

/// The size of a leaf of a BEP 52 merkle tree, and the smallest piece length.
pub const BLOCK_SIZE: u32 = 16 * 1024;
/// The largest piece length accepted. libtorrent refuses larger ones.
pub const MAX_PIECE_LENGTH: u32 = 128 * 1024 * 1024;

/// Which kind of torrent to make.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Version {
    /// v1 and v2 in one torrent: v1-only clients, v2 clients and web seeds
    /// can all use it.
    #[default]
    Hybrid,
    /// v2 only. Clients without BEP 52 support cannot open it.
    V2,
}

/// What can go wrong.
#[derive(Debug)]
#[non_exhaustive]
pub enum Error {
    /// The piece length is not a power of two from 16 KiB to 128 MiB.
    InvalidPieceLength(u32),
    /// The torrent's name is empty, `.`, `..`, or has `/`, `\` or NUL in it.
    InvalidName(String),
    /// A path in the torrent is empty, or has a component that is empty, `.`,
    /// `..`, or contains `/`, `\` or NUL; or, in a hybrid torrent, it starts
    /// with `.pad`, where the padding files go.
    InvalidPath(Vec<String>),
    /// Two files have this path, or it is a file for one and a folder for
    /// another.
    PathConflict(Vec<String>),
    /// There are no files, or every file is empty.
    NoData,
    /// More data than a torrent can describe (2^32 pieces at most).
    TooMuchData,
    /// These hashes were made for another piece length, or without the SHA-1
    /// hashes a hybrid torrent needs.
    HashesDoNotFit(Vec<String>),
    /// A tracker or web seed is empty or has a control character in it.
    InvalidUrl(String),
    /// Reading a file failed.
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    /// Not a regular file (a directory, a socket, ...).
    NotAFile { path: PathBuf },
    /// The file's size changed while it was being hashed.
    FileChanged { path: PathBuf },
    /// [`HashOptions::cancel`] was set.
    Cancelled,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidPieceLength(l) => write!(
                f,
                "invalid piece length {l}: it must be a power of two from 16 KiB to 128 MiB"
            ),
            Error::InvalidName(n) => write!(f, "invalid torrent name {n:?}"),
            Error::InvalidPath(p) => write!(f, "invalid path in the torrent: {p:?}"),
            Error::PathConflict(p) => write!(f, "more than one file at {p:?} in the torrent"),
            Error::NoData => write!(
                f,
                "nothing to make a torrent of: no files, or only empty ones"
            ),
            Error::TooMuchData => write!(f, "too much data: use a larger piece length"),
            Error::HashesDoNotFit(p) => write!(
                f,
                "the hashes of {p:?} were made for another piece length or torrent version"
            ),
            Error::InvalidUrl(u) => write!(f, "invalid tracker or web seed {u:?}"),
            Error::Io { path, source } => write!(f, "error reading {path:?}: {source}"),
            Error::NotAFile { path } => write!(f, "{path:?} is not a regular file"),
            Error::FileChanged { path } => {
                write!(f, "{path:?} changed size while it was being hashed")
            }
            Error::Cancelled => write!(f, "cancelled"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

pub(crate) fn check_piece_length(piece_length: u32) -> Result<(), Error> {
    if piece_length.is_power_of_two() && (BLOCK_SIZE..=MAX_PIECE_LENGTH).contains(&piece_length) {
        Ok(())
    } else {
        Err(Error::InvalidPieceLength(piece_length))
    }
}

/// A file to put in the torrent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InputFile {
    /// Its path in the torrent, one component per folder and then the file
    /// name. See [`CreateOptions::name`] for how this relates to the name.
    pub path: Vec<String>,
    /// Where its bytes are read from.
    pub source: PathBuf,
}

impl InputFile {
    pub fn new<S: Into<String>>(
        path: impl IntoIterator<Item = S>,
        source: impl Into<PathBuf>,
    ) -> Self {
        Self {
            path: path.into_iter().map(Into::into).collect(),
            source: source.into(),
        }
    }
}

/// A file whose hashes are known, for [`build`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HashedFile {
    /// Its path in the torrent, as in [`InputFile::path`].
    pub path: Vec<String>,
    pub hashes: FileHashes,
}

/// What goes in the torrent besides the files.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateOptions {
    /// The torrent's name. Clients save a multi-file torrent in a folder of
    /// this name, with the files at their paths under it.
    ///
    /// A torrent whose only file has the path `[name]` is a single-file
    /// torrent: clients save that file under this name, without a folder.
    pub name: String,
    /// A power of two from 16 KiB ([`BLOCK_SIZE`]) to 128 MiB.
    pub piece_length: u32,
    pub version: Version,
    /// Tracker tiers ([BEP 12]), in order: clients try a tier's trackers
    /// before moving to the next tier. One tier of one URL is the usual case.
    ///
    /// [BEP 12]: https://www.bittorrent.org/beps/bep_0012.html
    pub trackers: Vec<Vec<String>>,
    /// Web seeds ([BEP 19] `url-list`). For a multi-file torrent, a URL that
    /// ends in `/` is the folder holding the torrent's folder.
    ///
    /// [BEP 19]: https://www.bittorrent.org/beps/bep_0019.html
    pub web_seeds: Vec<String>,
    pub comment: Option<String>,
    /// `created by`. Not written when `None`.
    pub created_by: Option<String>,
    /// `creation date`, in seconds since 1970. Not written when `None`.
    pub creation_date: Option<u64>,
    /// The private flag ([BEP 27]). It is part of the info dictionary, so it
    /// changes the info hashes.
    ///
    /// [BEP 27]: https://www.bittorrent.org/beps/bep_0027.html
    pub private: bool,
}

impl CreateOptions {
    /// A hybrid torrent with this name and piece length and nothing else.
    pub fn new(name: impl Into<String>, piece_length: u32) -> Self {
        Self {
            name: name.into(),
            piece_length,
            version: Version::Hybrid,
            trackers: Vec::new(),
            web_seeds: Vec::new(),
            comment: None,
            created_by: None,
            creation_date: None,
            private: false,
        }
    }
}

/// A file of a created torrent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TorrentFile {
    pub path: Vec<String>,
    pub length: u64,
    /// The root of the file's merkle tree: the same in every v2 or hybrid
    /// torrent that has a file with these bytes, whatever its piece length.
    /// `None` for an empty file.
    pub pieces_root: Option<Sha256Hash>,
}

/// A created torrent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Torrent {
    /// The `.torrent` file.
    pub bytes: Vec<u8>,
    /// The v1 info hash (SHA-1 of the info dictionary). `None` for a v2-only
    /// torrent.
    pub info_hash_v1: Option<Sha1Hash>,
    /// The v2 info hash (SHA-256 of the info dictionary).
    pub info_hash_v2: Sha256Hash,
    /// A magnet link with the info hashes (`btih` for a hybrid, and `btmh`),
    /// the name, the trackers and the web seeds.
    pub magnet: String,
    /// The files in torrent order (padding files are not listed).
    pub files: Vec<TorrentFile>,
}

fn valid_component(c: &str) -> bool {
    !c.is_empty() && c != "." && c != ".." && !c.contains(['/', '\\', '\0'])
}

fn check_url(url: &str) -> Result<(), Error> {
    if url.is_empty() || url.chars().any(char::is_control) {
        return Err(Error::InvalidUrl(url.to_owned()));
    }
    Ok(())
}

fn percent_encode(s: &str, out: &mut String) {
    for &b in s.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Puts `leaf` at `path` in the file tree.
fn insert_file(tree: &mut Dict, path: &[String], leaf: Dict) -> Result<(), Error> {
    let conflict = || Error::PathConflict(path.to_vec());
    let (name, folders) = path.split_last().expect("paths are not empty");
    let mut node = tree;
    for folder in folders {
        let next = node
            .entry(folder.as_bytes().to_vec())
            .or_insert_with(|| Value::Dict(Dict::new()));
        let Value::Dict(next) = next else {
            unreachable!("the tree holds dictionaries only")
        };
        // A file is a dictionary with the one key "".
        if next.contains_key(b"".as_slice()) {
            return Err(conflict());
        }
        node = next;
    }
    if node.contains_key(name.as_bytes()) {
        return Err(conflict());
    }
    let mut file = Dict::new();
    file.insert(Vec::new(), Value::Dict(leaf));
    node.insert(name.as_bytes().to_vec(), Value::Dict(file));
    Ok(())
}

/// Makes the torrent of files whose hashes are known. Reads nothing from
/// disk; the same arguments always give the same bytes.
pub fn build(files: &[HashedFile], options: &CreateOptions) -> Result<Torrent, Error> {
    let piece_length = options.piece_length;
    check_piece_length(piece_length)?;
    let pl = u64::from(piece_length);
    let hybrid = options.version == Version::Hybrid;
    if !valid_component(&options.name) {
        return Err(Error::InvalidName(options.name.clone()));
    }
    for url in options.trackers.iter().flatten().chain(&options.web_seeds) {
        check_url(url)?;
    }

    // Torrent order: the order of the keys of the bencoded file tree, which
    // is the order of the paths compared component by component as bytes.
    let mut files: Vec<&HashedFile> = files.iter().collect();
    files.sort_by(|a, b| a.path.cmp(&b.path));
    for file in &files {
        let h = &file.hashes;
        // In a hybrid torrent the padding files are `.pad/<length>`.
        let reserved = hybrid && file.path.first().is_some_and(|c| c == ".pad");
        if file.path.is_empty() || !file.path.iter().all(|c| valid_component(c)) || reserved {
            return Err(Error::InvalidPath(file.path.clone()));
        }
        let pieces = FileHashes::num_pieces(h.length, piece_length);
        let layer_len = if pieces > 1 { pieces } else { 0 };
        if h.piece_length != piece_length
            || h.pieces_root.is_some() != (h.length > 0)
            || h.piece_layer.len() as u64 != layer_len
            || (hybrid && !h.has_v1())
        {
            return Err(Error::HashesDoNotFit(file.path.clone()));
        }
    }
    if files.iter().all(|f| f.hashes.length == 0) {
        return Err(Error::NoData);
    }
    let single_file = files.len() == 1 && files[0].path == [options.name.as_str()];

    let mut tree = Dict::new();
    let mut piece_layers = Dict::new();
    let mut v1_files = Vec::new();
    let mut v1_pieces: Vec<u8> = Vec::new();
    let mut v1_length = 0u64;
    let mut out_files = Vec::with_capacity(files.len());
    for file in &files {
        let h = &file.hashes;
        let mut leaf = Dict::new();
        leaf.insert(b"length".to_vec(), Value::Int(h.length));
        if let Some(root) = h.pieces_root {
            leaf.insert(b"pieces root".to_vec(), Value::Bytes(root.to_vec()));
            if !h.piece_layer.is_empty() {
                // Files with the same bytes share an entry.
                piece_layers
                    .entry(root.to_vec())
                    .or_insert_with(|| Value::Bytes(h.piece_layer.concat()));
            }
        }
        insert_file(&mut tree, &file.path, leaf)?;
        out_files.push(TorrentFile {
            path: file.path.clone(),
            length: h.length,
            pieces_root: h.pieces_root,
        });

        if !hybrid {
            continue;
        }
        let mut entry = Dict::new();
        entry.insert(b"length".to_vec(), Value::Int(h.length));
        entry.insert(
            b"path".to_vec(),
            Value::List(file.path.iter().map(|c| Value::str(c)).collect()),
        );
        v1_files.push(Value::Dict(entry));
        v1_length = v1_length.checked_add(h.length).ok_or(Error::TooMuchData)?;

        let tail = h.length % pl;
        let padded = files.len() > 1 && tail != 0;
        if let Some((last, full)) = h.v1_pieces.split_last() {
            v1_pieces.extend(full.iter().flatten());
            let last = if padded {
                last
            } else {
                h.v1_last_unpadded.as_ref().expect("checked by has_v1")
            };
            v1_pieces.extend_from_slice(last);
        }
        if padded {
            let pad = pl - tail;
            let mut entry = Dict::new();
            entry.insert(b"attr".to_vec(), Value::str("p"));
            entry.insert(b"length".to_vec(), Value::Int(pad));
            entry.insert(
                b"path".to_vec(),
                Value::List(vec![Value::str(".pad"), Value::str(&pad.to_string())]),
            );
            v1_files.push(Value::Dict(entry));
            v1_length = v1_length.checked_add(pad).ok_or(Error::TooMuchData)?;
        }
    }
    let most_pieces = if hybrid {
        v1_length.div_ceil(pl)
    } else {
        files
            .iter()
            .map(|f| FileHashes::num_pieces(f.hashes.length, piece_length))
            .max()
            .unwrap_or(0)
    };
    if most_pieces > u64::from(u32::MAX) {
        return Err(Error::TooMuchData);
    }

    let mut info = Dict::new();
    info.insert(b"file tree".to_vec(), Value::Dict(tree));
    info.insert(b"meta version".to_vec(), Value::Int(2));
    info.insert(b"name".to_vec(), Value::str(&options.name));
    info.insert(b"piece length".to_vec(), Value::Int(pl));
    if options.private {
        info.insert(b"private".to_vec(), Value::Int(1));
    }
    if hybrid {
        info.insert(b"pieces".to_vec(), Value::Bytes(v1_pieces));
        if single_file {
            info.insert(b"length".to_vec(), Value::Int(files[0].hashes.length));
        } else {
            info.insert(b"files".to_vec(), Value::List(v1_files));
        }
    }
    let info = Value::Dict(info);
    let mut raw_info = Vec::new();
    info.encode(&mut raw_info);
    let info_hash_v1: Option<Sha1Hash> = hybrid.then(|| Sha1::digest(&raw_info).into());
    let info_hash_v2: Sha256Hash = Sha256::digest(&raw_info).into();

    let mut torrent = Dict::new();
    torrent.insert(b"info".to_vec(), info);
    torrent.insert(b"piece layers".to_vec(), Value::Dict(piece_layers));
    // Like libtorrent: `announce` is the first tracker, and `announce-list`
    // is there when there is more than one.
    if let Some(first) = options.trackers.iter().flatten().next() {
        torrent.insert(b"announce".to_vec(), Value::str(first));
    }
    if options.trackers.iter().flatten().count() > 1 {
        let tiers = options
            .trackers
            .iter()
            .filter(|tier| !tier.is_empty())
            .map(|tier| Value::List(tier.iter().map(|t| Value::str(t)).collect()))
            .collect();
        torrent.insert(b"announce-list".to_vec(), Value::List(tiers));
    }
    // Like libtorrent: one web seed is a string, several are a list.
    match options.web_seeds.as_slice() {
        [] => {}
        [one] => {
            torrent.insert(b"url-list".to_vec(), Value::str(one));
        }
        many => {
            let list = many.iter().map(|u| Value::str(u)).collect();
            torrent.insert(b"url-list".to_vec(), Value::List(list));
        }
    }
    if let Some(comment) = &options.comment {
        torrent.insert(b"comment".to_vec(), Value::str(comment));
    }
    if let Some(created_by) = &options.created_by {
        torrent.insert(b"created by".to_vec(), Value::str(created_by));
    }
    if let Some(date) = options.creation_date {
        torrent.insert(b"creation date".to_vec(), Value::Int(date));
    }
    let mut bytes = Vec::new();
    Value::Dict(torrent).encode(&mut bytes);

    let mut magnet = String::from("magnet:?");
    if let Some(v1) = &info_hash_v1 {
        magnet.push_str(&format!("xt=urn:btih:{}&", hex(v1)));
    }
    // A multihash: 0x12 is SHA-256, 0x20 its length.
    magnet.push_str(&format!("xt=urn:btmh:1220{}&dn=", hex(&info_hash_v2)));
    percent_encode(&options.name, &mut magnet);
    for tracker in options.trackers.iter().flatten() {
        magnet.push_str("&tr=");
        percent_encode(tracker, &mut magnet);
    }
    for web_seed in &options.web_seeds {
        magnet.push_str("&ws=");
        percent_encode(web_seed, &mut magnet);
    }

    Ok(Torrent {
        bytes,
        info_hash_v1,
        info_hash_v2,
        magnet,
        files: out_files,
    })
}

/// Hashes `files` and makes their torrent: [`hash_files`], then [`build`].
pub fn create(
    files: &[InputFile],
    options: &CreateOptions,
    hash_options: &HashOptions<'_>,
) -> Result<Torrent, Error> {
    let sources: Vec<PathBuf> = files.iter().map(|f| f.source.clone()).collect();
    let hashes = hash_files(
        &sources,
        options.piece_length,
        options.version,
        hash_options,
    )?;
    let hashed: Vec<HashedFile> = files
        .iter()
        .zip(hashes)
        .map(|(file, hashes)| HashedFile {
            path: file.path.clone(),
            hashes,
        })
        .collect();
    build(&hashed, options)
}

/// Every regular file under `folder`, with its path relative to `folder` as
/// its path in the torrent. Links are followed. A name that is not UTF-8 is
/// an error. The order of the result is not defined (and does not matter to
/// [`create`]).
pub fn list_folder(folder: &Path) -> Result<Vec<InputFile>, Error> {
    fn visit(dir: &Path, prefix: &mut Vec<String>, out: &mut Vec<InputFile>) -> Result<(), Error> {
        let io = |source| Error::Io {
            path: dir.to_owned(),
            source,
        };
        for entry in std::fs::read_dir(dir).map_err(io)? {
            let entry = entry.map_err(io)?;
            let path = entry.path();
            let name = entry.file_name().into_string().map_err(|_| Error::Io {
                path: path.clone(),
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "the name is not valid UTF-8",
                ),
            })?;
            let metadata = std::fs::metadata(&path).map_err(|source| Error::Io {
                path: path.clone(),
                source,
            })?;
            prefix.push(name);
            if metadata.is_dir() {
                visit(&path, prefix, out)?;
            } else if metadata.is_file() {
                out.push(InputFile {
                    path: prefix.clone(),
                    source: path,
                });
            } else {
                return Err(Error::NotAFile { path });
            }
            prefix.pop();
        }
        Ok(())
    }
    let mut out = Vec::new();
    visit(folder, &mut Vec::new(), &mut out)?;
    Ok(out)
}
