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


def test_crew_performance_excluded_by_artist():
    # 팬은 "실제로 불렀다"는 뜻으로 그룹 공연에도 🎤를 쓴다. 목표는 BJ 솔로곡이므로 제외한다.
    # (실제 사례: 201300619의 바보즈 싱크룸 7곡, 200077825의 하데스 단체 노래깎기 2곡)
    comment = (
        "06:02:48 🎤 바보즈 - Pretty Girl(카라)\n"                 # 싱크룸 그룹 → 제외
        "04:22:50 🎤 [성공] 하데스 - 낭만 한도 초과(하이키)\n"        # 크루 단체 → 제외
        "06:40:06 🎤 [방종곡] 이츠(IT&#039;S) - 청록\n"             # BJ 솔로 → 포함
    )
    assert [s.title for s in parse_song_timeline([comment])] == ["청록"]


def test_crew_performance_excluded_by_tag():
    # 아티스트 자리엔 원곡자가 오고 크루명이 [태그]에만 있는 형태 — 태그도 봐야 잡힌다.
    comment = "05:31:40 🎤 [바보즈 불러놔] 트와이스 - Dance The Night Away\n"
    assert parse_song_timeline([comment]) == []


def test_crew_name_in_original_artist_note_is_kept():
    # 괄호는 두 마커 모두 **원곡자**다(`릴파ver - LADY(요네즈 켄시)`). 그러니 괄호 속 크루명은
    # "원래 크루 곡"이라는 뜻이지 "크루가 공연했다"가 아니다 — BJ가 부른 거라 남겨야 한다.
    # 괄호가 아니라 아티스트 자리·태그만 보는 이유.
    comment = "05:16:23 🎤 릴파ver - 두번째 지구(하데스)\n"
    assert [s.artist for s in parse_song_timeline([comment])] == ["릴파ver"]


def test_crew_performance_excluded_by_section_header():
    # 크루 신호가 **섹션 헤더에만** 있고 각 곡 줄의 아티스트 자리엔 원곡 가수가 들어가는 합방.
    # 줄 하나만 보면 평범한 솔로곡과 구분이 안 된다 (실제 사례: 202664153 25곡, 196641369 6곡).
    comment = (
        "🎵 하데스 후열 싱크룸 - 솜키띵초챈 순서\n"
        "04:33:20 🎤 카라(KARA) - Pretty Girl\n"      # 헤더가 크루 → 제외
        "06:07:18 🎤 소녀시대 - I Got A Boy\n"        # 헤더가 크루 → 제외
        "\n"                                          # 빈 줄이 구획을 끝낸다
        "🍊 방종 전 소통\n"
        "07:35:25 🎤 [방종곡] 아이유 - 무릎\n"         # 다른 구획의 BJ 솔로 → 포함
    )
    assert [s.title for s in parse_song_timeline([comment])] == ["무릎"]


def test_section_header_keyword_does_not_drop_solo_songs():
    # 헤더 판정은 **크루명만** 본다. `싱크룸`/`노래방` 같은 키워드로 넓히면
    # `🎵 후열소통, 알고리즘 따라 노래방`(195027767) 아래 BJ 솔로 8곡이 통째로 날아간다.
    comment = (
        "🎵 후열소통, 알고리즘 따라 노래방\n"
        "05:05:54 🎤 정인 - 장마\n"
        "05:11:49 🎤 윤하 - 혜성\n"
    )
    assert [s.title for s in parse_song_timeline([comment])] == ["장마", "혜성"]


def test_section_header_resets_on_blank_line():
    # 헤더의 유효 범위는 다음 빈 줄까지다 — 안 그러면 뒤따르는 모든 곡이 크루로 오염된다.
    comment = (
        "🎵 하데스 싱크룸\n"
        "04:39:11 🎤 뉴진스 - Ditto\n"
        "\n"
        "05:43:53 🎤 이무진 - 굴뚝마을의 푸펠\n"      # 헤더 없는 구획 → 포함
    )
    assert [s.title for s in parse_song_timeline([comment])] == ["굴뚝마을의 푸펠"]


def test_member_permutation_artist_excluded():
    # 즉석 조합(`솜띵키초`)은 조합 수가 무한해 목록으로 못 막는다 — 음절 집합으로 판정한다.
    comment = "07:03:48 🎤 솜띵키초 - 사랑은 선율을 타고(소녀시대)\n"
    assert parse_song_timeline([comment]) == []


def test_single_member_artist_is_kept():
    # 개인명은 남겨야 한다 — 노래배틀에서 팬이 `🎤 띵귤 - …`로 부른 사람을 명시한다(193160683).
    # `귤`이 멤버 음절 집합 밖이라 부분집합 판정에서 걸리지 않는다.
    comment = "03:27:58 🎤 띵귤 - 마지막 사랑(박기영)\n"
    assert [s.artist for s in parse_song_timeline([comment])] == ["띵귤"]


def test_collab_original_artists_are_not_crew():
    # 원곡이 합작인 곡(아이유x오혁)은 크루 공연이 아니다 — 'x 들어가면 그룹' 같은 휴리스틱
    # 대신 명시적 목록을 쓰는 이유다.
    comment = "03:12:00 🎤 아이유x오혁 - 어른\n"
    assert [s.title for s in parse_song_timeline([comment])] == ["어른"]


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
# timeline_songs_to_spans — 시작=댓글 시각, 끝은 미정(센티넬 end_s=start_s)
# --------------------------------------------------------------------------- #
def test_spans_use_comment_time_as_start():
    songs = [
        TimelineSong(time_s=100, title="A", artist="x"),
        TimelineSong(time_s=250, title="B", artist="y"),
    ]
    spans = timeline_songs_to_spans(songs, duration_s=10000)
    assert spans[0] == {"start_s": 100, "end_s": 100, "title": "A", "artist": "x"}
    assert spans[1] == {"start_s": 250, "end_s": 250, "title": "B", "artist": "y"}


def test_spans_end_is_sentinel_equal_to_start():
    """end는 daily가 정하지 않는다 — end_s=start_s(0길이 미정), 로컬 perf가 채운다."""
    songs = [
        TimelineSong(time_s=100, title="A", artist="x"),
        TimelineSong(time_s=100 + 3600, title="B", artist="y"),  # 다음 곡이 멀어도 무관
    ]
    spans = timeline_songs_to_spans(songs, duration_s=10000)
    assert all(s["end_s"] == s["start_s"] for s in spans)


def test_spans_sorted_by_time():
    songs = [
        TimelineSong(time_s=300, title="늦곡", artist="z"),
        TimelineSong(time_s=100, title="첫곡", artist="a"),
    ]
    spans = timeline_songs_to_spans(songs)
    assert [s["start_s"] for s in spans] == [100, 300]


# --------------------------------------------------------------------------- #
# no_timeline_note — 🎤 0곡일 때 로컬 확인 사유 메모
# --------------------------------------------------------------------------- #
def test_note_music_marker_is_not_a_priority_signal():
    # 🎵(합창/따라부르기/게스트)는 솔로 풀곡이 아니라 우선순위 신호가 아니다 — 타임라인
    # 있음 메모로만 처리된다("솔로곡 확인" 문구, 🎵 특별대우 없음).
    comments = ["05:07:35 🎵 갱십 - 이 지금\n05:14:47 🎵 올어바웃설이 - 2411\n"
                "05:20:00 🎵 x - y\n05:25:00 🎵 a - b\n05:30:00 🎵 c - d\n"]
    note = no_timeline_note(comments)
    assert "솔로곡 확인" in note
    assert "게스트/합방" not in note


def test_note_timeline_present_but_no_mic():
    comments = [
        "00:23:54 🍊 마녀의집?\n00:32:10 🍊 무장조\n00:40:40 🍊 나침반\n"
        "01:08:00 📝 히든\n01:34:50 🍊 금도끼\n"
    ]
    note = no_timeline_note(comments)
    assert "팬 타임라인 있으나" in note
    assert "솔로곡 확인" in note


def test_note_no_timeline_when_few_comments():
    note = no_timeline_note(["/업//업/", "그냥 종겜 데이였습니다"])
    assert "댓글 타임라인 없음" in note
