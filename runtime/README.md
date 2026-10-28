# Modified Rust Execution Payload

Source: d66a81c966aade525ec5f2ae2316c3236d62c68d, modified Tura-derived AGPL-3.0-or-later code.

The V456 caller reuses the V455 Rust payload. The four recorded executable hashes are in the aggregate release manifest; 602 recorded build-source inputs match this source. No new binary was built for this publication.

Build entry: cargo build --locked --release --bin tura_router --bin tura_runtime --bin tura_session_db --bin tura_exec. Use a compatible Rust toolchain; this source tree does not contain the historical toolchain-pin file mentioned by the older publication.

Runtime db/ and trace-evidence/ state are deliberately excluded. They are not source dependencies for the recorded build. Existing license and upstream provenance are preserved. Building is not installation, native session adoption, provider authentication or operational acceptance.
