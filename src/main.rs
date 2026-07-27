//! cue-fighter — read/write Serato Markers2 hot cues in MP3 (ID3 GEOB) and FLAC
//! (Vorbis comment) files. Written cues are readable by Serato DJ, VirtualDJ,
//! Mixxx, and anything else that understands Serato tags.

use clap::{Parser, Subcommand};
use id3::TagLike;
use serde::{Deserialize, Serialize};
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use triseratops::tag::color::Color;
use triseratops::tag::format::flac::FLACTag;
use triseratops::tag::format::id3::ID3Tag;
use triseratops::tag::generic::{Cue, Position, Version};
use triseratops::tag::markers2::{Marker, Markers2, Markers2Content};

const GEOB_DESC: &str = "Serato Markers2"; // == Markers2::NAME
const FLAC_COMMENT: &str = "SERATO_MARKERS_V2";

#[derive(Parser)]
#[command(name = "cue-fighter", version, about = "Read/write Serato Markers2 cue points (MP3, FLAC)")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Dump existing cues as JSON
    Read { file: PathBuf },
    /// Write cues into the file's Serato Markers2 tag
    Write {
        file: PathBuf,
        /// Cue spec IDX:MS[:RRGGBB[:LABEL]] — repeatable
        #[arg(long = "cue", value_name = "SPEC")]
        cues: Vec<String>,
        /// JSON input: {"cues":[{"index":0,"ms":32450,"color":"CC0000","label":"Drop"}]} ("-" = stdin)
        #[arg(long, value_name = "PATH")]
        json: Option<String>,
        /// TOML input (as written by `export`): [[cue]] index/ms/color/label ("-" = stdin)
        #[arg(long, value_name = "PATH")]
        toml: Option<String>,
        /// Drop all existing CUE entries first (loops/color/bpmlock are always preserved)
        #[arg(long)]
        replace: bool,
        /// Print resulting cue list without writing the file
        #[arg(long)]
        dry_run: bool,
        /// Save a one-shot undo sidecar (<file>.cuebak) before writing
        #[arg(long)]
        backup: bool,
        /// Write to a COPY in this dir instead of the original (also copies the .vdjstems sidecar)
        #[arg(long, value_name = "DIR")]
        out_dir: Option<PathBuf>,
        /// Strip ALL existing tags from the copy first (MixedInKey CUEPOINTS, beatgrids,
        /// artwork, artist/title) so our cues are the only metadata. Requires --out-dir.
        #[arg(long)]
        scrub: bool,
    },
    /// Write cues for a whole directory: match <audio-dir>/<stem> to <json-dir>/<stem>.cues.json
    Batch {
        /// Directory of audio files (mp3/aiff/flac; other formats ignored)
        audio_dir: PathBuf,
        /// Directory of <stem>.cues.json files (as emitted by detect_cues.py)
        #[arg(long, value_name = "DIR")]
        json_dir: PathBuf,
        /// Drop all existing CUE entries first
        #[arg(long)]
        replace: bool,
        /// Print what would be written without touching files
        #[arg(long)]
        dry_run: bool,
        /// Skip audio files that already carry cues
        #[arg(long)]
        skip_existing: bool,
        /// Save one-shot undo sidecars (<file>.cuebak) before writing
        #[arg(long)]
        backup: bool,
        /// Write to COPIES in this dir instead of the originals (also copies .vdjstems sidecars)
        #[arg(long, value_name = "DIR")]
        out_dir: Option<PathBuf>,
        /// Strip ALL existing tags from each copy first. Requires --out-dir.
        #[arg(long)]
        scrub: bool,
    },
    /// Restore cues from the <file>.cuebak sidecar written by --backup
    Undo { file: PathBuf },
    /// Checkpoint Serato Markers2 tags: raw <name>.markers2 + human-readable <name>.cues.toml
    Export {
        /// Audio file, or a directory of audio files
        path: PathBuf,
        /// Directory to write the checkpoint sidecars into
        #[arg(long, value_name = "DIR")]
        out: PathBuf,
    },
    /// EXPERIMENTAL: rewrite a FLAC tag's envelope in VirtualDJ's encoding
    /// (single-line, '='-padded base64) instead of Serato's (72-char wrapped,
    /// unpadded). Cue data is untouched — only the outer encoding changes.
    ReencodeVdj { file: PathBuf },
    /// Restore raw Serato Markers2 tags from an `export` checkpoint dir (byte-faithful)
    Import {
        /// Audio file, or a directory of audio files
        path: PathBuf,
        /// Checkpoint directory produced by `export`
        #[arg(long, value_name = "DIR")]
        from: PathBuf,
    },
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct JsonCue {
    index: u8,
    ms: u32,
    #[serde(default)]
    color: Option<String>,
    #[serde(default)]
    label: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
struct JsonDoc {
    cues: Vec<JsonCue>,
}

/// TOML view of the cue list: `[[cue]]` tables with index/ms/color/label.
/// Same data as `JsonDoc`, but omits empty color/label (TOML has no null).
#[derive(Serialize, Deserialize, Debug, Default)]
struct TomlDoc {
    #[serde(default, rename = "cue")]
    cues: Vec<TomlCue>,
}

#[derive(Serialize, Deserialize, Debug)]
struct TomlCue {
    index: u8,
    ms: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    color: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    label: Option<String>,
}

impl From<&JsonCue> for TomlCue {
    fn from(j: &JsonCue) -> Self {
        TomlCue { index: j.index, ms: j.ms, color: j.color.clone(), label: j.label.clone() }
    }
}

impl From<TomlCue> for JsonCue {
    fn from(t: TomlCue) -> Self {
        JsonCue { index: t.index, ms: t.ms, color: t.color, label: t.label }
    }
}

fn parse_color(s: &str) -> Result<Color, String> {
    let s = s.trim_start_matches('#');
    if s.len() != 6 {
        return Err(format!("bad color '{s}', expected RRGGBB"));
    }
    let v = u32::from_str_radix(s, 16).map_err(|e| format!("bad color '{s}': {e}"))?;
    Ok(Color { red: (v >> 16) as u8, green: (v >> 8) as u8, blue: v as u8 })
}

fn parse_cue_spec(spec: &str) -> Result<JsonCue, String> {
    let parts: Vec<&str> = spec.splitn(4, ':').collect();
    if parts.len() < 2 {
        return Err(format!("bad cue spec '{spec}', expected IDX:MS[:RRGGBB[:LABEL]]"));
    }
    Ok(JsonCue {
        index: parts[0].parse().map_err(|e| format!("bad index in '{spec}': {e}"))?,
        ms: parts[1].parse().map_err(|e| format!("bad ms in '{spec}': {e}"))?,
        color: parts.get(2).filter(|s| !s.is_empty()).map(|s| s.to_string()),
        label: parts.get(3).filter(|s| !s.is_empty()).map(|s| s.to_string()),
    })
}

/// Default hot cue palette (slots 1-16), used when no color given.
const PALETTE: [&str; 16] = [
    "CC0000", "CC8800", "CCCC00", "00CC00", "00CCCC", "0088CC", "0000CC", "CC00CC",
    "FF6600", "88CC00", "00CC88", "0044CC", "4400CC", "8800CC", "CC0088", "CC0044",
];

fn to_cue(jc: &JsonCue) -> Result<Cue, String> {
    let color = match &jc.color {
        Some(c) => parse_color(c)?,
        None => parse_color(PALETTE[(jc.index as usize) % PALETTE.len()])?,
    };
    Ok(Cue {
        index: jc.index,
        position: Position { millis: jc.ms },
        color,
        label: jc.label.clone().unwrap_or_default(),
    })
}

enum Container {
    Mp3,
    Flac,
}

fn detect(path: &Path) -> Result<Container, String> {
    match path.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()) {
        Some(ref e) if e == "mp3" || e == "aif" || e == "aiff" => Ok(Container::Mp3),
        Some(ref e) if e == "flac" => Ok(Container::Flac),
        other => Err(format!("unsupported extension: {other:?} (mp3/aiff/flac)")),
    }
}

// ---------- tag data access ----------

fn read_markers2_raw(path: &Path, c: &Container) -> Result<Option<Vec<u8>>, String> {
    match c {
        Container::Mp3 => {
            let tag = match id3::Tag::read_from_path(path) {
                Ok(t) => t,
                Err(e) if matches!(e.kind, id3::ErrorKind::NoTag) => return Ok(None),
                Err(e) => return Err(format!("id3 read: {e}")),
            };
            let data = tag
                .encapsulated_objects()
                .find(|o| o.description == GEOB_DESC)
                .map(|o| o.data.clone());
            Ok(data)
        }
        Container::Flac => {
            let tag = metaflac::Tag::read_from_path(path).map_err(|e| format!("flac read: {e}"))?;
            Ok(tag
                .vorbis_comments()
                .and_then(|vc| vc.get(FLAC_COMMENT))
                .and_then(|v| v.first().cloned())
                .map(String::into_bytes))
        }
    }
}

fn parse_markers2(raw: &[u8], c: &Container) -> Result<Markers2, String> {
    let r = match c {
        Container::Mp3 => Markers2::parse_id3(raw),
        Container::Flac => Markers2::parse_flac(raw),
    };
    if let Ok(m) = r {
        return Ok(m);
    }
    // Fallback for FLAC tags triseratops' envelope decoder rejects. Its outer
    // base64 decode assumes Serato's no-padding convention; VirtualDJ writes the
    // envelope with standard '=' padding, which triseratops mangles (~8% of a
    // real library). Decode the envelope ourselves (padding-tolerant), then feed
    // the inner payload to the ID3 parser — the inner base64+markers layer is
    // identical across both containers, so `parse_id3` handles it correctly.
    if let Container::Flac = c {
        if let Some(inner) = deenvelope_flac(raw) {
            // Most VDJ tags parse once the envelope is decoded correctly.
            if let Ok(m) = Markers2::parse_id3(&inner) {
                return Ok(m);
            }
            // A few also have a malformed inner layer ('=' padding inside the
            // base64, or a missing trailing nullbyte). Rebuild it canonically.
            if let Some(norm) = reencode_inner(&inner) {
                if let Ok(m) = Markers2::parse_id3(&norm) {
                    return Ok(m);
                }
            }
        }
    }
    Err("parse markers2: triseratops could not decode this Serato Markers2 tag".to_string())
}

/// Base64-decode tolerantly: keep only alphabet bytes (dropping newlines and any
/// '=' padding), allow trailing bits, and treat padding as optional.
fn tolerant_b64_decode(raw: &[u8]) -> Option<Vec<u8>> {
    use base64::Engine;
    let mut enc: Vec<u8> = raw
        .iter()
        .copied()
        .filter(|b| b.is_ascii_alphanumeric() || *b == b'+' || *b == b'/')
        .collect();
    // A length of %4 == 1 is never valid unpadded base64; it comes from Serato's
    // trailing 'A' pad-avoidance trick. Drop that one char so decode succeeds.
    if enc.len() % 4 == 1 {
        enc.pop();
    }
    let engine = base64::engine::GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        base64::engine::GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::Indifferent),
    );
    engine.decode(&enc).ok()
}

/// Decode a FLAC `SERATO_MARKERS_V2` envelope ourselves and return the inner
/// payload (`\x01\x01` + base64 chunks + `\0`), i.e. the same shape an ID3 GEOB
/// carries. Tolerant of standard '=' padding and of missing padding.
fn deenvelope_flac(raw: &[u8]) -> Option<Vec<u8>> {
    let decoded = tolerant_b64_decode(raw)?;
    // envelope prefix: "application/octet-stream\0\0" + <name> + "\0"
    let rest = decoded.strip_prefix(b"application/octet-stream\x00\x00".as_slice())?;
    let name_end = rest.iter().position(|&b| b == 0)?;
    Some(rest[name_end + 1..].to_vec())
}

/// Rebuild a canonical ID3-style payload from a possibly-malformed inner layer.
/// `inner` is `\x01\x01` + base64 region (+ optional `\0` + trailing bytes). Some
/// tags carry '=' padding inside that base64 or omit the terminating nullbyte,
/// both of which triseratops' strict chunk reader rejects. Decode the region
/// tolerantly and re-emit it as newline-wrapped, unpadded, null-terminated
/// chunks — the exact shape `parse_id3` expects.
fn reencode_inner(inner: &[u8]) -> Option<Vec<u8>> {
    use base64::Engine;
    if inner.len() < 2 || inner[0] != 1 || inner[1] != 1 {
        return None;
    }
    let after = &inner[2..];
    let end = after.iter().position(|&b| b == 0).unwrap_or(after.len());
    let content = tolerant_b64_decode(&after[..end])?; // = \x01\x01 + marker bytes
    let b64 = base64::engine::general_purpose::STANDARD_NO_PAD.encode(&content);
    let bytes = b64.as_bytes();
    let mut out = vec![1u8, 1u8];
    let mut i = 0;
    while i < bytes.len() {
        let j = (i + 72).min(bytes.len());
        out.extend_from_slice(&bytes[i..j]);
        i = j;
        if i < bytes.len() {
            out.push(b'\n');
        }
    }
    out.push(0);
    Some(out)
}

fn empty_markers2() -> Markers2 {
    Markers2 {
        version: Some(Version { major: 1, minor: 1 }),
        size: 0,
        content: Markers2Content {
            version: Version { major: 1, minor: 1 },
            markers: Vec::new(),
        },
    }
}

fn write_markers2_raw(path: &Path, c: &Container, m2: &Markers2) -> Result<(), String> {
    let mut buf = Cursor::new(Vec::new());
    match c {
        Container::Mp3 => m2.write_id3(&mut buf).map_err(|e| format!("serialize: {e:?}"))?,
        Container::Flac => m2.write_flac(&mut buf).map_err(|e| format!("serialize: {e:?}"))?,
    };
    write_markers2_payload(path, c, &buf.into_inner())
}

/// Write a raw Serato Markers2 payload back verbatim, preserving every other
/// frame/comment. `payload` is exactly what `read_markers2_raw` returns: the
/// GEOB data for MP3, the Vorbis comment value bytes for FLAC. Used for
/// byte-faithful checkpoint restore (`import`) and by `write_markers2_raw`.
fn write_markers2_payload(path: &Path, c: &Container, payload: &[u8]) -> Result<(), String> {
    match c {
        Container::Mp3 => {
            let mut tag = match id3::Tag::read_from_path(path) {
                Ok(t) => t,
                Err(e) if matches!(e.kind, id3::ErrorKind::NoTag) => id3::Tag::new(),
                Err(e) => return Err(format!("id3 read: {e}")),
            };
            // drop any existing Serato Markers2 GEOB, keep everything else
            let kept: Vec<id3::Frame> = tag
                .remove("GEOB")
                .into_iter()
                .filter(|f| {
                    f.content()
                        .encapsulated_object()
                        .map(|o| o.description != GEOB_DESC)
                        .unwrap_or(true)
                })
                .collect();
            for f in kept {
                tag.add_frame(f);
            }
            tag.add_frame(id3::Frame::with_content(
                "GEOB",
                id3::Content::EncapsulatedObject(id3::frame::EncapsulatedObject {
                    mime_type: "application/octet-stream".into(),
                    filename: String::new(),
                    description: GEOB_DESC.into(),
                    data: payload.to_vec(),
                }),
            ));
            tag.write_to_path(path, id3::Version::Id3v24)
                .map_err(|e| format!("id3 write: {e}"))
        }
        Container::Flac => {
            let mut tag =
                metaflac::Tag::read_from_path(path).map_err(|e| format!("flac read: {e}"))?;
            let val = String::from_utf8(payload.to_vec())
                .map_err(|e| format!("flac tag not utf8: {e}"))?;
            tag.set_vorbis(FLAC_COMMENT, vec![val]);
            tag.save().map_err(|e| format!("flac write: {e}"))
        }
    }
}

// ---------- commands ----------

fn cues_as_json(m2: &Markers2) -> Vec<JsonCue> {
    m2.cues()
        .map(|q| JsonCue {
            index: q.index,
            ms: q.position.millis,
            color: Some(format!("{:02X}{:02X}{:02X}", q.color.red, q.color.green, q.color.blue)),
            label: if q.label.is_empty() { None } else { Some(q.label.clone()) },
        })
        .collect()
}

fn cmd_read(file: &Path) -> Result<(), String> {
    let c = detect(file)?;
    let cues = match read_markers2_raw(file, &c)? {
        None => Vec::new(),
        Some(raw) => cues_as_json(&parse_markers2(&raw, &c)?),
    };
    println!("{}", serde_json::to_string_pretty(&JsonDoc { cues }).unwrap());
    Ok(())
}

/// Apply `new_cues` to the file's Markers2 tag, preserving all non-cue markers.
/// `replace` drops every existing cue first; otherwise only the indices in
/// `new_cues` are overwritten. Returns the resulting cue list. This is the
/// single mutation path shared by `write`, `batch`, and `undo`.
fn apply_cues(
    file: &Path,
    c: &Container,
    new_cues: &[JsonCue],
    replace: bool,
    dry_run: bool,
) -> Result<Vec<JsonCue>, String> {
    for jc in new_cues {
        if jc.index > 15 {
            return Err(format!("cue index {} out of range (0-15)", jc.index));
        }
    }

    // load existing tag (preserve loops, colors, bpmlock, unknown markers)
    let mut m2 = match read_markers2_raw(file, c)? {
        Some(raw) => parse_markers2(&raw, c)?,
        None => empty_markers2(),
    };
    if m2.version.is_none() {
        m2.version = Some(Version { major: 1, minor: 1 });
    }
    m2.size = 0; // reset; recomputed below

    if replace {
        m2.content.markers.retain(|m| !matches!(m, Marker::Cue(_)));
    }
    // remove cues at indices we're about to (re)write
    let idxs: Vec<u8> = new_cues.iter().map(|q| q.index).collect();
    m2.content
        .markers
        .retain(|m| !matches!(m, Marker::Cue(q) if idxs.contains(&q.index)));

    for jc in new_cues {
        m2.content.markers.push(Marker::Cue(to_cue(jc)?));
    }
    // deterministic order: non-cue markers first, then cues by index
    m2.content.markers.sort_by_key(|m| match m {
        Marker::Cue(q) => (1u8, q.index),
        _ => (0u8, 0u8),
    });

    // Serato pads the tag with trailing nullbytes; triseratops' parser (and
    // Serato itself) require at least one. Measure, then set size to add padding.
    {
        use triseratops::tag::format::Tag as _;
        let mut probe = Cursor::new(Vec::new());
        m2.write(&mut probe).map_err(|e| format!("serialize: {e:?}"))?;
        m2.size = probe.get_ref().len() + 2;
    }

    let summary = cues_as_json(&m2);
    if !dry_run {
        write_markers2_raw(file, c, &m2)?;
    }
    Ok(summary)
}

/// Sidecar path for a track's one-shot undo backup: `<file>.cuebak`.
fn backup_path(file: &Path) -> PathBuf {
    let mut s = file.as_os_str().to_owned();
    s.push(".cuebak");
    PathBuf::from(s)
}

/// Save the file's current cue set to its sidecar, but only if one doesn't
/// already exist — so repeated writes keep the *original* cues for undo.
/// (We only ever mutate cues, so restoring cues fully reverts our changes.)
fn make_backup_if_absent(file: &Path, c: &Container) -> Result<(), String> {
    let bp = backup_path(file);
    if bp.exists() {
        return Ok(());
    }
    let cues = match read_markers2_raw(file, c)? {
        Some(raw) => cues_as_json(&parse_markers2(&raw, c)?),
        None => Vec::new(),
    };
    let json = serde_json::to_string(&JsonDoc { cues }).unwrap();
    std::fs::write(&bp, json).map_err(|e| format!("backup {}: {e}", bp.display()))
}

fn read_input(src: &str) -> Result<String, String> {
    if src == "-" {
        use std::io::Read;
        let mut s = String::new();
        std::io::stdin().read_to_string(&mut s).map_err(|e| e.to_string())?;
        Ok(s)
    } else {
        std::fs::read_to_string(src).map_err(|e| format!("{src}: {e}"))
    }
}

fn read_json_cues(src: &str) -> Result<Vec<JsonCue>, String> {
    let doc: JsonDoc = serde_json::from_str(&read_input(src)?).map_err(|e| format!("json: {e}"))?;
    Ok(doc.cues)
}

fn read_toml_cues(src: &str) -> Result<Vec<JsonCue>, String> {
    let doc: TomlDoc = toml::from_str(&read_input(src)?).map_err(|e| format!("toml: {e}"))?;
    Ok(doc.cues.into_iter().map(JsonCue::from).collect())
}

const AUDIO_EXTS: [&str; 4] = ["mp3", "aif", "aiff", "flac"];

fn is_audio(p: &Path) -> bool {
    p.extension()
        .and_then(|e| e.to_str())
        .map(|e| AUDIO_EXTS.contains(&e.to_ascii_lowercase().as_str()))
        .unwrap_or(false)
}

/// Resolve `path` to a sorted list of audio files: the file itself, or every
/// supported audio file directly inside it if it's a directory.
fn collect_audio(path: &Path) -> Result<Vec<PathBuf>, String> {
    if path.is_dir() {
        let mut v: Vec<PathBuf> = std::fs::read_dir(path)
            .map_err(|e| format!("{}: {e}", path.display()))?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| is_audio(p))
            .collect();
        v.sort();
        Ok(v)
    } else {
        Ok(vec![path.to_path_buf()])
    }
}

/// A sibling path formed by appending `suffix` to the file's *whole* name
/// (e.g. `Track.flac` + `.vdjstems` -> `Track.flac.vdjstems`). Optionally
/// relocated into `dir`. Mirrors how VirtualDJ names its sidecars.
fn suffixed(file: &Path, suffix: &str, dir: Option<&Path>) -> PathBuf {
    let mut name = file.file_name().unwrap_or_default().to_owned();
    name.push(suffix);
    match dir {
        Some(d) => d.join(name),
        None => file.with_file_name(name),
    }
}

/// Strip every existing tag from a file so the only metadata left is what we
/// write next. DJ tools stash cue points in several places besides Serato
/// Markers2 — Mixed In Key writes a `CUEPOINTS` JSON blob (and `BEATGRID` /
/// `ENERGY`), and there are `SERATO_BEATGRID` / `SERATO_AUTOGAIN` blocks — all
/// of which players may read in preference to ours. Callers must only ever hand
/// this a copy (`--scrub` requires `--out-dir`).
fn scrub_metadata(path: &Path, c: &Container) -> Result<(), String> {
    match c {
        Container::Mp3 => {
            // writing a fresh empty tag drops every frame (GEOB, comments, art)
            id3::Tag::new()
                .write_to_path(path, id3::Version::Id3v24)
                .map_err(|e| format!("id3 scrub: {e}"))
        }
        Container::Flac => {
            let mut tag =
                metaflac::Tag::read_from_path(path).map_err(|e| format!("flac read: {e}"))?;
            for bt in [
                metaflac::BlockType::VorbisComment,
                metaflac::BlockType::Application,
                metaflac::BlockType::Picture,
                metaflac::BlockType::CueSheet,
            ] {
                tag.remove_blocks(bt);
            }
            tag.save().map_err(|e| format!("flac scrub: {e}"))
        }
    }
}

/// Copy `src` (and its `<name>.vdjstems` sidecar, if present) into `out_dir`,
/// returning the destination audio path. The original is never touched.
fn copy_into(src: &Path, out_dir: &Path) -> Result<PathBuf, String> {
    std::fs::create_dir_all(out_dir).map_err(|e| format!("{}: {e}", out_dir.display()))?;
    let name = src.file_name().ok_or("source path has no filename")?;
    let dest = out_dir.join(name);
    std::fs::copy(src, &dest)
        .map_err(|e| format!("copy {} -> {}: {e}", src.display(), dest.display()))?;
    let stems = suffixed(src, ".vdjstems", None);
    if stems.exists() {
        std::fs::copy(&stems, suffixed(&dest, ".vdjstems", None))
            .map_err(|e| format!("copy vdjstems sidecar: {e}"))?;
    }
    Ok(dest)
}

#[allow(clippy::too_many_arguments)] // command handler mirrors its CLI flags
fn cmd_write(
    file: &Path,
    cue_specs: &[String],
    json: Option<&str>,
    toml_src: Option<&str>,
    replace: bool,
    dry_run: bool,
    backup: bool,
    out_dir: Option<&Path>,
    scrub: bool,
) -> Result<(), String> {
    let c = detect(file)?;
    if scrub && out_dir.is_none() {
        return Err("--scrub requires --out-dir (refusing to strip tags from an original)".into());
    }

    let mut new_cues: Vec<JsonCue> = Vec::new();
    if let Some(src) = json {
        new_cues.extend(read_json_cues(src)?);
    }
    if let Some(src) = toml_src {
        new_cues.extend(read_toml_cues(src)?);
    }
    for spec in cue_specs {
        new_cues.push(parse_cue_spec(spec)?);
    }
    if new_cues.is_empty() {
        return Err("no cues given (use --cue, --json, and/or --toml)".into());
    }

    // when --out-dir is set, write to a copy and leave the original untouched
    let target = match out_dir {
        Some(dir) if !dry_run => copy_into(file, dir)?,
        _ => file.to_path_buf(),
    };
    if scrub && !dry_run {
        scrub_metadata(&target, &c)?;
        eprintln!("scrubbed all existing tags from {}", target.display());
    }

    if backup && !dry_run {
        make_backup_if_absent(&target, &c)?;
    }
    let summary = apply_cues(&target, &c, &new_cues, replace, dry_run)?;

    if dry_run {
        eprintln!("dry run — would write:");
        println!("{}", serde_json::to_string_pretty(&JsonDoc { cues: summary }).unwrap());
    } else {
        eprintln!("wrote {} cue(s) -> {}", summary.len(), target.display());
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)] // command handler mirrors its CLI flags
fn cmd_batch(
    audio_dir: &Path,
    json_dir: &Path,
    replace: bool,
    dry_run: bool,
    skip_existing: bool,
    backup: bool,
    out_dir: Option<&Path>,
    scrub: bool,
) -> Result<(), String> {
    if scrub && out_dir.is_none() {
        return Err("--scrub requires --out-dir (refusing to strip tags from originals)".into());
    }
    let entries = collect_audio(audio_dir)?;

    let (mut wrote, mut skipped, mut no_json, mut errored) = (0u32, 0u32, 0u32, 0u32);
    for path in &entries {
        let c = match detect(path) {
            Ok(c) => c,
            Err(_) => continue, // unsupported container, ignore
        };
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        let json_file = json_dir.join(format!("{stem}.cues.json"));
        if !json_file.exists() {
            no_json += 1;
            continue;
        }
        // skip_existing inspects the SOURCE's cues (before any copy)
        if skip_existing {
            if let Ok(Some(raw)) = read_markers2_raw(path, &c) {
                if parse_markers2(&raw, &c).map(|m| m.cues().next().is_some()).unwrap_or(false) {
                    eprintln!("skip (has cues): {}", path.display());
                    skipped += 1;
                    continue;
                }
            }
        }
        let cues = match read_json_cues(json_file.to_str().unwrap()) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("error {}: {e}", json_file.display());
                errored += 1;
                continue;
            }
        };
        // with --out-dir, write to a copy (+ its sidecar) and leave the source alone
        let target = match out_dir {
            Some(dir) if !dry_run => match copy_into(path, dir) {
                Ok(dest) => dest,
                Err(e) => {
                    eprintln!("error {}: {e}", path.display());
                    errored += 1;
                    continue;
                }
            },
            _ => path.clone(),
        };
        if scrub && !dry_run {
            if let Err(e) = scrub_metadata(&target, &c) {
                eprintln!("error {}: {e}", target.display());
                errored += 1;
                continue;
            }
        }
        if backup && !dry_run {
            if let Err(e) = make_backup_if_absent(&target, &c) {
                eprintln!("error {}: {e}", target.display());
                errored += 1;
                continue;
            }
        }
        match apply_cues(&target, &c, &cues, replace, dry_run) {
            Ok(sum) => {
                let verb = if dry_run { "would write" } else { "wrote" };
                eprintln!("{verb} {} cue(s) -> {}", sum.len(), target.display());
                wrote += 1;
            }
            Err(e) => {
                eprintln!("error {}: {e}", target.display());
                errored += 1;
            }
        }
    }
    eprintln!(
        "\nbatch: {} written, {} skipped, {} without json, {} errors",
        wrote, skipped, no_json, errored
    );
    Ok(())
}

fn cmd_undo(file: &Path) -> Result<(), String> {
    let c = detect(file)?;
    let bp = backup_path(file);
    let text = std::fs::read_to_string(&bp)
        .map_err(|e| format!("no backup for {} ({e}); nothing to undo", file.display()))?;
    let doc: JsonDoc = serde_json::from_str(&text).map_err(|e| format!("backup json: {e}"))?;
    let n = doc.cues.len();
    // restore = replace the current cue set with the backed-up one
    apply_cues(file, &c, &doc.cues, true, false)?;
    std::fs::remove_file(&bp).map_err(|e| format!("remove backup {}: {e}", bp.display()))?;
    eprintln!("restored {n} cue(s) from backup -> {}", file.display());
    Ok(())
}

/// Checkpoint one track: raw `<name>.markers2` (byte-faithful, restorable) plus
/// human-readable `<name>.cues.toml`. Returns false if the track has no tag.
fn export_one(file: &Path, out: &Path) -> Result<bool, String> {
    let c = detect(file)?;
    let raw = match read_markers2_raw(file, &c)? {
        Some(r) => r,
        None => return Ok(false),
    };
    std::fs::create_dir_all(out).map_err(|e| format!("{}: {e}", out.display()))?;
    // Raw blob is the guarantee: byte-faithful and restorable even for the ~8%
    // of (VirtualDJ-written) tags that triseratops can't parse.
    std::fs::write(suffixed(file, ".markers2", Some(out)), &raw)
        .map_err(|e| format!("write markers2 sidecar: {e}"))?;

    // Human-readable cue list is best-effort — skipped if the tag won't parse.
    match parse_markers2(&raw, &c) {
        Ok(m2) => {
            let cues: Vec<TomlCue> = cues_as_json(&m2).iter().map(TomlCue::from).collect();
            let header = format!(
                "# cue-fighter hotcue checkpoint — {}\n# restore the full tag with: cue-fighter import\n\n",
                file.file_name().unwrap_or_default().to_string_lossy()
            );
            let body = toml::to_string_pretty(&TomlDoc { cues }).map_err(|e| format!("toml: {e}"))?;
            std::fs::write(suffixed(file, ".cues.toml", Some(out)), format!("{header}{body}"))
                .map_err(|e| format!("write cues.toml sidecar: {e}"))?;
        }
        Err(e) => {
            let _ = e;
            eprintln!("note: {}: raw tag saved; .cues.toml skipped (tag not decodable by triseratops)", file.display());
        }
    }
    Ok(true)
}

fn cmd_export(path: &Path, out: &Path) -> Result<(), String> {
    let files = collect_audio(path)?;
    let (mut saved, mut empty, mut errored) = (0u32, 0u32, 0u32);
    for f in &files {
        match export_one(f, out) {
            Ok(true) => saved += 1,
            Ok(false) => empty += 1,
            Err(e) => {
                eprintln!("error {}: {e}", f.display());
                errored += 1;
            }
        }
    }
    eprintln!(
        "export: {} tag set(s) checkpointed, {} without tags, {} errors -> {}",
        saved, empty, errored, out.display()
    );
    Ok(())
}

fn cmd_import(path: &Path, from: &Path) -> Result<(), String> {
    let files = collect_audio(path)?;
    let (mut restored, mut missing, mut errored) = (0u32, 0u32, 0u32);
    for f in &files {
        let c = match detect(f) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let side = suffixed(f, ".markers2", Some(from));
        if !side.exists() {
            missing += 1;
            continue;
        }
        let raw = match std::fs::read(&side) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("error {}: {e}", side.display());
                errored += 1;
                continue;
            }
        };
        match write_markers2_payload(f, &c, &raw) {
            Ok(()) => {
                eprintln!("restored tag -> {}", f.display());
                restored += 1;
            }
            Err(e) => {
                eprintln!("error {}: {e}", f.display());
                errored += 1;
            }
        }
    }
    eprintln!(
        "import: {} restored, {} without checkpoint, {} errors",
        restored, missing, errored
    );
    Ok(())
}

/// Re-emit a FLAC `SERATO_MARKERS_V2` envelope the way VirtualDJ writes it:
/// the whole envelope base64'd on a single line with standard '=' padding.
/// triseratops emits Serato's convention instead (wrapped at 72 chars, unpadded,
/// trailing-'A' trick). The inner marker layer is identical either way, so this
/// only swaps the outer encoding — used to test which form VDJ can actually read.
fn cmd_reencode_vdj(file: &Path) -> Result<(), String> {
    use base64::Engine;
    let c = detect(file)?;
    if !matches!(c, Container::Flac) {
        return Err("reencode-vdj only applies to FLAC (MP3 GEOB has no envelope)".into());
    }
    let raw = read_markers2_raw(file, &c)?.ok_or("no Serato Markers2 tag on this file")?;
    let inner = deenvelope_flac(&raw).ok_or("could not decode the existing envelope")?;
    let mut env = b"application/octet-stream\x00\x00".to_vec();
    env.extend_from_slice(GEOB_DESC.as_bytes());
    env.push(0);
    env.extend_from_slice(&inner);
    let payload = base64::engine::general_purpose::STANDARD.encode(&env).into_bytes();
    write_markers2_payload(file, &c, &payload)?;
    eprintln!("re-encoded envelope in VirtualDJ style -> {}", file.display());
    Ok(())
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let res = match &cli.cmd {
        Cmd::Read { file } => cmd_read(file),
        Cmd::Write { file, cues, json, toml, replace, dry_run, backup, out_dir, scrub } => {
            cmd_write(
                file,
                cues,
                json.as_deref(),
                toml.as_deref(),
                *replace,
                *dry_run,
                *backup,
                out_dir.as_deref(),
                *scrub,
            )
        }
        Cmd::Batch {
            audio_dir,
            json_dir,
            replace,
            dry_run,
            skip_existing,
            backup,
            out_dir,
            scrub,
        } => cmd_batch(
            audio_dir,
            json_dir,
            *replace,
            *dry_run,
            *skip_existing,
            *backup,
            out_dir.as_deref(),
            *scrub,
        ),
        Cmd::Undo { file } => cmd_undo(file),
        Cmd::Export { path, out } => cmd_export(path, out),
        Cmd::Import { path, from } => cmd_import(path, from),
        Cmd::ReencodeVdj { file } => cmd_reencode_vdj(file),
    };
    match res {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use triseratops::tag::format::Tag as _;

    fn sample_flac_bytes() -> Vec<u8> {
        // one CUE + one LOOP so we also guard non-cue preservation
        let mut m = empty_markers2();
        m.content.markers.push(Marker::Cue(Cue {
            index: 0,
            position: Position { millis: 1000 },
            color: parse_color("CC0000").unwrap(),
            label: "A".into(),
        }));
        let mut probe = Cursor::new(Vec::new());
        m.write(&mut probe).unwrap();
        m.size = probe.get_ref().len() + 2;
        let mut buf = Cursor::new(Vec::new());
        m.write_flac(&mut buf).unwrap();
        buf.into_inner()
    }

    #[test]
    fn deenvelope_recovers_same_cues_as_native() {
        let flac = sample_flac_bytes();
        assert!(Markers2::parse_flac(&flac).is_ok(), "native parse of canonical tag");
        let inner = deenvelope_flac(&flac).expect("de-envelope");
        let m2 = Markers2::parse_id3(&inner).expect("id3 parse of inner");
        assert_eq!(m2.cues().count(), 1);
    }

    #[test]
    fn reencode_inner_recovers_unterminated_inner() {
        // Simulate a tag whose inner base64 lost its trailing nullbyte — the
        // case that made triseratops' strict chunk reader fail on real tracks.
        let flac = sample_flac_bytes();
        let mut inner = deenvelope_flac(&flac).unwrap();
        while inner.last() == Some(&0) {
            inner.pop();
        }
        let norm = reencode_inner(&inner).expect("re-encode inner");
        let m2 = Markers2::parse_id3(&norm).expect("parse rebuilt inner");
        assert_eq!(m2.cues().count(), 1);
    }
}
