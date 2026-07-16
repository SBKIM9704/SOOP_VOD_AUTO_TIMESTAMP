"""soopts CLI — SOOP VOD 노래 하이라이트 추출·식별.

파이프라인: collect(채팅→스티커) → songs(오디오 음악감지 + 스티커 + STT 전사).
각 단계는 work/{vod_id}/ 캐시를 쓰고 --force로 재계산한다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from soopts.config import Config, load_config
from soopts.log import get_logger, setup_logging
from soopts.paths import WorkPaths, work_paths

log = get_logger("cli")

_VOD_ID_RE = re.compile(r"(\d{5,})")


def extract_vod_id(url_or_id: str) -> str:
    s = url_or_id.strip()
    if s.isdigit():
        return s
    m = _VOD_ID_RE.search(s)
    if not m:
        raise ValueError(f"VOD 번호를 찾을 수 없습니다: {url_or_id!r}")
    return m.group(1)


def _ctx(args) -> tuple[Config, str, WorkPaths]:
    cfg = load_config(
        Path(args.config) if args.config else None,
        work_root=Path(args.work_root) if args.work_root else None,
    )
    vod_id = extract_vod_id(args.vod)
    work = work_paths(cfg.work_root, vod_id).ensure()
    return cfg, vod_id, work


# --------------------------------------------------------------------------- #
def _do_collect(cfg: Config, vod_id: str, work: WorkPaths, *, force: bool, reparse: bool):
    from soopts.collector.chat import fetch_chat
    from soopts.collector.meta import fetch_meta

    meta = fetch_meta(cfg, vod_id, work, force=force)
    fetch_chat(cfg, vod_id, meta, work, force=force, reparse=reparse)
    return meta


def cmd_collect(args) -> int:
    cfg, vod_id, work = _ctx(args)
    _do_collect(cfg, vod_id, work, force=args.force, reparse=args.reparse)
    return 0


def cmd_clips(args) -> int:
    """BJ가 부른 노래만 1080p 클립으로 추출(정밀 경계) → (옵션) 유튜브 unlisted 업로드."""
    cfg, vod_id, work = _ctx(args)
    from soopts.analyzers.audio_analyzer import sticker_burst_regions
    from soopts.collector.media import download_slice, resolve_m3u8_list
    from soopts.export.clips import make_clip
    from soopts.models import read_chat_jsonl, read_meta
    from soopts.output import fmt_hms

    if not work.chat.exists():
        raise RuntimeError("chat.jsonl 없음 — 먼저 `soopts collect` 로 채팅(스티커)을 수집하세요")
    meta = read_meta(work.meta) if work.meta.exists() else None
    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    a = cfg.audio
    regions = sticker_burst_regions(
        stickers, bucket_s=a.sticker_bucket_s, window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window, pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s, skip_opening_s=a.skip_opening_s,
        total_s=meta.total_duration if meta else None,
    )
    log.info("노래 후보 %d구간 → 1080p 클립 추출(%s)", len(regions), cfg.clip.quality)

    m3u8s = resolve_m3u8_list(args.vod, cfg.clip.quality)
    parts = meta.parts if meta else []
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for s, e in regions:
        ds = max(0.0, s - cfg.clip.dl_pad_before_s)
        de = e + cfg.clip.dl_pad_after_s
        m3u8, ls, le = _map_to_part(ds, de, parts, m3u8s)
        if m3u8 is None:
            continue
        raw = work.clips_dir / f"region_{int(ds)}.mp4"
        if not raw.exists() or args.force:
            download_slice(m3u8, ls, le, raw)
        clip = make_clip(cfg, vod_id, str(raw), ds, de, work.clips_dir, media_offset=ds)
        if clip:
            clips.append(clip)

    if not clips:
        print("추출된 노래 클립이 없습니다.")
        return 0

    # 정밀 컷된 클립(노래만)이라 전사가 깨끗함 → 가사 채워 곡 식별/설명에 사용
    if not args.no_stt:
        from soopts.analyzers.stt import _load_model, _transcribe_best
        model = _load_model(cfg)
        for c in clips:
            text, _lang = _transcribe_best(model, c.path, cfg)
            c.lyrics = text[: cfg.stt.lyric_chars]

    # 정리 출력
    print(f"\n🎬 노래 클립 {len(clips)}개 추출:")
    for c in clips:
        print(f"  [{fmt_hms(c.t)}~{fmt_hms(c.end)}] {c.duration}초  {Path(c.path).name}")

    if args.upload:
        _upload_clips(cfg, vod_id, meta, clips)
    else:
        print(f"\n클립 위치: {work.clips_dir}")
        print("업로드하려면 --upload 를 붙이세요 (최초 1회 OAuth 동의 필요).")
    return 0


def _upload_clips(cfg: Config, vod_id: str, meta, clips) -> None:
    from soopts.export.youtube import upload_unlisted
    from soopts.output import fmt_hms

    bj = meta.bj_nick if meta else ""
    vod_url = cfg.endpoints.vod_web_url.replace("{title_no}", str(vod_id))
    print("\n📤 유튜브 업로드(unlisted) 시작...")
    for c in clips:
        title = cfg.youtube.title_template.format(
            bj=bj, title=(c.title or "곡명 미상"), vod_id=vod_id, hms=fmt_hms(c.t)
        )
        desc = f"{bj} 다시보기 노래 구간\n원본: {vod_url.replace('{sec}', str(c.t))}"
        if c.lyrics:
            desc += f"\n\n가사(자동전사):\n{c.lyrics}"
        try:
            url = upload_unlisted(cfg, c.path, title, desc)
            c.title = title
            print(f"  ✅ {fmt_hms(c.t)} → {url}")
        except Exception as ex:  # noqa: BLE001
            print(f"  ❌ {fmt_hms(c.t)} 업로드 실패: {ex}")


def cmd_fetch(args) -> int:
    cfg, vod_id, work = _ctx(args)
    from soopts.collector.media import download_audio_full

    download_audio_full(args.vod, work.root / "audio.mp3", args.quality)
    print(f"오디오 저장: {work.root / 'audio.mp3'}")
    return 0


def cmd_songs(args) -> int:
    """노래 감지 → STT 전사·판별 → 타임라인 출력."""
    cfg, vod_id, work = _ctx(args)
    from soopts.models import read_meta, write_songs
    from soopts.output import render_songs, write_songs_txt

    meta = read_meta(work.meta) if work.meta.exists() else None
    songs = _songs_slice(cfg, vod_id, work, meta, args) if args.slice \
        else _songs_full(cfg, vod_id, work, args)

    write_songs(work.songs_json, songs, vod_id)
    write_songs_txt(work.songs_txt, meta, songs)
    print(render_songs(meta, songs))
    print(f"저장: {work.songs_txt}")
    if any(s.lyrics and not s.title for s in songs):
        print("\n※ 가사가 있는 곡은 Claude/사람이 가사로 곡명을 채우면 됩니다.")
    return 0


def _songs_full(cfg: Config, vod_id: str, work: WorkPaths, args):
    """전체 오디오 모드: inaSpeechSegmenter 음악감지 + 스티커 + STT."""
    from soopts.analyzers.audio_analyzer import detect_songs

    songs = detect_songs(cfg, vod_id, work, audio_path=args.audio, force=args.force)
    if songs and not args.no_stt:
        if not args.audio:
            log.warning("STT 전사 생략 — --audio 로 오디오/영상 파일을 지정하세요")
        else:
            from soopts.analyzers.stt import transcribe_songs

            songs = transcribe_songs(cfg, songs, args.audio)
    return songs


def _songs_slice(cfg: Config, vod_id: str, work: WorkPaths, meta, args):
    """효율 모드: 스티커로 노래 후보 위치 → 그 구간만 슬라이스 다운로드 → STT.

    전체 영상(수백 MB)을 받지 않고 후보 구간(구간당 수 MB)만 받는다. 스티커 적은
    조용한 곡은 놓칠 수 있다(재현율↓, 다운로드↓ 트레이드오프).
    """
    from soopts.analyzers.audio_analyzer import sticker_burst_regions
    from soopts.analyzers.stt import transcribe_slices
    from soopts.collector.media import download_slice, resolve_m3u8_list
    from soopts.models import Song, read_chat_jsonl

    if not work.chat.exists():
        raise RuntimeError("chat.jsonl 없음 — 먼저 `soopts collect` 로 채팅(스티커)을 수집하세요")
    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    total = meta.total_duration if meta else None
    a = cfg.audio
    regions = sticker_burst_regions(
        stickers,
        bucket_s=a.sticker_bucket_s,
        window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window,
        pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s,
        skip_opening_s=a.skip_opening_s,
        total_s=total,
    )
    log.info("스티커 기반 노래 후보 %d구간 (전체 다운로드 없이)", len(regions))
    if not regions:
        return []

    m3u8s = resolve_m3u8_list(args.vod)
    parts = meta.parts if meta else []
    if len(m3u8s) != len(parts) and parts:
        log.warning("m3u8 파트 수(%d) != 메타 파트 수(%d) — 매핑 부정확 가능", len(m3u8s), len(parts))
    slice_dir = work.root / "slices"
    slice_dir.mkdir(exist_ok=True)

    pairs = []
    for s, e in regions:
        m3u8, ls, le = _map_to_part(s, e, parts, m3u8s)
        if m3u8 is None:
            log.warning("구간 %d-%d 파트 매핑 실패 — 건너뜀", int(s), int(e))
            continue
        path = slice_dir / f"slice_{int(s)}_{int(e)}.mp4"
        if not path.exists() or args.force:
            download_slice(m3u8, ls, le, path)
        rate = sum(1 for t in stickers if s <= t <= e) / max((e - s) / 60.0, 1e-6)
        song = Song(t=int(s), end=int(e), duration=int(e - s),
                    sticker_rate=round(rate, 1), song_likely=rate >= cfg.audio.sticker_rate_strong)
        pairs.append((song, str(path)))

    if args.no_stt:
        return [s for s, _ in pairs]
    return transcribe_slices(cfg, pairs)


def _map_to_part(s: float, e: float, parts, m3u8s):
    """전역 시각 구간 (s,e)를 해당 파트의 m3u8 + 파트-로컬 시각으로 매핑.

    단일 파트(메타 없음)면 그대로 첫 m3u8 사용. 파트 경계를 넘으면 시작 파트 안으로 클램프.
    """
    if not parts:
        return m3u8s[0], s, e
    for p in parts:
        if p.offset_s <= s < p.offset_s + p.duration:
            if p.idx >= len(m3u8s):
                return None, 0, 0
            ls = s - p.offset_s
            le = min(e - p.offset_s, float(p.duration))  # 파트 끝으로 클램프
            return m3u8s[p.idx], ls, le
    return None, 0, 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="soopts", description="SOOP VOD 노래 하이라이트 추출·식별")
    p.add_argument("--config", help="soopts.toml 경로 (기본: CWD의 soopts.toml)")
    p.add_argument("--work-root", help="작업 디렉터리 루트 (기본: ./work)")
    p.add_argument("--force", action="store_true", help="캐시 무시하고 재계산")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")

    sub = p.add_subparsers(dest="cmd", required=True)

    def add_vod(sp):
        sp.add_argument("vod", help="VOD URL 또는 번호")

    sp = sub.add_parser("collect", help="메타 + 채팅(스티커) 수집")
    add_vod(sp)
    sp.add_argument("--reparse", action="store_true", help="raw XML에서 chat.jsonl만 재생성(네트워크 없음)")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("clips", help="BJ 부른 노래만 1080p 클립 추출 + (옵션)유튜브 업로드")
    add_vod(sp)
    sp.add_argument("--upload", action="store_true", help="유튜브 unlisted 자동 업로드까지 수행")
    sp.add_argument("--no-stt", action="store_true", help="클립 가사 전사 생략")
    sp.set_defaults(func=cmd_clips)

    sp = sub.add_parser("fetch", help="yt-dlp로 전체 오디오 다운로드")
    add_vod(sp)
    sp.add_argument("--quality", default="hls-hd", help="yt-dlp 포맷(기본 hls-hd)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("songs", help="노래 감지 + STT 전사·판별 + 타임라인")
    add_vod(sp)
    sp.add_argument("--audio", help="[전체모드] 오디오/영상 파일 경로 (노래감지·STT용)")
    sp.add_argument("--slice", action="store_true",
                    help="[효율모드] 스티커로 찾은 노래 후보 구간만 슬라이스 다운로드해 처리(전체 DL 없음)")
    sp.add_argument("--no-stt", action="store_true", help="STT 전사 생략(감지만)")
    sp.set_defaults(func=cmd_songs)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001
        if args.verbose:
            raise
        log.error("%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
