"""유튜브 서비스 모듈 — google 라이브러리 없이 도는 부분만."""

from soopts.export import youtube


def test_only_insert_apis_are_implemented():
    """유튜브 쓰기는 insert 둘(videos·playlistItems)뿐이라는 설계 결정을 고정한다(계정 정지 방어).

    조회·수정·삭제를 안 만든다는 전제 위에 "처음부터 제대로 만들어 올린다"는 흐름 전체가
    서 있다 — 올린 뒤 고칠 수 없으니 선택 조건이 그만큼 엄격한 것이다. 재생목록도 마찬가지로
    추가만 하고, 목록 조회·삭제·생성(playlists.insert)은 만들지 않는다.
    """
    assert not hasattr(youtube, "delete_video")
    assert not hasattr(youtube, "update_video_metadata")
    assert not hasattr(youtube, "set_visibility")
    assert not hasattr(youtube, "create_playlist")
    assert not hasattr(youtube, "list_playlist_items")
    assert not hasattr(youtube, "remove_from_playlist")


def test_video_id_from_url():
    """업로드가 만드는 youtu.be 형태가 기본이고, 사람이 넘길 수 있는 watch?v=도 받는다."""
    assert youtube.video_id_from_url("https://youtu.be/ZSSWN9ZemO4") == "ZSSWN9ZemO4"
    assert youtube.video_id_from_url("https://youtu.be/ZSSWN9ZemO4?t=42") == "ZSSWN9ZemO4"
    assert youtube.video_id_from_url("https://www.youtube.com/watch?v=ZSSWN9ZemO4") == "ZSSWN9ZemO4"
    assert youtube.video_id_from_url("ZSSWN9ZemO4") == "ZSSWN9ZemO4"


class _FakeInsert:
    def __init__(self, sink):
        self.sink = sink

    def insert(self, **kwargs):
        self.sink.append(kwargs)
        return self

    def execute(self):
        return {"id": "PLI_fake"}


class _FakeService:
    """playlistItems().insert(...).execute() 체인만 흉내낸다."""

    def __init__(self):
        self.calls: list[dict] = []

    def playlistItems(self):  # noqa: N802 — google 클라이언트의 메서드명을 그대로 흉내낸다
        return _FakeInsert(self.calls)


def _cfg(playlist_id: str):
    from soopts.config import Config

    cfg = Config()
    cfg.youtube.playlist_id = playlist_id
    return cfg


def test_add_to_playlist_builds_expected_request(monkeypatch):
    """실제로 보내는 요청 모양을 고정한다 — 러너에서 이 한 번의 호출이 전부다."""
    fake = _FakeService()
    monkeypatch.setattr(youtube, "_get_service", lambda cfg: fake)

    assert youtube.add_to_playlist(_cfg("PLtest"), "https://youtu.be/ZSSWN9ZemO4") is True
    assert fake.calls == [
        {
            "part": "snippet",
            "body": {
                "snippet": {
                    "playlistId": "PLtest",
                    "resourceId": {"kind": "youtube#video", "videoId": "ZSSWN9ZemO4"},
                }
            },
        }
    ]
    # position을 주지 않아야 목록 끝에 붙는다(소급 추가해둔 과거 영상 뒤로 시간순 누적).
    assert "position" not in fake.calls[0]["body"]["snippet"]


def test_add_to_playlist_skips_when_unset(monkeypatch):
    """playlist_id가 비어 있으면 API 서비스를 만들지도 않고 조용히 건너뛴다(설정 안 한 상태가 정상).

    _get_service를 폭발하게 만들어 "호출 자체가 없었다"를 증명한다 — 토큰이 있는 개발 머신에서
    이 테스트가 진짜 API를 찌르는 사고를 막는다.
    """
    def _boom(cfg):
        raise AssertionError("playlist_id가 비었는데 _get_service를 호출했습니다")

    monkeypatch.setattr(youtube, "_get_service", _boom)
    assert youtube.add_to_playlist(_cfg(""), "https://youtu.be/ZSSWN9ZemO4") is False
    assert youtube.add_to_playlist(_cfg("   "), "https://youtu.be/ZSSWN9ZemO4") is False


def test_playlist_failure_never_kills_the_run(monkeypatch):
    """업로드·DB 기록이 끝난 뒤의 부가 작업이라, 실패해도 예외가 밖으로 나가면 안 된다."""
    from soopts import youtube_pipeline

    def _raise(cfg, url):
        raise RuntimeError("playlistNotFound")

    notices: list[str] = []
    monkeypatch.setattr(youtube, "add_to_playlist", _raise)
    monkeypatch.setattr(youtube_pipeline, "_notify_slack", notices.append)

    ok = youtube_pipeline._add_to_playlist(_cfg("PLtest"), "https://youtu.be/ZSSWN9ZemO4")
    assert ok is False  # 예외가 밖으로 나가지 않고 실패로만 보고된다
    assert len(notices) == 1 and "playlistNotFound" in notices[0]


def test_upload_notice_reports_playlist_state():
    """미설정(False)이 성공만큼 눈에 띄어야 한다 — ID가 시크릿이라 코드만 봐선 켜졌는지 모른다."""
    from soopts import youtube_pipeline

    class P:
        offset_s, duration_s, title, artist = 0.0, 200.0, "t", "a"

    args = (_cfg("PLtest"), {"soop_title_no": 1, "title": "v"}, "https://youtu.be/x", "T", [P()], [])
    assert "✅ 재생목록에 추가됨" in youtube_pipeline.format_upload_notice(*args, True)
    assert "⚠️ 재생목록 미설정" in youtube_pipeline.format_upload_notice(*args, False)
    # 기본값(None)이면 줄 자체가 없다 — 기존 알림 형식을 그대로 둔다.
    notice = youtube_pipeline.format_upload_notice(*args)
    assert "재생목록" not in notice


def test_upload_uses_force_ssl_scope():
    """youtube.upload 스코프만으로는 부족했던 이력이 있어 스코프를 고정해 둔다."""
    assert youtube.SCOPES == ["https://www.googleapis.com/auth/youtube.force-ssl"]
