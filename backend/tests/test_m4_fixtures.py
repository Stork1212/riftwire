"""Self-tests for the M4 test fixtures (prepared ahead of M4): the stub Whisper must hear
the tone-coded words, report chunk-edge words, count its runs and crash on demand; the
marker clips must put flashes and beeps where intended, including with variable frame rate.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from tests.m4_fixtures import AMPLITUDE, marker_clip, plan_words, tone_word_audio

STUB = Path(__file__).with_name("m4_stub_whisper.py")


def run_stub(audio: Path, out: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    return subprocess.run([sys.executable, str(STUB), "model", str(audio), str(out)],
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, **(env or {})})


@pytest.fixture(scope="module")
def speech(tmp_path_factory):
    d = tmp_path_factory.mktemp("speech")
    words = plan_words(40, pause_every=10)
    return tone_word_audio(d / "speech.wav", words, 25.0), words


def test_stub_hears_every_word_at_the_right_time(speech, tmp_path):
    audio, truth = speech
    assert run_stub(audio, tmp_path / "w.json").returncode == 0
    got = json.loads((tmp_path / "w.json").read_text())["words"]
    assert [w["w"] for w in got] == [t.text for t in truth]
    for g, t in zip(got, truth, strict=True):
        assert abs(g["start_ms"] - t.start_ms) <= 30 and abs(g["end_ms"] - t.end_ms) <= 30


def test_chunk_times_are_relative_and_edge_words_are_flagged(speech, tmp_path):
    audio, truth = speech
    cut = (truth[12].start_ms + 120) / 1000          # inside word 12
    chunk = tmp_path / "chunk.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{cut:.3f}", "-i",
                    str(audio), "-t", "4", str(chunk)], check=True, timeout=60)
    run_stub(chunk, tmp_path / "c.json")
    got = json.loads((tmp_path / "c.json").read_text())["words"]
    assert got[0]["w"] == truth[12].text and got[0]["start_ms"] == 0 and got[0]["prob"] < 0.9
    second = got[1]
    assert abs(second["start_ms"] + cut * 1000 - truth[13].start_ms) <= 30   # offset maps back


def test_runs_are_logged_and_crash_on_demand(speech, tmp_path):
    audio, _ = speech
    log = tmp_path / "runs.jsonl"
    env = {"RIFTWIRE_STUB_WHISPER_LOG": str(log), "RIFTWIRE_STUB_WHISPER_FAIL_AFTER": "2"}
    codes = [run_stub(audio, tmp_path / f"{i}.json", env).returncode for i in range(4)]
    assert codes == [0, 0, 70, 0]                      # exactly one crash, then it recovers
    assert len(log.read_text().splitlines()) == 4
    assert not (tmp_path / "2.json").exists()          # this crash mode writes nothing


def test_crash_can_leave_truncated_output_like_a_killed_child(speech, tmp_path):
    audio, _ = speech
    env = {"RIFTWIRE_STUB_WHISPER_LOG": str(tmp_path / "runs.jsonl"),
           "RIFTWIRE_STUB_WHISPER_FAIL_AFTER": "0", "RIFTWIRE_STUB_WHISPER_TRUNCATE": "1"}
    r = run_stub(audio, tmp_path / "out.json", env)
    assert r.returncode == 70
    with pytest.raises(json.JSONDecodeError):
        json.loads((tmp_path / "out.json").read_text())


def test_fail_after_without_a_log_is_refused(speech, tmp_path):
    audio, _ = speech
    r = run_stub(audio, tmp_path / "out.json", {"RIFTWIRE_STUB_WHISPER_FAIL_AFTER": "1"})
    assert r.returncode == 64


def test_stub_output_keys_match_the_real_child(speech, tmp_path):
    audio, _ = speech
    assert run_stub(audio, tmp_path / "o.json", {}).returncode == 0
    out = json.loads((tmp_path / "o.json").read_text())
    assert {"words", "text", "language", "import_s", "load_s", "transcribe_s",
            "mlx_version", "mlx_whisper_version", "torch_installed",
            "mlx_peak_bytes"} <= set(out)


def test_a_word_starting_at_sample_zero_is_not_an_edge_word(tmp_path):
    words = plan_words(3, start_s=0.0)
    audio = tone_word_audio(tmp_path / "z.wav", words, 2.0)
    assert run_stub(audio, tmp_path / "z.json", {}).returncode == 0
    got = json.loads((tmp_path / "z.json").read_text())["words"]
    assert [w["prob"] for w in got] == [0.95, 0.95, 0.95]


def test_long_audio_is_fast_and_deterministic(tmp_path):
    import hashlib
    import time

    words = plan_words(1200, start_s=0.5)            # ~8.5 minutes of speech
    t0 = time.monotonic()
    a = tone_word_audio(tmp_path / "a.wav", words, 520.0)
    assert time.monotonic() - t0 < 60
    b = tone_word_audio(tmp_path / "b.wav", words, 520.0)
    assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def _frames(path: Path) -> list[tuple[float, float]]:
    out = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi", "-i",
                          f"movie={path},signalstats", "-show_entries",
                          "frame=pts_time:frame_tags=lavfi.signalstats.YAVG", "-of", "json"],
                         capture_output=True, text=True, check=True, timeout=120).stdout
    return [(float(f["pts_time"]), float(f["tags"]["lavfi.signalstats.YAVG"]))
            for f in json.loads(out)["frames"]]


def _beeps(path: Path) -> list[float]:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac",
                          "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True,
                         check=True, timeout=60).stdout
    s = [abs(int.from_bytes(raw[i:i + 2], "little", signed=True)) for i in range(0, len(raw), 2)]
    onsets, quiet = [], True
    for i, v in enumerate(s):
        if v > 8000 and quiet:
            onsets.append(i / 8000)
            quiet = False
        elif v < 500 and not quiet and all(x < 500 for x in s[i:i + 80]):
            quiet = True
    return onsets


@pytest.mark.parametrize("vfr", [False, True])
def test_marker_clip_puts_flashes_and_beeps_at_the_markers(tmp_path, vfr):
    markers = [1.0, 3.5, 5.2, 8.0]
    clip = marker_clip(tmp_path / "m.mp4", markers, 10.0, vfr=vfr)
    frames = _frames(clip)
    flashes = [t for t, y in frames if y > 128]
    assert len(flashes) == len(markers)
    assert all(abs(f - m) <= 1 / 30 + 1e-3 for f, m in zip(flashes, markers, strict=True))
    beeps = _beeps(clip)
    assert len(beeps) == len(markers)
    assert all(abs(b - m) <= 0.05 for b, m in zip(beeps, markers, strict=True))
    gaps = {round(b - a, 3) for (a, _), (b, _) in zip(frames, frames[1:], strict=False)}
    assert (len(gaps) > 1) is vfr            # VFR really has irregular frame durations


def test_off_grid_markers_still_flash_once_in_step_with_the_beep(tmp_path):
    clip = marker_clip(tmp_path / "off.mp4", [1.0005, 2.0009, 3.517], 5.0)
    frames = _frames(clip)
    flashes = [t for t, y in frames if y > 128]
    assert len(flashes) == 3                          # none dropped
    beeps = _beeps(clip)
    assert all(abs(f - b) <= 0.03 for f, b in zip(flashes, beeps, strict=True))


def test_codex_review_a_word_cut_by_the_file_end_is_not_faded_out(tmp_path):
    # Codex review of 3ffad28: clamping the word end moved its fade-out to the file end.
    import wave

    words = plan_words(1, start_s=0)
    cut = tone_word_audio(tmp_path / "cut.wav", words, 0.15)
    assert words[0].end_ms > 150
    with wave.open(str(cut)) as f:
        raw = f.readframes(f.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    tail = max(abs(x) for x in samples[-80:])               # the last 5 ms
    assert tail > 0.5 * 32767 * AMPLITUDE                      # still at full level: cut
