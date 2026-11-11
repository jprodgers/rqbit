//! Makes a hybrid (or v2-only) torrent of a folder or a file.
//!
//!     cargo run --release -p librqbit-create-torrent --example mktorrent -- \
//!         FOLDER --piece-length 1048576 [--name NAME] [--v2-only] \
//!         [--tracker URL]... [--web-seed URL]... [--comment TEXT] \
//!         [--threads N] [--roots] [-o OUT.torrent]
//!
//! Prints the info hashes, the magnet link and how fast the data was hashed;
//! with `--roots`, also "<pieces root>  <length>  <path>" for every file, in
//! the format of `scripts/bep52/bep52_ref.py roots`.

use std::path::PathBuf;
use std::time::Instant;

use librqbit_create_torrent::{
    CreateOptions, HashOptions, InputFile, Version, create, list_folder,
};

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let mut input: Option<PathBuf> = None;
    let mut piece_length: Option<u32> = None;
    let mut name: Option<String> = None;
    let mut version = Version::Hybrid;
    let mut trackers = Vec::new();
    let mut web_seeds = Vec::new();
    let mut comment = None;
    let mut threads = 0usize;
    let mut roots = false;
    let mut output: Option<PathBuf> = None;
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--piece-length" => piece_length = Some(value()?.parse()?),
            "--name" => name = Some(value()?),
            "--v2-only" => version = Version::V2,
            "--tracker" => trackers.push(vec![value()?]),
            "--web-seed" => web_seeds.push(value()?),
            "--comment" => comment = Some(value()?),
            "--threads" => threads = value()?.parse()?,
            "--roots" => roots = true,
            "-o" | "--output" => output = Some(value()?.into()),
            _ if input.is_none() && !arg.starts_with('-') => input = Some(arg.into()),
            _ => return Err(format!("unexpected argument {arg}").into()),
        }
    }
    let input = input.ok_or("which folder or file?")?;
    let piece_length = piece_length.ok_or("--piece-length is required")?;
    let name = match name {
        Some(name) => name,
        None => std::path::absolute(&input)?
            .file_name()
            .and_then(|n| n.to_str())
            .ok_or("give the torrent a --name")?
            .to_owned(),
    };
    let files = if input.is_file() {
        vec![InputFile::new([name.clone()], &input)]
    } else {
        list_folder(&input)?
    };

    let options = CreateOptions {
        version,
        trackers,
        web_seeds,
        comment,
        ..CreateOptions::new(name, piece_length)
    };
    let hash_options = HashOptions {
        threads,
        ..Default::default()
    };
    let started = Instant::now();
    let torrent = create(&files, &options, &hash_options)?;
    let elapsed = started.elapsed().as_secs_f64();

    if roots {
        for file in &torrent.files {
            let root = file.pieces_root.map_or("-".to_owned(), |r| hex(&r));
            println!("{root}  {}  {}", file.length, file.path.join("/"));
        }
    }
    if let Some(v1) = &torrent.info_hash_v1 {
        println!("v1 info hash: {}", hex(v1));
    }
    println!("v2 info hash: {}", hex(&torrent.info_hash_v2));
    println!("magnet: {}", torrent.magnet);
    let total: u64 = torrent.files.iter().map(|f| f.length).sum();
    eprintln!(
        "{} files, {:.1} MiB in {elapsed:.2} s: {:.0} MiB/s",
        torrent.files.len(),
        total as f64 / 1048576.0,
        total as f64 / 1048576.0 / elapsed.max(1e-9),
    );
    if let Some(output) = output {
        std::fs::write(output, &torrent.bytes)?;
    }
    Ok(())
}
