//! End-to-end tests: drive the compiled `cue-fighter` binary against real
//! audio files. Audio is synthesized with ffmpeg; every test no-ops (passes)
//! if ffmpeg is unavailable so CI without it stays green.
//!
//! These cover the write/read round-trip at the full 16-cue count, non-cue
//! marker survival is not exercised here (the CLI can't author loops) — that
//! invariant is covered by the manual mutagen decode in the verification
//! standard (see CLAUDE.md).

use std::path::{Path, PathBuf};
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_cue-fighter")
}

fn have_ffmpeg() -> bool {
    Command::new("ffmpeg")
        .arg("-version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn workdir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("cuefighter_test_{}_{}", std::process::id(), tag));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// 5s of silence in the requested container.
fn gen_audio(path: &Path) {
    let status = Command::new("ffmpeg")
        .args(["-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "5"])
        .arg(path)
        .status()
        .expect("spawn ffmpeg");
    assert!(status.success(), "ffmpeg failed for {}", path.display());
}

fn run(args: &[&str]) -> (bool, String, String) {
    let out = Command::new(bin()).args(args).output().expect("spawn cue-fighter");
    (
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

fn read_cues(file: &Path) -> serde_json::Value {
    let (ok, stdout, stderr) = run(&["read", file.to_str().unwrap()]);
    assert!(ok, "read failed: {stderr}");
    serde_json::from_str(&stdout).expect("read output is json")
}

fn write_16(file: &Path) -> PathBuf {
    // build a 16-cue json (default colors from the palette)
    let cues: Vec<String> = (0..16)
        .map(|i| format!(r#"{{"index":{i},"ms":{},"label":"c{i}"}}"#, i * 1000 + 40))
        .collect();
    let json = format!(r#"{{"cues":[{}]}}"#, cues.join(","));
    let jpath = file.with_extension("cues.json");
    std::fs::write(&jpath, json).unwrap();
    let (ok, _o, stderr) = run(&["write", file.to_str().unwrap(), "--json", jpath.to_str().unwrap()]);
    assert!(ok, "write failed: {stderr}");
    jpath
}

fn indices(v: &serde_json::Value) -> Vec<u64> {
    v["cues"].as_array().unwrap().iter().map(|c| c["index"].as_u64().unwrap()).collect()
}

fn roundtrip_16(ext: &str) {
    if !have_ffmpeg() {
        eprintln!("skipping {ext} roundtrip: ffmpeg not found");
        return;
    }
    let dir = workdir(ext);
    let track = dir.join(format!("track.{ext}"));
    gen_audio(&track);

    write_16(&track);
    let got = read_cues(&track);
    assert_eq!(indices(&got), (0..16).collect::<Vec<_>>(), "{ext}: all 16 indices survive");
    // first palette color is CC0000 for index 0
    assert_eq!(got["cues"][0]["color"].as_str().unwrap(), "CC0000", "{ext}: default palette color");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn roundtrip_mp3_16_cues() {
    roundtrip_16("mp3");
}

#[test]
fn roundtrip_flac_16_cues() {
    roundtrip_16("flac");
}

#[test]
fn backup_then_undo_restores_original() {
    if !have_ffmpeg() {
        eprintln!("skipping undo test: ffmpeg not found");
        return;
    }
    let dir = workdir("undo");
    let track = dir.join("track.flac");
    gen_audio(&track);

    // seed 2 original cues
    let (ok, _o, e) = run(&["write", track.to_str().unwrap(), "--cue", "0:1000::A", "--cue", "1:2000::B"]);
    assert!(ok, "seed write failed: {e}");

    // overwrite with 16, taking a backup first
    let jpath = dir.join("new.cues.json");
    let cues: Vec<String> = (0..16).map(|i| format!(r#"{{"index":{i},"ms":{}}}"#, i * 500 + 50)).collect();
    std::fs::write(&jpath, format!(r#"{{"cues":[{}]}}"#, cues.join(","))).unwrap();
    let (ok, _o, e) = run(&[
        "write", track.to_str().unwrap(), "--json", jpath.to_str().unwrap(), "--replace", "--backup",
    ]);
    assert!(ok, "backup write failed: {e}");
    assert_eq!(indices(&read_cues(&track)).len(), 16, "16 cues after write");
    assert!(backup_sidecar(&track).exists(), "backup sidecar created");

    // undo -> back to the original 2 cues, sidecar consumed
    let (ok, _o, e) = run(&["undo", track.to_str().unwrap()]);
    assert!(ok, "undo failed: {e}");
    assert_eq!(indices(&read_cues(&track)), vec![0, 1], "original 2 cues restored");
    assert!(!backup_sidecar(&track).exists(), "backup sidecar removed after undo");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn batch_writes_by_stem_and_skips_existing() {
    if !have_ffmpeg() {
        eprintln!("skipping batch test: ffmpeg not found");
        return;
    }
    let dir = workdir("batch");
    let adir = dir.join("audio");
    let jdir = dir.join("cues");
    std::fs::create_dir_all(&adir).unwrap();
    std::fs::create_dir_all(&jdir).unwrap();

    for stem in ["one", "two"] {
        let t = adir.join(format!("{stem}.flac"));
        gen_audio(&t);
        std::fs::write(
            jdir.join(format!("{stem}.cues.json")),
            r#"{"cues":[{"index":0,"ms":1000},{"index":1,"ms":2000},{"index":2,"ms":3000}]}"#,
        )
        .unwrap();
    }
    // a third audio file with no matching json -> counted as no-json, untouched
    gen_audio(&adir.join("three.flac"));

    let (ok, _o, e) = run(&["batch", adir.to_str().unwrap(), "--json-dir", jdir.to_str().unwrap()]);
    assert!(ok, "batch failed: {e}");
    assert_eq!(indices(&read_cues(&adir.join("one.flac"))).len(), 3);
    assert_eq!(indices(&read_cues(&adir.join("two.flac"))).len(), 3);
    assert_eq!(indices(&read_cues(&adir.join("three.flac"))).len(), 0, "no json -> untouched");

    // second run with --skip-existing should not error and leave cues intact
    let (ok, _o, e) = run(&[
        "batch", adir.to_str().unwrap(), "--json-dir", jdir.to_str().unwrap(), "--skip-existing",
    ]);
    assert!(ok, "second batch failed: {e}");
    assert_eq!(indices(&read_cues(&adir.join("one.flac"))).len(), 3, "still 3 after skip-existing");
    let _ = std::fs::remove_dir_all(&dir);
}

fn backup_sidecar(file: &Path) -> PathBuf {
    let mut s = file.as_os_str().to_owned();
    s.push(".cuebak");
    PathBuf::from(s)
}

fn sibling(file: &Path, suffix: &str) -> PathBuf {
    let mut s = file.as_os_str().to_owned();
    s.push(suffix);
    PathBuf::from(s)
}

#[test]
fn out_dir_writes_copy_and_copies_vdjstems_leaving_original_untouched() {
    if !have_ffmpeg() {
        eprintln!("skipping out-dir test: ffmpeg not found");
        return;
    }
    let dir = workdir("outdir");
    let src = dir.join("src");
    let out = dir.join("out");
    std::fs::create_dir_all(&src).unwrap();
    let track = src.join("track.flac");
    gen_audio(&track);
    std::fs::write(sibling(&track, ".vdjstems"), b"fake stems payload").unwrap(); // sidecar

    let (ok, _o, e) = run(&[
        "write", track.to_str().unwrap(), "--cue", "0:1234::VIA-OUTDIR",
        "--out-dir", out.to_str().unwrap(),
    ]);
    assert!(ok, "out-dir write failed: {e}");

    // original untouched (no cues), copy has the cue, sidecar carried over
    assert_eq!(indices(&read_cues(&track)).len(), 0, "original left untouched");
    let copy = out.join("track.flac");
    assert_eq!(indices(&read_cues(&copy)), vec![0], "copy has the written cue");
    assert!(out.join("track.flac.vdjstems").exists(), ".vdjstems sidecar copied");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn export_import_restores_tag_byte_faithfully() {
    if !have_ffmpeg() {
        eprintln!("skipping export/import test: ffmpeg not found");
        return;
    }
    let dir = workdir("export");
    let ckpt = dir.join("ckpt");
    let track = dir.join("track.flac");
    gen_audio(&track);
    // seed known cues, then checkpoint
    let (ok, _o, e) = run(&["write", track.to_str().unwrap(), "--cue", "0:1000::A", "--cue", "3:4000::D"]);
    assert!(ok, "seed failed: {e}");
    let (ok, _o, e) = run(&["export", track.to_str().unwrap(), "--out", ckpt.to_str().unwrap()]);
    assert!(ok, "export failed: {e}");
    assert!(ckpt.join("track.flac.markers2").exists(), "raw sidecar written");
    assert!(ckpt.join("track.flac.cues.toml").exists(), "human-readable toml written");

    // wreck the cues, then import -> original restored exactly
    let (ok, ..) = run(&["write", track.to_str().unwrap(), "--replace", "--cue", "7:9999::WRECK"]);
    assert!(ok);
    assert_eq!(indices(&read_cues(&track)), vec![7]);
    let (ok, _o, e) = run(&["import", track.to_str().unwrap(), "--from", ckpt.to_str().unwrap()]);
    assert!(ok, "import failed: {e}");
    let got = read_cues(&track);
    assert_eq!(indices(&got), vec![0, 3], "original cue indices restored");
    assert_eq!(got["cues"][0]["label"].as_str().unwrap(), "A");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn write_accepts_toml_input() {
    if !have_ffmpeg() {
        eprintln!("skipping toml-input test: ffmpeg not found");
        return;
    }
    let dir = workdir("tomlin");
    let track = dir.join("track.flac");
    gen_audio(&track);
    let toml = dir.join("cues.toml");
    std::fs::write(
        &toml,
        "[[cue]]\nindex = 0\nms = 500\nlabel = \"Intro\"\n\n[[cue]]\nindex = 1\nms = 1500\ncolor = \"00CC00\"\n",
    )
    .unwrap();
    let (ok, _o, e) = run(&["write", track.to_str().unwrap(), "--toml", toml.to_str().unwrap()]);
    assert!(ok, "toml write failed: {e}");
    let got = read_cues(&track);
    assert_eq!(indices(&got), vec![0, 1]);
    assert_eq!(got["cues"][1]["color"].as_str().unwrap(), "00CC00");
    let _ = std::fs::remove_dir_all(&dir);
}
