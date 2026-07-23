"""노래모음 유튜브 업로드 오케스트레이션 (하루 1건).

흐름: 완결된 VOD 하나 선택 → 노래 구간 합본 빌드 → **unlisted** 업로드 → DB 기록 → Slack.
한 방에 끝난다 — 올린 뒤 코드가 영상을 다시 손대는 경로는 없다(수정·삭제 API 미구현).
그래서 제목·설명·구간이 처음부터 맞아야 하고, 그게 "모든 곡이 검증된 VOD만 고른다"는
선택 조건의 이유다.

`batch.py`는 손대지 않는다 — 공용 헬퍼는 여기서 import만 한다(단방향). daily 파이프라인과
DB 컬럼도 겹치지 않아(`vods.youtube_*`는 `_vod_row`의 고정 키셋에 없다) 서로 간섭하지 않는다.

실패하면 재시도하지 않고 Slack + 비정상 종료로 끝낸다. `youtube_status`가 NULL로 남으므로
다음 날 실행이 같은 VOD를 자연히 다시 집는다 — 즉시 재시도 루프가 계정 정지를 부른 패턴이라
일부러 만들지 않았다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from soopts.batch import _notify_slack, _notify_slack_failure, vod_link
from soopts.config import Config
from soopts.log import get_logger
from soopts.output import fmt_hms
from soopts.paths import work_paths

log = get_logger("youtube")

# 유튜브 제한 — 초과분은 잘라서 보낸다(업로드 자체가 400으로 죽는 걸 막는다).
TITLE_MAX = 100
DESCRIPTION_MAX = 5000

# 설명 타임스탬프가 **진행바 챕터**가 되기 위한 유튜브 요건.
MIN_CHAPTERS = 3
MIN_CHAPTER_S = 10.0


# --------------------------------------------------------------------------- #
# 순수 함수 — 제목/설명/챕터
# --------------------------------------------------------------------------- #
def fmt_chapter_time(sec: float) -> str:
    """챕터 타임스탬프. 1시간 미만이면 `MM:SS`, 넘으면 `HH:MM:SS`(자리수 고정).

    첫 줄은 반드시 `00:00`이어야 유튜브가 챕터로 인식한다. 분·시를 zero-pad해 목록 폭을
    맞춘다 — `0:00`/`1:02:03`도 인식되지만, 자리수가 들쭉날쭉하면 읽기 나쁘고 `00:00`
    표기가 더 확실히 인식된다는 관찰도 있어 4자리/6자리로 통일한다.
    """
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapters_valid(placements: list[Any], total_s: float) -> bool:
    """설명의 타임스탬프 목록이 유튜브 **챕터**로 승격되는지(순수 함수).

    요건: 첫 타임스탬프가 0 / 3개 이상 / 각 구간 10초 이상. 못 미치면 챕터 마커만 안 생기고
    설명 속 타임스탬프 클릭 이동은 그대로 동작하므로, 실패해도 설명을 바꾸지 않는다.
    """
    if len(placements) < MIN_CHAPTERS or placements[0].offset_s != 0:
        return False
    bounds = [p.offset_s for p in placements] + [total_s]
    return all(b - a >= MIN_CHAPTER_S for a, b in zip(bounds, bounds[1:], strict=False))


def format_youtube_title(cfg: Config, date: str, placements: list[Any]) -> str:
    """영상 제목(순수 함수).

    1곡짜리 VOD가 전체의 1/4이라 "노래 모음 (1곡)"이 자주 나온다 — 그 경우엔 곡명을 그대로
    제목에 써서 어색함과 검색 손해를 피한다.
    """
    if len(placements) == 1:
        p = placements[0]
        title = cfg.youtube.title_template_single.format(
            date=date, title=p.title, artist=p.artist, n=1
        )
    else:
        title = cfg.youtube.title_template.format(date=date, n=len(placements))
    return title[:TITLE_MAX]


def format_youtube_description(
    cfg: Config, vod: dict[str, Any], placements: list[Any]
) -> str:
    """영상 설명 — 방송 출처 한 줄 + 챕터 타임라인(순수 함수).

    챕터 블록은 `타임스탬프 제목 - 아티스트` **한 줄만** 담는다. 같은 줄에 링크를 붙이면
    그게 통째로 챕터 이름이 되어 진행바에 URL이 박힌다.

    곡마다 원본 딥링크를 나열하던 섹션은 뺐다 — 챕터 목록과 곡명이 그대로 중복되는 데다
    (14곡이면 같은 곡명이 28줄), 긴 URL이 줄줄이 붙어 링크 도배처럼 보였다. 출처는 헤더의
    다시보기 링크 하나로 충분하고, 곡별 원본 시각이 필요한 쪽(검수 UI)은 DB의 `song_link`를
    쓰므로 설명에 없어도 잃는 정보가 없다.
    """
    title_no = str(vod["soop_title_no"])
    date = vod.get("broadcast_date") or ""
    head = [
        vod.get("title") or f"VOD {title_no}",
        f"{date} SOOP 방송 다시보기 ▸ {vod_link(cfg, title_no)}".strip(),
        "",
    ]
    chapters = [f"{fmt_chapter_time(p.offset_s)} {p.label}" for p in placements]
    return "\n".join([*head, *chapters])[:DESCRIPTION_MAX]


def format_upload_notice(
    cfg: Config, vod: dict[str, Any], url: str, title: str, placements: list[Any],
    dropped: list[dict[str, Any]],
) -> str:
    """업로드 완료 Slack 메시지 — 사람이 눈으로 확인할 수 있게 링크와 규모를 담는다.

    코드가 영상을 다시 손댈 수단이 없으므로(수정·삭제 API 미구현) 이 알림이 사실상 유일한
    사후 확인 창구다. 문제가 있으면 스튜디오에서 직접 처리한다.
    """
    title_no = str(vod["soop_title_no"])
    lines = [
        f"📼 업로드 완료({cfg.youtube.privacy}) — {title}",
        f"    {url}",
        f"    원본 VOD {title_no}: {vod_link(cfg, title_no)}",
        f"    {len(placements)}곡 / {fmt_hms(int(placements[-1].offset_s + placements[-1].duration_s))}",
    ]
    if dropped:
        lines.append(f"    ⚠️ 제외된 곡 {len(dropped)}개(상한 또는 구간 문제)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 오케스트레이션
# --------------------------------------------------------------------------- #
def _pick_target(
    title_no: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | tuple[None, None]:
    """업로드 대상 VOD와 그 곡 목록. title_no를 주면 그 VOD로 고정하되 자격 조건은 똑같이 본다.

    곡 목록을 함께 돌려주는 건 호출부가 같은 조회를 한 번 더 하지 않게 하기 위해서다.
    """
    from soopts import db

    if title_no:
        vod = db.fetch_vod_by_title_no(title_no)
        if not vod:
            raise RuntimeError(f"vods에 없는 VOD입니다: {title_no}")
        perfs = db.fetch_performances_for_vods([vod["id"]]).get(vod["id"], [])
        reason = db.youtube_block_reason(vod, perfs)
        if reason:
            raise RuntimeError(f"VOD {title_no}는 업로드 대상이 아닙니다 — {reason}")
        return vod, perfs

    candidates = db.fetch_youtube_candidates()
    perfs_by_vod = db.fetch_performances_for_vods([v["id"] for v in candidates])
    vod = db.select_youtube_target(candidates, perfs_by_vod)
    if not vod:
        return None, None
    return vod, perfs_by_vod[vod["id"]]


def run_youtube_upload(
    cfg: Config, *, title_no: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """일일 1건 업로드. 반환 dict는 CLI/테스트가 결과를 확인하는 용도다."""
    from soopts import db
    from soopts.collector.meta import fetch_meta
    from soopts.export import video

    vod, perfs = _pick_target(title_no)
    if not vod:
        log.info("업로드할 VOD가 없습니다 (모든 곡이 검증 완료된 미업로드 VOD 없음)")
        return {"status": "no_target"}

    tno = str(vod["soop_title_no"])
    log.info("대상 VOD %s (%s) — %d곡", tno, vod.get("broadcast_date"), len(perfs))

    work = work_paths(cfg.work_root, tno).ensure()
    out_dir = work.root / "ytbuild"
    try:
        meta = fetch_meta(cfg, tno, work)
        out, placements, dropped = video.build_vod_video(cfg, tno, perfs, meta, out_dir)
        if not placements or out is None:
            # 종결 마커를 안 찍으면 이 VOD가 매일 다시 뽑혀 큐가 영영 막힌다.
            db.mark_youtube_no_songs(tno)
            _notify_slack(f"⚠️ VOD {tno}: 합본에 넣을 수 있는 곡이 없어 'no_songs'로 종결")
            return {"status": "no_songs", "title_no": tno}

        total_s = placements[-1].offset_s + placements[-1].duration_s
        title = format_youtube_title(cfg, vod.get("broadcast_date") or "", placements)
        description = format_youtube_description(cfg, vod, placements)
        if not chapters_valid(placements, total_s):
            log.info("챕터 요건 미충족(%d곡) — 타임스탬프 클릭 이동만 동작합니다", len(placements))

        if dry_run:
            log.info("dry-run — 업로드하지 않습니다")
            print(f"[출력] {out}  ({fmt_hms(int(total_s))})")
            print(f"[제목] {title}")
            print(f"[설명]\n{description}")
            return {"status": "dry_run", "title_no": tno, "path": str(out),
                    "title": title, "description": description, "songs": len(placements)}

        from soopts.export import youtube

        url = youtube.upload_video(cfg, out, title, description)
        try:
            db.mark_youtube_uploaded(tno, url)
            db.set_performance_youtube_urls(
                [(p.perf_id, f"{url}?t={int(p.offset_s)}") for p in placements]
            )
        except Exception:
            # 영상은 이미 올라갔는데 큐 마커가 안 찍힌 상태다. 이대로 두면 내일 실행이 같은
            # VOD를 다시 골라 **중복 영상**을 올리고, 삭제 API가 없어 되돌릴 수 없다.
            # 사람이 손으로 채울 수 있게 URL을 알림에 크게 남긴다.
            _notify_slack(
                f"🚨 업로드는 됐는데 DB 기록에 실패 — VOD {tno}\n    {url}\n"
                f"    지금 수동으로 vods.youtube_url + youtube_status='uploaded'를 채워주세요. "
                f"안 그러면 다음 실행이 같은 영상을 또 올립니다(삭제 불가)."
            )
            raise
        _notify_slack(format_upload_notice(cfg, vod, url, title, placements, dropped))
        log.info("완료: %s (%d곡)", url, len(placements))
        return {"status": "uploaded", "title_no": tno, "url": url, "songs": len(placements)}
    except Exception as e:  # noqa: BLE001 — 재시도하지 않고 알린 뒤 죽는다
        _notify_slack_failure(f"youtube 업로드 (VOD {tno})", e)
        raise
    finally:
        # dry-run 산출물은 남긴다 — 사람이 눈으로 확인하려고 만든 것이라 지우면 의미가 없다.
        if not dry_run:
            _cleanup(out_dir)


def _cleanup(out_dir: Path) -> None:
    """빌드 산출물 정리. 러너 디스크는 다음 스텝에서도 쓰이므로 수 GB를 남기지 않는다."""
    import shutil

    shutil.rmtree(out_dir, ignore_errors=True)


def main_upload(cfg: Config, *, title_no: str | None, dry_run: bool) -> int:
    """CLI 진입점 — 실패는 exit 1로 워크플로에 그대로 드러낸다."""
    try:
        result = run_youtube_upload(cfg, title_no=title_no, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        log.error("업로드 실패: %s", e)
        return 1
    if result["status"] == "no_target":
        print("업로드 대상 VOD 없음")
    return 0


