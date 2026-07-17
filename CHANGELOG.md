# Changelog

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
