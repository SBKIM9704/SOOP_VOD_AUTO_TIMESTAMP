# soopts — SOOP VOD 노래 하이라이트 추출·식별기

SOOP 다시보기에서 **BJ가 노래한 구간을 찾아 어떤 곡인지까지** 알려주는 CLI 도구.

파이프라인:
1. **채팅 수집** — 스티커(작은 이모티콘) 반응을 얻는다. (로그인 불필요, 공개 API)
2. **노래 감지** — 오디오 음악 구간(inaSpeechSegmenter) ∩ 스티커 반응. BJ가 노래하면
   채팅에 스티커가 쏟아진다는 점으로 BGM과 실제 노래를 구분한다.
3. **가사 전사** — 각 노래 구간을 faster-whisper(로컬·무료)로 전사한다.
4. **곡 식별** — 전사된 가사로 곡명을 채운다(Claude/사람).

## 설치

```bash
uv venv && uv pip install -e ".[audio,stt,dev]"
# 또는: pip install -e ".[audio,stt]"
```

- `audio` extra: inaSpeechSegmenter (노래 구간 감지)
- `stt` extra: faster-whisper (가사 전사, API 키 불필요)
- `youtube` extra: 유튜브 unlisted 업로드
- `batch` extra: supabase/rapidfuzz/anthropic (`soopts daily`/`sync` 배치 전용, 아래 참고)
- `ffmpeg`, `yt-dlp` 필요 (오디오 추출·다운로드)

## 사용

```bash
# 1) 채팅(스티커) 수집
soopts collect 197718401

# 2) 오디오 확보 (또는 직접 받은 파일 사용)
soopts fetch 197718401                 # work/197718401/audio.mp3

# 3) 노래 감지 + 전사 + 타임라인
soopts songs 197718401 --audio work/197718401/audio.mp3

# 4) BJ 부른 노래만 1080p 클립 추출(정밀 경계) → 유튜브 unlisted 자동 업로드
soopts clips 197718401            # 클립 추출만 (검수용)
soopts clips 197718401 --upload   # 추출 + 유튜브 unlisted 업로드
```

### 노래 클립 추출 & 유튜브 업로드 (🎬)

`clips`는 **BJ가 부른 노래만** 1080p 클립으로 만들고, 선택적으로 유튜브에 unlisted 업로드한다:
1. 스티커로 노래 위치 특정(채팅만, 전체 다운로드 없음)
2. 후보 구간만 1080p 슬라이스 다운로드
3. inaSpeechSegmenter로 **음악 경계 정밀 탐지**(구간 내 최장 음악 블록=노래) → 클린 컷 (실측 1~5초 오차)
4. faster-whisper로 클립 가사 전사(설명란/식별용)
5. `--upload` 시 유튜브 unlisted 업로드

> ⚠️ **저작권**: unlisted여도 유튜브 Content ID가 원곡을 감지해 클레임/차단할 수 있음.
> 무엇을 올릴지는 사용자 판단·책임. `--upload` 없이 먼저 검수 권장.
> talk 위주 방송은 스티커 구간이 길어 1080p 다운로드가 커질 수 있음(노래 위주 VOD 권장).

#### 유튜브 업로드 최초 1회 설정 (OAuth)

```
설치:  uv pip install -e ".[youtube]"
1) https://console.cloud.google.com → 새 프로젝트
2) "YouTube Data API v3" 사용 설정(Enable)
3) OAuth 동의 화면(External, 테스트 사용자에 본인 계정 추가)
4) 사용자 인증 정보 → OAuth 클라이언트 ID → "데스크톱 앱" → client_secret.json 다운로드
5) soopts.toml [youtube] client_secret 에 그 파일 경로 지정
```
`--upload` 최초 실행 시 브라우저 동의 → 토큰 저장 → 이후 자동 업로드.

출력 예:

```
[ 01:44:29 ] 🎤 곡명 미상
    · 후보 · 293초 · 스티커 1.2/분
    · 가사: Your mama she told me don't worry about your size ... Silicon Barbie doll
[ 02:01:34 ] 🎤 곡명 미상
    · 유력 · 196초 · 스티커 6.4/분
    · 가사: ...슬픈 일들은 내일로 미뤄버려요...
```

→ 가사를 보면 각각 **All About That Bass**, **자우림 - 매직 카펫 라이드**임을 알 수 있다.

## 노래 감지 원리

- **유력** = 스티커 반응 강함(`sticker_rate_strong` 이상, 기본 2.5/분) → 떼창·후원 곡.
- **후보** = 오디오만 감지, 스티커 적음 → 잔잔한 감상곡 또는 BGM. 검수 대상.
- 방송 초반(`skip_opening_s`, 기본 4분)의 인사 스티커 폭증은 노래에서 제외.
- `[audio] min_sticker_rate > 0` 으로 스티커 적은 구간(BGM)을 아예 제거 가능.
- **가사 전사 팁**: 노래는 반주와 섞여 어렵다. `[stt] language`로 언어를 강제하고
  `model`을 `small` 이상으로 하면 정확도가 크게 오른다.

## 데일리 자동 배치 (GitHub Actions)

`soopts daily`/`soopts sync`는 스테이션 최신 VOD를 무인으로 처리하는 배치 커맨드다.
`.github/workflows/daily.yml`(04:00 KST)·`sync.yml`(05:00 KST)이 매일 자동 실행한다.

```
GitHub Actions (public repo = 분 무제한 무료)
  daily : VOD 목록 → 미처리 N개 → 감지/전사/식별 → DB 기록
          → 1080p 정밀 클립 컷 → 업로드(일 daily_upload_limit 상한 큐)
  sync  : 검수 확정 건 유튜브 제목/설명 갱신
        │
        ▼
     Supabase  ◄──── 프론트/검수 UI(별도 레포)가 상태 변경
```

- 진실의 원천은 항상 Supabase(`vods`/`performances`)다. 러너는 휘발성이라 클립 파일이
  실행 사이 사라질 수 있지만, 업로드 큐는 `clip_status='clipped'` + `start_s`/`end_s`만으로
  같은 경로를 재구성해 소진한다(`src/soopts/batch.py`의 `clip_file_path`/`_reslice_clip`).
- 신곡을 `songs` 테이블에 자동 생성하지 않는다 — 미식별 곡은 항상 `needs_review`로 검수 대기.
- 업로드 privacy는 항상 unlisted로 고정, 코드가 public 전환을 하지 않는다(사람 결정).

### 필요 GitHub Secrets

| 이름 | 용도 |
|---|---|
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Supabase 접속(RLS 우회) |
| `ANTHROPIC_API_KEY` | 가사→곡명 추측(daily 전용) |
| `YT_CLIENT_SECRET` | Google OAuth 클라이언트(client_secret.json 원문) |
| `YT_TOKEN` | 최초 로컬 OAuth 동의로 생성된 yt_token.json 원문 |
| `SLACK_WEBHOOK_URL` | (선택) daily 요약 알림 |

최초 설정: 로컬에서 `soopts clips <vod> --upload` 또는 `soopts daily` 1회 실행 →
브라우저 동의 → 생성된 `~/.config/soopts/yt_token.json` 내용을 `YT_TOKEN`에 등록.

### self-hosted runner 폴백

GitHub 호스티드 러너(미국 Azure IP)에서 SOOP API/스트림이 막히면(`verify-env.yml`로 확인),
`daily.yml`/`sync.yml`의 `runs-on: ubuntu-latest`만 self-hosted로 바꾸면 된다 — 나머지
워크플로우·코드는 동일. self-hosted 후보: 사용자 WSL2 PC 또는 상시 구동 서버(OCI A1 등).

### 수동 실행 / 옵션

```bash
soopts daily --count 1 --no-upload   # 감지·식별·클립까지만, 업로드 생략
soopts daily --bj other_bj_id        # 다른 스테이션 대상
soopts sync                          # 검수 확정 건 유튜브 메타데이터 갱신
```

기존에 수동으로 올려둔 클립을 소급 등록하려면 `scripts/backfill_existing_clips.py`
(1회성, 채널의 기존 업로드를 조회해 `needs_review`로 등록)를 실행한다.

## 캐시 / 재실행

각 단계는 `work/{vod_id}/`에 중간 산출물을 저장한다. `--force`로 재계산.
- `chat.jsonl` — 채팅/스티커 (`collect --reparse`로 raw XML에서 재생성, 네트워크 없음)
- `audio_segmentation.json` — 값비싼 음성 세그먼테이션 캐시 (파라미터 튜닝은 재실행 없이)

## 개발

```bash
ruff check src tests && pytest
```

테스트는 네트워크·ML 없이 순수 함수(구간 병합·스티커율·XML 파싱·dedup)를 검증한다.

## 참고

- 다운로드는 사용자 판단·로컬 수행 (약관/저작권). 개인 팬 활동 범위.
- SOOP API는 비공식 — 엔드포인트는 `soopts.toml`에서 패치.
