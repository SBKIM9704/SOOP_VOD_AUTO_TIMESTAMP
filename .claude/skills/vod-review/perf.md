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
   set -a; source .env; set +a; .venv/bin/python -m soopts transcribe <title_no> \
     --start <start_s> --end <end_s> --pad 45 --segments
   ```
   `--segments`는 `[{start,end,text}]`(VOD 절대초)를 출력한다 — 경계 판정에 필수다.

   **`--pad`는 넉넉히(45s) 준다.** 경계의 기준점은 곡을 둘러싼 **말**인데(③ 참조), 기본 15s로는
   그 말이 창 밖으로 나가 기준점 자체가 사라진다 — 실측에서 곡 시작 16초 전에 끝난 멘트가
   기본 pad에서는 보이지 않았다.

3. **Claude 검증 3종 + 식별.** 전사문을 읽고(공유 판정 규칙 SKILL.md 참조):
   - **① 진짜 노래?** 흐르는 가사가 이어짐(게임 대화·BGM·잡담 아님).
   - **② BJ 솔로?** 단일 목소리·라이브 느낌(합창·게스트·크루·스튜디오 음질=튼 것 아님). 애매하면 needs_human.
   - **③ 경계 정확?** 노래를 **둘러싼 "말"을 기준점으로** 잡는다:
     ```
     시작 = max(🎤 타임라인 시각 − 2s,  직전 발화 세그먼트 끝 + 1s)
     종료 = 마지막 가사 이후 첫 발화 세그먼트 시작 − 1s   (뒤에 말이 없으면 마지막 가사 끝 + 10s)
     ```
     **가사에 맞춰 자르지 마라.** Whisper는 가사만 받아쓰므로 반주 전주·아웃트로는 전사문에
     아예 안 보인다. 예전 규칙("종료 = 마지막 가사 + 2s")은 그래서 아웃트로를 통째로 잘랐고,
     실측 4곡에서 11~18초가 날아갔다(합본 영상에서 곡이 끝나기 전에 끊김).

     **시작의 `max`가 핵심이다.** 직전 발화가 끝난 지점이 곡 시작은 아니다 — 사이에 무음·MR
     로딩·마이크 준비가 들어가고 그 길이가 곡마다 7~29초로 들쭉날쭉하다. 팬 🎤 타임스탬프가
     그 안에서 실제 곡 시작(전주 포함)을 짚어준다. 반대로 팬이 곡 소개 멘트 시작점을 찍어
     타임라인이 **이른** 경우도 있어, 늦은 쪽을 취하면 양방향으로 교정된다.

     **🎤 시각은 `start_s`가 아니라 댓글에서 다시 읽는다** — 팬이 배치 실행 이후 타임라인을
     다듬는 일이 있어 DB 값과 어긋난다(실측: `start_s` 7435 대 댓글 7419, 7870 대 7846).
     ```bash
     set -a; source .env; set +a; .venv/bin/python -m soopts comments <title_no> --json
     ```

     **당기기·늘리기 양방향 모두 허용한다.** 트림 전용이던 예전 규칙으로는 이미 짧은 구간을
     되돌릴 수 없었다 — 첫 소절 10초가 잘린 채 업로드된 곡이 실제로 있었다(VOD 201651295).

     **"말"과 "가사"는 정규식으로 못 가른다 — 전사문을 읽고 문맥으로 판정하라.** 아웃트로
     떼창 `"띵띵"`(노래의 일부)과 BJ 인사 `"띵큐"`(노래 끝남)는 글자만 보면 거의 같다.
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
- **경계는 노래를 둘러싼 "말"로 잡는다**(③ 참조) — 앞은 `max(🎤 시각, 직전 발화 끝)`, 뒤는
  다음 발화 직전. 반주 전주·아웃트로를 살리되 멘트·잡담·다음 곡은 넣지 않는다. 당기기·늘리기
  모두 가능하다. 비정상적으로 짧아지면(구간 <30s) 보류하고 사람 확인.
- **대량 처리 시 진행/결과 요약을 보여준다.** 파괴적 판단(needs_human·경계 보정)은 가사 인용 근거를 남긴다.
- 전사 실패한 곡은 건너뛰고(그대로 pending) 표에 남긴다.
- 처리 후 캐시 정리: `rm -f work/*/clips/seg_*.mp4`
