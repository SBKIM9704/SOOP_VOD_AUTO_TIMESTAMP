"""`soopts daily` 오케스트레이션 — GitHub Actions 무인 배치.

스테이션 최신 VOD 중 미처리분 → 댓글 타임라인(🎤/🎵)을 마커로 파싱 → 곡별 span(시작=시각,
끝=다음 곡 6분 캡) → 제목/가수로 카탈로그 매칭 → DB(performances)에 노래 구간 기록.
다운로드·세그멘테이션·STT 없이 수 초에 끝난다(초경량). 댓글 타임라인이 없는 VOD는
'manual'로 표시만 하고, 사람이 로컬에서 analyze_vod.py 전체 전사 후 `soopts ingest`로 처리한다.

산출물은 **타임스탬프**다. 시청은 SOOP 원본 딥링크(`song_link()`)로 연결하며,
이 저장소는 영상을 어디에도 업로드하지 않는다.

상태의 진실은 파일이 아니라 DB(vods.status + performances.identify_status)다.
러너가 초기화되어 로컬 캐시가 사라져도 DB만 보면 어디까지 처리됐는지 알 수 있다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from soopts.config import Config
from soopts.log import get_logger
from soopts.paths import work_paths

log = get_logger("batch")

# 런 1회 동안 실시간 Slack 알림을 몇 건까지 즉시 보낼지 — 이후는 카운트만 하고 최종
# 상세 리포트의 각주로만 남긴다(한 배치가 통째로 무너져도 메시지가 폭주하지 않도록).
REALTIME_ALERT_LIMIT = 3


# --------------------------------------------------------------------------- #
# 순수 함수 (테스트 대상)
# --------------------------------------------------------------------------- #
@dataclass
class TimelineEvent:
    """크로놀로지컬 상세 리포트 한 줄 — 감지/구간/업로드 단계에서 시간순으로 쌓인다."""

    kind: str  # "detection" | "region"
    title_no: str | None = None
    label: str | None = None
    detail: str | None = None
    duration_s: float | None = None
    ok: bool | None = None  # True=성공, False=실패, None=건너뜀(노래 아님 등)
    count: int | None = None
    clip_duration_s: float | None = None
    stt_duration_s: float | None = None


@dataclass
class RunContext:
    """daily 1회 실행 동안 공유되는 상태 — 실시간 알림 예산 + 크로놀로지컬 이벤트 로그.

    alert_limit 이후의 실패는 Slack을 스팸하지 않도록 억제하고 개수만 세어, 최종 상세
    리포트의 각주로만 남는다. VOD 단위가 아니라 런 전체 단위로 세는 이유: "한 VOD의
    구간이 전부 실패"도, "VOD 여러 개가 조금씩 실패"도 결국 사람에게는 같은 "이 배치가
    맛이 갔다"는 신호라, 굳이 VOD별로 예산을 나눌 이유가 없다.
    """

    events: list[TimelineEvent] = field(default_factory=list)
    alert_limit: int = REALTIME_ALERT_LIMIT
    alert_sent: int = 0
    alert_suppressed: int = 0

    def record(self, event: TimelineEvent) -> None:
        self.events.append(event)

    def alert(self, text: str) -> None:
        if self.alert_sent < self.alert_limit:
            _notify_slack(text)
            self.alert_sent += 1
        else:
            self.alert_suppressed += 1


def fmt_duration_s(seconds: float) -> str:
    """초 → "5초"/"1분 5초"/"1시간 1분 1초". 순수 함수."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}시간")
    if m:
        parts.append(f"{m}분")
    if s or not parts:
        parts.append(f"{s}초")
    return " ".join(parts)


def cooldown_cutoff(days: int, now: datetime | None = None) -> str | None:
    """쿨다운 경계 날짜 'YYYY-MM-DD'(이 날짜까지 처리 대상). days<=0이면 None(쿨다운 없음).

    기준은 **KST**다 — broadcast_date는 SOOP reg_date(KST)의 날짜 부분인데 러너는 UTC로
    돌아서, UTC 날짜로 재면 04시(KST) 런에서 하루씩 어긋난다.
    """
    if days <= 0:
        return None
    kst_now = (now or datetime.now(UTC)).astimezone(timezone(timedelta(hours=9)))
    return (kst_now.date() - timedelta(days=days)).isoformat()


def next_vod_status(detected: int) -> str:
    """VOD 처리 직후 상태. 감지된 노래가 없으면 검수·업로드할 게 없으니 바로 종결(done),
    있으면 업로드 큐 소진까지 거쳐야 하니 analyzed로 남긴다."""
    return "done" if detected == 0 else "analyzed"


def quality_warning(cfg: Config, stats: dict[str, Any]) -> str | None:
    """품질 지표가 임계값 미달이면 사유를, 정상이면 None을 반환한다.

    이 게이트가 없어서 STT가 몇 달간 전량 실패(413)하는 동안에도 실행은 계속 "성공"으로
    끝났다. 곡은 가사 없이 needs_review로 쌓였고, 요약에는 감지 곡 수만 찍혀 정상처럼
    보였다. 예외가 아니라 **품질 저하**로 나타나는 장애를 잡으려면 수치를 직접 봐야 한다.
    """
    attempted = stats.get("stt_attempted", 0)
    if attempted == 0:
        return None
    ok = stats.get("stt_ok", 0)
    rate = ok / attempted
    if rate < cfg.stt.min_success_rate:
        return (
            f"STT 성공률 {rate:.0%} (가사 확보 {ok}/{attempted}곡)가 임계값 "
            f"{cfg.stt.min_success_rate:.0%} 미만 — 전사가 대량 실패하는 장애일 수 있다"
        )
    return None


def format_summary(stats: dict[str, int]) -> str:
    """일일 배치 결과 한 줄 요약. Slack/로그 공용."""
    parts = [
        f"VOD {stats.get('vods', 0)}건",
        f"감지 {stats.get('detected', 0)}곡",
        f"자동매칭 {stats.get('auto_matched', 0)}",
        f"검수대기 {stats.get('needs_review', 0)}",
    ]
    attempted = stats.get("stt_attempted", 0)
    if attempted:
        ok = stats.get("stt_ok", 0)
        parts.append(f"가사확보 {ok}/{attempted}({ok / attempted:.0%})")
    # 자동매칭이 가사 덕분인지 댓글 타임라인 덕분인지 구분한다 — 예전엔 STT가 죽어도
    # 타임라인 힌트만으로 매칭이 되어 성공률이 정상처럼 보였다.
    if stats.get("hint_available") or stats.get("lyrics_only"):
        parts.append(
            f"근거 타임라인 {stats.get('hint_available', 0)}/가사 {stats.get('lyrics_only', 0)}"
        )
    if "deleted" in stats:
        parts.append(f"삭제 {stats['deleted']}건")
    return " / ".join(parts)


def vod_link(cfg: Config, title_no: str) -> str:
    """VOD 웹 플레이어 링크 (Slack 하이퍼링크용, 특정 시각 지정 없음)."""
    return cfg.endpoints.vod_web_url.replace("{title_no}", str(title_no)).replace(
        "?change_second={sec}", ""
    )


def song_link(cfg: Config, title_no: str, start_s: float) -> str:
    """노래 시작 시각으로 바로 이동하는 SOOP 딥링크.

    유튜브 업로드를 대체하는 시청 경로다. 영상을 복제해 어딘가에 올리는 대신 원본
    VOD의 해당 시각을 가리키므로 저장 비용이 없고, 저작권도 원 방송 플랫폼 안에
    머문다. title_no와 start_s만으로 계산되니 DB에 따로 저장할 것도 없다.
    """
    return cfg.endpoints.vod_web_url.replace("{title_no}", str(title_no)).replace(
        "{sec}", str(int(start_s))
    )


def format_vod_result(cfg: Config, title_no: str, stats: dict[str, Any]) -> str:
    """VOD 하나 처리 결과 — Slack에 개별 발송할 상세 메시지."""
    link = vod_link(cfg, title_no)
    if stats.get("error"):
        return f"❌ VOD {title_no} 처리 실패: {link}\n    {stats['error']}"

    lines = [f"✅ VOD {title_no} 처리 완료: {link}"]
    lines.append(
        f"    감지 {stats.get('detected', 0)}곡 "
        f"(자동매칭 {stats.get('auto_matched', 0)}, 검수대기 {stats.get('needs_review', 0)})"
    )
    return "\n".join(lines)


def _resolved_title_artist(perf: dict[str, Any]) -> tuple[str, str]:
    """확정 곡(songs join)이 있으면 그 title/artist를, 없으면 title_guess/미상으로 폴백."""
    song = perf.get("songs") or {}
    title = song.get("title") or perf.get("title_guess") or "곡명 미상"
    artist = song.get("artist") or "아티스트 미상"
    return title, artist


def format_detailed_summary(cfg: Config, ctx: RunContext, stats: dict[str, Any]) -> str:
    """일일 배치 전체의 크로놀로지컬 상세 리포트 — Slack 최종 메시지/narrate_with_llm 입력.

    format_summary(stats)의 한 줄 요약을 그대로 첫 줄에 포함해(기존 소비자/포맷 호환),
    그 아래에 VOD별 감지 방식·구간별 성공/실패/소요시간·업로드 단계 요약을 시간순으로
    덧붙인다. 모든 수치는 여기서 결정론적으로 계산되고, narrate_with_llm은 이 텍스트의
    표현만 다듬을 뿐 숫자를 새로 만들거나 바꾸지 않는다.
    """
    lines = [format_summary(stats)]
    by_vod: dict[str, list[TimelineEvent]] = {}
    for ev in ctx.events:
        by_vod.setdefault(ev.title_no, []).append(ev)

    for title_no, events in by_vod.items():
        lines.append(f"\n▶ VOD {title_no} ({vod_link(cfg, title_no)})")
        for ev in events:
            if ev.kind == "detection":
                lines.append(f"    감지 방식: {ev.detail} ({fmt_duration_s(ev.duration_s or 0)})")

    if ctx.alert_suppressed:
        lines.append(
            f"\n※ 실시간 실패 알림은 {ctx.alert_limit}건까지만 즉시 발송되며, "
            f"추가 {ctx.alert_suppressed}건은 이 요약에만 반영됨."
        )
    if stats.get("manual_skipped"):
        lines.append(
            f"\n※ 댓글 타임라인이 없어 서버 처리를 건너뛰고 'manual'로 표시한 VOD: "
            f"{stats['manual_skipped']}건 (로컬에서 `soopts ingest`로 처리)"
        )
    return "\n".join(lines)


_STANDALONE_NUMBER_RE = re.compile(r"(?<!\d)\d+(?!\d)")
_HMS_RE = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")


def _narration_preserves_numbers(deterministic_text: str, narrated_text: str) -> bool:
    """Groq가 다듬은 문장이 원문의 숫자를 하나도 빠뜨리지 않았는지 확인한다.

    HH:MM:SS 시각 라벨은 "50분 39초"처럼 표현이 자연스럽게 바뀔 수 있어 검증에서
    제외한다 — 그 외 독립된 숫자(곡 수, 소요시간 등)는 원문에 있던 게 응답에도 그대로
    있어야 한다(부분집합 관계).
    """

    def numbers(t: str) -> set[str]:
        return set(_STANDALONE_NUMBER_RE.findall(_HMS_RE.sub(" ", t)))

    return numbers(deterministic_text) <= numbers(narrated_text)


_NARRATE_PROMPT = (
    "다음은 이미 확정된 배치 처리 결과 데이터다(사람이 만든 수치·사실, 오류 없음). "
    "이 안의 사실과 숫자를 하나도 바꾸거나 빠뜨리지 말고, 자연스럽게 읽히는 한국어 "
    "문장으로 다시 정리해서 써라. 새로운 사실이나 추측을 추가하지 마라. 마크다운 기호 "
    "없이 일반 텍스트로만 답하라.\n\n"
)


def narrate_with_llm(deterministic_text: str, *, api_key: str | None = None) -> str:
    """결정론적 리포트를 Groq로 다듬는다 — 실패하거나 숫자가 어긋나면 원문 그대로 반환한다.

    analyzers/identify.py의 disambiguate_with_llm과 동일한 "실패해도 안전" 철학:
    알림 채널의 신뢰성이 산문 다듬기보다 우선한다.
    """
    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            max_tokens=1500,
            reasoning_effort="low",
            messages=[{"role": "user", "content": _NARRATE_PROMPT + deterministic_text}],
        )
        narrated = (resp.choices[0].message.content or "").strip()
        if narrated and _narration_preserves_numbers(deterministic_text, narrated):
            return narrated
        log.warning("narrate_with_llm 출력 검증 실패 — 원문으로 폴백")
    except Exception as ex:  # noqa: BLE001
        log.warning("narrate_with_llm 실패 — 원문으로 폴백: %s", ex)
    return deterministic_text


# --------------------------------------------------------------------------- #
# daily
# --------------------------------------------------------------------------- #
def _select_vods(cfg: Config, bj_id: str, count: int) -> list[dict[str, Any]]:
    """처리할 VOD를 우선순위 재시도 > 신규 > 백필로 최대 count개 고른다.

    재시도는 DB에서 바로 뽑고, 나머지는 SOOP 목록을 최신순으로 페이지를 넘겨 가며 채운다.
    처리 완료분을 건너뛰므로 신규가 없으면 자연히 과거로 내려가 백필하고, 목록 마지막
    페이지(SOOP 만료의 자연 바닥)에 닿으면 멈춘다. 과거가 없거나 최신만 남으면 그대로
    수렴한다 — 별도 분기가 필요 없다.

    쿨다운(`station.min_vod_age_days`) 안의 최신 VOD는 후보에서 빠진다. 슬롯이 비지는
    않는다 — 순회가 그만큼 과거로 더 내려가 백필로 채우고, 백필까지 마르면 그냥 덜 처리하고
    끝난다(VOD가 창 밖으로 나오면 다음 런이 잡는다).
    """
    from soopts import db
    from soopts.collector.vod_list import iter_vod_pages

    if count <= 0:
        return []
    cutoff = cooldown_cutoff(cfg.station.min_vod_age_days)
    retryable = db.fetch_retryable(count)
    candidates: list[dict[str, Any]] = []
    existing_by_no: dict[str, dict[str, Any]] = {}
    targets = db.select_targets(retryable, candidates, existing_by_no, count, cutoff)

    if len(targets) < count:
        for page in iter_vod_pages(cfg, bj_id):
            candidates.extend(page)
            existing_by_no.update(db.fetch_existing([str(c["title_no"]) for c in page]))
            targets = db.select_targets(retryable, candidates, existing_by_no, count, cutoff)
            if len(targets) >= count:
                break
    return db.upsert_pending(targets)


def run_daily(cfg: Config, *, bj_id: str, count: int) -> dict[str, Any]:
    from soopts import db

    try:
        picked = _select_vods(cfg, bj_id, count)
    except Exception as e:  # noqa: BLE001
        _notify_slack_failure("VOD 목록 조회", e)
        raise

    ctx = RunContext()
    stats: dict[str, Any] = {
        "vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0,
        "stt_attempted": 0, "stt_ok": 0, "hint_available": 0, "lyrics_only": 0,
    }
    for vod_row in picked:
        title_no = vod_row["soop_title_no"]
        try:
            vod_stats = _process_vod(cfg, vod_row, bj_id, ctx)
            if vod_stats.get("no_timeline"):
                # 🎤 곡이 0인 VOD는 서버에서 처리하지 않는다 — 'manual'로 표시(사유 메모 포함)해
                # 재시도 큐에서 빼고, 사람이 로컬 analyze_vod.py로 처리한다. 메모(note)는 vods.error에
                # 남아 `soopts vods --status manual`에서 로컬 처리 우선순위 힌트가 된다.
                db.mark_vod(title_no, "manual", error=vod_stats.get("note"))
                stats["manual_skipped"] = stats.get("manual_skipped", 0) + 1
                stats["vods"] += 1
                continue
            stats["detected"] += vod_stats["detected"]
            stats["auto_matched"] += vod_stats["auto_matched"]
            stats["needs_review"] += vod_stats["needs_review"]
            for k in ("stt_attempted", "stt_ok", "hint_available", "lyrics_only"):
                stats[k] += vod_stats.get(k, 0)
            db.mark_vod(title_no, next_vod_status(vod_stats["detected"]))
            _notify_slack(format_vod_result(cfg, title_no, vod_stats))
        except Exception as e:  # noqa: BLE001
            log.error("VOD %s 처리 실패: %s", title_no, e)
            db.mark_vod(title_no, "failed", error=str(e)[:500])
            _notify_slack(format_vod_result(cfg, title_no, {"error": str(e)[:500]}))
        stats["vods"] += 1

    deterministic = format_detailed_summary(cfg, ctx, stats)
    log.info("daily 완료: %s", deterministic)
    _notify_slack(narrate_with_llm(deterministic))

    # 요약을 먼저 보낸 뒤에 판정한다 — 여기서 죽더라도 무엇이 처리됐는지는 남아야 한다.
    # DB 기록도 이미 끝났으므로 실패로 표시해도 작업이 유실되지 않는다.
    warning = quality_warning(cfg, stats)
    if warning:
        _notify_slack(f"⚠️ 품질 경보: {warning}")
        raise RuntimeError(warning)
    return {**stats, "text": deterministic}


def span_to_song(span: dict[str, Any]):
    """ingest 입력 span(dict) → Song. 순수 함수 — start_s/end_s만 필수.

    로컬 분석(analyze_vod.py 등)으로 뽑은 곡 구간이라 채팅 스티커 분석이 없다. sticker_rate=0.0,
    song_likely=True로 둔다 — 사람/Claude가 '노래'라고 단언한 구간이므로 카탈로그 매칭이
    되면 auto_matched로 승격시킨다(댓글 힌트 경로와 같은 취급). title/lyrics는 있으면 식별
    단서로 쓰고, 둘 다 없으면 song_id NULL·needs_review로 남는다.
    """
    from soopts.models import Song

    try:
        start = int(span["start_s"])
        end = int(span["end_s"])
    except (KeyError, TypeError, ValueError) as ex:
        raise ValueError(f"span에 정수 start_s/end_s가 필요합니다: {span!r} ({ex})") from ex
    if end <= start:
        raise ValueError(f"end_s는 start_s보다 커야 합니다: {span!r}")
    return Song(
        t=start, end=end, duration=end - start,
        sticker_rate=0.0, song_likely=True,
        lyrics=(span.get("lyrics") or ""),
        title=(span.get("title") or None),
    )


def ingest_vod(
    cfg: Config, *, title_no: str, bj_id: str, songs: list[dict[str, Any]]
) -> dict[str, Any]:
    """로컬 분석(analyze_vod.py 전체 전사 등)으로 뽑은 곡 목록(spans)을 DB(performances/vods)에 기록한다.

    감지/다운로드/STT/세그멘테이션을 전부 생략한다 — Claude가 로컬에서 영상을 보고 곡
    구간·제목을 이미 정했으므로, 이 함수는 식별(카탈로그 매칭)과 DB 기록만 한다. daily가
    'manual'로 표시해 둔 무-타임라인 VOD를 사람이 처리하는 경로다.

    멱등하다: 재-ingest 시 clear_machine_performances로 이전 기계 생성분을 지우고 다시 넣되
    사람이 확정한(confirmed) 행은 보존한다. 완료 후 vods.status를 analyzed/done으로 승격해
    'manual' 대기 상태에서 뺀다.
    """
    from soopts import db
    from soopts.collector.meta import fetch_meta

    if not songs:
        raise ValueError("ingest할 곡(spans)이 비어 있습니다")

    work = work_paths(cfg.work_root, title_no).ensure()
    existing = db.fetch_existing([title_no]).get(title_no)
    if existing:
        vod_row = existing
    else:
        meta = fetch_meta(cfg, title_no, work)
        vod_row = db.upsert_pending([{
            "soop_title_no": title_no,
            "title": meta.title,
            "broadcast_date": None,
            "duration_s": meta.total_duration,
            "retry_count": 0,
        }])[0]

    cleared = db.clear_machine_performances(vod_row["id"])
    if cleared:
        log.info("VOD %s 재-ingest — 기존 기계 생성 %d건 정리", title_no, cleared)

    song_objs = [span_to_song(sp) for sp in songs]
    artists = [sp.get("artist") or "" for sp in songs]
    stats = _record_songs(vod_row["id"], song_objs, artists, db.load_song_catalog())
    db.mark_vod(title_no, next_vod_status(stats["detected"]))

    text = (
        f"VOD {title_no} ingest 완료 — {stats['detected']}곡 기록 "
        f"(자동매칭 {stats['auto_matched']}, 검수대기 {stats['needs_review']})"
    )
    log.info(text)
    _notify_slack(f"📥 {text}\n    {vod_link(cfg, title_no)}")
    return {**stats, "text": text}


def _record_songs(
    vod_row_id: int, song_objs: list, artists: list[str], catalog: list
) -> dict[str, Any]:
    """Song 목록을 식별(카탈로그 매칭) 후 performances에 기록하고 stats를 반환한다(상태 마킹은 호출부).

    댓글 타임라인 처리(_process_vod)와 로컬 ingest(ingest_vod)가 공유하는 코어다.
    제목이 있으면 `resolve_song_match`(가사 추측 생략), 제목 없이 가사만 있으면 `identify_song`,
    둘 다 없으면 needs_review(None)로 남긴다. 감지된 노래 수만큼 hint_available로 센다.
    """
    from soopts import db
    from soopts.analyzers.identify import identify_song, resolve_song_match

    results: list[Any] = []
    auto_matched = 0
    for s, artist in zip(song_objs, artists, strict=True):
        if s.title:
            r = resolve_song_match(s.title, artist or "", s.lyrics, s.song_likely, catalog)
        elif s.lyrics:
            r = identify_song(s.lyrics, s.song_likely, catalog)
        else:
            r = None
        results.append(r)
        if r is not None and r.identify_status == "auto_matched":
            auto_matched += 1

    db.insert_performances(vod_row_id, song_objs, results)
    return {
        "detected": len(song_objs), "auto_matched": auto_matched,
        "needs_review": len(song_objs) - auto_matched,
        "hint_available": len(song_objs), "lyrics_only": 0,
        "stt_attempted": 0, "stt_ok": 0,
    }


def set_vod_manual(cfg: Config, *, title_no: str, clear_machine: bool) -> dict[str, Any]:
    """VOD 하나를 'manual'로 되돌린다 — vod-audit 스킬이 Claude 판정 후 호출하는 적용 함수.

    clear_machine=True면 기계 생성 performances를 먼저 지운다(confirmed는 보존). 판정 자체는
    코드가 하지 않는다 — 어떤 VOD를 넘길지는 스킬 안에서 Claude가 원본 댓글을 읽고 정한다.
    """
    from soopts import db

    existing = db.fetch_existing([title_no]).get(title_no)
    if existing is None:
        raise ValueError(f"VOD {title_no}는 DB에 없습니다")

    cleared = db.clear_machine_performances(existing["id"]) if clear_machine else 0
    confirmed = db.count_confirmed_performances(existing["id"])
    db.mark_vod(title_no, "manual")
    text = (
        f"VOD {title_no} → manual (기계 {cleared}건 삭제"
        f"{f', confirmed {confirmed}건 보존' if confirmed else ''})"
    )
    log.info(text)
    return {"title_no": title_no, "cleared": cleared, "confirmed": confirmed, "text": text}


def _process_vod(
    cfg: Config, vod_row: dict[str, Any], bj_id: str, ctx: RunContext
) -> dict[str, Any]:
    """댓글 타임라인 → 곡별 span → 식별 → DB 기록. 초경량(다운로드·STT·세그멘테이션 없음).

    타임라인의 🎤/🎵 항목이 시각·제목·가수를 모두 주므로, 시작=시각·끝=다음 곡(6분 캡)으로
    span을 만들고 제목/가수로 카탈로그를 매칭해 기록한다 — 예전의 구간 다운로드·경계 탐지·
    Whisper 전사·Groq 가사추측을 전부 없앴다(VOD당 수 초). 타임라인이 없으면(게임 방송 등)
    {"no_timeline": True}를 돌려 호출부가 'manual'로 표시한다(로컬 처리 대상).

    재처리는 멱등하다: clear_machine_performances로 이전 기계 생성분을 지우고 다시 넣되
    사람 확정(confirmed)분은 보존한다. 상태 마킹은 호출부(run_daily)가 한다.
    """
    from soopts import db
    from soopts.analyzers.comment_timeline import (
        no_timeline_note,
        parse_song_timeline,
        timeline_songs_to_spans,
    )
    from soopts.collector.comments import fetch_comments
    from soopts.collector.meta import fetch_meta

    title_no = vod_row["soop_title_no"]
    work = work_paths(cfg.work_root, title_no).ensure()
    meta = fetch_meta(cfg, title_no, work)

    t0 = time.monotonic()
    comments = fetch_comments(cfg, bj_id, title_no)
    timeline = parse_song_timeline(comments)
    if not timeline:
        # 🎤 곡이 0 → 로컬로 넘긴다. 어떤 걸 실제로 돌려봐야 하는지 사유를 메모로 남긴다
        # (🎵 합방곡은 강하게 권함 vs 게임 방송은 볼 것 없음).
        note = no_timeline_note(comments)
        ctx.record(TimelineEvent(
            kind="detection", title_no=title_no,
            detail=f"🎤 0곡 → manual ({note})", duration_s=time.monotonic() - t0,
        ))
        log.info("VOD %s 🎤 0곡 — manual: %s", title_no, note)
        return {"no_timeline": True, "note": note}
    ctx.record(TimelineEvent(
        kind="detection", title_no=title_no,
        detail=f"댓글 타임라인 {len(timeline)}곡", duration_s=time.monotonic() - t0,
    ))

    db.clear_machine_performances(vod_row["id"])
    spans = timeline_songs_to_spans(timeline, meta.total_duration)
    song_objs = [span_to_song(sp) for sp in spans]
    artists = [sp["artist"] for sp in spans]
    stats = _record_songs(vod_row["id"], song_objs, artists, db.load_song_catalog())
    stats["mode"] = "comment_timeline"
    return stats


def _notify_slack(text: str) -> None:
    import os

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        import requests

        requests.post(webhook, json={"text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("Slack 알림 실패: %s", e)


def _notify_slack_failure(context: str, exc: Exception) -> None:
    """개별 VOD/구간 단위가 아니라 배치 전체를 죽이는 예외(SOOP API 502, DB 연결 실패 등)가
    나면 로그뿐 아니라 Slack에도 어느 단계에서 무슨 에러였는지 남긴다 — 이런 경우는 루프
    진입 전/후라 기존 per-VOD 알림으로는 커버되지 않는다."""
    import traceback

    tb = traceback.format_exc()
    _notify_slack(f"❌ soopts 실패 — {context}\n{type(exc).__name__}: {exc}\n```\n{tb[-1500:]}\n```")
