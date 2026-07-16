# Changelog

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
