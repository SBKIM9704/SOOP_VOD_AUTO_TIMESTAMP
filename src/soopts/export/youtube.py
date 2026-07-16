"""유튜브 비공개(unlisted) 업로드 — YouTube Data API v3.

최초 1회 OAuth 동의가 필요하다(사용자 계정). 이후에는 저장된 토큰으로 자동 업로드.
무거운 google 라이브러리는 함수 내부에서만 import.

준비물(사용자):
  1) Google Cloud 프로젝트 → YouTube Data API v3 사용 설정
  2) OAuth 2.0 클라이언트(데스크톱 앱) 생성 → client_secret.json 다운로드
  3) [youtube] client_secret 경로 지정. 최초 실행 시 브라우저 동의 → 토큰 저장(이후 자동)
"""

from __future__ import annotations

from pathlib import Path

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("export.youtube")

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _get_service(cfg: Config):
    """OAuth 자격으로 youtube API 서비스를 만든다(토큰 없으면 최초 동의)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path(cfg.youtube.token_file).expanduser()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = Path(cfg.youtube.client_secret).expanduser()
            if not secret.exists():
                raise RuntimeError(
                    f"OAuth 클라이언트 파일 없음: {secret}. "
                    "Google Cloud에서 OAuth 클라이언트(데스크톱) 생성 후 client_secret.json 경로를 "
                    "[youtube] client_secret 에 지정하세요."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), _SCOPES)
            creds = flow.run_local_server(port=0)  # 최초 1회 브라우저 동의
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_unlisted(cfg: Config, video_path: str, title: str, description: str = "") -> str:
    """영상을 unlisted로 업로드하고 영상 URL을 반환한다."""
    from googleapiclient.http import MediaFileUpload

    service = _get_service(cfg)
    body = {
        "snippet": {"title": title[:100], "description": description, "categoryId": cfg.youtube.category_id},
        "status": {"privacyStatus": cfg.youtube.privacy, "selfDeclaredMadeForKids": cfg.youtube.made_for_kids},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    vid = resp["id"]
    url = f"https://youtu.be/{vid}"
    log.info("업로드 완료(unlisted): %s  %s", title, url)
    return url
