//! What libtorrent 2.0 asks of a seeder of a v2-only torrent.
//!
//! libtorrent 2.0 (the library of every qBittorrent release so far) treats the
//! last piece of a file as a whole piece padded with zeros, and requests the
//! whole 16 KiB block the file ends in. libtorrent 2.1 asks only for the bytes
//! that exist. A seeder has to answer both, or qBittorrent cannot finish any
//! file whose length is not a multiple of 16 KiB.

use std::time::Duration;

use anyhow::Context;
use bytes::Bytes;
use librqbit_core::{Id20, torrent_metainfo::torrent_from_bytes};

use crate::tests::{
    e2e_v2::{PIECE_LEN, connect_with_hash, sample_files},
    e2e_v2_hashes::start_seeder,
    fake_peer::run,
};

const BLOCK: usize = 16 * 1024;

#[tokio::test(flavor = "multi_thread")]
async fn test_v2_seeder_pads_the_block_a_file_ends_in() -> anyhow::Result<()> {
    run(async {
        let files = sample_files(52);
        let torrent = Bytes::from(files.torrent("v2", PIECE_LEN, false).to_bytes());
        let info_hash: Id20 = torrent_from_bytes(&torrent)?.info_hash;
        let (_dir, seeder, _handle) = start_seeder(&files, torrent).await?;
        let addr = seeder.listen_addr().context("addr")?;
        let (mut peer, _) = connect_with_hash(addr, info_hash, true).await?;
        peer.interested().await?;

        let pieces = files.v2_pieces(PIECE_LEN);
        let mut asked = 0;
        for (index, piece) in pieces.iter().enumerate() {
            let tail = piece.len() % BLOCK;
            if tail == 0 {
                continue;
            }
            let index = u32::try_from(index)?;
            let begin = piece.len() - tail;
            // As libtorrent 2.0 asks: the whole block.
            peer.request(index, u32::try_from(begin)?, u32::try_from(BLOCK)?)
                .await?;
            // As libtorrent 2.1 asks: the bytes that exist.
            peer.request(index, u32::try_from(begin)?, u32::try_from(tail)?)
                .await?;
            let got = peer.expect_pieces(2, Duration::from_secs(10)).await?;
            let (padded, exact) = (&got[0], &got[1]);
            assert_eq!((padded.0, padded.1 as usize), (index, begin));
            assert_eq!(padded.2.len(), BLOCK, "piece {index}: not a whole block");
            assert_eq!(padded.2[..tail], piece[begin..], "piece {index}: data");
            assert!(
                padded.2[tail..].iter().all(|b| *b == 0),
                "piece {index}: the padding is not zeros"
            );
            assert_eq!(exact.2, piece[begin..], "piece {index}: exact request");
            asked += 1;
        }
        // big.bin, small.bin and the file one byte longer than a piece.
        assert_eq!(asked, 3, "the sample files changed");

        // A block that is past the end of the data altogether is still not
        // a valid request: small.bin has 1000 bytes.
        let small = pieces
            .iter()
            .position(|p| p.len() == 1000)
            .context("small.bin")?;
        peer.request(
            u32::try_from(small)?,
            u32::try_from(BLOCK)?,
            u32::try_from(BLOCK)?,
        )
        .await?;
        peer.expect_closed(Duration::from_secs(10)).await?;
        seeder.stop().await;
        Ok(())
    })
    .await
}
