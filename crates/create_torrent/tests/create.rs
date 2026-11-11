//! Tests against torrents made by libtorrent, and of the promises in the
//! crate documentation.
//!
//! `tests/fixtures` holds `.torrent` files made by libtorrent 2.1.1 with
//! `scripts/bep52/make_fixtures.py` from files whose bytes follow a formula;
//! the tests make the same files again and expect the very same torrents.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use librqbit_create_torrent::{
    BLOCK_SIZE, CreateOptions, Error, HashOptions, HashedFile, InputFile, Progress, Version, build,
    create, hash_file, hash_files, list_folder,
};

const FILES: &str = include_str!("fixtures/files.txt");
const P: u32 = 2 * BLOCK_SIZE;

/// The same bytes as `content()` in `make_fixtures.py`.
fn content(seed: u64, size: u64) -> Vec<u8> {
    (0..size)
        .map(|i| (((i + seed) as u32).wrapping_mul(2654435761) >> 24) as u8)
        .collect()
}

/// A data set of the fixtures, written under `dir`.
fn write_set(dir: &Path, set: &str) -> Vec<InputFile> {
    let mut files = Vec::new();
    for line in FILES.lines() {
        let mut fields = line.splitn(4, ' ');
        if fields.next() != Some(set) {
            continue;
        }
        let seed: u64 = fields.next().unwrap().parse().unwrap();
        let size: u64 = fields.next().unwrap().parse().unwrap();
        let path: Vec<&str> = fields.next().unwrap().split('/').collect();
        let source = dir.join(format!("{set}-{}", files.len()));
        std::fs::write(&source, content(seed, size)).unwrap();
        files.push(InputFile::new(path, source));
    }
    assert!(!files.is_empty(), "no such set: {set}");
    files
}

fn fixture(name: &str) -> Vec<u8> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    std::fs::read(&path).unwrap_or_else(|e| panic!("{path:?}: {e}"))
}

fn like_the_fixtures(name: &str, piece_length: u32, version: Version) -> CreateOptions {
    CreateOptions {
        version,
        created_by: Some("libtorrent (fixture)".into()),
        creation_date: Some(1_700_000_000),
        ..CreateOptions::new(name, piece_length)
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn same_bytes_as_libtorrent() {
    let dir = tempfile::tempdir().unwrap();
    // (set, torrent name, piece lengths)
    let sets: [(&str, &str, &[u32]); 7] = [
        ("store", "store", &[P, 2 * P]),
        ("lone", "lone.bin", &[P]),
        ("one", "one", &[P]),
        ("empty_first", "empty_first", &[P]),
        ("empty_last", "empty_last", &[P]),
        ("aligned", "aligned", &[P]),
        ("twins", "twins", &[P]),
    ];
    for (set, name, piece_lengths) in sets {
        let files = write_set(dir.path(), set);
        for &piece_length in piece_lengths {
            for (label, version) in [("hybrid", Version::Hybrid), ("v2", Version::V2)] {
                let options = like_the_fixtures(name, piece_length, version);
                let torrent = create(&files, &options, &HashOptions::default()).unwrap();
                let expected = fixture(&format!("{set}.{label}.{piece_length}.torrent"));
                assert!(
                    torrent.bytes == expected,
                    "{set} {label} {piece_length}: not the bytes libtorrent made"
                );
                assert_eq!(torrent.info_hash_v1.is_some(), version == Version::Hybrid);
            }
        }
    }
}

#[test]
fn does_not_depend_on_input_order_or_threads() {
    let dir = tempfile::tempdir().unwrap();
    let mut files = write_set(dir.path(), "store");
    let options = CreateOptions::new("store", P);
    let one_thread = HashOptions {
        threads: 1,
        ..Default::default()
    };
    let expected = create(&files, &options, &one_thread).unwrap();
    files.reverse();
    for threads in [1, 2, 7] {
        let hash_options = HashOptions {
            threads,
            ..Default::default()
        };
        assert_eq!(create(&files, &options, &hash_options).unwrap(), expected);
    }
    // No `created by` and no `creation date` unless asked for.
    let text = String::from_utf8_lossy(&expected.bytes);
    assert!(!text.contains("created by") && !text.contains("creation date"));
}

#[test]
fn build_from_kept_hashes_is_create() {
    let dir = tempfile::tempdir().unwrap();
    let files = write_set(dir.path(), "store");
    for version in [Version::Hybrid, Version::V2] {
        let options = CreateOptions {
            version,
            ..CreateOptions::new("store", P)
        };
        let expected = create(&files, &options, &HashOptions::default()).unwrap();
        let hashed: Vec<HashedFile> = files
            .iter()
            .map(|f| HashedFile {
                path: f.path.clone(),
                hashes: hash_file(&f.source, P, version).unwrap(),
            })
            .collect();
        assert_eq!(build(&hashed, &options).unwrap(), expected);

        // A subset is a torrent of its own, from the same hashes.
        let subset: Vec<HashedFile> = hashed.iter().take(5).cloned().collect();
        let subset_files: Vec<InputFile> = files.iter().take(5).cloned().collect();
        assert_eq!(
            build(&subset, &options).unwrap(),
            create(&subset_files, &options, &HashOptions::default()).unwrap()
        );
    }
    // Hashes made without SHA-1 cannot make a hybrid torrent.
    let v2_only = HashedFile {
        path: vec!["a".into()],
        hashes: hash_file(&files[0].source, P, Version::V2).unwrap(),
    };
    let err = build(std::slice::from_ref(&v2_only), &CreateOptions::new("t", P)).unwrap_err();
    assert!(matches!(err, Error::HashesDoNotFit(_)), "{err}");
    // Nor do hashes for another piece length fit.
    let options = CreateOptions {
        version: Version::V2,
        ..CreateOptions::new("t", 2 * P)
    };
    let err = build(&[v2_only], &options).unwrap_err();
    assert!(matches!(err, Error::HashesDoNotFit(_)), "{err}");
}

#[test]
fn pieces_root_does_not_depend_on_piece_length_or_torrent() {
    let dir = tempfile::tempdir().unwrap();
    let files = write_set(dir.path(), "store");
    let mut roots: BTreeMap<Vec<String>, Vec<Option<[u8; 32]>>> = BTreeMap::new();
    for piece_length in [BLOCK_SIZE, P, 8 * P, 1 << 22] {
        for version in [Version::Hybrid, Version::V2] {
            let options = CreateOptions {
                version,
                ..CreateOptions::new("another name", piece_length)
            };
            let torrent = create(&files, &options, &HashOptions::default()).unwrap();
            for file in torrent.files {
                roots.entry(file.path).or_default().push(file.pieces_root);
            }
        }
    }
    assert_eq!(roots.len(), files.len());
    for (path, roots) in roots {
        assert!(roots.windows(2).all(|w| w[0] == w[1]), "{path:?}");
    }
}

#[test]
fn known_roots() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("f");
    // One block of zeros: the root is the SHA-256 of the block.
    std::fs::write(&path, vec![0u8; BLOCK_SIZE as usize]).unwrap();
    let h = hash_file(&path, P, Version::Hybrid).unwrap();
    assert_eq!(
        hex(&h.pieces_root.unwrap()),
        "4fe7b59af6de3b665b67788cc2f99892ab827efae3a467342b3bb4e3bc8e5bfe"
    );
    assert!(h.piece_layer.is_empty());
    // "abc": a file shorter than a block is hashed as it is, not padded.
    std::fs::write(&path, b"abc").unwrap();
    let h = hash_file(&path, P, Version::Hybrid).unwrap();
    assert_eq!(
        hex(&h.pieces_root.unwrap()),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(
        hex(&h.v1_last_unpadded.unwrap()),
        "a9993e364706816aba3e25717850c26c9cd0d89d"
    );
    assert_eq!(h.v1_pieces.len(), 1);
    assert_ne!(h.v1_pieces[0], h.v1_last_unpadded.unwrap());
    // Empty: no root, no pieces.
    std::fs::write(&path, b"").unwrap();
    let h = hash_file(&path, P, Version::Hybrid).unwrap();
    assert_eq!(h.pieces_root, None);
    assert!(h.v1_pieces.is_empty() && h.v1_last_unpadded.is_none());
}

#[test]
fn file_tree_order_is_by_path_component() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("f");
    std::fs::write(&source, content(1, 40_000)).unwrap();
    // By whole path, "a.b/x" and "a.bin" come before "a/b": the order
    // libtorrent 2.1.1's creator gives the v1 file list, after which it
    // refuses its own hybrid torrent. The file tree's order is the right one.
    let paths: [&[&str]; 5] = [
        &["a.b", "x.bin"],
        &["a.bin"],
        &["a", "b.bin"],
        &["a-b", "y.bin"],
        &["a b", "z.bin"],
    ];
    let files: Vec<InputFile> = paths
        .iter()
        .map(|p| InputFile::new(p.iter().copied(), &source))
        .collect();
    let torrent = create(&files, &CreateOptions::new("t", P), &HashOptions::default()).unwrap();
    let order: Vec<String> = torrent.files.iter().map(|f| f.path.join("/")).collect();
    assert_eq!(
        order,
        ["a/b.bin", "a b/z.bin", "a-b/y.bin", "a.b/x.bin", "a.bin"]
    );
    // The v1 file list has the same order (with a padding file after each).
    let text = String::from_utf8_lossy(&torrent.bytes);
    let at = |name: &str| text.find(name).unwrap_or_else(|| panic!("{name}"));
    let v1 = &text[at("5:files")..at("12:meta version")];
    let positions: Vec<usize> = ["5:b.bin", "5:z.bin", "5:y.bin", "5:x.bin", "5:a.bin"]
        .iter()
        .map(|n| v1.find(n).unwrap())
        .collect();
    assert!(positions.windows(2).all(|w| w[0] < w[1]), "{positions:?}");
    assert_eq!(v1.matches("4:attr1:p").count(), 5);
}

#[test]
fn magnet_and_fields() {
    let dir = tempfile::tempdir().unwrap();
    let files = write_set(dir.path(), "aligned");
    let options = CreateOptions {
        trackers: vec![
            vec!["http://t1.example/announce".into()],
            vec![
                "udp://t2.example:6969".into(),
                "http://t3.example/a b".into(),
            ],
        ],
        web_seeds: vec!["https://data.example/v1/".into()],
        comment: Some("a comment".into()),
        private: true,
        ..CreateOptions::new("my data", P)
    };
    let torrent = create(&files, &options, &HashOptions::default()).unwrap();
    let v1 = hex(&torrent.info_hash_v1.unwrap());
    let v2 = hex(&torrent.info_hash_v2);
    assert_eq!(
        torrent.magnet,
        format!(
            "magnet:?xt=urn:btih:{v1}&xt=urn:btmh:1220{v2}&dn=my%20data\
             &tr=http%3A%2F%2Ft1.example%2Fannounce&tr=udp%3A%2F%2Ft2.example%3A6969\
             &tr=http%3A%2F%2Ft3.example%2Fa%20b&ws=https%3A%2F%2Fdata.example%2Fv1%2F"
        )
    );
    let text = String::from_utf8_lossy(&torrent.bytes);
    for expected in [
        "8:announce26:http://t1.example/announce",
        "13:announce-listll26:http://t1.example/announceel21:udp://t2.example:696921:http://t3.example/a bee",
        "8:url-list24:https://data.example/v1/",
        "7:comment9:a comment",
        "7:privatei1e",
        "4:name7:my data",
    ] {
        assert!(text.contains(expected), "{expected}");
    }
    // The private flag is in the info dictionary: other hashes without it.
    let public = CreateOptions {
        private: false,
        ..options.clone()
    };
    let public = create(&files, &public, &HashOptions::default()).unwrap();
    assert_ne!(public.info_hash_v2, torrent.info_hash_v2);
    // Trackers, web seeds and the comment are not.
    let bare = CreateOptions {
        private: true,
        ..CreateOptions::new("my data", P)
    };
    let bare = create(&files, &bare, &HashOptions::default()).unwrap();
    assert_eq!(bare.info_hash_v2, torrent.info_hash_v2);
    assert_eq!(bare.info_hash_v1, torrent.info_hash_v1);
    // Several web seeds are a list; a v2-only magnet has no btih.
    let v2_only = CreateOptions {
        version: Version::V2,
        web_seeds: vec!["https://a.example/".into(), "https://b.example/".into()],
        ..CreateOptions::new("my data", P)
    };
    let v2_only = create(&files, &v2_only, &HashOptions::default()).unwrap();
    assert!(v2_only.magnet.starts_with("magnet:?xt=urn:btmh:1220"));
    assert!(
        String::from_utf8_lossy(&v2_only.bytes)
            .contains("8:url-listl18:https://a.example/18:https://b.example/e")
    );
}

#[test]
fn rejects_what_it_cannot_make_a_good_torrent_of() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    let empty = dir.path().join("empty");
    std::fs::write(&data, content(7, 50_000)).unwrap();
    std::fs::write(&empty, b"").unwrap();
    let make = |files: &[InputFile], options: &CreateOptions| {
        create(files, options, &HashOptions::default())
    };
    let good = CreateOptions::new("t", P);
    let file = |path: &[&str]| InputFile::new(path.iter().copied(), &data);

    for piece_length in [0, 1, 8192, 3 * BLOCK_SIZE, 1 << 28] {
        let err = make(&[file(&["a"])], &CreateOptions::new("t", piece_length)).unwrap_err();
        assert!(matches!(err, Error::InvalidPieceLength(_)), "{err}");
    }
    for name in ["", ".", "..", "a/b", "a\\b", "a\0"] {
        let err = make(&[file(&["a"])], &CreateOptions::new(name, P)).unwrap_err();
        assert!(matches!(err, Error::InvalidName(_)), "{name:?}: {err}");
    }
    let bad_paths: [&[&str]; 8] = [
        &[],
        &[""],
        &["."],
        &["a", ".."],
        &["a/b"],
        &["a\\b"],
        &[".pad", "100"],
        &[".pad"],
    ];
    for path in bad_paths {
        let err = make(&[file(path)], &good).unwrap_err();
        assert!(matches!(err, Error::InvalidPath(_)), "{path:?}: {err}");
    }
    // `.pad` is only special where there are padding files.
    let v2 = CreateOptions {
        version: Version::V2,
        ..good.clone()
    };
    make(&[file(&[".pad", "100"])], &v2).unwrap();

    let conflicts: [[&[&str]; 2]; 3] = [
        [&["a"], &["a"]],
        [&["a"], &["a", "b"]],
        [&["a", "b", "c"], &["a", "b"]],
    ];
    for [first, second] in conflicts {
        let err = make(&[file(first), file(second)], &good).unwrap_err();
        assert!(matches!(err, Error::PathConflict(_)), "{err}");
    }

    let err = make(&[], &good).unwrap_err();
    assert!(matches!(err, Error::NoData), "{err}");
    let err = make(&[InputFile::new(["e"], &empty)], &good).unwrap_err();
    assert!(matches!(err, Error::NoData), "{err}");

    let err = make(&[InputFile::new(["a"], dir.path().join("missing"))], &good).unwrap_err();
    assert!(matches!(err, Error::Io { .. }), "{err}");
    let err = make(&[InputFile::new(["a"], dir.path())], &good).unwrap_err();
    assert!(matches!(err, Error::NotAFile { .. }), "{err}");

    let bad_url = CreateOptions {
        web_seeds: vec!["https://a.example/\n".into()],
        ..good.clone()
    };
    let err = make(&[file(&["a"])], &bad_url).unwrap_err();
    assert!(matches!(err, Error::InvalidUrl(_)), "{err}");
}

#[test]
fn progress_and_cancel() {
    let dir = tempfile::tempdir().unwrap();
    let files = write_set(dir.path(), "store");
    let sources: Vec<PathBuf> = files.iter().map(|f| f.source.clone()).collect();
    let total: u64 = sources
        .iter()
        .map(|s| std::fs::metadata(s).unwrap().len())
        .sum();
    let most = AtomicU64::new(0);
    let report = |p: Progress| {
        assert_eq!(p.total_bytes, total);
        most.fetch_max(p.hashed_bytes, Ordering::Relaxed);
    };
    let options = HashOptions {
        threads: 3,
        progress: Some(&report),
        cancel: None,
    };
    hash_files(&sources, P, Version::Hybrid, &options).unwrap();
    assert_eq!(most.load(Ordering::Relaxed), total);

    let cancel = AtomicBool::new(true);
    let options = HashOptions {
        cancel: Some(&cancel),
        ..Default::default()
    };
    let err = hash_files(&sources, P, Version::Hybrid, &options).unwrap_err();
    assert!(matches!(err, Error::Cancelled), "{err}");
}

#[test]
fn list_folder_finds_every_file() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("a/b")).unwrap();
    std::fs::write(dir.path().join("a/b/c.bin"), b"c").unwrap();
    std::fs::write(dir.path().join("a/d.bin"), b"").unwrap();
    std::fs::write(dir.path().join("e.bin"), b"e").unwrap();
    let mut files = list_folder(dir.path()).unwrap();
    files.sort_by(|a, b| a.path.cmp(&b.path));
    let paths: Vec<String> = files.iter().map(|f| f.path.join("/")).collect();
    assert_eq!(paths, ["a/b/c.bin", "a/d.bin", "e.bin"]);
    assert_eq!(files[0].source, dir.path().join("a/b/c.bin"));
}
