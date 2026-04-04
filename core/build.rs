// trading-system/core/build.rs
//
// Compiles proto/trading.proto into Rust via tonic-build.
// Output lands in $OUT_DIR/trading.rs and is included via include! in bridge/mod.rs.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::configure()
        .build_server(true)
        .build_client(false) // Rust is the server; Python is the client
        .compile_protos(
            &["../proto/trading.proto"],
            &["../proto"],
        )?;
    Ok(())
}
