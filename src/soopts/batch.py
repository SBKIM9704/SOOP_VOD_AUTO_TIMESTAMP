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

# STT로 전사할 노래 구간의 최대 길이(초). 16kHz 모노 WAV는 초당 32KB라 이 상한이면 Groq
# 업로드 한도(25MB) 안쪽이다. 무-타임라인 sweep은 연속 메들리를 한 음악 블록으로 병합해
# 10분 넘는 구간을 만들 수 있는데, 그대로 올리면 413으로 거절돼 VOD 전체가 재시도를
# 헛돈다. 가사는 어차피 앞부분만으로 곡을 식별하므로(lyric_chars로 잘림) 상한을 둬도 무해하다.
STT_MAX_SECONDS = 540.0

# 파트 길이를 모를 때 download_slice에 넘길 '끝' 값. 실제 VOD 길이보다 훨씬 커서 결국
# 플레이리스트의 마지막 세그먼트까지 전부 받게 된다(download_slice는 있는 세그먼트로 클램프).
_SWEEP_UNKNOWN_PART_END_S = 1e9


class SweepTooLongError(RuntimeError):
    """무-타임라인 VOD가 러너에서 sweep하기엔 너무 길다 — 재시도해도 결과가 같은 영구 실패.

    일반 구간 실패(네트워크 등)와 달리 다시 돌려도 같은 길이라 결과가 바뀌지 않으므로,
    호출부가 retry_count를 곧바로 상한으로 올려 큐에서 제외한다. 그러지 않으면 매 런
    우선순위 슬롯을 차지하며 3번 헛돈다(daily_vod_count=1이면 런 3개가 통째로 낭비된다).
    RuntimeError를 상속하므로 이 예외를 따로 잡지 않는 경로에서는 기존과 똑같이 다뤄진다.
    """


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


def sweep_part_range(
    idx: int, parts: list, total_duration_s: float
) -> tuple[float, float] | None:
    """full_sweep에서 파트 idx의 (전역 오프셋, 다운로드 끝 시각). 오프셋 불명이면 None(=건너뜀).

    - 메타에 파트가 있으면 그 파트의 offset_s/duration을 쓴다.
    - m3u8 수가 메타 파트 수보다 많아 해당 파트 정보가 없으면 **None**을 준다. 오프셋을 0으로
      추정하면 그 파트의 곡이 전부 틀린 전역 시각으로 DB에 들어가 딥링크가 엉뚱한 데를
      가리킨다 — 잘못된 타임스탬프보다 건너뛰고 재시도로 남기는 편이 낫다.
    - 파트 정보가 아예 없으면(단일 파트/메타 파싱 실패) 오프셋 0에 전체 길이를 쓴다.
    - 길이를 모르면(메타 파싱 실패 시 total_duration=0) 끝을 아주 크게 잡는다. 0을 그대로
      넘기면 download_slice의 `s <= end_s` 조건에 첫 세그먼트 하나만 걸려, 예외 없이 6초만
      훑고 "노래 0곡"으로 끝난다(조용한 데이터 손실).
    """
    if parts:
        if idx >= len(parts):
            return None
        offset, pdur = float(parts[idx].offset_s), float(parts[idx].duration)
    else:
        offset, pdur = 0.0, float(total_duration_s or 0.0)
    return offset, (pdur if pdur > 0 else _SWEEP_UNKNOWN_PART_END_S)


def sweep_allowed(is_new: bool, sweep_used: int, sweep_limit: int) -> bool:
    """이번 VOD의 full_sweep(무-타임라인 전체 오디오 처리)을 이번 런에서 실행해도 되는지.

    재시도(is_new=False)는 우선순위상 항상 실행한다 — 이미 큐에 커밋된 작업이고 수도 최대
    count개다. 신규는 이번 런의 sweep 예산(sweep_limit) 안에서만 실행하고, 초과분은 미룬다.
    sweep은 전체 다운로드+전체 세그멘테이션이라 길어서, 여러 개가 한 런에 몰리면 러너
    타임아웃 위험이 커진다(무-타임라인 옛 VOD를 백필할 때 특히).
    """
    return (not is_new) or (sweep_used < sweep_limit)


def sweep_too_long_reason(total_duration_s: float, max_s: float) -> str | None:
    """VOD가 sweep 상한보다 길면 실패 사유를, 아니면 None을 반환한다(max_s=0이면 가드 끔).

    무-타임라인 VOD는 전체 세그멘테이션을 하는데, 병적으로 긴 VOD(예: 9시간+)는 어떤
    러너 timeout 안에도 못 끝나 3번(MAX_RETRIES) 헛 타임아웃하며 큐를 막는다. 다운로드
    전에 길이만 보고 빠르게 실패시켜 사람에게 알리고 — 시청 경로는 어차피 SOOP 딥링크라,
    이런 VOD는 timeout 없는 수동 CLI로 처리하면 된다.
    """
    if max_s and total_duration_s > max_s:
        return (
            f"VOD 길이 {fmt_duration_s(total_duration_s)}가 sweep 상한 "
            f"{fmt_duration_s(max_s)} 초과 — 러너 sweep 불가(수동 CLI로 처리)"
        )
    return None


def retry_reason(detected: int, region_errors: list[str]) -> str | None:
    """구간 실패가 하나라도 있으면 VOD를 재시도 상태로 되돌릴 사유를, 없으면 None을 반환한다.

    예전엔 **전부 실패**했을 때만 예외를 던져 재시도로 넘겼다. 그래서 20곡 중 19곡이
    성공하고 1곡이 다운로드 실패로 빠지면 VOD는 그대로 analyzed로 종결됐고, 그 1곡은
    영영 사라졌다(201142227 04:54:08 구간 등 — VOD는 '완료'로 찍혔는데 곡만 유실).
    재처리는 clear_machine_performances가 confirmed만 남기고 나머지를 다시 만드니
    손실 없이 멱등하다 — 따라서 실패가 하나라도 남으면 재시도시키는 편이 안전하다.
    재시도 횟수는 MAX_RETRIES가 상한을 둬, 영영 못 받는 구간이 큐를 막지는 않는다.
    """
    if not region_errors:
        return None
    example = region_errors[0]
    if detected == 0:
        return f"후보 전부 실패 (예: {example})"
    return f"{detected}곡 성공했으나 {len(region_errors)}개 구간 실패 — 재시도 필요 (예: {example})"


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
    if stats.get("sweep_deferred"):
        lines.append(
            f"\n※ 이번 런 sweep 예산 초과로 다음 런에 미룬 무-타임라인 VOD: "
            f"{stats['sweep_deferred']}건"
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
    """
    from soopts import db
    from soopts.collector.vod_list import iter_vod_pages

    if count <= 0:
        return []
    retryable = db.fetch_retryable(count)
    candidates: list[dict[str, Any]] = []
    existing_by_no: dict[str, dict[str, Any]] = {}
    targets = db.select_targets(retryable, candidates, existing_by_no, count)

    if len(targets) < count:
        for page in iter_vod_pages(cfg, bj_id):
            candidates.extend(page)
            existing_by_no.update(db.fetch_existing([str(c["title_no"]) for c in page]))
            targets = db.select_targets(retryable, candidates, existing_by_no, count)
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
    sweep_used = 0
    for vod_row in picked:
        title_no = vod_row["soop_title_no"]
        is_new = (vod_row.get("retry_count") or 0) == 0
        allow_sweep = sweep_allowed(is_new, sweep_used, cfg.station.sweep_limit)
        try:
            vod_stats = _process_vod(cfg, vod_row, bj_id, ctx, allow_sweep=allow_sweep)
            if vod_stats.get("deferred"):
                # 이번 런 sweep 예산 초과로 미룬 신규 무-타임라인 VOD — pending 행을 지워
                # 다음 런에 신규로 다시 잡히게 한다(retry_count를 태우지 않는다).
                db.delete_vod(title_no)
                stats["sweep_deferred"] = stats.get("sweep_deferred", 0) + 1
                continue
            if vod_stats.get("mode") == "full_sweep":
                sweep_used += 1
            stats["detected"] += vod_stats["detected"]
            stats["auto_matched"] += vod_stats["auto_matched"]
            stats["needs_review"] += vod_stats["needs_review"]
            for k in ("stt_attempted", "stt_ok", "hint_available", "lyrics_only"):
                stats[k] += vod_stats.get(k, 0)
            if vod_stats.get("not_song_skipped"):
                stats["not_song_skipped"] = (
                    stats.get("not_song_skipped", 0) + vod_stats["not_song_skipped"]
                )
            db.mark_vod(title_no, next_vod_status(vod_stats["detected"]))
            _notify_slack(format_vod_result(cfg, title_no, vod_stats))
        except SweepTooLongError as e:
            # 재시도해도 같은 결과 — 큐에서 즉시 빼 매 런 슬롯을 낭비하지 않게 한다.
            log.error("VOD %s sweep 불가(영구): %s", title_no, e)
            db.mark_vod_unretryable(title_no, str(e)[:500])
            _notify_slack(format_vod_result(cfg, title_no, {"error": str(e)[:500]}))
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


def process_single_vod(cfg: Config, *, title_no: str, bj_id: str) -> dict[str, Any]:
    """단일 VOD를 배치 파이프라인으로 처리해 DB(performances/vods)에 기록한다 — 로컬 수동 실행용.

    daily의 러너 전용 제약을 우회한다: sweep 예산과 길이 가드를 끄고(allow_sweep=True,
    enforce_guard=False) timeout 없는 PC에서 끝까지 돌린다. 댓글 타임라인 없는 초장시간
    VOD처럼 GitHub 러너로는 못 돌리는 걸 처리하는 탈출구다. 산출물·상태는 daily와 똑같이
    Supabase에 남으므로 시청 딥링크가 동일하게 생성된다(수동 CLI의 songs/clips가 로컬
    파일만 남기는 것과 달리, 이 경로는 DB에 기록한다).
    """
    from soopts import db
    from soopts.collector.meta import fetch_meta

    work = work_paths(cfg.work_root, title_no).ensure()
    existing = db.fetch_existing([title_no]).get(title_no)
    if existing:
        vod_row = existing  # 가드로 failed 처리된 초장시간 VOD는 보통 이미 행이 있다
    else:
        meta = fetch_meta(cfg, title_no, work)
        vod_row = db.upsert_pending([{
            "soop_title_no": title_no,
            "title": meta.title,
            "broadcast_date": None,
            "duration_s": meta.total_duration,
            "retry_count": 0,
        }])[0]

    ctx = RunContext()
    stats: dict[str, Any] = {
        "vods": 1, "detected": 0, "auto_matched": 0, "needs_review": 0,
        "stt_attempted": 0, "stt_ok": 0, "hint_available": 0, "lyrics_only": 0,
    }
    try:
        vod_stats = _process_vod(
            cfg, vod_row, bj_id, ctx, allow_sweep=True, enforce_guard=False
        )
        for k in ("detected", "auto_matched", "needs_review",
                  "stt_attempted", "stt_ok", "hint_available", "lyrics_only"):
            stats[k] += vod_stats.get(k, 0)
        if vod_stats.get("not_song_skipped"):
            stats["not_song_skipped"] = vod_stats["not_song_skipped"]
        db.mark_vod(title_no, next_vod_status(vod_stats["detected"]))
        _notify_slack(format_vod_result(cfg, title_no, vod_stats))
    except Exception as e:  # noqa: BLE001
        log.error("VOD %s 처리 실패: %s", title_no, e)
        db.mark_vod(title_no, "failed", error=str(e)[:500])
        _notify_slack(format_vod_result(cfg, title_no, {"error": str(e)[:500]}))

    deterministic = format_detailed_summary(cfg, ctx, stats)
    log.info("process 완료: %s", deterministic)
    return {**stats, "text": deterministic}


def _find_candidates(
    cfg: Config, bj_id: str, title_no: str
) -> tuple[list[tuple], str, str]:
    """(candidates, mode, detail) — 댓글 타임라인이 있으면 그걸 우선 쓰고, 없으면 전체 오디오 sweep으로 폴백.

    댓글 타임라인은 팬이 자원해서 남긴 비공식 타임스탬프라 정확한 노래 길이를 모르니
    ds/de를 넉넉히 잡아 뒤에서 inaSpeechSegmenter가 정밀 경계를 찾게 한다. hint가 있으면
    이미 아티스트/제목이 확정된 것이므로 이후 identify 단계에서 Claude 가사 추측을 건너뛴다.

    타임라인이 없으면 mode="full_sweep"로 신호만 보내고 후보는 비운다 — 후보 열거는
    _run_full_sweep이 전체 오디오를 파트별로 받아 음악 세그멘테이션으로 직접 한다. 예전엔
    여기서 스티커 버스트로 후보를 만들었으나, 스티커가 잡담/리액션에도 흩뿌려지면 11~25분짜리
    거대 구간으로 뭉쳐지고 그 안에서 최장 음악블록 1개만 잡혀 곡을 통째로 놓쳤다(무-타임라인
    VOD가 거의 전부 '노래 아님'으로 스킵되던 원인). mode는 "comment_timeline"|"full_sweep".
    """
    from soopts.analyzers.comment_timeline import extract_song_timeline
    from soopts.collector.comments import fetch_comments

    comment_failed: Exception | None = None
    try:
        comments = fetch_comments(cfg, bj_id, title_no)
        timeline = extract_song_timeline(comments)
    except Exception as ex:  # noqa: BLE001
        log.warning("VOD %s 댓글 조회/추출 실패 — 전체 오디오 sweep으로 폴백: %s", title_no, ex)
        timeline = []
        comment_failed = ex

    if timeline:
        log.info("VOD %s 댓글 타임라인 사용 — 노래 %d곡", title_no, len(timeline))
        c = cfg.comment
        candidates = comment_candidates(timeline, c.pad_before_s, c.pad_after_s)
        return candidates, "comment_timeline", f"댓글 타임라인 (노래 {len(timeline)}곡 파싱됨)"

    detail = (
        f"댓글 조회/추출 실패({comment_failed}) — 전체 오디오 음악 sweep"
        if comment_failed is not None
        else "댓글에 노래 타임라인 없음 — 전체 오디오 음악 sweep"
    )
    return [], "full_sweep", detail


def _process_region(
    cfg: Config,
    vod_row: dict[str, Any],
    title_no: str,
    *,
    song,
    hint,
    media_path: str,
    local_start: float,
    local_end: float,
    stt_model,
    catalog,
    stats: dict[str, Any],
    ctx: RunContext,
    region_t0: float,
    clip_dur: float | None,
    label: str,
) -> None:
    """구간 하나의 공통 tail: STT 전사 → 곡 식별 → DB 기록 → stats/이벤트 갱신.

    댓글 타임라인 경로와 전체 sweep 경로가 이 함수를 공유한다. media_path의
    [local_start, local_end] 만 잘라 전사한다(파트 전체/후보 구간 전체가 넘어와도 노래
    구간만 본다). 예외는 잡지 않는다 — 호출부가 region_errors에 기록하고 재시도로 넘긴다.
    """
    from soopts import db
    from soopts.analyzers.identify import identify_song, resolve_song_match
    from soopts.analyzers.stt import _transcribe_best

    stt_t0 = time.monotonic()
    transcribe_span_s = min(local_end - local_start, STT_MAX_SECONDS)  # 전사할 오디오 길이
    text, _lang = _transcribe_best(
        stt_model, media_path, cfg, start=local_start, dur=transcribe_span_s
    )
    stt_dur = time.monotonic() - stt_t0  # 전사에 걸린 실제 시간(리포트용)
    song.lyrics = text[: cfg.stt.lyric_chars]
    # 전사 성공률은 조용한 장애를 잡는 유일한 신호다 — Groq가 413으로 전량 거절하던
    # 시절에도 실행은 계속 "성공"으로 끝났고 곡만 검수 큐에 쌓였다.
    stats["stt_attempted"] += 1
    if song.lyrics.strip():
        stats["stt_ok"] += 1

    stats["hint_available" if hint is not None else "lyrics_only"] += 1
    if hint is not None:
        # 댓글에 이미 아티스트/제목이 있으니 Claude 가사 추측을 건너뛰고 바로 매칭한다.
        result = resolve_song_match(hint.title, hint.artist or "", song.lyrics, True, catalog)
    elif song.lyrics:
        result = identify_song(song.lyrics, song.song_likely, catalog)
        if result is None:
            # Groq가 확정적으로 "노래 아님"으로 판단 — DB 기록 없이 완전히 건너뛴다
            # (신곡/새 status를 만들지 않는다).
            log.info("VOD %s 구간 %s — Groq 판정: 노래 아님, 건너뜀", title_no, label)
            stats["not_song_skipped"] = stats.get("not_song_skipped", 0) + 1
            ctx.record(TimelineEvent(
                kind="region", title_no=title_no, label=label, ok=None,
                detail="노래 아님(Groq) — 건너뜀",
                duration_s=time.monotonic() - region_t0,
                clip_duration_s=clip_dur, stt_duration_s=stt_dur,
            ))
            return
    else:
        result = None

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


def _run_full_sweep(
    cfg: Config,
    vod_row: dict[str, Any],
    title_no: str,
    meta,
    m3u8s: list[str],
    parts: list,
    work,
    stickers: list[float],
    stt_model,
    catalog,
    stats: dict[str, Any],
    ctx: RunContext,
) -> None:
    """무-타임라인 폴백: 전체 오디오를 파트별로 받아 inaSpeechSegmenter로 노래를 전부 열거한다.

    댓글 힌트가 없으니 재현율을 위해 파트 전체를 최저 화질(오디오만 쓰므로 무방)로 받아
    음악 구간을 하나도 빠뜨리지 않고 뽑는다 — 스티커 버스트로 대략 위치만 잡고 그 안에서
    최장 음악블록 1개만 취하던 예전 방식은 한 버스트에 여러 곡이 있으면 나머지를 통째로
    놓쳤다(무-타임라인 VOD가 거의 전부 '노래 아님'으로 스킵되던 원인). 느리지만
    (전체 다운로드+전체 세그멘테이션) 무-타임라인 VOD는 드물고 여기선 정확도가 우선이다.

    파트 하나의 다운로드/세그멘테이션 실패는 그 파트만 건너뛰되 region_errors에 남긴다 —
    그 파트의 곡을 통째로 잃으므로 VOD를 재시도 대상으로 만들어야 한다.
    """
    from soopts.analyzers.audio_analyzer import (
        _segment,
        merge_intervals,
        music_intervals,
        sticker_rate,
    )
    from soopts.collector.media import download_slice
    from soopts.models import Song
    from soopts.output import fmt_hms

    a = cfg.audio
    if parts and len(m3u8s) != len(parts):
        log.warning(
            "VOD %s m3u8 파트 수(%d) != 메타 파트 수(%d) — 오프셋 매핑 불가한 파트는 건너뛴다",
            title_no, len(m3u8s), len(parts),
        )
    for idx, m3u8 in enumerate(m3u8s):
        part_label = f"파트{idx}"
        part_range = sweep_part_range(idx, parts, meta.total_duration)
        if part_range is None:
            log.warning("VOD %s %s 메타 오프셋 없음 — 건너뜀", title_no, part_label)
            stats["region_errors"].append(f"{part_label}: 메타 파트 정보 없음(오프셋 불명)")
            continue
        offset, end_s = part_range
        media = work.clips_dir / f"part_{idx}.mp4"
        part_t0 = time.monotonic()
        try:
            if not media.exists():
                download_slice(m3u8, 0.0, end_s, media, workers=cfg.collector.segment_workers)
            seg = _segment(str(media), a.vad_engine)
        except Exception as ex:  # noqa: BLE001
            log.warning("VOD %s %s 다운로드/세그멘테이션 실패: %s", title_no, part_label, ex)
            stats["region_errors"].append(f"{part_label}: {ex}")
            ctx.record(TimelineEvent(
                kind="region", title_no=title_no, label=part_label, ok=False,
                detail=f"전체 sweep: {type(ex).__name__}: {ex}",
                duration_s=time.monotonic() - part_t0,
            ))
            ctx.alert(format_region_failure_alert(cfg, title_no, part_label, "전체 sweep", ex))
            continue

        intervals = merge_intervals(music_intervals(seg), a.merge_gap_s, a.min_music_s)
        log.info("VOD %s %s 음악 구간 %d개", title_no, part_label, len(intervals))
        for ls, le in intervals:
            g_start, g_end = offset + ls, offset + le
            if g_start < a.skip_opening_s or (le - ls) < cfg.clip.min_song_s:
                continue
            label = f"{fmt_hms(int(g_start))}~{fmt_hms(int(g_end))}"
            region_t0 = time.monotonic()
            try:
                rate = sticker_rate((g_start, g_end), stickers)
                song = Song(
                    t=int(g_start), end=int(g_end), duration=int(le - ls),
                    sticker_rate=round(rate, 1),
                    song_likely=rate >= a.sticker_rate_strong,
                    lyrics="",
                )
                _process_region(
                    cfg, vod_row, title_no, song=song, hint=None,
                    media_path=str(media), local_start=ls, local_end=le,
                    stt_model=stt_model, catalog=catalog, stats=stats, ctx=ctx,
                    region_t0=region_t0, clip_dur=None, label=label,
                )
            except Exception as ex:  # noqa: BLE001
                log.warning("VOD %s 구간 %s 실패: %s", title_no, label, ex)
                stats["region_errors"].append(f"{label}: {ex}")
                ctx.record(TimelineEvent(
                    kind="region", title_no=title_no, label=label, ok=False,
                    detail=f"STT/식별: {type(ex).__name__}: {ex}",
                    duration_s=time.monotonic() - region_t0,
                ))
                ctx.alert(format_region_failure_alert(cfg, title_no, label, "STT/식별", ex))


def _process_vod(
    cfg: Config, vod_row: dict[str, Any], bj_id: str, ctx: RunContext,
    *, allow_sweep: bool = True, enforce_guard: bool = True,
) -> dict[str, Any]:
    """collect → 노래 구간 후보 → 구간별로 STT+식별+DB 기록.

    후보 감지는 두 갈래다: 댓글 타임라인이 있으면 그 시각별로 후보 구간을 받아
    detect_song_span으로 경계를 다듬고(comment_timeline), 없으면 전체 오디오를 파트별로
    받아 음악 세그멘테이션으로 곡을 전부 열거한다(full_sweep, _run_full_sweep).

    allow_sweep=False면 full_sweep으로 판명된 VOD를 처리하지 않고 곧바로
    {"deferred": True}로 되돌린다(무거운 다운로드 전에) — 이번 런의 sweep 예산을 넘긴 경우다.
    호출부가 pending 행을 지워 다음 런에 미룬다. 반환 stats["mode"]로 어느 갈래였는지 알린다.

    enforce_guard=False면 sweep 길이 가드를 끈다 — timeout 없는 로컬 수동 실행
    (process_single_vod)에서 초장시간 무-타임라인 VOD를 끝까지 처리하기 위한 탈출구다.

    구간 하나의 실패는 그 구간만 건너뛰고 나머지는 계속 진행한다(이전엔 VOD 전체를
    실패 처리해 이미 성공한 구간까지 버렸다). 다만 실패가 하나라도 남으면 — 전부 실패든
    일부 실패든 — 재시도 대상이 되도록 예외를 던진다(retry_reason). 예전엔 전부 실패했을
    때만 던져, 일부만 실패하면 그 구간이 영영 유실됐다. 재처리는 멱등하므로(성공분은
    clear_machine_performances가 지우고 다시 만든다) 재시도에 손실이 없다.
    ctx에는 실시간 실패 알림 예산과 크로놀로지컬 이벤트를 기록한다.
    """
    from soopts import db
    from soopts.analyzers.audio_analyzer import sticker_rate
    from soopts.analyzers.stt import _load_model
    from soopts.collector.chat import fetch_chat
    from soopts.collector.media import download_slice, map_to_part, resolve_m3u8_list
    from soopts.collector.meta import fetch_meta
    from soopts.export.clips import detect_song_span
    from soopts.models import Song, read_chat_jsonl
    from soopts.output import fmt_hms

    title_no = vod_row["soop_title_no"]
    work = work_paths(cfg.work_root, title_no).ensure()
    meta = fetch_meta(cfg, title_no, work)

    # 감지 갈래(댓글 타임라인 vs 전체 sweep)를 먼저 정한다 — sweep 예산 초과 시 무거운
    # 다운로드/세그멘테이션 전에 미루기 위해, 채팅 수집·기계행 정리보다 앞서 판정한다.
    detect_t0 = time.monotonic()
    candidates, mode, detail = _find_candidates(cfg, bj_id, title_no)
    if mode == "full_sweep":
        # 예산보다 먼저 길이 가드를 본다 — 초장시간 VOD는 미뤄봐야 다음 런에도 못 하므로
        # (defer→재선정 무한루프) 바로 실패시켜 사람에게 넘긴다. 로컬 수동 실행은
        # enforce_guard=False로 이 가드를 끄고 끝까지 처리한다.
        too_long = (
            sweep_too_long_reason(
                float(meta.total_duration or 0.0), cfg.station.sweep_max_duration_s
            )
            if enforce_guard
            else None
        )
        if too_long:
            raise SweepTooLongError(
                f"{too_long} — 로컬에서 `soopts process {title_no}` 로 처리하세요"
            )
        if not allow_sweep:
            log.info("VOD %s 무-타임라인이나 이번 런 sweep 예산 초과 — 다음 런으로 미룸", title_no)
            return {"deferred": True, "mode": mode}
    ctx.record(TimelineEvent(
        kind="detection", title_no=title_no, detail=detail,
        duration_s=time.monotonic() - detect_t0,
    ))

    fetch_chat(cfg, title_no, meta, work)

    # 재처리면 이전 실행이 남긴 기계 생성 행을 먼저 치운다(사람 확정분은 유지).
    cleared = db.clear_machine_performances(vod_row["id"])
    if cleared:
        log.info("VOD %s 재처리 — 기존 기계 생성 %d건 정리", title_no, cleared)

    stickers = [float(m.t) for m in read_chat_jsonl(work.chat) if m.kind == "ogq"]
    stats: dict[str, Any] = {
        "detected": 0, "auto_matched": 0, "needs_review": 0, "region_errors": [],
        "stt_attempted": 0, "stt_ok": 0, "hint_available": 0, "lyrics_only": 0,
        "mode": mode,
    }

    m3u8s = resolve_m3u8_list(title_no, cfg.clip.quality)
    parts = meta.parts
    work.clips_dir.mkdir(parents=True, exist_ok=True)
    catalog = db.load_song_catalog()
    stt_model = _load_model(cfg)

    if mode == "full_sweep":
        _run_full_sweep(
            cfg, vod_row, title_no, meta, m3u8s, parts, work,
            stickers, stt_model, catalog, stats, ctx,
        )
    else:
        for ds, de, hint in candidates:
            label = f"{fmt_hms(int(ds))}~{fmt_hms(int(de))}"
            region_t0 = time.monotonic()
            step = "구간 매핑"
            clip_dur: float | None = None
            try:
                m3u8, ls, le = map_to_part(ds, de, parts, m3u8s)
                if m3u8 is None:
                    log.warning("VOD %s 구간 %s 파트 매핑 실패 — 건너뜀", title_no, label)
                    continue
                step = "다운로드"
                raw = work.clips_dir / f"region_{int(ds)}.mp4"
                if not raw.exists():
                    download_slice(m3u8, ls, le, raw, workers=cfg.collector.segment_workers)
                step = "경계 탐지"
                span = detect_song_span(cfg, str(raw), ds, de, media_offset=ds)
                if span is None:
                    continue
                clip, local_start, local_end = span
                clip_dur = time.monotonic() - region_t0
                rate = sticker_rate((clip.t, clip.end), stickers)
                song = Song(
                    t=clip.t, end=clip.end, duration=clip.duration,
                    sticker_rate=round(rate, 1),
                    song_likely=(hint is not None) or (rate >= cfg.audio.sticker_rate_strong),
                    lyrics="",
                )
                step = "STT/식별"
                _process_region(
                    cfg, vod_row, title_no, song=song, hint=hint,
                    media_path=str(raw), local_start=local_start, local_end=local_end,
                    stt_model=stt_model, catalog=catalog, stats=stats, ctx=ctx,
                    region_t0=region_t0, clip_dur=clip_dur, label=label,
                )
            except Exception as ex:  # noqa: BLE001
                log.warning("VOD %s 구간 %s [%s] 실패: %s", title_no, label, step, ex)
                stats["region_errors"].append(f"{label}: {ex}")
                ctx.record(TimelineEvent(
                    kind="region", title_no=title_no, label=label, ok=False,
                    detail=f"{step}: {type(ex).__name__}: {ex}",
                    duration_s=time.monotonic() - region_t0,
                    clip_duration_s=clip_dur,
                ))
                ctx.alert(format_region_failure_alert(cfg, title_no, label, step, ex))
                continue

    reason = retry_reason(stats["detected"], stats["region_errors"])
    if reason:
        raise RuntimeError(reason)
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
