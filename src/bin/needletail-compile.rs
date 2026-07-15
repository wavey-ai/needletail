use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;
use needletail::service_plan::RelayProgram;

#[derive(Debug, Parser)]
#[command(
    name = "needletail-compile",
    about = "Compile a validated Needletail relay program into native service desired state"
)]
struct Args {
    /// Relay program JSON containing topology, carrier endpoints, and stage.
    #[arg(long)]
    program: PathBuf,

    /// Emit indented JSON for review and audit artifacts.
    #[arg(long)]
    pretty: bool,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let bytes = std::fs::read(&args.program)
        .with_context(|| format!("failed to read relay program {}", args.program.display()))?;
    let program: RelayProgram = serde_json::from_slice(&bytes)
        .with_context(|| format!("invalid relay program JSON in {}", args.program.display()))?;
    let plan = program.compile().context("relay program did not compile")?;
    let output = if args.pretty {
        serde_json::to_string_pretty(&plan)
    } else {
        serde_json::to_string(&plan)
    }
    .context("failed to serialize compiled service plan")?;
    println!("{output}");
    Ok(())
}
