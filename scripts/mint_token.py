"""유튜브 OAuth 토큰 발급 (로컬 1회, 브라우저 동의 필요).

러너는 브라우저를 못 띄우므로 토큰은 사람이 로컬에서 만들어 시크릿으로 올려야 한다.

사용:
    python scripts/mint_token.py [--client-secret client_secret.json] [--out yt_token.json]
    gh secret set YT_TOKEN < yt_token.json

주의:
  - 동의 화면에서 **업로드할 채널의 계정**으로 로그인할 것(브랜드 계정이 여러 개면 선택 화면이 뜬다).
  - OAuth 동의 화면이 "Testing"이면 refresh token이 7일 만에 만료돼 크론이 매주 죽는다.
    Cloud Console에서 **In production**으로 게시한 뒤 발급할 것.
  - 스코프를 바꿨다면 기존 토큰 파일을 지우고 다시 받아야 반영된다.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from soopts.export.youtube import SCOPES


def main() -> int:
    ap = argparse.ArgumentParser(description="유튜브 OAuth 토큰 발급(로컬 1회)")
    ap.add_argument("--client-secret", default="client_secret.json",
                    help="Google Cloud OAuth 데스크톱 클라이언트 JSON 경로")
    ap.add_argument("--out", default="yt_token.json", help="발급된 토큰 저장 경로")
    args = ap.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = Path(args.client_secret).expanduser()
    if not secret.exists():
        raise SystemExit(f"OAuth 클라이언트 파일이 없습니다: {secret}")

    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    if not creds.refresh_token:
        raise SystemExit(
            "refresh_token이 없습니다 — 이미 동의한 클라이언트라 재발급되지 않았을 수 있습니다. "
            "Google 계정의 'third-party access'에서 이 앱 접근을 해제한 뒤 다시 실행하세요."
        )

    out = Path(args.out).expanduser()
    out.write_text(creds.to_json(), encoding="utf-8")
    out.chmod(0o600)
    print(f"토큰 저장: {out}")
    print(f"다음: gh secret set YT_TOKEN < {out}")
    print(f"로컬 사용: cp {out} ~/.config/soopts/yt_token.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
