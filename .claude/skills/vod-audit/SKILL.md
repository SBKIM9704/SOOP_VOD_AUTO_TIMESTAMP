---
name: vod-audit
description: Supabase에 기록된 VOD를 원본 댓글과 대조해 노래 타임라인 분류·처리 결과를 Claude가 직접 검증. "VOD 검증", "댓글 확인해서 정리", "잘못 처리된 영상 찾아줘", "/vod-audit [title_no...]" 호출.
allowed-tools: Bash
metadata:
  argument-hint: "[title_no ...] (없으면 처리된 전체 감사)"
---

# VOD 감사 스킬 (vod-audit)

Supabase에 처리 완료로 기록된 VOD를, **원본 댓글을 직접 읽어** 노래 타임라인 분류가 맞았는지
검증한다. 코드(Groq gpt-oss)가 "게임 타임라인 vs 노래 타임라인", "티저 언급 vs 실제 부름"을
뭉개는 문제를 **Claude의 판단으로** 대체하는 로컬 감사 경로다. 판정은 Claude가, DB/댓글 I/O와
적용은 `soopts` 명령이 담당한다.

> **범위**: 이건 사람이 로컬에서 돌리는 감사다. 무인 `daily` 러너는 헤드리스라 스킬을 못 쓰므로
> 자동 파이프라인의 타임라인 판정은 여전히 코드(`extract_song_timeline`)로 남는다. 이 스킬은
> 그 코드 판정을 사후 검증/교정한다.

## 인자
`$ARGUMENTS` — 검증할 title_no 목록(공백 구분). 비어 있으면 처리된 전체(analyzed/done)를 감사.

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`가 있어야 한다(댓글 API는 공개라 Groq 불요).
모든 `soopts` 호출은 한 줄에서 env를 불러 실행한다:

```bash
set -a; source .env; set +a; .venv/bin/python -m soopts <subcommand> ...
```

## 실행 순서

1. **대상 목록 확보.** 인자가 없으면 처리된 전체를 JSON으로 받는다:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts vods --status analyzed,done --json
   ```
   각 항목: `title_no, status, title, machine_perfs, confirmed_perfs`. 인자가 있으면 그 목록만 감사.

2. **VOD별 원본 댓글 확보.** 감사할 각 title_no에 대해:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts comments <title_no> --json
   ```

3. **Claude가 직접 판정.** 절대 개수만 보고 판단하지 말고 **원본 댓글 본문을 읽는다.** 다음을 구분:
   - **진짜 노래 타임라인** — BJ가 실제로 노래를 부른 곡별 타임스탬프(예: `01:23:45 🎵 이승철 - 다시 사랑한다면`). → 서버 처리가 맞음, 유지.
   - **게임/잡담 타임라인** — 게임명·리액션·주제만 나열(예: `마녀의집`, `히든기믹`, `금도끼줄까`). 노래 항목 0. → 노래 타임라인 아님.
   - **티저/예고 언급** — 미래 발매·티저 얘기(예: `30일에 솔로곡 '어린 나' 티저`)는 이 방송에서 부른 게 아님. → 노래 아님.
   - **타임라인 자체 없음** — 댓글이 적거나 타임라인 댓글이 없음.
   - **machine_perfs 교차검증**: full_sweep가 곡을 잡았고(machine_perfs>0) 그게 실제 부른 곡으로 보이면 **그건 올바른 감지다 — 팬 타임라인이 없다는 이유만으로 지우지 말 것.**

4. **근거표 제시.** 각 VOD에 대해: `title_no · status · 판정(+원본 댓글 인용 근거) · machine/confirmed · 권고`.
   권고는 다음 중 하나:
   - **keep** — 분류/처리가 맞음. 변경 없음.
   - **to-manual** — 노래를 놓쳤거나 잘못 추측했고, 로컬에서 analyze_vod.py 전체 전사로 다시 처리해야 함.
     (기계곡을 지우고 status를 manual로 되돌림 → 이후 analyze_vod.py+`soopts ingest`로 재기록)
   - **investigate** — 애매함. 사람 확인 필요.

5. **승인 후에만 적용.** 파괴적 변경(기계곡 삭제 + status 변경)은 반드시 사용자 승인을 받는다.
   승인된 VOD에만:
   ```bash
   set -a; source .env; set +a; .venv/bin/python -m soopts set-manual <title_no> --clear-machine
   ```
   (`--clear-machine` 없이 호출하면 기계곡은 두고 status만 manual로 바꾼다. `confirmed` 곡은 항상 보존.)

## 규칙
- **개수·상태값만 믿지 말고 원본 댓글을 읽어라.** 이 스킬의 존재 이유가 그거다.
- **모든 권고에 원본 댓글 근거를 붙여라** — 왜 노래/게임/티저로 봤는지 인용.
- **올바른 full_sweep 감지를 지우지 마라.** machine_perfs가 실제 부른 곡이면 유지가 기본.
- **파괴적 적용은 사용자 승인 없이는 절대 하지 마라.** dry-run처럼 표부터 보여주고 확인받는다.
- 댓글 조회가 실패한 VOD는 건너뛰고 그 사실을 표에 남긴다(임의 판정 금지).
