# Changelog

## [v0.7.0] - 2026-07-22

방송 직후 VOD를 서버가 건드리지 않도록 **쿨다운**을 넣었다. 팬 댓글 타임라인이 아직 없거나
쓰이는 중인 VOD를 잡으면 되돌릴 수 없다 — 한 번 처리한 VOD는 자동 재처리 대상이 아니라서,
🎤 0개는 `manual`(사람이 전체 전사로 푸는 제일 비싼 큐)로 빠지고, 절반만 쓰인 타임라인은
그만큼만 기록된 채 `analyzed`로 끝나 나머지 곡을 조용히 잃는다.

### Added
- **VOD 쿨다운 (`station.min_vod_age_days`, 기본 7일)**: `broadcast_date`가 쿨다운 안이면
  `select_targets`가 후보에서 제외한다. 재시도는 면제(이미 착수한 작업), `broadcast_date`가
  없으면 거르지 않음. 경계 날짜(`batch.cooldown_cutoff`)는 **KST** 기준 — SOOP `reg_date`가
  KST인데 러너는 UTC라 UTC로 재면 04시(KST) 슬롯에서 하루 어긋난다. 쿨다운으로 빈 슬롯은
  페이징이 과거로 더 내려가 백필로 채우므로 처리량 손해는 없다 (45cf7aa, #43)

### Changed
- 릴리즈 스킬을 Python(pyproject) 기준으로 수정하고, 태그가 stale한 로컬 `main`의 옛 커밋에
  찍히던 버그를 방지 (1a8f33a, #42)

## [v0.6.0] - 2026-07-22

서버를 **댓글 타임라인 🎤 파싱 전용**(미디어·STT 없음)으로 전환하고, 무-타임라인·검증 작업을
**로컬 스킬**로 분리. performance를 로컬에서 전면 검증·보강하고 종료 경계까지 정밀화했다.

### Added
- **`perf-review` 스킬 + 프리미티브 5종**: 기록된 performance를 로컬에서 재전사해 진짜 노래·BJ
  솔로·시간을 검증하고 가사·제목·song_id·`local_review`를 채운다. `perfs`/`set-perf`/
  `match-song`/`add-song`/`transcribe` 프리미티브로 오케스트레이션 (4798954, #37)
- **`manual-ingest` 스킬**: 서버가 `manual`로 남긴 무-타임라인 VOD를 로컬 전체 전사 후 BJ 솔로
  풀곡만 골라 `soopts ingest`로 기록 (4798954, #37)
- **`scripts/analyze_vod.py`**: 무-타임라인 VOD 전체 오디오를 파트별로 받아 Whisper로 전량
  전사(재개 캐시·GROQ 키 로테이션). 게임 BGM에 오탐하던 segmenter sweep을 대체 (61d1d35, #35)
- **`transcribe --segments`**: 텍스트 대신 세그먼트별 타임스탬프 JSON을 출력하는 종료 경계
  보정 프리미티브(`stt._transcribe_segments`). perf-review ③단계에 편입 (962c1c9, #40)

### Changed
- **서버를 댓글 타임라인 🎤 파싱 전용으로 전환**: HLS 다운로드·segmenter·STT를 배치에서 제거,
  🎤 마커만 파싱해 DB 기록. 무-타임라인 VOD는 `manual`로 표시해 로컬 처리 (c373561, #33)
- **🎤만 대상**: 🎵(합창·게스트·튼음악)를 우선순위 신호에서 제거 — BJ가 혼자 부른 풀곡만
  데이터화 (1ee6e40, #34)
- **로컬 도구 역할 정리 + 무-타임라인 처리 문서 일원화**: 실효 없던 vod-video-ingest 제거
  (f596d7c, #36)
- **`local_review` 축 도입**: 로컬 검증 워크플로(pending→verified)를 작업 기준으로, `needs_review`는
  검토 후에도 사람이 봐야 할 소수에만 붙이는 escalation으로 정정 (691655a, #39)
- **임시 체크리스트 gitignore**: 생성되는 needs_review.md 추적 해제 (1d0c2f9, #38)

산출물을 **타임스탬프**로 재정의(유튜브·영상 생산 제거)하고, 배치의 유실·재시도
신뢰성과 무-타임라인 VOD 감지를 전면 보강.

### Added
- **무-타임라인 전체 오디오 sweep**: 댓글 타임라인이 없으면 파트별로 전체 오디오를 받아
  inaSpeechSegmenter로 음악 구간을 **전부** 열거한다. 스티커 버스트로 대략 위치만 잡고
  최장 음악블록 1개만 취하던 방식은 한 구간의 나머지 곡을 통째로 놓쳤다 (f78ce65, #30)
- **sweep 예산·가드**: `sweep_limit`(런당 신규 sweep 수), `sweep_max_duration_s`(초장시간
  가드). 초과분은 pending 행을 지워 다음 런에 재선정(retry_count 미소모), 러너로 불가능한
  길이는 즉시 큐에서 제외 (f78ce65, #30)
- **`soopts process <id>`**: 배치 파이프라인을 로컬에서 돌려 **DB에 기록**하는 탈출구.
  기존 `songs`/`clips`는 로컬 파일만 남겨 딥링크가 생기지 않았다 (f78ce65, #30)
- **VOD 선택 우선순위**: 재시도 > 신규 > 백필 — 목록을 최신순으로 넘기며 밀린 과거까지
  자동으로 따라잡는다 (fa9d571, #27)
- **품질 게이트**: STT 성공률이 임계값 미만이면 실행을 실패 처리 — 전사가 조용히 전량
  실패하던 장애를 표면화 (8001310, #23)
- **세그먼트 병렬 다운로드**: 제출 순서를 보존하는 슬라이딩 윈도로 구간 다운로드 단축
  (300초 구간 11.8s → 5.6s, byte-identical) (8001310, #23)

### Fixed
- **구간 부분 실패의 영구 유실**: 후보가 **전부** 실패해야 재시도로 넘겼던 탓에, 19곡
  성공 + 1곡 실패면 VOD가 analyzed로 종결되고 그 곡은 영영 사라졌다. 이제 실패가 하나라도
  있으면 재시도 대상이 된다(재처리는 멱등) (f78ce65, #30)
- **m3u8 플레이리스트 재시도 누락**: 세그먼트에만 재시도가 있어, 큰 플레이리스트가 잘리면
  IncompleteRead가 그대로 새어 '다운로드 실패'로 표면화됐다. 재시도+지수 백오프를 공통
  관문으로 통합 (f78ce65, #30)
- **sweep 파트 매핑 2건**: 메타 파트가 비면 첫 6초 세그먼트만 받고 조용히 끝나던 문제,
  m3u8 수 > 메타 파트 수일 때 오프셋 0 폴백으로 **틀린 전역 시각**이 기록될 수 있던 문제
  (f78ce65, #30)
- **재처리 멱등성**: `clear_machine_performances`로 중복 행 방지(사람 확정분은 보존)
  (4687c94, #25)
- **upsert 배치 23502**: 행 키를 균일하게 맞춰 NOT NULL 위반 해소 (eb71438, #22)
- **중단된 실행의 pending VOD**: 재시도 대상에 포함해 큐에 영구히 남던 문제 해소
  (eb0948a, #21)
- **삭제 큐 설정 누락 · 슬라이스 STT 413** (7538d3c, #19)

### Changed
- **유튜브·영상 생산 제거**: 산출물을 타임스탬프로 재정의. 시청은 SOOP 원본 딥링크로
  연결하므로 저장 비용·저작권 노출·단일 벤더 계정 의존이 사라졌다. 클립 재인코딩(구간당
  6.6분, 전체 런타임의 76%)도 함께 제거 (af63e27, #20)
- **배치 주기·처리량**: 6시간마다(하루 4번) 실행, 런당 VOD 1개 — 런 소요를 "가장 무거운
  VOD 하나"로 고정해 예측 가능성과 실패 격리를 확보. `timeout-minutes` 240
  (f78ce65/3d7075c/1b13b8e, #30/#29/#26)
- **문서 최신화**: CLAUDE.md·README를 현재 파이프라인 상태로 갱신 (1d63797, #28)

## [v0.4.0] - 2026-07-18

데일리 자동 배치 파이프라인(GitHub Actions) 도입 + 곡 식별/전사 API 전환.

### Added
- **자동 배치 파이프라인**: `soopts daily`/`sync` — VOD 목록 → 미처리 감지/전사/식별 →
  Supabase 기록 → 1080p 클립 컷 → 유튜브 업로드 큐, GitHub Actions 스케줄 구동 (849e4f3, #5)
- **구간 단위 즉시 처리**: 곡 감지 즉시 처리 + Slack 개별 VOD 알림 (f76634a, #9)
- **곡 식별 API 전환**: Claude → Gemini → Groq API로 이관 (곡 식별/댓글 타임라인 추출) (8d4d173/f2979d6, #11/#12)
- **가사 전사 전환**: faster-whisper(로컬) → Groq Whisper API — CPU 러너 재시도 루프로 인한 배치 지연 해소 (d184fb9, #17)
- **유튜브 삭제 큐**: `youtube_deletion_queue` 소진 — 검수 UI의 구간 수정/삭제 요청을 daily/sync가 처리 (d184fb9, #17)
- **루프 밖 예외 Slack 알림**: SOOP API 502 등 배치 최상위 예외도 Slack에 기록 (963173b, #14)
- **영상 제목/설명 포맷 · 업로드 신뢰성 개선** (5fb08e6, #16)

### Fixed
- **곡 매칭 버그 · 업로드 큐 필터 · 댓글 페이지네이션 · 실시간 Slack 알림** (1aae1c6, #15)
- **타임라인 캡핑**: 리스트 순서 대신 실제 시각순 기준으로 (구간 겹침 방지) (d184fb9, #17)
- **업로드 큐 song_id NULL 방어 필터** (d184fb9, #17)
- **gpt-oss 추론 모델 빈 답변**: max_tokens 부족 문제 (aab4d2e, #13)
- **실패 VOD 재시도 upsert**: identity 컬럼 id 포함으로 거부되던 문제 (b4eb59b, #10)
- **감지 노래 0곡 VOD**: 바로 `done`으로 종결 (96c1ed6, #8)
- **CI 파이프 종료코드 유실**: daily/sync 워크플로우 `set -o pipefail` (18f46b1, #6)
- **verify-env collect 단계** `-v` 위치 수정 (e2a6071, #4)

### Changed
- **미사용 코드/문서 정리**: `resolve_m3u8`/`read_songs` 제거, CLAUDE.md 테스트 문서 최신화 (d184fb9, #17)
- **public 릴리즈 대비**: README/LICENSE/CLAUDE.md 정비 + fixture 익명화 (74889ba, #7)
- **verify-env 워크플로우 추가**: 러너 IP 점검용 (ff14417, #3)

## [v0.3.0] - 2026-07-16

첫 릴리즈 — SOOP VOD 노래 하이라이트 추출·식별 + 클립/유튜브 도구.

### Added
- **노래 감지·식별**: 채팅 스티커 반응 + inaSpeechSegmenter 음악감지로 BJ가 부른 노래 구간 탐지,
  faster-whisper(로컬) 전사로 곡 식별 (`soopts collect` / `songs`)
- **효율 슬라이스 모드**: 전체 다운로드 없이 스티커로 노래 위치만 특정해 해당 구간만 다운로드
  (멀티파트 VOD 지원). 13.3시간 VOD에서 ~92% 다운로드 절감
- **노래 클립 추출**: 후보 구간 1080p 다운로드 → 음악 경계 정밀 탐지(최장 음악 블록) → 클린 컷
  (정답 대비 1~5초 오차) (`soopts clips`)
- **유튜브 unlisted 업로드**: YouTube Data API v3, OAuth 최초 1회 동의 후 자동 (`soopts clips --upload`, `soopts upload`)
- **곡명 확인 2단계**: 클립 추출·가사전사 → clips.json 곡명 확인 → 업로드
- **업로드 제목에 방송 날짜**: `{title} - {bj} ({date})` (file_info_key에서 방송일 추출)

### Notes
- 무거운 ML(tensorflow/faster-whisper)·google 라이브러리는 지연 import로 `import soopts` 경량 유지
- 다운로드는 사용자 로컬 책임(약관/저작권). 유튜브 업로드는 Content ID 클레임 가능성 유의
