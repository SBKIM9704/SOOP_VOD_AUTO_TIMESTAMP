# 단계: perf — performance 구간 재전사로 검증·보강

이미 기록된 `performances`(daily 자동매칭 또는 ingest가 만든 것)를 **로컬에서 한 번 더 검증·보강**
한다. 각 곡의 `(vod, start_s, end_s)` 구간만 다시 전사해 ①진짜 노래인지 ②BJ가 혼자 불렀는지
③시작/끝이 맞는지 확인하고, 정확한 가사·제목·`song_id`를 채운 뒤 `local_review`를 기록한다.
구간만 받으므로 ingest보다 가볍다(곡당 분 단위).

## 두 축 — local_review로 일하고, needs_review는 사람 escalation으로만
- **`local_review`** (이 단계의 작업 기준): `pending`(미검토) → `verified`(로컬 검토 완료).
  이 단계는 `pending`을 돌며 검토하고, **끝나면 항상 `verified`로** 만든다(=검토했다는 사실).
- **`identify_status`** (식별 + escalation): `auto_matched`(해결·연결됨) / `confirmed`(사람 확정,
  건드리지 않음) / **`needs_review`**(로컬 검토 후에도 사람이 봐야 할 소수 — 애매/식별불가).
  needs_review는 기본값이 아니라 검토하고도 남는 소수에만 붙이는 escalation 플래그다.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`(+`_2`/`_3` 선택).
**전사 명령은 `PATH`에 `.venv/bin`을 넣어 실행한다** — `transcribe`가 `yt-dlp`를 subprocess로
부르는데 이 repo는 `yt-dlp`를 `.venv/bin`에만 두므로, `PATH`를 안 맞추면 `FileNotFoundError`로
**조용히 빈 전사**가 나온다(에러 안 뜸). 프리앰블: `set -a; source .env; set +a; export PATH="$PWD/.venv/bin:$PATH"`.

## 실행 순서

1. **대상 목록.** (라우터가 이미 받았으면 재사용.)
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

3. **Claude 검증 3종 + 식별.** 전사문을 읽고(공유 판정 규칙 SKILL.md 참조):
   - **① 진짜 노래?** 흐르는 가사가 이어짐(게임 대화·BGM·잡담 아님).
   - **② BJ 솔로?** 단일 목소리·라이브 느낌(합창·게스트·크루·스튜디오 음질=튼 것 아님). 애매하면 needs_human.
   - **③ 시간 정확?** `--segments`로 tail을 읽어 **가사가 실제로 끝나는 세그먼트**를 찾는다.
     서버 `end_s`는 "다음 곡 시작(없으면 +360s 캡)"이라 노래 뒤 방종 멘트("오늘의 방종곡
     XXX였습니다 땡큐~")·잡담이 섞여 대개 **길다**. 실제 종료 = 마지막 가사 세그먼트 끝(+2s 여유).
     시작은 🎤 타임스탬프라 신뢰 → **종료만 당긴다(트림 전용, 늘리지 않음)**.
     키워드 없는 잡담 tail도 있으니 **정규식 말고 tail을 직접 읽어** 판정하라.
   - **식별:** 가사로 제목/가수를 정하고 카탈로그 매칭 확인:
     ```bash
     set -a; source .env; set +a; .venv/bin/python -m soopts match-song --title "<제목>" --artist "<가수>" --lyrics "<가사일부>"
     ```
     - `song_id` 나옴 → 카탈로그에 있음.
     - `song_id: null` → 신곡. **실제로 없을 때만** draft 등록(match-song으로 먼저 확인, 중복 방지):
       ```bash
       set -a; source .env; set +a; .venv/bin/python -m soopts add-song --title "<제목>" --artist "<가수>" --lyrics "<가사>"
       # 출력된 uuid가 song_id
       ```
   - **⚠️ song_id가 이미 있어도(auto_matched) 믿지 말고 교차검증하라.** 연결된
     `songs.title`/`artist`가 **방금 전사한 가사와 실제로 일치하는지** 확인한다 — 기존 링크가
     엉뚱한 곡을 가리켜도 "식별됨"처럼 보인다. 불일치면 `match-song`으로 재식별해 song_id를
     재연결(또는 draft). 이 라벨은 유튜브 오버레이가 **영상 픽셀에 구워져** 사후 수정이 재업로드
     밖에 없으므로(업로드 예정 VOD는 특히) 제목 정확성을 최우선으로 검증한다.
     - **표기 관례 주의(오탐 방지):** 커버버전 vs 원곡자 아티스트 차이는 오류가 아니다 —
       예: BJ가 뷰렛 커버를 불러도 카탈로그는 원곡자 `S.E.S.`로 뜬다. 또 곡명과 OST명이 겹치면
       혼동하기 쉽다(예: `오렌지`는 *4월은 너의 거짓말* OST라 "거짓말"로 오인 금지). 가사로 판단.

4. **적용(set-perf).** 검토했으면 **항상 `--local-review verified`**, `identify-status`는 결과에 따라:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts set-perf <id> \
     --lyrics "<정확한 가사>" --title-guess "<제목>" --song-id <song_id> \
     --identify-status auto_matched --local-review verified \
     --start-s <보정시작> --end-s <보정끝>
   ```
   - **확실히 해결**(진짜 노래 + BJ + 시간 OK + 식별/draft 연결) → `--identify-status auto_matched --local-review verified` (+ song_id).
   - **로컬 검토했지만 사람이 봐야 함**(노래 아님 의심 / BJ 불확실 / 끝내 식별 불가) →
     `--identify-status needs_review --local-review verified`.
   - **바꿀 필드만 보낸다**(안 보낸 필드는 유지). 이미 `confirmed`인 건 건드리지 않는다.

## 규칙 (공유 규칙에 더해)
- **전사문을 실제로 읽고 판정하라** — title_guess만 믿지 말 것(그게 틀렸을 수 있어 재검증하는 것).
- **song_id가 연결돼 있어도 그 곡이 맞는지 가사로 확인하라** — auto_matched는 "식별 시도됨"일 뿐
  정답 보장이 아니다. 틀린 song_id가 유튜브 오버레이/설명에 그대로 박히고, 오버레이는 재업로드로만
  고칠 수 있다(삭제/수정 API 없음).
- **검토했으면 `local_review=verified`.** 사람 손이 필요한 경우만 `identify_status=needs_review`로 표시.
- **종료는 노래 실제 끝까지만** — 방종 멘트·잡담·다음 곡 인트로 제외. `end_s`는 당기기만(트림 전용).
  비정상적으로 짧아지면(구간 <30s) 보류하고 사람 확인.
- **대량 처리 시 진행/결과 요약을 보여준다.** 파괴적 판단(needs_human·경계 보정)은 가사 인용 근거를 남긴다.
- 전사 실패한 곡은 건너뛰고(그대로 pending) 표에 남긴다.
- 처리 후 캐시 정리: `rm -f work/*/clips/seg_*.mp4`
