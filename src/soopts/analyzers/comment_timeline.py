"""댓글에서 노래 타임라인 추출 — 팬이 자원해서 남기는 타임라인 댓글을 마커 기반으로 파싱한다.

한 팬 계정이 일정한 컨벤션으로 타임라인을 달아준다: `HH:MM:SS 🎤 아티스트 - 제목`. 🎤(마이크)는
**BJ가 실제로 부른 곡**을 뜻해 신뢰도가 높다. 이 마커 + 시각 + `아티스트 - 제목` 형식이
안정적이라, 예전 Groq LLM 추출(곡을 빠뜨리곤 했다)을 정규식으로 대체한다.

🎤가 없는 항목(옛 포맷의 아이콘 없는 곡, 🎵로 적힌 게스트/합방 공연 등)은 서버에서 세지 않는다
— "노래인지" 코드가 확신할 수 없어서다. 그런 VOD(🎤가 하나도 없음)는 타임라인 없음으로 취급돼
manual→로컬 처리로 넘어가고, claude-video가 영상을 보고 판단한다. 놓치거나 잘못 잡은 건
`vod-audit` 스킬(Claude가 원본 댓글 판정)이 사후 교정한다.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from soopts.log import get_logger

log = get_logger("analyzers.comment_timeline")

# 노래 마커는 🎤(BJ가 실제로 부른 곡)만 인정한다. 🎵/🎶는 합방·콘서트 게스트 공연이나 클립
# 참조에도 쓰여 애매하므로 서버에선 세지 않는다 — 이런 VOD(🎤가 하나도 없음)는 타임라인
# 없음으로 취급돼 manual→로컬 처리로 넘어가고, 거기서 claude-video가 판단한다.
_SONG_MARK = re.compile(r"🎤")
# 라인 맨 앞의 타임스탬프(HH:MM:SS 또는 MM:SS).
_TS = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)")
# 노래가 아니라 '클립/영상 참조'인 항목 — 실제 공연이 아니므로 제외한다.
_REF_KEYWORDS = ("편집본", "클립이슈", "틀어놓", "티저", "뮤비", "챌린지", "샤라웃")
# 선행 이모지/기호(마커 제외한 하트·아이콘 등)를 벗겨 '아티스트 - 제목'만 남기기 위한 패턴.
_LEAD_JUNK = re.compile(r"^[^\w가-힣(\[]+")


@dataclass
class TimelineSong:
    time_s: int
    title: str
    artist: str | None = None


def hms_to_s(hms: str) -> int:
    """"HH:MM:SS"/"MM:SS" → 초. 순수 함수."""
    parts = [int(p) for p in hms.strip().split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def _parse_line(line: str) -> TimelineSong | None:
    """타임라인 한 줄 → TimelineSong(노래일 때만), 아니면 None. 순수 함수.

    조건: 맨 앞 타임스탬프 + 🎤 마커 + `아티스트 - 제목` 형식. 클립/티저 참조는 제외.
    """
    line = line.strip().lstrip("└").strip()
    m = _TS.match(line)
    if not m or not _SONG_MARK.search(line):
        return None
    if any(k in line for k in _REF_KEYWORDS):
        return None
    # 타임스탬프·마커·선행 이모지·[태그]를 벗겨 '아티스트 - 제목'만 남긴다.
    body = html.unescape(line[m.end():])
    body = _SONG_MARK.sub("", body)
    body = _LEAD_JUNK.sub("", body).strip()
    body = re.sub(r"^\[[^\]]*\]\s*", "", body).strip()  # [방종곡]·[튠걸고] 등
    if " - " not in body:
        return None
    artist, title = (p.strip() for p in body.split(" - ", 1))
    # 제목 끝의 장식 하트/이모지(예: "... 💛")를 벗긴다.
    title = re.sub(r"[\s💛💚💜💙🩷🖤🩶✨🔥]+$", "", title).strip()
    if not title:
        return None
    return TimelineSong(time_s=hms_to_s(m.group(1)), title=title, artist=artist or None)


def parse_song_timeline(comments: list[str]) -> list[TimelineSong]:
    """댓글 목록에서 노래 타임라인 항목을 마커 기반으로 추출한다. 순수 함수(네트워크·LLM 없음).

    타임라인이 없으면(게임 방송·타임라인 미기재) 빈 리스트. 시각 오름차순으로 정렬해 반환한다.
    """
    songs: list[TimelineSong] = []
    for comment in comments:
        for raw in comment.split("\n"):
            song = _parse_line(raw)
            if song is not None:
                songs.append(song)
    songs.sort(key=lambda s: s.time_s)
    if songs:
        log.info("댓글 타임라인에서 노래 %d곡 파싱", len(songs))
    return songs


# 🎤가 아닌 음악 마커(게스트/합방 공연에 쓰임) + 타임라인 여부 판정용 시각 라인.
_NOTE_MARK = re.compile(r"[🎵🎶]")
_TS_LINE = re.compile(r"(?m)^\s*└?\s*\d{1,2}:\d{2}(?::\d{2})?\s")


def no_timeline_note(comments: list[str]) -> str:
    """🎤 곡이 0일 때 vods.status='manual'에 남길 사유 메모. 순수 함수 — 로컬 처리 우선순위 힌트.

    게임 방송도 타임라인 댓글(시각 나열)은 있으므로, '타임라인 유무'만으론 숨은 곡을 못 가른다.
    🎵/🎶(합방·콘서트 게스트 공연) 표기가 있으면 로컬에서 곡을 찾을 가능성이 높다 → 강하게 권함.
    """
    joined = "\n".join(comments)
    if _NOTE_MARK.search(joined):
        return "🎵 게스트/합방 곡으로 보임 — 로컬 claude-video 처리 권장"
    if len(_TS_LINE.findall(joined)) >= 5:
        return "타임라인 있으나 🎤 표기 없음 — 로컬에서 한번 확인 권장"
    return "댓글 타임라인 없음(게임 방송 가능성)"


def timeline_songs_to_spans(
    songs: list[TimelineSong], duration_s: int | None = None, *, max_song_s: int = 360
) -> list[dict]:
    """TimelineSong 목록 → ingest용 span dict 목록. 순수 함수.

    타임라인은 곡 **시작 시각**만 주므로 끝은 추정한다: `end = 다음 곡 시작`, 단 최대 곡
    길이(기본 6분)로 캡한다 — 곡 사이 잡담 구간이 end로 과대 반영되는 걸 막는다. 마지막 곡은
    `start + max_song_s`(VOD 길이로 캡). 산출물은 곡 시작 딥링크라 끝 정밀도는 부차적이다.
    """
    ordered = sorted(songs, key=lambda s: s.time_s)
    spans: list[dict] = []
    for i, s in enumerate(ordered):
        nxt = ordered[i + 1].time_s if i + 1 < len(ordered) else (duration_s or s.time_s + max_song_s)
        end = min(nxt, s.time_s + max_song_s)
        if duration_s:
            end = min(end, duration_s)
        if end <= s.time_s:  # 같은 시각 중복 등 — 최소 길이 보장
            end = s.time_s + max_song_s
        spans.append({"start_s": s.time_s, "end_s": end, "title": s.title, "artist": s.artist or ""})
    return spans


# 예전 이름 호환 — batch.py는 이 이름으로 호출한다.
extract_song_timeline = parse_song_timeline
