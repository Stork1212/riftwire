from __future__ import annotations

import time
from fractions import Fraction

import pytest

from app.services import media
from tests.conftest import FFMPEG, FFPROBE, requires_ffmpeg

pytestmark = requires_ffmpeg


def probe(path):
    return media.probe(FFPROBE, path, timeout=30)


@pytest.mark.parametrize("name,family,ext", [
    ("basic.mp4", "mov", "mp4"),
    ("basic.mov", "mov", "mov"),
    ("basic.mkv", "matroska", "mkv"),
    ("basic.webm", "matroska", "webm"),
])
def test_supported_containers(media_dir, name, family, ext):
    info = probe(media_dir / name)
    assert info.family == family
    assert (info.width, info.height) == (320, 240)
    assert info.has_audio is True
    assert info.audio_codec in ("aac", "opus")
    assert 1900 <= info.duration_ms <= 2200
    assert media.choose_extension(info, ext) == ext


def test_rational_frame_rate_is_exact(media_dir):
    info = probe(media_dir / "basic.mp4")
    assert Fraction(info.fps_num, info.fps_den) == Fraction(30000, 1001)
    assert info.is_variable_frame_rate is False


def test_no_audio(media_dir):
    info = probe(media_dir / "noaudio.mp4")
    assert info.has_audio is False
    assert info.audio_codec is None
    assert info.video_codec == "h264"


def test_very_short(media_dir):
    info = probe(media_dir / "short.mp4")
    assert info.duration_ms is not None and info.duration_ms <= 400
    media.verify_decodable(FFMPEG, media_dir / "short.mp4", timeout=30)


def test_variable_frame_rate_detected(media_dir):
    info = probe(media_dir / "vfr.mp4")
    assert info.is_variable_frame_rate is True
    assert info.fps_num and info.fps_den


def test_rotation_and_sar_give_display_dimensions(media_dir):
    assert (probe(media_dir / "rotated.mp4").width, probe(media_dir / "rotated.mp4").height) \
        == (240, 320)
    ana = probe(media_dir / "anamorphic.mp4")
    assert (ana.width, ana.height) == (427, 240)


@pytest.mark.parametrize("name", ["garbage.mp4", "text.mp4", "image_as.mp4",
                                  "audio_only.mp4"])
def test_invalid_media_rejected_by_probe(media_dir, name):
    with pytest.raises(media.InvalidMedia):
        probe(media_dir / name)


def test_truncated_file_rejected(media_dir):
    with pytest.raises(media.InvalidMedia):
        info = probe(media_dir / "truncated.mp4")
        media.verify_decodable(FFMPEG, media_dir / "truncated.mp4", timeout=30,
                               duration_ms=info.duration_ms)


@pytest.mark.parametrize("name", ["basic.mp4", "basic.mov", "basic.mkv", "basic.webm",
                                  "vfr.mp4", "short.mp4", "rotated.mp4"])
def test_valid_files_decode(media_dir, name):
    info = probe(media_dir / name)
    media.verify_decodable(FFMPEG, media_dir / name, timeout=30, duration_ms=info.duration_ms)


def test_content_decides_extension(media_dir):
    info = probe(media_dir / "basic.mkv")
    assert media.choose_extension(info, "mp4") == "mkv"


def test_timeout_kills_process_group():
    start = time.monotonic()
    with pytest.raises(media.MediaToolTimeout):
        media.run_command([FFMPEG, "-v", "error", "-re", "-f", "lavfi", "-i",
                           "testsrc2=duration=30", "-f", "null", "-"], timeout=1)
    assert time.monotonic() - start < 8


def test_cancellation_kills_child():
    pids = []
    t0 = time.monotonic()
    with pytest.raises(media.CommandCancelled):
        media.run_command([FFMPEG, "-v", "error", "-re", "-f", "lavfi", "-i",
                           "testsrc2=duration=30", "-f", "null", "-"], timeout=30,
                          on_spawn=lambda pid, sig: pids.append((pid, sig)),
                          should_cancel=lambda: time.monotonic() - t0 > 0.5)
    pid, sig = pids[0]
    assert sig  # start signature recorded
    assert not media.pid_alive(pid)


def test_missing_executable():
    with pytest.raises(media.MediaToolMissing):
        media.run_command(["/nonexistent/ffmpeg-xyz"], timeout=1)


def test_args_must_be_list():
    with pytest.raises(ValueError):
        media.run_command("ffmpeg -version", timeout=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("name,expect", [
    ("basic.mp4", (360, 270)), ("portrait.mp4", (152, 270)),
    ("anamorphic.mp4", (480, 270)), ("short.mp4", (360, 270)),
])
def test_thumbnail_aspect_preserved(media_dir, tmp_path, name, expect):
    dst = tmp_path / "t.jpg"
    info = probe(media_dir / name)
    media.render_thumbnail(FFMPEG, media_dir / name, dst,
                           seek=media.thumbnail_seek_seconds(info.duration_ms),
                           width=480, height=270, timeout=30)
    assert media.is_valid_jpeg(dst)
    t = probe_image(dst)
    assert t == expect or abs(t[0] - expect[0]) <= 2 and abs(t[1] - expect[1]) <= 2


def probe_image(path):
    import json
    import subprocess
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams",
                          str(path)], capture_output=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def test_tool_status():
    st = media.tool_status(FFMPEG)
    assert st.available and st.version and "ffmpeg" in st.version
    assert media.tool_status("definitely-not-a-tool").available is False
