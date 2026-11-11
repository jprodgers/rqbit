// Wrapper for sha1/sha256 libraries to be able to swap them easily,
// e.g. to measure performance, or change implementations depending on platform.
//
// Sha1 computation is the majority of CPU usage of librqbit.
//
// Three backends, exactly one of which must be enabled:
// - sha1-crypto-hash: the system library (OpenSSL on Linux). The default.
// - sha1-ring: aws-lc-rs (C and assembly, built from source).
// - sha1-rustcrypto: the RustCrypto sha1 and sha2 crates. Pure Rust; no C
//   compiler, CMake or system library needed. On CPUs with SHA instructions
//   (x86-64 SHA-NI, ARMv8 crypto) it is as fast as the other two; on older
//   CPUs it is slower (measured on x86-64 with the instructions disabled:
//   about 515 MiB/s for SHA-1 and 245 MiB/s for SHA-256, against roughly
//   1200 MiB/s with them).

pub trait ISha1 {
    fn new() -> Self;
    fn update(&mut self, buf: &[u8]);
    fn finish(self) -> [u8; 20];
}

/// SHA-256 hash trait for BEP 52 (BitTorrent v2) support.
pub trait ISha256 {
    fn new() -> Self;
    fn update(&mut self, buf: &[u8]);
    fn finish(self) -> [u8; 32];

    fn finish_id32(self) -> [u8; 32]
    where
        Self: Sized,
    {
        self.finish()
    }
}

assert_cfg::exactly_one! {
    feature = "sha1-crypto-hash",
    feature = "sha1-ring",
    feature = "sha1-rustcrypto",
}

#[cfg(feature = "sha1-crypto-hash")]
mod crypto_hash_impl {
    use super::{ISha1, ISha256};

    pub struct Sha1CryptoHash {
        inner: crypto_hash::Hasher,
    }

    impl ISha1 for Sha1CryptoHash {
        fn new() -> Self {
            Self {
                inner: crypto_hash::Hasher::new(crypto_hash::Algorithm::SHA1),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            use std::io::Write;
            self.inner.write_all(buf).unwrap();
        }

        fn finish(mut self) -> [u8; 20] {
            let result = self.inner.finish();
            debug_assert_eq!(result.len(), 20);
            let mut result_arr = [0u8; 20];
            result_arr.copy_from_slice(&result);
            result_arr
        }
    }

    pub struct Sha256CryptoHash {
        inner: crypto_hash::Hasher,
    }

    impl ISha256 for Sha256CryptoHash {
        fn new() -> Self {
            Self {
                inner: crypto_hash::Hasher::new(crypto_hash::Algorithm::SHA256),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            use std::io::Write;
            self.inner.write_all(buf).unwrap();
        }

        fn finish(mut self) -> [u8; 32] {
            let result = self.inner.finish();
            debug_assert_eq!(result.len(), 32);
            let mut result_arr = [0u8; 32];
            result_arr.copy_from_slice(&result);
            result_arr
        }
    }
}

#[cfg(feature = "sha1-ring")]
mod ring_impl {
    use super::{ISha1, ISha256};

    use aws_lc_rs::digest::{Context, SHA1_FOR_LEGACY_USE_ONLY as SHA1, SHA256};

    pub struct Sha1Ring {
        ctx: Context,
    }

    impl ISha1 for Sha1Ring {
        fn new() -> Self {
            Self {
                ctx: Context::new(&SHA1),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            self.ctx.update(buf);
        }

        fn finish(self) -> [u8; 20] {
            let result = self.ctx.finish();
            debug_assert_eq!(result.as_ref().len(), 20);
            let mut result_arr = [0u8; 20];
            result_arr.copy_from_slice(result.as_ref());
            result_arr
        }
    }

    pub struct Sha256Ring {
        ctx: Context,
    }

    impl ISha256 for Sha256Ring {
        fn new() -> Self {
            Self {
                ctx: Context::new(&SHA256),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            self.ctx.update(buf);
        }

        fn finish(self) -> [u8; 32] {
            let result = self.ctx.finish();
            debug_assert_eq!(result.as_ref().len(), 32);
            let mut result_arr = [0u8; 32];
            result_arr.copy_from_slice(result.as_ref());
            result_arr
        }
    }
}

#[cfg(feature = "sha1-rustcrypto")]
mod rustcrypto_impl {
    use super::{ISha1, ISha256};

    use sha1::Digest;

    pub struct Sha1RustCrypto {
        inner: sha1::Sha1,
    }

    impl ISha1 for Sha1RustCrypto {
        fn new() -> Self {
            Self {
                inner: sha1::Sha1::new(),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            self.inner.update(buf);
        }

        fn finish(self) -> [u8; 20] {
            self.inner.finalize().into()
        }
    }

    pub struct Sha256RustCrypto {
        inner: sha2::Sha256,
    }

    impl ISha256 for Sha256RustCrypto {
        fn new() -> Self {
            Self {
                inner: sha2::Sha256::new(),
            }
        }

        fn update(&mut self, buf: &[u8]) {
            self.inner.update(buf);
        }

        fn finish(self) -> [u8; 32] {
            self.inner.finalize().into()
        }
    }
}

#[cfg(feature = "sha1-crypto-hash")]
pub type Sha1 = crypto_hash_impl::Sha1CryptoHash;

#[cfg(feature = "sha1-rustcrypto")]
pub type Sha1 = rustcrypto_impl::Sha1RustCrypto;

#[cfg(feature = "sha1-rustcrypto")]
pub type Sha256 = rustcrypto_impl::Sha256RustCrypto;

#[cfg(feature = "sha1-ring")]
pub type Sha1 = ring_impl::Sha1Ring;

#[cfg(feature = "sha1-crypto-hash")]
pub type Sha256 = crypto_hash_impl::Sha256CryptoHash;

#[cfg(feature = "sha1-ring")]
pub type Sha256 = ring_impl::Sha256Ring;

#[cfg(test)]
mod tests {
    use super::{ISha1, ISha256, Sha1, Sha256};

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    // The same vectors for whichever backend is compiled in, fed in pieces
    // of awkward sizes so that the block buffering is exercised too.
    #[test]
    fn test_known_vectors() {
        let million_a = vec![b'a'; 1_000_000];
        let cases: [(&[u8], &str, &str); 3] = [
            (
                b"abc",
                "a9993e364706816aba3e25717850c26c9cd0d89d",
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            ),
            (
                b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
                "84983e441c3bd26ebaae4aa1f95129e5e54670f1",
                "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
            ),
            (
                &million_a,
                "34aa973cd4c4daa4f61eeb2bdbad27316534016f",
                "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
            ),
        ];
        for (data, sha1, sha256) in cases {
            for step in [1usize, 63, 64, 65, 16384, usize::MAX] {
                let mut h1 = Sha1::new();
                let mut h256 = Sha256::new();
                for chunk in data.chunks(step.min(data.len())) {
                    h1.update(chunk);
                    h256.update(chunk);
                }
                assert_eq!(hex(&h1.finish()), sha1, "sha1, step {step}");
                assert_eq!(hex(&h256.finish()), sha256, "sha256, step {step}");
            }
        }
    }

    fn assert_sha256_impl<T: ISha256>() {}

    #[test]
    fn test_sha256_known_vector_empty() {
        assert_sha256_impl::<Sha256>();
        let mut h = Sha256::new();
        h.update(b"");
        let got = h.finish();
        let expected: [u8; 32] = [
            0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14, 0x9a, 0xfb, 0xf4, 0xc8, 0x99, 0x6f,
            0xb9, 0x24, 0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b, 0x93, 0x4c, 0xa4, 0x95, 0x99, 0x1b,
            0x78, 0x52, 0xb8, 0x55,
        ];
        assert_eq!(got, expected);
    }
}
