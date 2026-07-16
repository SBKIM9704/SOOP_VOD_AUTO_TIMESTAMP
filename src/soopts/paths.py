"""work/{vod_id} 디렉터리 레이아웃 단일 소스 (노래 전용)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkPaths:
    root: Path            # work/{vod_id}
    meta: Path            # meta.json
    chat: Path            # chat.jsonl (스티커 반응 계산용)
    raw_dir: Path         # raw/ (채팅 XML 네트워크 캐시)
    segmentation: Path    # audio_segmentation.json (값비싼 STT 세그먼트 캐시)
    songs_json: Path      # songs.json (감지·식별 결과)
    songs_txt: Path       # songs_{vod_id}.txt (복붙용)

    def ensure(self) -> WorkPaths:
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        return self


def work_paths(work_root: Path, vod_id: str) -> WorkPaths:
    root = Path(work_root) / str(vod_id)
    return WorkPaths(
        root=root,
        meta=root / "meta.json",
        chat=root / "chat.jsonl",
        raw_dir=root / "raw",
        segmentation=root / "audio_segmentation.json",
        songs_json=root / "songs.json",
        songs_txt=root / f"songs_{vod_id}.txt",
    )


def raw_chat_path(raw_dir: Path, part_idx: int, start_time: int) -> Path:
    return raw_dir / f"chat_p{part_idx}_{start_time}.xml"
