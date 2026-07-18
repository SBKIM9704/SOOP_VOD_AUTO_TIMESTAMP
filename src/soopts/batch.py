"""`soopts daily` 오케스트레이션 — GitHub Actions 무인 배치.

스테이션 최신 VOD 중 미처리분 → 감지(스티커 구간)+1080p 정밀 클립 컷+STT →
Groq/rapidfuzz 곡 식별 → DB(performances)에 노래 구간(start_s/end_s) 기록.

산출물은 **타임스탬프**다. 시청은 SOOP 원본 딥링크(`song_link()`)로 연결하며,
이 저장소는 영상을 어디에도 업로드하지 않는다 — 예전엔 유튜브 unlisted 업로드가
붙어 있었으나 저장 비용·저작권 노출·단일 벤더 계정 의존을 이유로 제거했다.

상태의 진실은 파일이 아니라 DB(vods.status + performances.identify_status)다.
러너가 초기화되어 받아둔 구간 파일이 사라져도 DB만 보면 어디까지 처리됐는지 알 수 있다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
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


def comment_candidates(
    timeline: list, pad_before_s: float, pad_after_s: float
) -> list[tuple[float, float, object]]:
    """댓글 타임라인 → (ds, de, hint) 후보 목록. 순수 함수.

    de는 pad_after_s만큼 넉넉히 잡되, 다음 곡의 시작 시각을 넘지 않도록 캡핑한다 —
    안 그러면 곡 간격이 pad_after_s보다 짧을 때 다음 곡을 침범해 가사가 섞인다(실제로
    "고양이"/"Lip" 두 곡이 섞여 잘못 식별된 사례로 확인됨).

    "다음 곡"은 timeline의 리스트 순서(timeline[i+1])가 아니라 **실제 시각** 기준으로
    찾는다 — Groq가 댓글에서 곡을 추출하는 순서가 항상 시간순이라는 보장이 없어(실제로
    리스트 순서와 시각 순서가 어긋나 캡핑이 빗나가고 두 구간이 122초 겹친 사례가 확인됨),
    리스트 위치에 의존하면 이 캡핑 자체가 무력화될 수 있다.
    """
    times_sorted = sorted({t.time_s for t in timeline})
    candidates = []
    for t in timeline:
        ds = max(0.0, t.time_s - pad_before_s)
        de = t.time_s + pad_after_s
        later = [s for s in times_sorted if s > t.time_s]
        if later:
            de = min(de, later[0])
        candidates.append((ds, de, t))
    return candidates


def next_vod_status(detected: int) -> str:
    """VOD 처리 직후 상태. 감지된 노래가 없으면 검수·업로드할 게 없으니 바로 종결(done),
    있으면 업로드 큐 소진까지 거쳐야 하니 analyzed로 남긴다."""
    return "done" if detected == 0 else "analyzed"


def format_summary(stats: dict[str, int]) -> str:
    """일일 배치 결과 한 줄 요약. Slack/로그 공용."""
    parts = [
        f"VOD {stats.get('vods', 0)}건",
        f"감지 {stats.get('detected', 0)}곡",
        f"자동매칭 {stats.get('auto_matched', 0)}",
        f"검수대기 {stats.get('needs_review', 0)}",
    ]
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
    region_errors = stats.get("region_errors") or []
    if region_errors:
        lines.append(f"    일부 구간 실패({len(region_errors)}건): " + " / ".join(region_errors))
    return "\n".join(lines)


def format_region_failure_alert(
    cfg: Config, title_no: str, label: str, step: str, exc: Exception
) -> str:
    """구간 처리 실패 실시간 알림 — 실패 시점에 바로 Slack에 보낸다."""
    return (
        f"⚠️ VOD {title_no} 구간 실패: {label}\n"
        f"    단계: {step}\n"
        f"    {type(exc).__name__}: {exc}\n"
        f"    {vod_link(cfg, title_no)}"
    )


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
            elif ev.kind == "region":
                mark = "성공" if ev.ok else ("건너뜀" if ev.ok is None else "실패")
                extra = (
                    f" [경계 {fmt_duration_s(ev.clip_duration_s)} / "
                    f"STT {fmt_duration_s(ev.stt_duration_s or 0)}]"
                    if ev.clip_duration_s is not None
                    else ""
                )
                tail = f" — {ev.detail}" if ev.detail else ""
                lines.append(
                    f"    구간 {ev.label}: {mark} ({fmt_duration_s(ev.duration_s or 0)}){extra}{tail}"
                )


    if ctx.alert_suppressed:
        lines.append(
            f"\n※ 실시간 실패 알림은 {ctx.alert_limit}건까지만 즉시 발송되며, "
            f"추가 {ctx.alert_suppressed}건은 이 요약에만 반영됨."
        )
    if stats.get("not_song_skipped"):
        lines.append(
            f"\n※ Groq가 '노래 아님'으로 판정해 검수 큐에 올리지 않고 건너뛴 구간: "
            f"{stats['not_song_skipped']}건"
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
def run_daily(cfg: Config, *, bj_id: str, count: int) -> dict[str, Any]:
    from soopts import db
    from soopts.collector.vod_list import fetch_recent_vods

    try:
        candidates = fetch_recent_vods(cfg, bj_id, count * 3)
        picked = db.pick_unprocessed_vods(candidates, count)
    except Exception as e:  # noqa: BLE001
        _notify_slack_failure("VOD 목록 조회", e)
        raise

    ctx = RunContext()
    stats: dict[str, Any] = {"vods": 0, "detected": 0, "auto_matched": 0, "needs_review": 0}
    for vod_row in picked:
        title_no = vod_row["soop_title_no"]
        try:
            vod_stats = _process_vod(cfg, vod_row, bj_id, ctx)
            stats["detected"] += vod_stats["detected"]
            stats["auto_matched"] += vod_stats["auto_matched"]
            stats["needs_review"] += vod_stats["needs_review"]
            if vod_stats.get("not_song_skipped"):
                stats["not_song_skipped"] = (
                    stats.get("not_song_skipped", 0) + vod_stats["not_song_skipped"]
                )
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
    return {**stats, "text": deterministic}


def _find_candidates(
    cfg: Config, bj_id: str, title_no: str, stickers: list[float], meta
) -> tuple[list[tuple], str, str]:
    """(candidates, mode, detail) — 댓글 타임라인이 있으면 그걸 우선 쓰고, 없으면 스티커 감지로 폴백.

    댓글 타임라인은 팬이 자원해서 남긴 비공식 타임스탬프라 정확한 노래 길이를 모르니
    ds/de를 넉넉히 잡아 뒤에서 inaSpeechSegmenter가 정밀 경계를 찾게 한다. hint가 있으면
    이미 아티스트/제목이 확정된 것이므로 이후 identify 단계에서 Claude 가사 추측을 건너뛴다.
    mode는 "comment_timeline"|"sticker_burst", detail은 어느 쪽을 왜 썼는지(상세 리포트용).
    """
    from soopts.analyzers.comment_timeline import extract_song_timeline
    from soopts.collector.comments import fetch_comments

    comment_failed: Exception | None = None
    try:
        comments = fetch_comments(cfg, bj_id, title_no)
        timeline = extract_song_timeline(comments)
    except Exception as ex:  # noqa: BLE001
        log.warning("VOD %s 댓글 조회/추출 실패 — 스티커 감지로 폴백: %s", title_no, ex)
        timeline = []
        comment_failed = ex

    if timeline:
        log.info("VOD %s 댓글 타임라인 사용 — 노래 %d곡", title_no, len(timeline))
        c = cfg.comment
        candidates = comment_candidates(timeline, c.pad_before_s, c.pad_after_s)
        return candidates, "comment_timeline", f"댓글 타임라인 (노래 {len(timeline)}곡 파싱됨)"

    from soopts.analyzers.audio_analyzer import sticker_burst_regions

    a = cfg.audio
    regions = sticker_burst_regions(
        stickers, bucket_s=a.sticker_bucket_s, window_buckets=a.sticker_window_buckets,
        min_per_window=a.sticker_min_per_window, pad_before_s=a.sticker_pad_before_s,
        pad_after_s=a.sticker_pad_after_s, skip_opening_s=a.skip_opening_s,
        total_s=meta.total_duration,
    )
    log.info("VOD %s 노래 후보 %d구간(스티커 감지)", title_no, len(regions))
    candidates = [
        (max(0.0, s - cfg.clip.dl_pad_before_s), e + cfg.clip.dl_pad_after_s, None)
        for s, e in regions
    ]
    detail = (
        f"댓글 조회/추출 실패({comment_failed}) — 스티커 감지 {len(regions)}구간"
        if comment_failed is not None
        else f"댓글에 노래 타임라인 없음 — 스티커 감지 {len(regions)}구간"
    )
    return candidates, "sticker_burst", detail


def _process_vod(
    cfg: Config, vod_row: dict[str, Any], bj_id: str, ctx: RunContext
) -> dict[str, Any]:
    """collect → 노래 구간 후보(댓글 타임라인 우선, 없으면 스티커 감지) → 구간별로 하나씩
    (1080p 정밀 클립+STT+식별+DB 기록).

    구간 하나의 실패는 그 구간만 건너뛰고 나머지는 계속 진행한다(이전엔 VOD 전체를
    실패 처리해 이미 성공한 구간까지 버렸다 — VOD 201651295에서 실제로 이렇게 30분
    분량 작업이 통째로 날아간 사례가 있어 구간 단위로 바꿨다). 다만 후보 구간이
    있었는데 전부 실패하면(예: 네트워크 장애) 재시도 대상이 되도록 예외를 던진다 —
    그렇지 않으면 next_vod_status가 이걸 "노래 없음"과 구분 못 하고 done으로
    잘못 종결시킨다. ctx에는 실시간 실패 알림 예산과 크로놀로지컬 이벤트를 기록한다.
    """
    from soopts import db
    from soopts.analyzers.audio_analyzer import sticker_rate
    from soopts.analyzers.identify import identify_song, resolve_song_match
    from soopts.analyzers.stt import _load_model, _transcribe_best
    from soopts.collector.chat import fetch_chat
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
    from soopts.collector.meta import fetch_meta
    from soopts.export.clips import detect_song_span
    from soopts.models import Song, read_chat_jsonl
    from soopts.output import fmt_hms

    title_no = vod_row["soop_title_no"]
    work = work_paths(cfg.work_root, title_no).ensure()
    meta = fetch_meta(cfg, title_no, work)
    fetch_chat(cfg, title_no, meta, work)

    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    detect_t0 = time.monotonic()
    candidates, mode, detail = _find_candidates(cfg, bj_id, title_no, stickers, meta)
    ctx.record(TimelineEvent(
        kind="detection", title_no=title_no, detail=detail,
        duration_s=time.monotonic() - detect_t0,
    ))
    stats: dict[str, Any] = {"detected": 0, "auto_matched": 0, "needs_review": 0, "region_errors": []}
    if not candidates:
        return stats

    m3u8s = resolve_m3u8_list(title_no, cfg.clip.quality)
    parts = meta.parts
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    catalog = db.load_song_catalog()
    stt_model = None

    for ds, de, hint in candidates:
        label = f"{fmt_hms(int(ds))}~{fmt_hms(int(de))}"
        region_t0 = time.monotonic()
        step = "구간 매핑"
        clip_dur: float | None = None
        stt_dur: float | None = None
        try:
            m3u8, ls, le = map_to_part(ds, de, parts, m3u8s)
            if m3u8 is None:
                log.warning("VOD %s 구간 %s 파트 매핑 실패 — 건너뜀", title_no, label)
                continue
            step = "다운로드"
            raw = work.clips_dir / f"region_{int(ds)}.mp4"
            if not raw.exists():
                download_slice(m3u8, ls, le, raw)
            step = "경계 탐지"
            span = detect_song_span(cfg, str(raw), ds, de, media_offset=ds)
            if span is None:
                continue
            clip, local_start, local_end = span
            clip_dur = time.monotonic() - region_t0

            step = "STT 전사"
            stt_t0 = time.monotonic()
            if stt_model is None:
                stt_model = _load_model(cfg)
            # 노래 구간만 잘라 전사한다 — raw는 앞뒤 패딩이 붙은 후보 구간 전체다.
            text, _lang = _transcribe_best(
                stt_model, str(raw), cfg, start=local_start, dur=local_end - local_start
            )
            stt_dur = time.monotonic() - stt_t0
            lyrics = text[: cfg.stt.lyric_chars]
            rate = sticker_rate((clip.t, clip.end), stickers)
            song = Song(
                t=clip.t, end=clip.end, duration=clip.duration,
                sticker_rate=round(rate, 1),
                song_likely=(hint is not None) or (rate >= cfg.audio.sticker_rate_strong),
                lyrics=lyrics,
            )

            step = "곡 식별"
            if hint is not None:
                # 댓글에 이미 아티스트/제목이 있으니 Claude 가사 추측을 건너뛰고 바로 매칭한다.
                result = resolve_song_match(
                    hint.title, hint.artist or "", song.lyrics, True, catalog
                )
            elif song.lyrics:
                result = identify_song(song.lyrics, song.song_likely, catalog)
                if result is None:
                    # Groq가 확정적으로 "노래 아님"으로 판단 — DB 기록 없이 완전히 건너뛴다
                    # (clip is None과 같은 선상 — 신곡/새 status를 만들지 않는다).
                    log.info("VOD %s 구간 %s — Groq 판정: 노래 아님, 건너뜀", title_no, label)
                    stats["not_song_skipped"] = stats.get("not_song_skipped", 0) + 1
                    ctx.record(TimelineEvent(
                        kind="region", title_no=title_no, label=label, ok=None,
                        detail="노래 아님(Groq) — 건너뜀",
                        duration_s=time.monotonic() - region_t0,
                        clip_duration_s=clip_dur, stt_duration_s=stt_dur,
                    ))
                    continue
            else:
                result = None

            step = "DB 기록"
            db.insert_performances(vod_row["id"], [song], [result])

            stats["detected"] += 1
            if result and result.identify_status == "auto_matched":
                stats["auto_matched"] += 1
            else:
                stats["needs_review"] += 1
            ctx.record(TimelineEvent(
                kind="region", title_no=title_no, label=label, ok=True,
                detail=(result.title_guess if result else None),
                duration_s=time.monotonic() - region_t0,
                clip_duration_s=clip_dur, stt_duration_s=stt_dur,
            ))
        except Exception as ex:  # noqa: BLE001
            log.warning("VOD %s 구간 %s [%s] 실패: %s", title_no, label, step, ex)
            stats["region_errors"].append(f"{label}: {ex}")
            ctx.record(TimelineEvent(
                kind="region", title_no=title_no, label=label, ok=False,
                detail=f"{step}: {type(ex).__name__}: {ex}",
                duration_s=time.monotonic() - region_t0,
                clip_duration_s=clip_dur, stt_duration_s=stt_dur,
            ))
            ctx.alert(format_region_failure_alert(cfg, title_no, label, step, ex))
            continue

    if stats["detected"] == 0 and stats["region_errors"]:
        raise RuntimeError(
            f"후보 {len(candidates)}구간 전부 실패 (예: {stats['region_errors'][0]})"
        )
    return stats


# --------------------------------------------------------------------------- #
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
