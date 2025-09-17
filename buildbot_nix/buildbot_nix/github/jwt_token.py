from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .repo_token import RepoToken

if TYPE_CHECKING:
    from pathlib import Path


class JWTToken(RepoToken):
    app_id: int
    app_private_key_file: Path
    lifetime: timedelta

    expiration: datetime
    token: str

    def __init__(
        self,
        app_id: int,
        app_private_key_file: Path,
        lifetime: timedelta = timedelta(minutes=10),
    ) -> None:
        self.app_id = app_id
        self.app_private_key_file = app_private_key_file
        self.lifetime = lifetime

        self.token, self.expiration = JWTToken.generate_token(
            self.app_id, self.app_private_key_file, lifetime
        )

    @staticmethod
    def generate_token(
        app_id: int, app_private_key_file: Path, lifetime: timedelta
    ) -> tuple[str, datetime]:
        def build_jwt_payload(
            app_id: int, lifetime: timedelta
        ) -> tuple[dict[str, Any], datetime]:
            jwt_iat_drift: timedelta = timedelta(seconds=60)
            now: datetime = datetime.now(tz=UTC)
            iat: datetime = now - jwt_iat_drift
            exp: datetime = iat + lifetime
            jwt_payload = {
                "iat": int(iat.timestamp()),
                "exp": int(exp.timestamp()),
                "iss": str(app_id),
            }
            return (jwt_payload, exp)

        def rs256_sign(data: str, private_key_file: Path) -> str:
            signature = subprocess.run(  # noqa: S603
                [
                    "openssl",
                    "dgst",
                    "-binary",
                    "-sha256",
                    "-sign",
                    str(private_key_file),
                ],
                input=data.encode("utf-8"),
                stdout=subprocess.PIPE,
                check=True,
                cwd=os.environ.get("CREDENTIALS_DIRECTORY"),
            ).stdout
            return base64url(signature)

        def base64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        jwt, expiration = build_jwt_payload(app_id, lifetime)
        jwt_payload = json.dumps(jwt).encode("utf-8")
        json_headers = json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8")
        encoded_jwt_parts = f"{base64url(json_headers)}.{base64url(jwt_payload)}"
        encoded_mac = rs256_sign(encoded_jwt_parts, app_private_key_file)
        return (f"{encoded_jwt_parts}.{encoded_mac}", expiration)

        # installations = paginated_github_request("https://api.github.com/app/installations?per_page=100", generated_jwt)

        # return list(map(lambda installation: create_installation_access_token(installation['id']).json()["token"], installations))

    def get(self) -> str:
        if self.expiration - datetime.now(tz=UTC) < self.lifetime * 0.2:
            self.token, self.expiration = JWTToken.generate_token(
                self.app_id, self.app_private_key_file, self.lifetime
            )

        return self.token

    def get_as_secret(self) -> str:
        return "%(secret:github-jwt-token)"
