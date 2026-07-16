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
- `ffmpeg`, `yt-dlp` 필요 (오디오 추출·다운로드)

## 사용

```bash
# 1) 채팅(스티커) 수집
soopts collect 197718401

# 2) 오디오 확보 (또는 직접 받은 파일 사용)
soopts fetch 197718401                 # work/197718401/audio.mp3

# 3) 노래 감지 + 전사 + 타임라인
soopts songs 197718401 --audio work/197718401/audio.mp3
```

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
