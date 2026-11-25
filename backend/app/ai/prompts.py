"""Versioned prompt registry: files under ``prompts/`` with TOML front matter (stdlib
``tomllib``; no new dependency).

A prompt is identified by ``id`` + ``version`` and recorded with its SHA-256 on every
result (CLAUDE.md rule 16). Game, channel and category instructions are data files, so
RumbleFrame and GTA never appear in code. Placeholders are ``{{name}}``; an unknown or
missing placeholder is an error, never a silent blank.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.ai.schemas import EvidenceBundle

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"
_FRONT = re.compile(r"\A\+\+\+\n(.*?)\n\+\+\+\n(.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^## (system|user)\s*$", re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    schema: str
    meta: dict
    system: str
    user: str
    sha256: str
    path: Path

    def render(self, **overrides: str) -> tuple[str, str]:
        values = {k: str(v) for k, v in self.meta.get("defaults", {}).items()} | overrides

        def fill(text: str) -> str:
            missing = sorted({m for m in _PLACEHOLDER.findall(text) if m not in values})
            if missing:
                raise PromptError(f"{self.id} v{self.version}: missing values {missing}")
            return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
        return fill(self.system), fill(self.user)


def load_file(path: Path) -> Prompt:
    raw = path.read_text(encoding="utf-8")
    m = _FRONT.match(raw)
    if not m:
        raise PromptError(f"{path}: missing +++ TOML front matter")
    meta = tomllib.loads(m.group(1))
    for key in ("id", "version", "schema"):
        if key not in meta:
            raise PromptError(f"{path}: front matter needs {key!r}")
    parts = _SECTION.split(m.group(2))
    sections = dict(zip(parts[1::2], (p.strip() for p in parts[2::2]), strict=False))
    if set(sections) != {"system", "user"}:
        raise PromptError(f"{path}: needs exactly one '## system' and one '## user' section")
    return Prompt(id=meta["id"], version=int(meta["version"]), schema=meta["schema"],
                  meta=meta, system=sections["system"], user=sections["user"],
                  sha256=hashlib.sha256(raw.encode()).hexdigest(), path=path)


def load(prompt_id: str, version: int | None = None, root: Path = PROMPTS_DIR) -> Prompt:
    found = [p for p in (load_file(f) for f in sorted(root.rglob("*.md"))
                         if f.read_text(encoding="utf-8").startswith("+++"))
             if p.id == prompt_id and (version is None or p.version == version)]
    if not found:
        raise PromptError(f"no prompt {prompt_id!r}" + (f" v{version}" if version else ""))
    return max(found, key=lambda p: p.version)


def channel_voice(channel: str | None, root: Path = PROMPTS_DIR) -> str:
    if not channel:
        return "Neutral, accurate, energetic short-form gaming voice."
    path = root / "channels" / f"{channel}.md"
    if not path.is_file() or path.resolve().parent != (root / "channels").resolve():
        raise PromptError(f"no channel voice file for {channel!r}")
    return path.read_text(encoding="utf-8").strip()


def _quote(text: str) -> str:
    # JSON-quoted, with angle brackets escaped so no word can close the evidence block.
    return json.dumps(text, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def render_evidence(bundle: EvidenceBundle) -> str:
    """Compact, line-oriented evidence. Every item carries its id; text is JSON-quoted so
    transcript content cannot break out of its data block."""
    lines = [f"recording {bundle.recording_id}  duration {bundle.duration_ms / 1000:.1f}s"
             f"  game {bundle.game or 'unknown'}"]
    for w in bundle.windows:
        lines.append(f"\nWINDOW {w.id}  {w.t0 / 1000:.1f}s-{w.t1 / 1000:.1f}s")
        ws = [x for x in bundle.words if w.t0 <= x.t0 < w.t1]
        for i in range(0, len(ws), 12):
            chunk = ws[i:i + 12]
            lines.append(f"  words @{chunk[0].t0 / 1000:.1f}s: " + " ".join(
                f"{x.id}={_quote(x.text)}" for x in chunk))
        for e in bundle.events:
            if e.t1 > w.t0 and e.t0 < w.t1:
                val = "" if e.value is None else f" value={e.value:g}"
                lines.append(f"  {e.nature} {e.kind} {e.id} {e.t0 / 1000:.1f}s-"
                             f"{e.t1 / 1000:.1f}s{val}")
        for k in bundle.keyframes:
            if w.t0 <= k.t <= w.t1:
                lines.append(f"  keyframe {k.id} @{k.t / 1000:.1f}s")
    return "\n".join(lines)
