# 단계: ingest — manual VOD 전체 전사 → 솔로 풀곡 기록

`daily` 러너가 🎤 타임라인을 못 읽어 `vods.status='manual'`로 남긴 VOD(무-타임라인, 순수 게임,
전-🎵 콘서트, 옛 아이콘 없는 형식 등), 또는 audit이 `to-manual`로 되돌린 VOD를 처리한다.
**로컬에서 VOD 전체를 전사**해 **BJ가 혼자 부른 풀곡**만 찾아 `soopts ingest`로 DB에 기록한다.

> 세 단계 중 가장 무겁다 — 파트 전체 다운로드 + Whisper 전체 전사(긴 VOD는 수십 분, GROQ 키
> 로테이션). 반드시 이 단계가 필요한 VOD에만 돌린다.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`(+`_2`/`_3` 로테이션 선택).
**`analyze_vod.py`는 `PATH`에 `.venv/bin`을 넣어 실행한다** — 파트 다운로드에 `yt-dlp`를 부르는데
이 repo는 `yt-dlp`를 `.venv/bin`에만 두므로 `PATH`를 안 맞추면 `FileNotFoundError`로 다운로드가
조용히 실패한다. 프리앰블: `set -a; source .env; set +a; export PATH="$PWD/.venv/bin:$PATH"`.

## 실행 순서

1. **대상 확인.** (라우터가 이미 받았으면 재사용.)
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts vods --status manual --json
   ```

2. **전체 전사.** 파트별 다운로드 → Whisper 전체 전사(재개 가능, `work/`에 캐시):
   ```bash
   set -a; source .env; set +a; .venv/bin/python -u scripts/analyze_vod.py <title_no>
   ```
   결과: `work/{title_no}/transcript.txt` (전역 `[H:MM:SS]` 타임스탬프).

3. **전사문 읽고 솔로 풀곡 판정.** `transcript.txt`를 **Read로 읽어**, 공유 판정 규칙(SKILL.md)으로
   **BJ가 혼자 처음부터 끝까지 부른 곡**만 고른다. 각 곡의 시작/끝 시각(초)과 가사로 추정한
   제목/가수를 정한다.
   - 게임 대화·BGM·합창·게스트·크루·잠깐 흥얼거림은 제외(공유 규칙 그대로).
   - 문맥으로 구분: 인트로/대기화면 BGM("이 영상에서 만나요" 류)은 노래가 아니다.

4. **spans 작성 후 ingest.**
   ```bash
   cat > /tmp/spans_<title_no>.json <<'JSON'
   {"songs": [{"start_s": 1023, "end_s": 1245, "title": "...", "artist": "...", "lyrics": "..."}]}
   JSON
   set -a; source .env; set +a; .venv/bin/python -m soopts ingest <title_no> /tmp/spans_<title_no>.json
   ```
   ingest는 카탈로그 매칭 후 `performances`를 기록하고 status를 `analyzed`로 승격한다(멱등).
   미매칭 제목은 `needs_review`로 남아 이후 perf 단계/리뷰 UI에서 해소된다.

5. **정리.** 처리 후 큰 캐시 삭제(진실은 DB):
   ```bash
   rm -f work/<title_no>/clips/*.mp4 work/<title_no>/clips/*.wav
   ```

## 규칙 (공유 규칙에 더해)
- **기록 전 곡 목록(시각·제목)을 사용자에게 보여주고 승인받는다.**
- **곡을 하나도 못 찾으면(순수 게임 방송) ingest하지 말고 그대로 둔다** — 억지로 기록 금지.
  게임 방송 확정이면 사용자 확인 후 `done` 처리 여부를 물어본다.
- 전사·다운로드 실패 VOD는 건너뛰고 표에 남긴다.
- ingest로 만들어진 performance는 `local_review=pending`이므로, 이어서 **perf 단계**로 검증·보강한다.
