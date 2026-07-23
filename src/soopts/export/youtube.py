"""유튜브 업로드 — YouTube Data API v3 (노래모음 합본 1일 1건).

최초 1회 OAuth 동의가 필요하다(`scripts/mint_token.py`). 이후에는 저장된 토큰으로 자동
갱신·업로드한다. 무거운 google 라이브러리는 함수 내부에서만 import한다.

**이 모듈이 호출하는 유튜브 API는 업로드(videos.insert) 하나뿐이다.** 조회(videos.list·
search.list)도 수정(videos.update)도 삭제(videos.delete)도 구현하지 않는다 — 2026-07-18에
계정(SBKIM9704)이 정지됐고, 원인 가설이 봇 같은 업로드 케이던스와 조회·수정·삭제 API
남용이었다. 예전 버전에 있던 `delete_video`/`update_video_metadata`는 의도적으로 되살리지
않았다. 필요해 보이더라도 다시 추가하지 말 것 — 제목·설명·공개범위를 나중에 고칠 수 없다는
전제가 "처음부터 제대로 만들어 올린다"는 파이프라인 전체 설계를 떠받치고 있다.
사람이 손볼 일이 생기면 유튜브 스튜디오에서 직접 한다.

youtube.force-ssl 스코프를 쓴다(youtube.upload만으로는 videos.update가 403
insufficientPermissions로 막힌다 — 실제로 겪음). 스코프를 바꾼 뒤에는 저장된 토큰 파일을
지우고 다시 동의를 받아야 한다.

준비물(사용자, `~/.claude/plans/happy-squishing-dragon.md` Phase 0):
  1) Google Cloud 프로젝트 → YouTube Data API v3 사용 설정
  2) OAuth 동의 화면을 **In production**으로 게시(Testing은 refresh token 7일 만료)
  3) OAuth 2.0 클라이언트(데스크톱 앱) JSON → `[youtube] client_secret`
  4) 채널 **전화 인증**(미인증이면 15분 초과 업로드가 막힌다)
"""

from __future__ import annotations

from pathlib import Path

from soopts.config import Config
from soopts.log import get_logger

log = get_logger("export.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

_TITLE_MAX = 100
_DESCRIPTION_MAX = 5000
# 업로드 청크. `chunksize=-1`(단일 요청)로 두면 google 클라이언트가 **파일 전체를 메모리에**
# 올린다 — 7분짜리(약 100MB)는 넘어가지만 100분짜리(1.5GB+)면 러너 메모리를 위협하고,
# 전송 중 끊기면 처음부터 다시 올려야 한다. 청크로 나누면 스트리밍 + 이어올리기가 된다.
_UPLOAD_CHUNK_BYTES = 16 * 1024 * 1024


def _load_credentials(cfg: Config, token_path: Path):
    """토큰 파일을 읽되, 안에 박힌 `client_secret`을 현재 클라이언트 파일 값으로 덮어쓴다.

    토큰 JSON은 발급 시점의 client_id/client_secret을 그대로 품고 있고, 갱신 요청에 그 값을
    쓴다. 그래서 Cloud Console에서 클라이언트 시크릿을 재발급하면 **refresh_token은 멀쩡한데도**
    갱신이 `invalid_client: The provided client secret is invalid`로 죽는다(실제로 겪음).
    client_id가 같을 때만 갈아끼우므로, 클라이언트 자체가 바뀐 경우엔 손대지 않고 그대로 둔다.
    """
    import json

    from google.oauth2.credentials import Credentials

    info = json.loads(token_path.read_text(encoding="utf-8"))
    secret_path = Path(cfg.youtube.client_secret).expanduser()
    if secret_path.exists():
        conf = json.loads(secret_path.read_text(encoding="utf-8"))
        conf = conf.get("installed") or conf.get("web") or {}
        if conf.get("client_id") == info.get("client_id") and conf.get("client_secret"):
            if conf["client_secret"] != info.get("client_secret"):
                log.info("토큰의 client_secret이 현재 클라이언트와 달라 갱신용으로 교체합니다")
            info["client_secret"] = conf["client_secret"]
    return Credentials.from_authorized_user_info(info, SCOPES)


def _get_service(cfg: Config):
    """OAuth 자격으로 youtube API 서비스를 만든다(토큰 없으면 최초 동의).

    러너는 브라우저를 못 띄우므로 헤드리스에서는 토큰 파일이 반드시 있어야 한다 —
    워크플로가 `YT_TOKEN` 시크릿을 이 경로로 복원한다.
    """
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path(cfg.youtube.token_file).expanduser()
    creds = None
    if token_path.exists():
        creds = _load_credentials(cfg, token_path)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = Path(cfg.youtube.client_secret).expanduser()
            if not secret.exists():
                raise RuntimeError(
                    f"OAuth 토큰({token_path})도 클라이언트 파일({secret})도 없습니다. "
                    "로컬에서 `python scripts/mint_token.py`로 토큰을 발급한 뒤 "
                    "`gh secret set YT_TOKEN < yt_token.json`으로 올리세요."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)  # 최초 1회 브라우저 동의
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_video(cfg: Config, video_path: str | Path, title: str, description: str = "") -> str:
    """영상을 `cfg.youtube.privacy`(기본 unlisted)로 올리고 영상 URL을 반환한다.

    재시도 래퍼를 두지 않는다 — 실패하면 예외가 오케스트레이터로 올라가 Slack 알림 +
    비정상 종료로 끝나고, VOD는 youtube_status가 NULL로 남아 **다음 날 자연히 재시도**된다.
    실패할 때마다 즉시 다시 찌르는 루프가 계정 정지를 부른 패턴이라 일부러 만들지 않았다.
    (resumable 업로드의 청크 루프는 한 요청 안의 전송이라 여기 해당하지 않는다.)
    """
    from googleapiclient.http import MediaFileUpload

    service = _get_service(cfg)
    body = {
        "snippet": {
            "title": title[:_TITLE_MAX],
            "description": description[:_DESCRIPTION_MAX],
            "categoryId": cfg.youtube.category_id,
        },
        "status": {
            "privacyStatus": cfg.youtube.privacy,
            "selfDeclaredMadeForKids": cfg.youtube.made_for_kids,
        },
    }
    media = MediaFileUpload(
        str(video_path), chunksize=_UPLOAD_CHUNK_BYTES, resumable=True, mimetype="video/mp4"
    )
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            log.info("업로드 %d%%", int(status.progress() * 100))
    vid = resp["id"]
    url = f"https://youtu.be/{vid}"
    log.info("업로드 완료(%s): %s  %s", cfg.youtube.privacy, title, url)
    return url


