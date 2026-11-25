"""The Short's outro: the whole asset, its own duration, every audio variant.

Synthetic stand-ins with the approved RumbleFrame outro's format (1080×1920, 30 fps,
75 frames = 2.5 s): with an audio sting (PR #6), with a silent audio track, and with no
audio track (v1 launch kit, PR #11). The gameplay is a 20 s synthetic clip with a loud
440 Hz tone. Whisper is replaced by fixed words; everything else (FFmpeg, ffprobe, the
rights checks, the workspace and reservations) runs for real."""

from __future__ import annotations

import math
import struct
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.errors import ServiceUnavailable
from app.models import DiskReservation
from app.services import acquisition, clips, outro
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.test_ingest import drain
from tests.test_rights import World

pytestmark = requires_ffmpeg

CLIP_MS = 15_000
OUTRO_FRAMES = 75
WORDS = [{"w": "Getaway", "start_ms": 1000, "end_ms": 1600, "prob": 0.9},
         {"w": "is", "start_ms": 1600, "end_ms": 1900, "prob": 0.9},
         {"w": "insane", "start_ms": 1900, "end_ms": 2600, "prob": 0.9}]


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True,
                   timeout=120)


@pytest.fixture(scope="module")
def assets(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("outro")
    video = ["-f", "lavfi", "-i", f"color=c=0xFF00FF:s=1080x1920:r=30:d={OUTRO_FRAMES / 30}"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-frames:v", str(OUTRO_FRAMES)]
    loud = "aevalsrc=0.95*sin(2*PI*1000*t)|0.95*sin(2*PI*1000*t):s=48000:d=2.5"
    out = {}
    out["sting"] = d / "sting.mp4"
    _ff(*video, "-f", "lavfi", "-i", loud, *enc, "-c:a", "aac", "-shortest", str(out["sting"]))
    out["silent"] = d / "silent.mp4"
    _ff(*video, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.5", *enc, "-c:a", "aac",
        "-shortest", str(out["silent"]))
    out["none"] = d / "none.mp4"
    _ff(*video, *enc, str(out["none"]))
    out["gameplay"] = d / "gameplay.mp4"
    tone = "aevalsrc=0.9*sin(2*PI*440*t)|0.9*sin(2*PI*440*t):s=48000:d=20"
    _ff("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:d=20", "-f", "lavfi", "-i", tone,
        "-vf", "hue=s=0", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", str(out["gameplay"]))
    out["corrupt"] = d / "corrupt.mp4"
    out["corrupt"].write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"garbage" * 100)
    return out


@pytest.fixture
def recording(db, settings, assets, monkeypatch):
    w = World(db)
    w.licence()
    sid = w.source(url=None, kind="local", creator_channel_id=w.local)
    rec = import_file(db, settings, assets["gameplay"]).recording
    drain(db, settings)
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id)
    monkeypatch.setattr(clips, "_transcribe", lambda *_a: list(WORDS))
    return rec.id


def _render(db, settings, rec_id, start_ms=2_000, length=CLIP_MS, tmp=None) -> Path:
    data = clips.cut(db, settings, rec_id, start_ms, start_ms + length, layout="split",
                     title="Getaway")
    path = (tmp or settings.temp_dir) / f"short-{start_ms}.mp4"
    path.write_bytes(data)
    return path


def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_type,width,height,r_frame_rate,nb_read_frames,duration,"
                        "sample_rate:format=duration", "-of", "json", str(path)],
                       check=True, capture_output=True, timeout=120)
    import json
    return json.loads(r.stdout)


def _pcm(path: Path, start: float, dur: float, rate: int = 8000) -> list[float]:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start}", "-t", f"{dur}",
                          "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le",
                          "-"], check=True, capture_output=True, timeout=60).stdout
    return [v / 32768 for v in struct.unpack(f"<{len(raw) // 2}h", raw)]


def _rms_db(samples: list[float]) -> float:
    if not samples:
        return -120.0
    rms = math.sqrt(sum(v * v for v in samples) / len(samples))
    return 20 * math.log10(rms) if rms > 0 else -120.0


def _peak_db(path: Path, start: float, dur: float) -> float:
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{start}", "-t", f"{dur}",
                        "-i", str(path), "-vn", "-af", "astats=measure_overall=Peak_level"
                        ":measure_perchannel=none", "-f", "null", "-"],
                       capture_output=True, timeout=60, text=True)
    lines = [ln for ln in r.stderr.splitlines() if "Peak level dB" in ln]
    return float(lines[-1].split(":")[-1])


def _frame(path: Path, t: float) -> bytes:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t}", "-i", str(path),
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          check=True, capture_output=True, timeout=60).stdout


def _count(frame: bytes, rows: range, test) -> int:
    n = 0
    for y in rows:
        row = frame[y * 1080 * 3:(y + 1) * 1080 * 3]
        for x in range(0, 1080 * 3, 3):
            if test(row[x], row[x + 1], row[x + 2]):
                n += 1
    return n


def _red(r, g, b):
    return r > 190 and g < 90 and b < 90


def _magenta(r, g, b):
    return r > 200 and g < 60 and b > 200


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb)


# ------------------------------------------------------------------------- duration

@pytest.mark.parametrize("kind", ["sting", "silent", "none"])
def test_fifteen_seconds_plus_the_outro_is_seventeen_and_a_half(
        db, settings, assets, recording, kind):
    settings.clip_outro_path = assets[kind]
    settings.clip_outro_duration_seconds = 2.5            # validated brand setting
    path = _render(db, settings, recording)
    info = _probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"], v["r_frame_rate"]) == (1080, 1920, "30/1")
    assert int(v["nb_read_frames"]) == 450 + OUTRO_FRAMES     # every outro frame kept
    assert abs(float(info["format"]["duration"]) - 17.5) <= 0.05
    assert abs(float(a["duration"]) - float(v["duration"])) <= 0.05   # in sync
    assert a["sample_rate"] == "48000"
    # all 75 outro frames are the outro; the frame before is gameplay
    assert _count(_frame(path, 15.0 + 1 / 60), range(900, 902), _magenta) == 2 * 1080
    assert _count(_frame(path, 17.5 - 1 / 30), range(900, 902), _magenta) == 2 * 1080
    assert _count(_frame(path, 15.0 - 2 / 30), range(900, 902), _magenta) == 0


def test_outro_duration_comes_from_the_asset_and_mismatch_refuses(
        db, settings, assets, recording):
    settings.clip_outro_path = assets["none"]
    asset = outro.load(settings)
    assert (asset.frames, asset.fps, asset.duration, asset.has_audio) == (75, 30, 2.5, False)
    settings.clip_outro_duration_seconds = 2.0             # the old hard-coded value
    with pytest.raises(ServiceUnavailable) as e:
        _render(db, settings, recording)
    assert e.value.code == "outro_duration_mismatch"
    _no_leftovers(db, settings)


# ---------------------------------------------------------------------------- audio

def test_original_gameplay_audio_is_preserved(db, settings, assets, recording):
    settings.clip_outro_path = assets["sting"]
    path = _render(db, settings, recording, start_ms=2_000)
    out = _pcm(path, 0.5, 14.0)
    src = _pcm(assets["gameplay"], 2.5, 14.0)
    assert _corr(out, src) > 0.99
    assert abs(_rms_db(out) - _rms_db(src)) < 0.5             # no gain change either


def test_continue_mode_keeps_gameplay_under_the_sting_without_clipping(
        db, settings, assets, recording):
    settings.clip_outro_path = assets["sting"]                # 0.95 full-scale sting
    settings.clip_outro_audio, settings.clip_outro_sting_gain_db = "continue", 0.0
    settings.clip_outro_fade_ms = 500
    path = _render(db, settings, recording, start_ms=2_000)
    assert _peak_db(path, 15.0, 2.5) < 0.0                     # limited, never clipped
    assert _rms_db(_pcm(path, 15.2, 1.5)) > -12                # gameplay + sting audible
    settings.clip_outro_path = assets["silent"]               # the gameplay part alone
    quiet = _render(db, settings, recording, start_ms=3_000)
    under = _pcm(quiet, 15.1, 1.5)
    assert _rms_db(under) > -6                                 # gameplay continues
    assert _corr(under, _pcm(assets["gameplay"], 18.1, 1.5)) > 0.99   # the next seconds
    assert _rms_db(_pcm(quiet, 17.3, 0.15)) < _rms_db(under) - 6       # then fades out


def test_outro_mode_ends_gameplay_audio_at_the_cut(db, settings, assets, recording):
    settings.clip_outro_path = assets["none"]
    settings.clip_outro_audio, settings.clip_outro_fade_ms = "outro", 0
    path = _render(db, settings, recording, start_ms=2_000)
    assert _rms_db(_pcm(path, 15.1, 2.3)) < -60                # silence, as the v1 kit asks
    assert _rms_db(_pcm(path, 13.0, 1.5)) > -6                 # gameplay intact before
    settings.clip_outro_fade_ms = 400                          # a short transition
    faded = _render(db, settings, recording, start_ms=3_000)
    assert _rms_db(_pcm(faded, 15.0, 0.15)) > -30
    assert _rms_db(_pcm(faded, 15.6, 1.8)) < -60


def test_recording_that_ends_at_the_cut_still_gets_the_whole_outro(
        db, settings, assets, recording):
    settings.clip_outro_path = assets["sting"]
    path = _render(db, settings, recording, start_ms=5_000)    # ends exactly at 20 s
    info = _probe(path)
    assert abs(float(info["format"]["duration"]) - 17.5) <= 0.05


# ------------------------------------------------------------------------- captions

def test_captions_stay_in_place_and_in_time(db, settings, assets, recording):
    settings.clip_outro_path = assets["silent"]
    path = _render(db, settings, recording, start_ms=2_000)
    band = range(633 + 40, 633 + 108)
    assert _count(_frame(path, 0.5), band, _red) == 0           # before the first word
    assert _count(_frame(path, 1.3), band, _red) > 400          # "Getaway" spoken
    assert _count(_frame(path, 1.3), range(0, 600), _red) == 0  # nowhere else
    assert _count(_frame(path, 16.0), band, _red) == 0          # never over the outro


# -------------------------------------------------------------------------- failure

def _no_leftovers(db, settings) -> None:
    assert list(settings.temp_dir.glob("clip-work-*")) == []
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(DiskReservation)) == 0


def test_failed_render_leaves_no_output(db, settings, assets, recording, monkeypatch):
    settings.clip_outro_path = assets["corrupt"]
    with pytest.raises(ServiceUnavailable) as e:
        _render(db, settings, recording)
    assert e.value.code == "outro_invalid"
    _no_leftovers(db, settings)
    settings.clip_outro_path = assets["sting"]
    real = clips._run

    def broken(args, timeout, error):
        if "-filter_complex" in args:                  # the render itself fails
            args = list(args)
            args[args.index("-filter_complex") + 1] += ";nullsrc,nonexistent_filter"
        return real(args, timeout, error)
    monkeypatch.setattr(clips, "_run", broken)
    with pytest.raises(ServiceUnavailable):
        _render(db, settings, recording)
    _no_leftovers(db, settings)


# ------------------------------------------- rights scope of the post-cut audio tail
# Independent review of PR #14 (2026-11-25): in ``continue`` mode, and ``outro`` mode with
# a fade, the outro plays source audio from *after* the clip. That audio is footage used
# in the export, so it must stay within the rights-checked length.

@pytest.fixture
def limited(db, settings, assets, monkeypatch):
    from tests.test_rights import FULL_SCOPE

    def make(max_clip_seconds: int | None) -> str:
        w = World(db)
        scope = dict(FULL_SCOPE)
        if max_clip_seconds is not None:
            scope["max_clip_seconds"] = max_clip_seconds
        w.licence(scope=scope)
        sid = w.source(url=None, kind="local", creator_channel_id=w.local)
        rec = import_file(db, settings, assets["gameplay"]).recording
        drain(db, settings)
        with db.session() as s:
            acquisition.attach_media(s, settings, sid, rec.id)
        return rec.id
    monkeypatch.setattr(clips, "_transcribe", lambda *_a: list(WORDS))
    return make


def _tail(db, settings, rec_id, start_ms, length=CLIP_MS):
    from app.models import Recording
    with db.session() as s:
        rec = s.get(Recording, rec_id)
        s.expunge(rec)
    return clips.outro_tail(db, settings, rec, start_ms + length, length)


@pytest.mark.parametrize("limit, mode, fade, expect_ms, reason", [
    (None, "continue", 500, 2500, "within_rights_scope"),        # no limit: the whole bumper
    (18, "continue", 500, 2500, "within_rights_scope"),          # 15 + 2.5 <= 18
    (15, "continue", 500, 0, "tail_dropped:clip_too_long"),      # limit reached at the cut
    (17, "continue", 500, 0, "tail_dropped:clip_too_long"),      # 17.5 > 17: dropped whole
    (16, "outro", 400, 400, "within_rights_scope"),              # fade tail fits
    (15, "outro", 400, 0, "tail_dropped:clip_too_long"),         # fade tail would exceed
    (15, "outro", 0, 0, "no_tail_wanted_or_recording_ends_at_the_cut"),
])
def test_tail_is_checked_against_the_rights_scope(db, settings, assets, limited, limit, mode,
                                                  fade, expect_ms, reason):
    settings.clip_outro_path = assets["silent"]
    settings.clip_outro_audio, settings.clip_outro_fade_ms = mode, fade
    rec = limited(limit)
    tail, decision, why = _tail(db, settings, rec, start_ms=2_000)
    assert (tail, why) == (expect_ms, reason)
    assert (decision is not None) == (tail > 0)


def test_restricted_clip_renders_without_post_cut_audio_and_with_the_whole_outro(
        db, settings, assets, limited):
    settings.clip_outro_path = assets["silent"]                  # isolate the gameplay part
    settings.clip_outro_audio, settings.clip_outro_fade_ms = "continue", 500
    rec = limited(15)                                            # clip uses the full limit
    path = _render(db, settings, rec, start_ms=2_000)
    info = _probe(path)
    video = next(x for x in info["streams"] if x["codec_type"] == "video")
    assert abs(float(info["format"]["duration"]) - 17.5) <= 0.05
    assert int(video["nb_read_frames"]) == 450 + OUTRO_FRAMES        # outro intact
    assert _rms_db(_pcm(path, 15.05, 2.3)) < -60                 # no source audio past 15 s
    assert _rms_db(_pcm(path, 13.0, 1.5)) > -6                   # the clip's own audio intact


def test_allowed_tail_still_continues_the_gameplay(db, settings, assets, limited):
    settings.clip_outro_path = assets["silent"]
    settings.clip_outro_audio, settings.clip_outro_fade_ms = "continue", 500
    rec = limited(18)
    path = _render(db, settings, rec, start_ms=2_000)
    under = _pcm(path, 15.1, 1.5)
    assert _rms_db(under) > -6
    assert _corr(under, _pcm(assets["gameplay"], 17.1, 1.5)) > 0.99


@pytest.mark.parametrize("limit, tail_audible", [(15, False), (16, True)])
def test_fade_mode_tail_respects_the_limit(db, settings, assets, limited, limit, tail_audible):
    settings.clip_outro_path = assets["none"]
    settings.clip_outro_audio, settings.clip_outro_fade_ms = "outro", 400
    path = _render(db, settings, limited(limit), start_ms=2_000)
    level = _rms_db(_pcm(path, 15.0, 0.15))
    assert (level > -30) if tail_audible else (level < -60)       # fade tail only if it fits
    assert abs(float(_probe(path)["format"]["duration"]) - 17.5) <= 0.05


def test_recording_ending_at_the_clip_boundary_uses_no_tail(db, settings, assets, limited):
    settings.clip_outro_path = assets["sting"]
    settings.clip_outro_audio, settings.clip_outro_fade_ms = "continue", 500
    rec = limited(15)
    tail, decision, why = _tail(db, settings, rec, start_ms=5_000)      # ends at 20 s
    assert (tail, decision, why) == (0, None, "no_tail_wanted_or_recording_ends_at_the_cut")
    path = _render(db, settings, rec, start_ms=5_000)
    assert abs(float(_probe(path)["format"]["duration"]) - 17.5) <= 0.05
