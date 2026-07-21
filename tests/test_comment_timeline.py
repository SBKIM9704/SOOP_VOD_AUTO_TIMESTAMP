from soopts.analyzers.comment_timeline import (
    TimelineSong,
    hms_to_s,
    no_timeline_note,
    parse_song_timeline,
    timeline_songs_to_spans,
)


def test_hms_to_s_full():
    assert hms_to_s("01:03:22") == 3802


def test_hms_to_s_minutes_seconds_only():
    assert hms_to_s("3:22") == 202


# --------------------------------------------------------------------------- #
# parse_song_timeline — 마커(🎤/🎵/🎶) 기반 추출
# --------------------------------------------------------------------------- #
def test_parse_mic_lines_into_songs():
    comment = (
        "📢 방송 예정\n"
        "00:03:37 💛 띵하\n"                       # 마커 없음 → 제외
        "01:03:22 🎤 아이유 - 애타는 마음\n"
        "01:48:15 🎤 윤하 - 기다리다\n"
    )
    songs = parse_song_timeline([comment])
    assert songs == [
        TimelineSong(time_s=3802, title="애타는 마음", artist="아이유"),
        TimelineSong(time_s=6495, title="기다리다", artist="윤하"),
    ]


def test_tags_and_html_entities_stripped():
    # [방종곡] 등 태그와 html 엔티티(&#039; → ')는 정리된다.
    comment = "07:37:15 🎤 [방종곡] 이츠(IT&#039;S) - 청록\n"
    songs = parse_song_timeline([comment])
    assert songs == [TimelineSong(time_s=27435, title="청록", artist="이츠(IT'S)")]


def test_note_markers_excluded_mic_only():
    # 🎵/🎶(합방·콘서트 게스트 공연)는 서버에서 세지 않는다 — 🎤(BJ 본인)만 인정.
    comment = (
        "05:07:35 🎵 갱십 - 이 지금\n"      # 게스트 공연 → 제외
        "07:37:15 🎤 윤하 - 혜성\n"          # BJ 부름 → 포함
    )
    assert [s.title for s in parse_song_timeline([comment])] == ["혜성"]


def test_clip_and_teaser_references_excluded():
    comment = (
        "01:13:23 🎵 [클립이슈] 키마 - Heavy Serenade 연습 편집본\n"  # 클립 참조
        "00:58:31 🎵 키마 솔로곡 어린 나 티저 분석\n"                 # 티저 (— 없음이라 어차피 제외)
        "02:47:18 🎤 윤하 - Delete\n"                               # 진짜 공연
    )
    songs = parse_song_timeline([comment])
    assert [s.title for s in songs] == ["Delete"]


def test_iconless_timeline_yields_no_songs():
    # 과거 옛 포맷: 곡을 적었지만 🎤/🎵 아이콘이 없다 → 노래인지 판단 불가 → 타임라인 없음으로 취급
    # (호출부가 no_timeline → manual → 로컬 처리). 마커가 있어야만 곡으로 센다.
    comment = (
        "01:03:22 아이유 - 애타는 마음\n"
        "01:48:15 윤하 - 기다리다\n"
    )
    assert parse_song_timeline([comment]) == []


def test_game_only_comment_yields_no_songs():
    comment = "00:23:54 🍊 마녀의집?\n01:12:23 📝 히든기믹\n01:34:50 🍊 금도끼줄까?"
    assert parse_song_timeline([comment]) == []


def test_nested_lines_and_sorting():
    comment = (
        "05:39:23 🎤 라붐 - 상상더하기\n"
        "└05:34:43 🎤 트와이스 - Dance The Night Away\n"  # └ 중첩 + 더 이른 시각 → 정렬됨
    )
    songs = parse_song_timeline([comment])
    assert [s.time_s for s in songs] == [20083, 20363]


# --------------------------------------------------------------------------- #
# timeline_songs_to_spans — 시작=시각, 끝=다음 곡(6분 캡)
# --------------------------------------------------------------------------- #
def test_spans_end_at_next_song():
    songs = [
        TimelineSong(time_s=100, title="A", artist="x"),
        TimelineSong(time_s=250, title="B", artist="y"),  # 150초 뒤 → end=250
    ]
    spans = timeline_songs_to_spans(songs, duration_s=10000)
    assert spans[0] == {"start_s": 100, "end_s": 250, "title": "A", "artist": "x"}


def test_spans_cap_at_max_song_length():
    songs = [
        TimelineSong(time_s=100, title="A", artist="x"),
        TimelineSong(time_s=100 + 3600, title="B", artist="y"),  # 1시간 뒤(사이 잡담)
    ]
    spans = timeline_songs_to_spans(songs, duration_s=10000, max_song_s=360)
    assert spans[0]["end_s"] == 100 + 360  # 6분 캡


def test_spans_last_song_uses_cap_and_duration():
    songs = [TimelineSong(time_s=9800, title="끝곡", artist="z")]
    spans = timeline_songs_to_spans(songs, duration_s=9900, max_song_s=360)
    assert spans[0]["end_s"] == 9900  # start+360=10160 이지만 VOD 길이로 캡


# --------------------------------------------------------------------------- #
# no_timeline_note — 🎤 0곡일 때 로컬 처리 우선순위 힌트
# --------------------------------------------------------------------------- #
def test_note_flags_guest_songs_when_music_marker_present():
    # 🎵(합방/게스트 공연)가 있으면 로컬에서 곡을 찾을 가능성이 높다 → 강하게 권함.
    comments = ["05:07:35 🎵 갱십 - 이 지금\n05:14:47 🎵 올어바웃설이 - 2411"]
    assert "로컬 claude-video" in no_timeline_note(comments)


def test_note_weak_hint_when_timeline_but_no_music():
    # 게임 방송도 타임라인(시각 나열)은 있다 — 음악 표기 없으면 약한 권고.
    comments = [
        "00:23:54 🍊 마녀의집?\n00:32:10 🍊 무장조\n00:40:40 🍊 나침반\n"
        "01:08:00 📝 히든\n01:34:50 🍊 금도끼\n"
    ]
    note = no_timeline_note(comments)
    assert "한번 확인" in note
    assert "claude-video" not in note


def test_note_says_no_timeline_when_few_comments():
    assert "게임 방송" in no_timeline_note(["/업//업/", "그냥 종겜 데이였습니다"])
