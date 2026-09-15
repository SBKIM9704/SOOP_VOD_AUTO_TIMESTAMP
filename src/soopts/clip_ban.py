"""클립금지 레지스트리 — BJ가 2차 배포를 막은 VOD/곡을 유튜브 게이트에서 거른다.

BJ는 가끔 "이 곡(또는 오늘 방송 전체)은 클립 금지"라고 밝힌다. 근거는 두 군데에 있고 둘 다
사람이 읽어야 나온다: 팬 타임라인의 `[클립금지]` 태그, 그리고 방송 전사문의 육성
(실측 `205491275` 15872s "참고로 오늘 부른 곡들 클립 금지입니다. 아무데도 올리지 마세요").
기계가 자동으로 판정할 대상이 아니므로 이 모듈은 **판정하지 않는다** — `vod-review`의
perf/audit 단계에서 사람이 확인한 결과를 받아 적는 장부일 뿐이다.

**막을 지점은 유튜브 업로드뿐이다.** 이 레포의 산출물은 SOOP 딥링크(타임스탬프)이고 BJ가 막은
건 영상 2차 배포다. 그래서 `parse_song_timeline`은 `[클립금지]` 곡도 그대로 `performances`에
기록한다(딥링크는 계속 제공된다). 파서에서 버리면 타임스탬프까지 같이 사라져 산출물이 손해다.

**두 범위는 막는 방식이 다르다(2026-09).**
- `[[vod]]`(방송 전체): `db.youtube_block_reason`이 그 VOD를 업로드 후보에서 **통째로** 뺀다.
- `[[song]]`(곡 하나): VOD는 그대로 올리되 `split_banned_perfs`가 **그 곡만** 빌드 입력에서
  뺀다. 게이트(`youtube_block_reason`)와 빌드 선택(`youtube_pipeline._pick_target`)이 같은
  함수를 써서 자르므로 "게이트는 통과인데 영상에는 금지 곡이 들어가는" 어긋남이 없다.
  남는 곡이 0개면 게이트가 다시 막는다 — 만들 영상이 없기 때문이다.

**왜 DB 컬럼이 아니라 레포 파일인가.** `vods`/`performances` 스키마의 주인은 별도 private
레포(`singgyul_sing_book`)라 여기서 컬럼을 만들 수 없다. 그전까지는 금지 건을
`identify_status='needs_review'`(곡 단위)나 `youtube_status='no_songs'`(VOD 단위)처럼
**다른 뜻의 필드를 빌려** 표현했는데, 나중에 읽는 사람은 그게 "식별이 덜 됐다"인지
"클립금지라 막았다"인지 구분할 수 없다. 여기에 사유와 근거 인용을 함께 적어 그 모호함을
없앤다. 커밋되는 파일이라 "러너 디스크에 의존하지 말 것"이라는 `batch.py`의 휘발성 러너
제약에도 걸리지 않는다 — 러너는 체크아웃한 레포에서 그대로 읽는다.

파일 형식(`clip_bans.toml`, 기본 경로는 CWD — `soopts.toml`과 같은 관례):

    [[vod]]                       # 방송 전체 금지
    title_no = "205491275"
    reason   = "BJ가 방송 중 전체 클립금지 선언"
    evidence = "전사 15872s: \"오늘 부른 곡들 클립 금지입니다\""

    [[song]]                      # 곡 하나 금지
    perf_id  = 2293
    title_no = "205967387"
    reason   = "팬 [클립금지] 태그 + BJ 육성"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # py3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # py3.10
    import tomli as tomllib  # type: ignore

DEFAULT_PATH = Path("clip_bans.toml")


@dataclass(frozen=True)
class ClipBans:
    """금지 장부. `vods`는 title_no→사유, `songs`는 perf_id→사유.

    비어 있는 인스턴스가 "금지 없음"이다 — 레지스트리 파일이 없는 환경에서도 게이트가
    그대로 동작하도록, 없음을 None이 아니라 빈 장부로 표현한다.
    """

    vods: dict[str, str] = field(default_factory=dict)
    songs: dict[int, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.vods or self.songs)


def parse_clip_bans(data: dict[str, Any]) -> ClipBans:
    """toml 파싱 결과 → ClipBans(순수 함수).

    `reason`을 **필수**로 본다. 사유 없는 금지는 나중에 "이거 풀어도 되나"를 판단할 근거가
    없고, 그 판단 불가를 없애자는 게 이 레지스트리의 목적이다. 빠지면 조용히 넘기지 않고
    ValueError로 죽인다 — 오타로 한 줄이 무시되면 막아야 할 영상이 올라가는데, 업로드는
    수정·삭제 API가 없어 되돌릴 수 없다.

    title_no는 문자열로 정규화한다. toml에 숫자로 적어도 DB의 `soop_title_no`는 text라
    비교가 어긋나기 때문이다.
    """
    vods: dict[str, str] = {}
    for row in data.get("vod", []):
        title_no = str(row.get("title_no") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not title_no or not reason:
            raise ValueError(f"[[vod]] 항목에 title_no와 reason이 모두 필요합니다: {row!r}")
        vods[title_no] = reason

    songs: dict[int, str] = {}
    for row in data.get("song", []):
        perf_id = row.get("perf_id")
        reason = str(row.get("reason") or "").strip()
        if perf_id is None or not reason:
            raise ValueError(f"[[song]] 항목에 perf_id와 reason이 모두 필요합니다: {row!r}")
        songs[int(perf_id)] = reason

    return ClipBans(vods=vods, songs=songs)


def clip_ban_reason(
    bans: ClipBans | None, vod: dict[str, Any], perfs: list[dict[str, Any]]
) -> str | None:
    """이 VOD가 **통째로** 막히는 이유. None이면 막히지 않는다(순수 함수).

    **VOD 전체 금지(`[[vod]]`)만 차단 사유다.** 곡 단위 금지(`[[song]]`)는 그 곡만 빌드에서
    빼면 되므로(`split_banned_perfs`) VOD를 막지 않는다. 예전에는 곡 하나가 VOD 전체를
    막았는데, 그 대가가 컸다 — 2026-09 실측으로 금지 곡 11건이 VOD 10개·곡 165개를
    묶어두고 있었고, 대부분은 팬 태그 하나 때문이었다.

    "빼면 챕터·`?t=` 오프셋이 밀린다"는 예전 우려는 코드와 맞지 않는다. `build_vod_video`는
    실제로 만들어진 클립을 `ffprobe`로 재서 offset을 누적하므로(`video.py`), 입력에서 곡을
    빼면 그 뒤 곡들의 오프셋도 그대로 다시 계산된다. 금지 곡은 `placements`에 없으니
    `performances.youtube_url`도 받지 않는다.

    `perfs`는 지금 쓰지 않지만 시그니처에 남긴다 — 호출부가 곡 목록과 함께 판정하는 흐름이고,
    "곡 구성 때문에 VOD 전체를 막아야 하는" 새 사유가 생기면 여기로 들어와야 한다.
    """
    if not bans:
        return None
    title_no = str(vod.get("soop_title_no") or "")
    if title_no in bans.vods:
        return f"클립금지(VOD 전체): {bans.vods[title_no]}"
    return None


def split_banned_perfs(
    bans: ClipBans | None, perfs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """곡 목록을 (합본에 넣을 곡, 클립금지로 뺄 곡)으로 나눈다(순수 함수). 입력 순서를 지킨다.

    금지 곡도 `performances` 행은 그대로 둔다 — 막는 건 영상뿐이고 SOOP 딥링크(타임스탬프)는
    계속 제공한다. 여기서 빼는 건 **빌드 입력**이라, 금지 곡은 영상·챕터·설명·
    `performances.youtube_url` 어디에도 들어가지 않는다.
    """
    if not bans:
        return list(perfs), []
    kept: list[dict[str, Any]] = []
    banned: list[dict[str, Any]] = []
    for perf in perfs:
        (banned if bans.songs.get(perf.get("id")) else kept).append(perf)
    return kept, banned


def song_ban_reason(bans: ClipBans | None, perf_id: Any) -> str | None:
    """곡 하나의 금지 사유(없으면 None). 로그·Slack에 "왜 뺐는지"를 싣기 위한 조회다."""
    if not bans:
        return None
    return bans.songs.get(perf_id)


def load_clip_bans(path: Path | None = None) -> ClipBans:
    """레지스트리 파일을 읽는다. 파일이 없으면 빈 장부(금지 없음).

    없어도 예외를 내지 않는 건, 이 파일이 있어야만 파이프라인이 도는 필수 설정이 아니라
    "예외를 적어두는 장부"이기 때문이다. 반대로 파일이 있는데 형식이 틀리면
    `parse_clip_bans`가 죽는다 — 그쪽은 조용히 넘기면 안 되는 실패다.
    """
    candidate = path if path is not None else DEFAULT_PATH
    if not candidate.exists():
        return ClipBans()
    with open(candidate, "rb") as fh:
        return parse_clip_bans(tomllib.load(fh))
