# Vendored: network-interface 2.0.5, without its C file

This is the `network-interface` crate, version 2.0.5, from
https://github.com/EstebanBorai/network-interface (MIT OR Apache-2.0; the
licence files are kept next to this one), with one change: upstream compiles a
C file on macOS and the BSDs for a single function, and here that function is
written in Rust (`src/target/unix/ffi/mod.rs`). `build.rs`, the C file and the
`cc` build dependency are gone; nothing else differs. The examples and the
project's CI files were left out.

Why: JETCARP must build without a C compiler, and this was the only C left in
the client's pure-Rust build (`librqbit` with `--no-default-features
--features pure-rust`). It comes in through `librqbit-dualstack-sockets` and
`librqbit-upnp`.

The workspace's `Cargo.toml` substitutes it with `[patch.crates-io]`. A patch
only applies in the workspace that is being built, so a program that depends on
`librqbit` and wants the same has to repeat it in its own `Cargo.toml`:

    [patch.crates-io]
    network-interface = { git = "https://github.com/jprodgers/rqbit", branch = "claude/kind-fermi-dx3tx8" }

Checked by type-checking the client for `aarch64-apple-darwin` and
`x86_64-apple-darwin` on Linux with the C compiler disabled. It has not been
run on a Mac yet.
