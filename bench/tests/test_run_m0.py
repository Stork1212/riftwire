"""Launcher tests: run_m0.sh is executed for real against stub commands in a temp git repo.

Stubs record every call and exit with scenario-controlled codes, so each control-flow path
(plan failure, budget, floor skips vs real failures, git safety, the per-volume watchdog)
is exercised without mlx, network or the Mac.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("pgrep") is None,
                                reason="git and pgrep required")

STUB_PY = r"""#!/bin/bash
echo "$*" >> "$STUB_LOG"
case "$*" in
  *"plan --signals-only"*)           exit "${STUB_PLAN_SIG_RC:-0}" ;;
  *"plan --models tiny,base,small"*) exit "${STUB_PLAN_FULL_RC:-0}" ;;
  *"plan --models tiny,base "*)      exit "${STUB_PLAN_TB_RC:-0}" ;;
  *"fetch-models --models tiny,base"*) exit "${STUB_FETCH_TB_RC:-0}" ;;
  *"fetch-models --models small"*)   exit "${STUB_FETCH_SMALL_RC:-0}" ;;
  *" all "*)
     [ -n "${STUB_DIRTY_DURING_ALL:-}" ] && touch "$STUB_REPO/stray.txt"
     [ -n "${STUB_ALL_SLEEP:-}" ] && sleep "$STUB_ALL_SLEEP" && echo ALL-FINISHED-SLEEP
     exit "${STUB_ALL_RC:-0}" ;;
esac
exit 0
"""
STUB_SETUP = """#!/bin/bash
echo "setup $*" >> "$STUB_LOG"
[ -n "${STUB_SETUP_SLEEP:-}" ] && sleep "$STUB_SETUP_SLEEP" && echo SETUP-FINISHED-SLEEP
exit "${STUB_SETUP_RC:-0}"
"""


def _exe(p: Path, text: str) -> None:
    p.write_text(text)
    p.chmod(0o755)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "bench" / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(BENCH / "run_m0.sh", r / "bench" / "run_m0.sh")
    _exe(r / "bench" / "setup_bench.sh", STUB_SETUP)
    _exe(r / "bench" / ".venv" / "bin" / "python", STUB_PY)
    (r / "bench" / "run.py").write_text("# stub\n")
    (r / ".gitignore").write_text(".venv\ndata/\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=r, check=True, env=env)
    return r


def run(repo: Path, tmp_path: Path, timeout: float = 60, **env_over) -> tuple[int, str, list]:
    log = tmp_path / "calls.log"
    log.write_text("")
    hooks = Path(env_over.pop("RIFTWIRE_BENCH_TEST_FREE_DIR", tmp_path / "free"))
    hooks.mkdir(exist_ok=True)
    (hooks / "_default").write_text(str(70 * 1024 * 1024))  # plenty, unless a test says not
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "RIFTWIRE_PYTHON": str(repo / "bench" / ".venv" / "bin" / "python"),
        "RIFTWIRE_BENCH_OUT": str(tmp_path / "out"),
        "STUB_LOG": str(log), "STUB_REPO": str(repo),
        "RIFTWIRE_BENCH_TEST_FREE_DIR": str(hooks),
        **{k: str(v) for k, v in env_over.items()},
    }
    p = subprocess.run(["bash", str(repo / "bench" / "run_m0.sh")], env=env,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr, log.read_text().splitlines()


def test_happy_path_runs_every_step_in_order(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path)
    assert rc == 0, out
    steps = [c.split()[1] if not c.startswith("setup") else "setup" for c in calls]
    assert steps == ["plan", "setup", "fetch-models", "fetch-models", "all"]
    assert "--models tiny,base,small --out" in calls[-1]
    assert "git tree unchanged" in out
    csv = (tmp_path / "out" / "free_space.csv").read_text().splitlines()
    assert csv[0] == "utc,volume,free_kb,free_gib,phase" and len(csv) > 5


def test_failed_plan_stops_before_setup(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_PLAN_FULL_RC=1)
    assert rc == 1 and "plan failed" in out
    assert len(calls) == 1  # nothing after the plan


def test_over_budget_even_without_small_refuses(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_PLAN_FULL_RC=11, STUB_PLAN_TB_RC=11)
    assert rc == 11 and "approved" in out
    assert all("plan" in c for c in calls)


def test_floor_plan_drops_small_and_never_fetches_it(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_PLAN_FULL_RC=10)
    assert rc == 0, out
    assert not any("--models small" in c for c in calls)
    assert "--models tiny,base --out" in calls[-1]


def test_small_floor_skip_is_tolerated_and_reported(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_FETCH_SMALL_RC=10)
    assert rc == 0, out
    assert "small SKIPPED: free-space floor" in out
    assert "--models tiny,base --out" in calls[-1]


@pytest.mark.parametrize("code", [1, 2, 3])
def test_small_real_failure_is_not_skipped(repo, tmp_path, code):
    rc, out, calls = run(repo, tmp_path, STUB_FETCH_SMALL_RC=code)
    assert rc == 1 and "small download FAILED" in out
    assert not any(" all " in f" {c} " for c in calls)


def test_tiny_base_failure_stops(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_FETCH_TB_RC=1)
    assert rc == 1 and not any("small" in c and "fetch" in c for c in calls)


def test_setup_failure_stops(repo, tmp_path):
    rc, out, calls = run(repo, tmp_path, STUB_SETUP_RC=1)
    assert rc == 1 and "setup failed" in out and len(calls) == 2


def test_dirty_tree_refused_before_anything(repo, tmp_path):
    (repo / "notes.txt").write_text("uncommitted work")
    rc, out, calls = run(repo, tmp_path)
    assert rc == 4 and calls == []
    assert (repo / "notes.txt").read_text() == "uncommitted work"


def test_tree_change_during_run_is_detected(repo, tmp_path):
    rc, out, _ = run(repo, tmp_path, STUB_DIRTY_DURING_ALL=1)
    assert rc == 4 and "changed during the run" in out


def _mount(p: Path) -> str:
    return subprocess.run(["df", "-P", str(p)], capture_output=True, text=True
                          ).stdout.splitlines()[1].split()[-1]


@pytest.mark.skipif(not Path("/dev/shm").is_dir(), reason="needs a second volume")
def test_watchdog_monitors_second_volume_and_stops_children(repo, tmp_path):
    data = Path("/dev/shm") / f"rw-test-{os.getpid()}"
    data.mkdir(exist_ok=True)
    try:
        assert _mount(data) != _mount(repo)
        hooks = tmp_path / "free"
        hooks.mkdir()
        hook = hooks / _mount(data).replace("/", "_")
        hook.write_text(str(70 * 1024 * 1024))
        import threading
        import time

        def breach():
            time.sleep(4)
            hook.write_text(str(59 * 1024 * 1024))
        threading.Thread(target=breach, daemon=True).start()
        t0 = time.monotonic()
        rc, out, calls = run(repo, tmp_path, RIFTWIRE_DATA_DIR=data,
                             RIFTWIRE_BENCH_TEST_FREE_DIR=hooks, STUB_SETUP_SLEEP=30)
        assert rc == 3, out
        assert time.monotonic() - t0 < 20            # stopped, did not wait 30 s
        assert "SETUP-FINISHED-SLEEP" not in out
        assert f"FLOOR BREACH on {_mount(data)}" in out
        assert _mount(data) in out.split("watching volumes:")[1].splitlines()[0]
        assert not any(c.startswith("fetch") or "fetch-models" in c for c in calls)
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_signals_only_resume_plans_and_runs_only_signals(repo, tmp_path):
    prev = tmp_path / "prev"
    prev.mkdir()
    (prev / "results.json").write_text('{"transcription": [1]}')
    log = tmp_path / "calls.log"
    log.write_text("")
    env_args = ["--signals-only", str(prev)]
    p = subprocess.run(
        ["bash", str(repo / "bench" / "run_m0.sh"), *env_args],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "RIFTWIRE_PYTHON": str(repo / "bench" / ".venv" / "bin" / "python"),
             "STUB_LOG": str(log), "STUB_REPO": str(repo),
             "RIFTWIRE_BENCH_TEST_FREE_DIR": str(_hooks(tmp_path))},
        capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines()
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(calls) == 2 and "plan --signals-only" in calls[0]
    assert "all --resume --skip-transcription --out " + str(prev) in calls[1]
    assert not any("fetch" in c or c.startswith("setup") for c in calls)


def test_signals_only_floor_refusal_stops(repo, tmp_path):
    prev = tmp_path / "prev"
    prev.mkdir()
    (prev / "results.json").write_text("{}")
    log = tmp_path / "calls.log"
    log.write_text("")
    p = subprocess.run(
        ["bash", str(repo / "bench" / "run_m0.sh"), "--signals-only", str(prev)],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "RIFTWIRE_PYTHON": str(repo / "bench" / ".venv" / "bin" / "python"),
             "STUB_LOG": str(log), "STUB_REPO": str(repo), "STUB_PLAN_SIG_RC": "10",
             "RIFTWIRE_BENCH_TEST_FREE_DIR": str(_hooks(tmp_path))},
        capture_output=True, text=True, timeout=60)
    assert p.returncode == 10
    assert len(log.read_text().splitlines()) == 1


def _hooks(tmp_path: Path) -> Path:
    h = tmp_path / "free"
    h.mkdir(exist_ok=True)
    (h / "_default").write_text(str(70 * 1024 * 1024))
    return h
