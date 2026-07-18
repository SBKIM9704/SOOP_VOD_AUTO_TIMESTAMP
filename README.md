# soopts

[![daily](https://github.com/SBKIM9704/SOOP_VOD_AUTO_TIMESTAMP/actions/workflows/daily.yml/badge.svg)](https://github.com/SBKIM9704/SOOP_VOD_AUTO_TIMESTAMP/actions/workflows/daily.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**SOOP(아프리카TV) VOD 다시보기에서 BJ가 부른 노래 구간을 자동으로 찾고, 가사 전사로 어떤 곡인지까지 식별하는 CLI.**

채팅 스티커 반응과 오디오 음악 감지를 겹쳐서 BGM과 실제 노래를 구분하고, 정밀 경계를 찾아
가사를 전사한다. 산출물은 **타임스탬프**이며, 시청은 SOOP 원본 다시보기 딥링크로 연결한다 —
영상을 만들지도, 어디에 올리지도 않는다. 매일 새 VOD를 무인으로 처리하는 GitHub Actions 배치도 포함한다.

```
[ 01:44:29 ] 🎤 곡명 미상
    · 후보 · 293초 · 스티커 1.2/분
    · 가사: Your mama she told me don't worry about your size ... Silicon Barbie doll
[ 02:01:34 ] 🎤 곡명 미상
    · 유력 · 196초 · 스티커 6.4/분
    · 가사: ...슬픈 일들은 내일로 미뤄버려요...
```
→ 가사를 보면 각각 **All About That Bass**, **자우림 - 매직 카펫 라이드**임을 알 수 있다.

## 목차

- [작동 원리](#작동-원리)
- [설치](#설치)
- [사용법](#사용법)
- [노래 구간 감지](#노래-구간-감지-)
- [노래 감지 원리](#노래-감지-원리)
- [데일리 자동 배치 (GitHub Actions)](#데일리-자동-배치-github-actions)
- [캐시 / 재실행](#캐시--재실행)
- [개발](#개발)
- [참고 / 면책](#참고--면책)

## 작동 원리

1. **채팅 수집** — 스티커(작은 이모티콘) 반응을 얻는다. (로그인 불필요, 공개 API)
2. **노래 감지** — 오디오 음악 구간(inaSpeechSegmenter) ∩ 스티커 반응. BJ가 노래하면
   채팅에 스티커가 쏟아진다는 점으로 BGM과 실제 노래를 구분한다.
3. **가사 전사** — 각 노래 구간을 Groq Whisper API로 전사한다.
4. **곡 식별** — 전사된 가사로 곡명을 채운다(Groq/rapidfuzz 매칭, 미확정은 사람 검수).
5. **결과** — 노래 구간(시작·끝 시각)과 곡명. 시청은 SOOP 원본 딥링크로 연결한다.

## 설치

```bash
uv venv && uv pip install -e ".[audio,stt,dev]"
# 또는: pip install -e ".[audio,stt]"
```

| extra | 용도 |
|---|---|
| `audio` | inaSpeechSegmenter (노래 구간 감지) |
| `stt` | Groq Whisper API (가사 전사, `GROQ_API_KEY` 필요) |
| `batch` | supabase/rapidfuzz/groq (`soopts daily` 배치 전용) |
| `dev` | pytest/ruff |

시스템 의존성: `ffmpeg`, `yt-dlp` (오디오 추출·다운로드).

## 사용법

```bash
# 1) 채팅(스티커) 수집
soopts collect 197718401

# 2) 오디오 확보 (또는 직접 받은 파일 사용)
soopts fetch 197718401                 # work/197718401/audio.mp3

# 3) 노래 감지 + 전사 + 타임라인
soopts songs 197718401 --audio work/197718401/audio.mp3

# 4) BJ 부른 노래만 정밀 경계 감지 + 가사 전사 (영상 파일은 만들지 않음)
soopts clips 197718401
```

## 노래 구간 감지 (🎵)

`clips`는 **BJ가 부른 노래만** 골라 시작·끝 시각을 찾고 가사를 전사한다:
1. 스티커로 노래 위치 특정(채팅만, 전체 다운로드 없음)
2. 후보 구간만 540p 슬라이스 다운로드 (오디오만 쓰므로 최저 화질로 충분 — 1080p 대비 8배 절감)
3. inaSpeechSegmenter로 **음악 경계 정밀 탐지**(구간 내 최장 음악 블록=노래, 실측 1~5초 오차)
4. Groq Whisper API로 그 경계 구간만 가사 전사(곡 식별용)

**영상 파일을 만들지 않는다.** 다운로드한 구간은 경계 탐지와 전사의 입력일 뿐이고, 결과 공유는
`?change_second=` 딥링크로 원본 다시보기의 해당 시각을 가리킨다.

> talk 위주 방송은 스티커 구간이 길어 다운로드가 커질 수 있음(노래 위주 VOD 권장).

## 노래 감지 원리

- **유력** = 스티커 반응 강함(`sticker_rate_strong` 이상, 기본 2.5/분) → 떼창·후원 곡.
- **후보** = 오디오만 감지, 스티커 적음 → 잔잔한 감상곡 또는 BGM. 검수 대상.
- 방송 초반(`skip_opening_s`, 기본 4분)의 인사 스티커 폭증은 노래에서 제외.
- `[audio] min_sticker_rate > 0` 으로 스티커 적은 구간(BGM)을 아예 제거 가능.
- **가사 전사 팁**: 노래는 반주와 섞여 어렵다. `[stt] language`로 언어를 강제하면
  정확도가 크게 오른다(미지정 시 en/ko 둘 다 시도해 더 그럴듯한 쪽을 채택).

## 데일리 자동 배치 (GitHub Actions)

`soopts daily`는 스테이션 최신 VOD를 무인으로 처리하는 배치 커맨드로,
[`daily.yml`](.github/workflows/daily.yml)이 6시간마다(KST 04·10·16·22시) 자동 실행한다.

```
GitHub Actions (public repo = 분 무제한 무료)
  daily : VOD 선택(재시도 > 신규 > 백필) → 감지/전사/식별 → DB에 노래 구간 기록
        │
        ▼
     Supabase  ◄──── 프론트/검수 UI(별도 레포)가 상태 변경
```

- **산출물은 타임스탬프다.** 영상을 만들지도 업로드하지도 않는다. 시청 경로는 `song_link()`가
  만드는 SOOP 딥링크(`?change_second=<노래 시작초>`)이며 `soop_title_no`+`start_s`로 계산된다.
- 진실의 원천은 항상 Supabase(`vods.status`/`performances.identify_status`)다. 러너는 휘발성이라
  받아둔 구간 파일이 실행 사이 사라져도, 어디까지 처리됐는지는 DB만 보면 알 수 있다.
- **VOD 선택은 재시도 > 신규 > 백필 순.** 중단된 실행이 남긴 VOD를 먼저 재시도하고, 신규가
  없으면 처리 기록보다 과거로 내려가며 백필한다. SOOP 목록 만료가 자연 바닥선이다.
- **품질 게이트** — STT 성공률이 임계값(`min_success_rate`, 기본 50%) 미만이면 실행을 실패로
  처리한다. 전사가 대량 실패하는(예: API 한도 초과) 조용한 장애를 드러내기 위함이다.
- 신곡을 `songs` 테이블에 자동 생성하지 않는다 — 미식별 곡은 항상 `needs_review`로 검수 대기.
- 재처리는 멱등이다 — 같은 구간은 `(vod_id, start_s)`로 upsert되어 중복이 쌓이지 않고, 사람이
  확정한(`confirmed`) 건은 재처리해도 보존된다.

### 필요 GitHub Secrets

| 이름 | 용도 |
|---|---|
| `SUPABASE_URL` | Supabase 프로젝트 URL (경로 없이, 예: `https://xxxx.supabase.co`) |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase 접속(RLS 우회) |
| `GROQ_API_KEY` | 가사 전사(Whisper API) + 가사→곡명 추측 + 댓글 타임라인 추출(daily 전용, Groq 무료 티어 — 카드 불요) |
| `SLACK_WEBHOOK_URL` | (선택) daily 요약 알림 |

### self-hosted runner 폴백

GitHub 호스티드 러너(미국 Azure IP)에서 SOOP API/스트림이 막히면([`verify-env.yml`](.github/workflows/verify-env.yml)로 확인),
`daily.yml`의 `runs-on: ubuntu-latest`만 self-hosted로 바꾸면 된다 — 나머지
워크플로우·코드는 동일. self-hosted 후보: 사용자 PC 또는 상시 구동 서버.

### 수동 실행 / 옵션

```bash
soopts daily --count 5               # 처리할 VOD 수 지정 (기본: soopts.toml daily_vod_count=3)
soopts daily --bj other_bj_id        # 다른 스테이션 대상
```

## 캐시 / 재실행

각 단계는 `work/{vod_id}/`에 중간 산출물을 저장한다. `--force`로 재계산.
- `chat.jsonl` — 채팅/스티커 (`collect --reparse`로 raw XML에서 재생성, 네트워크 없음)
- `audio_segmentation.json` — 값비싼 음성 세그먼테이션 캐시 (파라미터 튜닝은 재실행 없이)

## 개발

```bash
ruff check src tests && pytest
```

테스트는 네트워크·ML 없이 순수 함수(구간 병합·스티커율·XML 파싱·dedup·식별 판정)를 검증한다.
XML/JSON fixture는 전부 익명화된 테스트용 값이다(실제 시청자 계정 정보 없음).

## 참고 / 면책

- 다운로드는 사용자 판단·로컬 수행 (약관/저작권). 개인 팬 활동 범위.
- SOOP API는 비공식 — 엔드포인트는 `soopts.toml`에서 패치.
- 영상을 재배포하지 않는다 — 산출물은 원본 다시보기를 가리키는 타임스탬프 딥링크뿐이다.
- 신곡을 자동으로 노래책 DB에 등록하지 않는다 — 항상 사람 검수를 거친다.

## License

[MIT](LICENSE)
