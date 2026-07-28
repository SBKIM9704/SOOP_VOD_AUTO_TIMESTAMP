# 단계: audit — 처리된 VOD 감사 (댓글만, 미디어 없음)

`daily` 러너는 **🎤 마커만** 세는 정규식 파서(`parse_song_timeline`)로 처리한다. 이 규칙은
**오탐이 적은 대신 구조적으로 놓치는 것**이 있다. audit은 처리 완료(`analyzed`/`done`) VOD를
**원본 댓글과 직접 대조**해 그 누락·오탐·오분류를 잡는다. 전사도 다운로드도 없다 — 댓글만 읽는다.

> 이건 코드로 대체할 수 없어 스킬로 둔 backstop이다(CLAUDE.md 참고). 자동 판정·삭제하는
> 코드 경로(`recheck`)를 다시 만들지 말 것 — 판정이 사람 판단 영역이라 여기 있는 것이다.

## 이 단계가 잡는 것 (🎤-only 규칙의 빈틈)

1. **놓친 곡 (false negative).** `🎵`/`🎶` 또는 아이콘 없는 줄 중 **실제로는 BJ가 혼자 부른 풀곡**이
   섞여 있는 경우(팬이 아이콘을 잘못 달았거나 옛 형식). 🎤+🎵 혼합 VOD는 🎤만 기록되고 나머지는
   조용히 누락된다 → 진짜 솔로곡이 보이면 **to-manual**로 되돌려 전체 전사로 복구.
2. **크루곡 오탐 (false positive).** `_CREW_NAMES`에 아직 없는 새 크루의 그룹곡이 🎤로 새어 들어와
   기록된 경우. 아티스트 슬롯/`[태그]`에 크루명이 있으면 그룹 공연 → 잘못 기록된 것.
   (새 크루는 등록 전까지 누수되며, 그 backstop이 바로 이 단계다.)
3. **오분류.** 게임/잡담만 있는 타임라인, 티저·예고 언급(미래 발매곡은 이 방송에서 부른 게 아님)을
   노래로 오인했는지.

## 실행 순서

1. **대상 목록.** (라우터가 이미 받았으면 재사용.)
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts vods --status analyzed,done --json
   ```
   각 항목: `title_no, status, title, machine_perfs, confirmed_perfs`.

2. **VOD별 원본 댓글.** 감사할 각 title_no에 대해:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts comments <title_no> --json
   ```

3. **Claude 판정.** 댓글 본문을 읽고, 공유 판정 규칙(SKILL.md)으로 각 줄을 분류한다:
   - **진짜 노래 타임라인** — 🎤 곡별 타임스탬프가 실제 부른 곡. → 서버 처리 맞음.
   - **놓친 솔로곡** — 🎵/아이콘 없는 줄이 실제 BJ 솔로 풀곡. → 누락.
   - **크루곡 오탐** — 기록된 🎤가 그룹/크루 공연. → 잘못 기록.
   - **게임/잡담** — 게임명·주제만(`마녀의집`·`히든기믹` 등), 노래 0.
   - **티저/예고** — 미래 발매·티저 언급(`30일에 솔로곡 티저`)은 부른 게 아님.
   - **machine_perfs 교차검증:** 기록된 곡(machine_perfs>0)이 실제 부른 곡으로 보이면 **keep이 기본.**
     팬 타임라인 형식이 달라졌다는 이유만으로 지우지 말 것.

4. **근거표.** VOD별로: `title_no · status · 판정(+댓글 인용 근거) · machine/confirmed · 권고`.
   권고는:
   - **keep** — 분류·처리 맞음. 변경 없음.
   - **to-manual** — 곡을 놓쳤거나 크루곡을 잘못 기록. 로컬 전체 전사로 다시 처리해야 함.
   - **investigate** — 애매. 사람 확인 필요.

5. **승인 후에만 적용.** `to-manual` 승인된 VOD에만:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts set-manual <title_no> --clear-machine
   ```
   (`--clear-machine`은 기계 생성 performances를 삭제하고 status를 manual로 되돌린다.
   생략하면 기계곡은 두고 status만 바꾼다. `confirmed`는 항상 보존.)
   → 이후 이 VOD는 큐에서 **ingest 단계** 대상이 된다(전체 전사로 재기록).

## 규칙 (공유 규칙에 더해)
- **올바른 감지를 지우지 마라.** machine_perfs가 실제 부른 곡이면 keep이 기본.
- **모든 권고에 원본 댓글 근거를 인용하라.**
- 댓글 조회 실패 VOD는 건너뛰고 표에 남긴다(임의 판정 금지).
