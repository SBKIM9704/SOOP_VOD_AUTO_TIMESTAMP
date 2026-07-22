---
name: perf-review
description: performances를 로컬에서 전면 검증·보강 — 구간을 다시 전사해 진짜 노래인지·BJ가 부른 건지·시간이 맞는지 확인하고, 가사·제목·song_id를 채운 뒤 local_review 상태를 기록. "performance 검증", "needs_review 재분석", "곡 데이터 정밀 처리", "/perf-review" 호출.
allowed-tools: Bash
metadata:
  argument-hint: "[--local pending|--identify needs_review] (기본: local_review=pending 전체)"
---

# performance 로컬 정밀 검증·보강 스킬 (perf-review)

DB의 `performances`를 **로컬에서 한 번 더 검증·보강**한다. 각 곡의 `(vod, start_s, end_s)` 구간을
다시 전사해 ①진짜 노래인지 ②BJ가 혼자 부른 건지 ③시작/끝 시간이 맞는지 확인하고, 정확한
가사·제목·`song_id`를 채운 뒤 검증 상태(`local_review`)를 기록한다. 판정은 Claude가 전사문으로,
DB I/O는 `soopts` 프리미티브가 담당한다.

- `identify_status` = 식별 축(어떤 곡인가: auto_matched/needs_review/confirmed)
- `local_review` = **검증 축**(이 스킬이 채움): `pending`(미검증) / `verified`(로컬 확인 완료) / `needs_human`(사람 직접)

## 인자
`$ARGUMENTS` — 없으면 `--local pending` 전체. `--identify needs_review`로 미매칭만, 특정 대상 지정 가능.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`(+`_2`/`_3` 선택).
실행: `set -a; source .env; set +a; .venv/bin/python -m soopts ...`

## 실행 순서

1. **대상 목록.**
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts perfs --local pending --json
   ```
   각 항목: `id, title_no, start_s, end_s, title_guess, song_id, identify_status, local_review, has_lyrics`.

2. **곡별 구간 전사.** (구간만 받아 빠름. 제목이 영어권이면 `--lang en`.)
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts transcribe <title_no> --start <start_s> --end <end_s>
   ```

3. **Claude 검증 3종 + 식별.** 전사문을 읽고:
   - **① 진짜 노래?** 흐르는 가사가 이어짐(게임 대화·BGM·잡담 아님).
   - **② BJ 솔로?** 단일 목소리·라이브 느낌(합창·게스트·스튜디오 음질=튼 것 아님). 애매하면 needs_human.
   - **③ 시간 정확?** 노래가 `[start_s, end_s]`를 채우는가. 어긋나면 실제 경계로 보정값 계산.
   - **식별:** 가사로 제목/가수를 정하고 카탈로그 매칭 확인:
     ```bash
     set -a; source .env; set +a; .venv/bin/python -m soopts match-song --title "<제목>" --artist "<가수>" --lyrics "<가사일부>"
     ```
     - `song_id` 나옴 → 카탈로그에 있음.
     - `song_id: null` → 신곡. draft로 등록:
       ```bash
       set -a; source .env; set +a; .venv/bin/python -m soopts add-song --title "<제목>" --artist "<가수>" --lyrics "<가사>"
       # 출력된 uuid가 song_id
       ```

4. **적용(set-perf).** 검증·보강 결과를 반영:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts set-perf <id> \
     --lyrics "<정확한 가사>" --title-guess "<제목>" --song-id <song_id> \
     --identify-status auto_matched --local-review verified \
     --start-s <보정시작> --end-s <보정끝>
   ```
   - **3종 통과 + 식별됨** → `--local-review verified` (+ song_id, identify-status auto_matched)
   - **노래 아님 / BJ 불확실 / 식별 불가** → `--local-review needs_human` (사람이 직접 처리)
   - 바꿀 필드만 보낸다(안 보낸 필드는 유지).

## 규칙
- **전사문을 실제로 읽고 판정하라** — title_guess만 믿지 말 것(그게 틀렸을 수 있어 재검증하는 것).
- **애매하면 `verified` 주지 말고 `needs_human`으로.** 특히 "BJ 본인 vs 게스트/튼음악"이 오디오로
  불확실하면 사람에게 넘긴다(확정은 화면이 있어야 하는 경우가 있음).
- **draft 신곡은 실제로 카탈로그에 없을 때만** 만든다 — match-song으로 먼저 확인(중복 방지).
- **대량 처리 시 사용자에게 진행/결과 요약을 보여준다.** 파괴적 판단(needs_human·경계 보정)은
  근거(가사 인용)를 남긴다.
- 전사 실패한 곡은 건너뛰고(그대로 pending) 표에 남긴다.
- 처리 후 큰 캐시 정리: `rm -f work/*/clips/seg_*.mp4`
