---
name: vod-video-ingest
description: 댓글 타임라인(🎤)이 없어 서버가 'manual'로 남긴 VOD를 claude-video로 영상 분석해 부른 곡·시작/종료를 찾아 DB에 기록. "manual VOD 처리", "무-타임라인 영상 노래 뽑기", "/vod-video-ingest [title_no]" 호출.
allowed-tools: Bash
metadata:
  argument-hint: "[title_no] (없으면 manual 목록에서 고름)"
---

# 무-타임라인 VOD 로컬 처리 스킬 (vod-video-ingest)

서버(daily)는 댓글 타임라인의 **🎤 마커**만 파싱해 처리한다. 🎤가 없는 VOD(게임 방송, 🎵 합방/
콘서트, 옛 아이콘 없는 타임라인)는 `vods.status='manual'`로 남는다. 이 스킬은 그런 VOD를
[claude-video](https://github.com/bradautomates/claude-video) `/watch`로 **영상을 직접 분석**해
스트리머가 부른 곡과 시작/종료 시각을 찾고, `soopts ingest`로 DB에 기록한다.

> **이 스킬의 자리 — "화면으로 확정".** 무-타임라인 VOD에서 *노래가 있는지·어디인지*는 먼저
> `scripts/analyze_vod.py`(전체 오디오 전사, 긴 VOD도 빠름·재개 가능)로 찾는 게 효율적이다.
> 이 스킬(claude-video 프레임)은 그다음 **시각이 필요할 때** 쓴다: ① 가사로 곡명이 안 잡힐 때
> 화면의 곡 제목 오버레이를 읽거나, ② "BJ가 실제로 부르는지 vs 인트로 대기화면/튼 음악인지"를
> 프레임으로 확정할 때. 즉 `analyze_vod.py`=찾기(오디오), 이 스킬=확정(화면). 순수 게임 방송처럼
> 노래가 없다고 판명되면 이 스킬까지 갈 필요 없다.

판정(어디서 노래를 부르는가)은 Claude가 영상/자막으로, DB 기록은 `soopts`가 담당한다.

## 인자
`$ARGUMENTS` — 처리할 title_no. 없으면 manual 목록을 보여주고 고른다.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`가 있어야 한다(ingest의
카탈로그 매칭이 Groq를 쓸 수 있음). `soopts`는 한 줄에서 env를 불러 실행한다:

```bash
set -a; source .env; set +a; .venv/bin/python -m soopts <subcommand> ...
```

## 실행 순서

1. **대상 확인.** 인자가 없으면 manual VOD 목록을 받는다:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts vods --status manual --json
   ```
   목표는 **BJ가 혼자 부른 풀곡만** 기록하는 것이다 — 🎵(합창·따라부르기·게스트 공연)는 대상이
   아니다. `note`는 왜 manual인지 알려준다("팬 타임라인 있으나 🎤 없음" / "댓글 타임라인 없음").
   로컬에서 영상을 봐서 **BJ가 혼자 풀곡을 부른 구간이 있으면** 기록하고, 없으면 그대로 둔다.

2. **영상 확보.** `/watch`는 yt-dlp 기반이다. SOOP VOD는 최신 yt-dlp의 `sooplive` extractor가
   받을 가능성이 높으니 **VOD URL을 먼저 그대로 넘겨본다.** 안 받으면(추출기 미지원/로그인 필요)
   영상을 로컬 파일로 받아 파일 경로를 넘긴다. *(SOOP 지원 여부는 첫 실행에서 한 번 확인.)*
   VOD URL: `https://vod.sooplive.co.kr/player/<title_no>`

3. **claude-video로 곡 찾기.** 장시간 VOD(5~8h)는 프레임 전량 추출이 비싸니 **transcript(자막/
   Whisper) 모드를 우선** 써서 노래 부르는 구간을 찾고, 그 지점만 프레임을 떠 화면의 곡 제목/
   가수를 읽는다. `/watch`에 이렇게 요청한다:
   > 이 VOD에서 **스트리머가 혼자 풀곡을 부른** 구간만 찾아줘. 합창·따라부르기·게스트 공연·
   > BGM·클립 재생·티저·잡담은 제외(부분만 따라 부른 것도 제외).
   > 각 곡의 시작 시각·끝 시각(초)과 화면에 뜬 곡 제목·가수를 아래 JSON으로:
   > `{"songs": [{"start_s": 1023, "end_s": 1245, "title": "...", "artist": "..."}]}`

4. **spans 저장.** 결과 JSON을 파일로 저장한다(예: `spans_<title_no>.json`). `start_s`/`end_s`만
   필수, `title`/`artist`는 카탈로그 매칭용(있으면 자동매칭률↑).

5. **DB 기록.** 사용자에게 곡 목록을 먼저 보여 확인받은 뒤:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts ingest <title_no> spans_<title_no>.json
   ```
   ingest는 기존 기계 생성분을 지우고(confirmed 보존) 다시 넣으며, status를 analyzed/done으로
   승격해 manual에서 뺀다. 멱등하니 재실행해도 안전하다.

## 규칙
- **스트리머가 혼자 부른 풀곡만.** 합창·따라부르기·부분 따라 부르기·게스트 공연·BGM·클립
  재생·티저·잡담은 제외한다(서버의 🎤-only 규칙과 동일 기준 — 🎵는 대상 아님).
- **긴 VOD는 transcript 우선** — 프레임 전량 추출로 토큰을 태우지 말 것.
- **기록 전 사용자 확인.** 곡 목록(시각·제목)을 보여주고 승인받은 뒤 ingest.
- 곡을 하나도 못 찾으면(순수 게임 방송 등) ingest하지 말고 그대로 둔다(manual 유지) — 억지로
  기록하지 않는다.
