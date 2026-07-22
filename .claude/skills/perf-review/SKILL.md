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

**두 축 — local_review로 일하고, needs_review는 사람 escalation으로만:**
- **`local_review`** (이 스킬의 작업 기준): `pending`(미검토) → `verified`(로컬 검토 완료). 이 스킬은
  `pending`을 돌며 검토하고, 끝나면 **항상 `verified`로** 만든다(=검토했다는 사실).
- **`identify_status`** (식별 + 사람 escalation): `auto_matched`(해결·연결됨, 사람 불필요) /
  `confirmed`(사람 확정) / **`needs_review`**(로컬 검토 후에도 **사람이 봐야 할 때만** — 애매/식별불가).
  즉 needs_review는 기본값이 아니라, 검토하고도 남는 소수에만 붙이는 escalation 플래그다.

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
   # 종료 경계를 볼 땐 세그먼트별 타임스탬프로:
   set -a; source .env; set +a; .venv/bin/python -m soopts transcribe <title_no> --start <start_s> --end <end_s> --segments
   ```
   `--segments`는 `[{start,end,text}]`(VOD 절대초)를 출력한다 — 가사가 끝나고 아웃트로 잡담이
   시작되는 시각을 찾는 용도.

3. **Claude 검증 3종 + 식별.** 전사문을 읽고:
   - **① 진짜 노래?** 흐르는 가사가 이어짐(게임 대화·BGM·잡담 아님).
   - **② BJ 솔로?** 단일 목소리·라이브 느낌(합창·게스트·스튜디오 음질=튼 것 아님). 애매하면 needs_human.
   - **③ 시간 정확?** `--segments`로 tail을 읽어 **노래 가사가 실제로 끝나는 세그먼트**를 찾는다.
     서버가 잡은 `end_s`는 "다음 곡 시작(없으면 +360s 캡)"이라 노래 뒤 BJ 방종 멘트("오늘의
     방종곡 XXX였습니다 땡큐~")·잡담이 섞여 대개 **길다**. 실제 종료 = 마지막 가사 세그먼트 끝
     (+2s 여유). 시작은 🎤 타임스탬프라 신뢰 → **종료만 당긴다(트림 전용, 늘리지 않음)**.
     키워드("땡큐/였습니다")가 없는 잡담 tail도 있으니 **정규식 말고 tail을 직접 읽어** 판정하라.
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

4. **적용(set-perf).** 검토했으면 **항상 `--local-review verified`** 로 만들고, `identify-status`는
   결과에 따라 정한다:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts set-perf <id> \
     --lyrics "<정확한 가사>" --title-guess "<제목>" --song-id <song_id> \
     --identify-status auto_matched --local-review verified \
     --start-s <보정시작> --end-s <보정끝>
   ```
   - **확실히 해결**(진짜 노래 + BJ + 시간 OK + 식별/draft 연결) → `--identify-status auto_matched`
     `--local-review verified` (+ song_id).
   - **로컬 검토했지만 사람이 봐야 함**(노래 아님 의심 / BJ 불확실 / 끝내 식별 불가) →
     `--identify-status needs_review --local-review verified`. needs_review는 이런 소수에만.
   - 바꿀 필드만 보낸다(안 보낸 필드는 유지). 이미 `confirmed`인 건 건드리지 않는다.

## 규칙
- **전사문을 실제로 읽고 판정하라** — title_guess만 믿지 말 것(그게 틀렸을 수 있어 재검증하는 것).
- **검토했으면 `local_review=verified`** — 검토 자체는 끝난 것이므로. 사람 손이 필요한 경우는
  `identify_status=needs_review`로만 표시한다(애매/식별불가/BJ 불확실 등 소수).
- **종료는 노래 실제 끝까지만** — BJ 방종 멘트·잡담·다음 곡 인트로는 제외. `end_s`는 당기기만
  하고(트림 전용) 늘리지 않는다(시작 신뢰). 비정상적으로 짧아지면(구간 <30s) 보류하고 사람 확인.
- **draft 신곡은 실제로 카탈로그에 없을 때만** 만든다 — match-song으로 먼저 확인(중복 방지).
- **대량 처리 시 사용자에게 진행/결과 요약을 보여준다.** 파괴적 판단(needs_human·경계 보정)은
  근거(가사 인용)를 남긴다.
- 전사 실패한 곡은 건너뛰고(그대로 pending) 표에 남긴다.
- 처리 후 큰 캐시 정리: `rm -f work/*/clips/seg_*.mp4`
