---
name: manual-ingest
description: 서버가 'manual'로 남긴 무-타임라인 VOD를 로컬에서 전체 전사(analyze_vod.py)해 BJ가 혼자 부른 풀곡을 찾아 DB에 기록. "manual VOD 처리", "무-타임라인 영상 처리", "/manual-ingest [title_no]" 호출.
allowed-tools: Bash, Read
metadata:
  argument-hint: "[title_no] (없으면 manual 목록에서 고름)"
---

# manual VOD 로컬 처리 스킬 (manual-ingest)

서버(daily)는 댓글 타임라인의 **🎤(BJ 솔로곡)**만 처리하고, 🎤가 없는 VOD는 `vods.status='manual'`로
남긴다. 이 스킬은 그런 VOD를 **로컬에서 전체 오디오 전사**해 **BJ가 혼자 부른 풀곡**만 찾아
`soopts ingest`로 DB에 기록한다. 판정(무엇이 솔로 풀곡인가)은 Claude가 전사문을 읽고, 다운로드·
전사·DB 기록은 `soopts`/`analyze_vod.py`가 담당한다.

## 인자
`$ARGUMENTS` — 처리할 title_no. 없으면 manual 목록을 보여주고 고른다.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`(+`_2`/`_3` 로테이션 선택).
모든 명령은 한 줄에서 env를 불러 실행: `set -a; source .env; set +a; .venv/bin/python -m soopts ...`

## 실행 순서

1. **대상 확인.** 인자가 없으면 manual 목록:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts vods --status manual --json
   ```

2. **전체 전사.** (파트별 다운로드 → Whisper 전체 전사, 재개 가능. 긴 VOD는 수십 분.)
   ```bash
   set -a; source .env; set +a; .venv/bin/python -u scripts/analyze_vod.py <title_no>
   ```
   결과: `work/{title_no}/transcript.txt` (전역 `[H:MM:SS]` 타임스탬프).

3. **전사문 읽고 솔로 풀곡 판정.** `transcript.txt`를 Read로 읽어 **BJ가 혼자 처음부터 끝까지
   부른 곡**만 고른다:
   - 포함: 흐르는 가사가 이어지는 실제 노래 구간.
   - 제외: 게임 대화·잡담, 인트로/대기화면 BGM("이 영상에서 만나요" 류), 합창·따라부르기·게스트
     공연, 잠깐 흥얼거림. (서버의 🎤-only 기준과 동일 — 🎵는 대상 아님.)
   - 각 곡의 시작/끝 시각(초)과 가사로 추정한 제목/가수를 정한다.

4. **spans 작성 후 ingest.**
   ```bash
   cat > /tmp/spans_<title_no>.json <<'JSON'
   {"songs": [{"start_s": 1023, "end_s": 1245, "title": "...", "artist": "...", "lyrics": "..."}]}
   JSON
   set -a; source .env; set +a; .venv/bin/python -m soopts ingest <title_no> /tmp/spans_<title_no>.json
   ```
   ingest는 카탈로그 매칭 후 기록하고 status를 analyzed로 승격한다(멱등).

5. **정리.** 처리 후 큰 캐시를 지운다(진실은 DB):
   ```bash
   rm -f work/<title_no>/clips/*.mp4 work/<title_no>/clips/*.wav
   ```

## 규칙
- **BJ가 혼자 부른 풀곡만** 기록. 게임·BGM·합창·게스트·부분 흥얼거림 제외.
- **기록 전 곡 목록(시각·제목)을 사용자에게 보여주고 승인받는다.**
- 곡을 하나도 못 찾으면(순수 게임 방송) ingest하지 말고 그대로 둔다 — 억지로 기록 금지.
  (게임 방송 확정이면 사용자 확인 후 done 처리 여부를 물어본다.)
- 전사·다운로드 실패 VOD는 건너뛰고 표에 남긴다.
