# SOOP VOD 노래 하이라이트 추출·식별기 — 설계서

- 버전: v1.0 (2026-07-16) — 노래 전용으로 정리
- 형태: Python CLI (`soopts`)
- 목표: SOOP 다시보기에서 **BJ가 노래한 구간을 자동으로 찾고, 가사 전사로 어떤 곡인지까지** 식별해 복붙 가능한 타임라인 초안을 만든다.

> 배경: 채팅 스파이크·프레임 감지·다중신호 fusion 등을 실험했으나, 실전 평가에서 이 방송(버추얼 BJ 언박싱) 유형에는 **노래 감지 + 곡 식별**이 가장 명확한 가치를 냈다. 나머지는 제거하고 노래에 집중한다.

---

## 1. 검증된 사실

| 항목 | 결과 |
|---|---|
| VOD 메타 API | `POST api.m.sooplive.com/station/video/a/view` (nTitleNo, nApiLevel=10) — 제목·BJ·파트 duration |
| 채팅 리플레이 | `GET videoimg.sooplive.com/php/ChatLoadSplit.php?rowKey={file_info_key}_c&startTime={초}` — 공개, 로그인 불필요. `<chat>`/`<ogq>`(스티커) 포함 |
| 노래 감지 | inaSpeechSegmenter로 music 구간 추출. 실측: 01:44/02:01 두 곡을 최장 음악블록으로 정확 감지 |
| 곡 식별 | faster-whisper(로컬)로 노래 구간 전사 → 가사로 곡 인식. 실측: "Silicon Barbie doll"→All About That Bass, "내일로 미뤄버려요"→매직 카펫 라이드 |
| 스티커 신호 | BJ가 노래(특히 떼창곡)하면 채팅에 스티커 폭증 → BGM과 실제 노래 구분에 유효 |

---

## 2. 파이프라인

```
[VOD URL/번호]
      │
      ▼
1. collect   메타 + 채팅 수집 → meta.json, chat.jsonl(스티커 포함), raw/*.xml
      │
      ▼
2. detect    오디오 음악감지(inaSpeechSegmenter) ∩ 스티커 반응 → 노래 구간
      │        - 30초 미만 제거, 인접 병합, 오프닝(인사 스티커) 제외
      │        - 분당 스티커 ≥ 2.5 → "노래 유력", 아니면 "노래 후보"
      ▼
3. transcribe  각 노래 구간 오디오 → faster-whisper 전사 → 가사
      │        - 언어 강제 + small 이상 모델 권장(반주 섞임 대응)
      ▼
4. identify  가사로 곡명 채움 (Claude/사람)
      │
      ▼
[출력: songs_{vod_id}.txt — 노래 타임라인 초안]
```

각 단계는 `work/{vod_id}/` 캐시를 쓰고 `--force`로 재계산. 값비싼 음성 세그먼테이션은
`audio_segmentation.json`에 캐시해 파라미터 튜닝을 재실행 없이 한다.

---

## 3. 모듈

- `collector/` — `meta.py`(view API), `chat.py`(300초 순회 + raw XML 캐시 + dedup), `xml_parse.py`(관대한 lxml 파서, `<chat>`/`<ogq>` 추출), `media.py`(yt-dlp 오디오)
- `analyzers/audio_analyzer.py` — 음악 구간 감지 + 스티커 반응 판별 → `Song`. inaSpeechSegmenter는 10분 청크로 처리(메모리 제한), 절대시각 반환
- `analyzers/stt.py` — 노래 구간 전사(faster-whisper, 로컬). 모델 1회 로드 후 각 구간 전사
- `output/` — 노래 타임라인 txt 렌더
- `config.py` — Endpoints/Collector/Audio/Stt (soopts.toml, 부분 오버라이드)
- `models.py` — `ChatMsg`, `MetaResult`, `Song`

무거운 ML(inaSpeechSegmenter=tensorflow, faster-whisper)은 **메서드 내부에서만 import** →
`import soopts` 경량 유지(테스트로 강제).

### Song 스키마
```json
{"t": 6269, "end": 6562, "duration": 293,
 "sticker_rate": 1.2, "song_likely": false,
 "lyrics": "...don't worry about your size...", "title": null}
```

---

## 4. CLI

```
soopts collect <vod> [--reparse]           # 메타 + 채팅(스티커) 수집
soopts fetch   <vod> [--quality hls-hd]    # 전체 오디오 mp3 다운로드
soopts songs   <vod> --audio <파일> [--no-stt]   # 노래 감지 + 전사 + 타임라인
```
전역: `--config`, `--work-root`, `--force`, `-v`.

---

## 5. 기술 스택 / 리스크

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.10+ |
| HTTP / XML | requests / lxml(recover) |
| 노래 감지 | inaSpeechSegmenter (extra `audio`) |
| 가사 전사 | faster-whisper 로컬 (extra `stt`) |
| 오디오 | ffmpeg + yt-dlp |

| 리스크 | 대응 |
|---|---|
| SOOP API 변경 | 엔드포인트 config 분리, raw XML 캐시로 재파싱 |
| BGM 오탐 | 스티커 반응으로 필터(`min_sticker_rate`), 유력/후보 2단계 |
| 노래 STT 부정확(반주 섞임) | 언어 강제 + small↑ 모델. 그래도 애매하면 "곡명 미상 + 가사"로 두고 수동 보완 |
| 조용한 감상곡(스티커 적음) | 오디오로 감지하되 "후보"로 표시(재현율 우선) |
| 약관/저작권 | 다운로드는 사용자 로컬 수행, 개인 팬 활동 범위 |

## 6. 범위 제외

채팅 자동 등록 / 실시간 분석 / 곡명 자동 검색매칭(가사→검색 API) / GUI.
곡명 식별은 전사 가사를 Claude·사람이 인식하는 것을 기본으로 한다.
