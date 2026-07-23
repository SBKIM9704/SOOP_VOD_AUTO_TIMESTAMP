"""유튜브 서비스 모듈 — google 라이브러리 없이 도는 부분만."""

from soopts.export import youtube


def test_only_upload_api_is_implemented():
    """유튜브 API 호출은 업로드 하나뿐이라는 설계 결정을 테스트로 고정한다(계정 정지 방어).

    조회·수정·삭제를 안 만든다는 전제 위에 "처음부터 제대로 만들어 올린다"는 흐름 전체가
    서 있다 — 올린 뒤 고칠 수 없으니 선택 조건이 그만큼 엄격한 것이다.
    """
    assert not hasattr(youtube, "delete_video")
    assert not hasattr(youtube, "update_video_metadata")
    assert not hasattr(youtube, "set_visibility")


def test_upload_uses_force_ssl_scope():
    """youtube.upload 스코프만으로는 부족했던 이력이 있어 스코프를 고정해 둔다."""
    assert youtube.SCOPES == ["https://www.googleapis.com/auth/youtube.force-ssl"]
