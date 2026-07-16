"""출력: 노래 타임라인 (복붙용 txt)."""

from __future__ import annotations

from pathlib import Path

from soopts.models import MetaResult, Song


def fmt_hms(sec: int) -> str:
    """초 → HH:MM:SS."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_songs(meta: MetaResult | None, songs: list[Song]) -> str:
    """노래 타임라인 텍스트를 만든다."""
    lines: list[str] = []
    if meta:
        lines.append(f"# {meta.title}")
        if meta.bj_nick:
            lines.append(f"# BJ: {meta.bj_nick}")
    lines.append(f"# 🎤 노래 {len(songs)}곡 (오디오 음악감지 + 스티커 반응 + STT 곡식별)")
    lines.append("")
    for s in sorted(songs, key=lambda s: s.t):
        name = s.title or "곡명 미상"
        tag = "유력" if s.song_likely else "후보"
        lines.append(f"[ {fmt_hms(s.t)} ] 🎤 {name}")
        detail = f"    · {tag} · {s.duration}초 · 스티커 {s.sticker_rate}/분"
        lines.append(detail)
        if s.lyrics and not s.title:
            lines.append(f"    · 가사: {s.lyrics[:80]}")
    return "\n".join(lines) + "\n"


def write_songs_txt(path: Path, meta: MetaResult | None, songs: list[Song]) -> Path:
    path.write_text(render_songs(meta, songs), encoding="utf-8")
    return path
