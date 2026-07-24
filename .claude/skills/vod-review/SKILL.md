---
name: vod-review
description: daily 러너가 처리한 결과를 로컬에서 사람 판단으로 마감하는 통합 검토 콘솔. 큐 상태를 보여주고 VOD/performance 상태에 따라 감사·전체전사·구간검증 단계로 라우팅. "VOD 검토", "밀린 거 처리", "manual 처리", "performance 검증", "댓글 대조 감사", "/vod-review [title_no...]" 호출.
allowed-tools: Bash, Read
metadata:
  argument-hint: "[title_no ...] 또는 [--stage audit|ingest|perf] (없으면 큐 대시보드부터)"
---

# VOD 로컬 검토 콘솔 (vod-review)

무인 `daily` 러너는 댓글 타임라인의 **🎤(BJ 솔로곡)** 마커만 싸게 파싱해 DB에 기록하고, 나머지는
사람에게 넘긴다(헤드리스라 스킬을 못 씀). 이 스킬은 그 **로컬 사람-판단 단계 전체를 하나의 입구로**
묶는다. 큐 상태를 보여주고, 각 VOD/performance가 상태 머신 어디에 있는지 보고 알맞은 단계로 보낸다.

**설계 원칙(세 단계 공통): 코드가 I/O, Claude가 판정.** 다운로드·전사·DB 쓰기는 `soopts`/
`analyze_vod.py`가, "진짜 노래인가·BJ가 혼자 불렀나·경계가 맞나"는 Claude가 결정한다.

## 세 단계 = 하나의 파이프라인

상태 머신(`vods.status` / `performances.identify_status` / `performances.local_review`) 위에서 흐른다:

```
daily 러너 결과
 ├─ status=manual (🎤 없음)        → [ingest]   전체 전사 → 솔로 풀곡 → ingest → analyzed
 │                                                 ↓ (perf 생성, local_review=pending)
 └─ status=analyzed/done (🎤 있음)  → [audit]    댓글 대조: 놓친 곡·오탐·오분류 없나?
                                       │ keep ────↓
                                       └ to-manual → (manual 큐로 반환 → ingest)
                                                     ↓
 performances.local_review=pending   → [perf]    구간 재전사 → 검증·보강 → verified
```

비용 프로파일이 크게 다르므로(단계별로 문을 나눠 둔 이유) **싼 것부터**가 기본 순서다:

| 단계 | 미디어 | 전사 범위 | 대략 비용 |
|---|---|---|---|
| **audit** | 없음(댓글만) | 없음 | 초 단위 |
| **perf** | 구간만 | span | 분 단위 |
| **ingest** | 파트 전체 | VOD 전체 | 수십 분, GROQ 키 로테이션 |

## 사전 준비
`.env`에 `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`(audit는 이것만으로 충분 — 댓글 API는 공개).
`ingest`/`perf`는 전사를 하므로 `GROQ_API_KEY`(+`_2`/`_3` 로테이션 선택)도 필요.
모든 명령은 한 줄에서 env를 불러 실행한다:

```bash
set -a; source .env; set +a; .venv/bin/python -m soopts <subcommand> ...
```

**`ingest`/`perf`는 `PATH`에 `.venv/bin`을 넣어야 한다.** 전사는 세그먼트 다운로드에 `yt-dlp`를
subprocess로 부르는데, 이 repo는 `yt-dlp`를 `.venv/bin`에만 두고 시스템 `PATH`엔 두지 않는다.
`PATH`를 안 맞추면 `FileNotFoundError: yt-dlp`로 **조용히 빈 전사**가 나온다(에러가 표에 안 뜸).
그래서 전사 단계의 명령은 다음 프리앰블로 실행한다(audit는 전사가 없어 불필요):

```bash
set -a; source .env; set +a; export PATH="$PWD/.venv/bin:$PATH"; .venv/bin/python -m soopts <subcommand> ...
```

## 실행 순서 (라우터)

1. **큐 대시보드.** 인자가 없으면 세 상태를 한 번에 집계해 보여준다:
   ```bash
   set -a; source .env; set +a
   .venv/bin/python -m soopts vods --status manual --json          # ingest 대상
   .venv/bin/python -m soopts vods --status analyzed,done --json   # audit 대상
   .venv/bin/python -m soopts perfs --local pending --json         # perf 대상
   ```
   `manual N · 미검토 analyzed/done M · local_review=pending K`로 요약한다.

2. **라우팅.**
   - **인자에 `--stage audit|ingest|perf`** → 그 단계만 실행.
   - **인자에 title_no 목록** → 각 VOD의 `status`를 보고: `manual`→ingest, `analyzed/done`→audit.
   - **인자 없음** → 대시보드를 보여주고, **싼 단계부터** 권한다(audit → 남은 perf → 필요 시 ingest).
     한 번에 전체를 자동 실행하지 말 것(ingest는 VOD당 수십 분). 무엇부터 할지 사용자에게 확인.

3. **단계 절차 로드.** 해당 단계 파일을 **Read로 읽고 그 절차를 따른다**(이 SKILL.md에는 요약만 있음):
   - `audit.md` — 처리된 VOD를 원본 댓글과 대조(미디어 없음).
   - `ingest.md` — manual VOD 전체 전사 → 솔로 풀곡 → `soopts ingest`.
   - `perf.md` — performance 구간 재전사 → 검증·보강 → `local_review`.

4. **결과 요약.** 처리한 VOD/perf를 근거표로 정리하고, 큐가 얼마나 줄었는지 보고한다.

---

## 공유 판정 규칙 — "무엇이 BJ 솔로 풀곡인가" (세 단계 공통 기준)

세 단계는 입력만 다를 뿐(댓글 / 전체 전사 / 구간 전사) **똑같은 판정**을 한다. 여기서 한 번만 못박는다:

- **포함:** BJ가 **혼자, 처음부터 끝까지** 부른 실제 노래. 흐르는 가사가 이어지는 구간.
- **제외:**
  - 게임 대화·잡담·리액션
  - 인트로/대기화면/아웃트로 BGM(튼 음원, "이 영상에서 만나요" 류)
  - 합창·떼창·따라부르기, **게스트/크루/콘서트 공연**, 잠깐 흥얼거림·한두 소절
  - 스튜디오 음질(라이브가 아니라 원곡을 튼 것)
- **마커 의미(댓글 기준):** `🎤`=BJ 솔로(대상). `🎵`/`🎶`=게스트/크루/콘서트/클립(대상 아님).
  아이콘 없는 옛 형식은 모호 → 기본 제외.
- **크루/그룹곡 제외:** 크루명이 **아티스트 슬롯이나 `[태그]`**에 있으면 그룹 공연 → 제외.
  (**괄호 안**은 항상 원곡 아티스트라 예외 — `릴파ver - LADY(요네즈 켄시)`는 솔로 커버로 유지.)
- **방향성(repo 원칙):** 애매하면 **놓치는 쪽**을 택한다. 잘못 기록하는 것보다 낫다.
  판단이 안 서면 `needs_review`/`needs_human`으로 사람에게 넘긴다.

## 공유 안전 규칙

- **개수·상태값만 믿지 말고 실제 입력(댓글/전사문)을 읽고 판정하라.** 이 스킬의 존재 이유다.
- **파괴적 적용(기계곡 삭제·status 변경·set-perf·ingest)은 반드시 사용자 승인 후.** 표부터 보여주고 확인.
- **모든 판정에 근거를 붙여라** — 왜 노래/게임/티저/크루로 봤는지 댓글·가사 인용.
- **`confirmed`(사람 확정) performance는 어느 단계에서도 건드리지 않는다.**
- 조회·전사·다운로드가 실패한 대상은 건너뛰고 그 사실을 표에 남긴다(임의 판정 금지).
