//! Verify every raw SoundKit v2 Opus LL-HLS part that `aep1-48k-probe
//! receive-hls --raw-part-output-dir` saved.
//!
//! The probe's `soundkit-opus` check also requires the encrypted flag, which
//! the qualification source does not set. This tool checks transport
//! integrity only: each packet header must parse, describe 48 kHz Opus with
//! the expected frame count, carry a CRC32, and the CRC32 over the header and
//! payload must match. File names carry the presentation time, so the tool
//! also reports missing and repeated parts.

use frame_header::{EncodingFlag, FrameHeaderV2};
use std::collections::BTreeSet;

fn main() {
    let mut args = std::env::args().skip(1);
    let directory = args.next().expect("usage: soundkit-part-check DIR PART_MS");
    let part_ms: u64 = args.next().and_then(|v| v.parse().ok()).unwrap_or(5);
    let expected_frames = 48_000 * part_ms / 1_000;
    let (mut parts, mut packets, mut crc_ok, mut crc_missing, mut crc_bad) = (0u64, 0u64, 0u64, 0u64, 0u64);
    let (mut malformed, mut frame_mismatch, mut encrypted, mut bytes_total) = (0u64, 0u64, 0u64, 0u64);
    let mut pts = BTreeSet::new();
    let mut repeated_pts = 0u64;
    for entry in std::fs::read_dir(&directory).expect("read directory") {
        let path = entry.expect("directory entry").path();
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        let Some(pts_ms) = name
            .strip_suffix(".bin")
            .and_then(|stem| stem.rsplit("pts-").next())
            .and_then(|value| value.parse::<u64>().ok())
        else {
            continue;
        };
        parts += 1;
        if !pts.insert(pts_ms) {
            repeated_pts += 1;
        }
        let bytes = std::fs::read(&path).expect("read part");
        bytes_total += bytes.len() as u64;
        let mut rest = &bytes[..];
        let mut frames = 0u64;
        let mut part_ok = !rest.is_empty();
        while !rest.is_empty() {
            let Ok(header_size) = FrameHeaderV2::header_size(rest) else {
                part_ok = false;
                break;
            };
            let Ok(header) = FrameHeaderV2::decode(&mut &rest[..header_size]) else {
                part_ok = false;
                break;
            };
            let end = header_size + header.payload_size() as usize;
            if end > rest.len()
                || header.encoding() != &EncodingFlag::Opus
                || header.sample_rate() != 48_000
                || header.payload_size() == 0
            {
                part_ok = false;
                break;
            }
            packets += 1;
            if header.is_encrypted() {
                encrypted += 1;
            }
            if header.packet_crc32_value().is_none() {
                crc_missing += 1;
            } else if matches!(
                header.verify_packet_crc32(&rest[..header_size], &rest[header_size..end]),
                Ok(true)
            ) {
                crc_ok += 1;
            } else {
                crc_bad += 1;
            }
            frames += u64::from(header.frame_count());
            rest = &rest[end..];
        }
        if !part_ok {
            malformed += 1;
        } else if frames != expected_frames {
            frame_mismatch += 1;
        }
    }
    let (first, last) = (pts.first().copied().unwrap_or(0), pts.last().copied().unwrap_or(0));
    let span_parts = if pts.is_empty() { 0 } else { (last - first) / part_ms + 1 };
    println!(
        "{{\"schema\":\"needletail.soundkit-part-check.v1\",\"parts\":{parts},\"packets\":{packets},\
\"crc_verified\":{crc_ok},\"crc_missing\":{crc_missing},\"crc_mismatches\":{crc_bad},\
\"malformed_parts\":{malformed},\"frame_count_mismatches\":{frame_mismatch},\
\"encrypted_packets\":{encrypted},\"first_pts_ms\":{first},\"last_pts_ms\":{last},\
\"missing_parts_in_span\":{},\"repeated_pts\":{repeated_pts},\"bytes\":{bytes_total}}}",
        span_parts.saturating_sub(pts.len() as u64)
    );
}
