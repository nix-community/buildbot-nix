import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from buildbot_nix.common import (
    HttpResponse,
    atomic_write_file,
    http_request,
)

from .jwt_token import JWTToken
from .repo_token import RepoToken


class InstallationToken(RepoToken):
    GITHUB_TOKEN_LIFETIME: timedelta = timedelta(minutes=60)

    jwt_token: JWTToken
    installation_id: int

    token: str
    expiration: datetime
    installations_token_map_name: Path

    @staticmethod
    def _create_installation_access_token(
        jwt_token: JWTToken, installation_id: int
    ) -> HttpResponse:
        return http_request(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            data={},
            headers={"Authorization": f"Bearer {jwt_token.get()}"},
            method="POST",
        )

    @staticmethod
    def _generate_token(
        jwt_token: JWTToken, installation_id: int
    ) -> tuple[str, datetime]:
        token = InstallationToken._create_installation_access_token(
            jwt_token, installation_id
        ).json()["token"]
        expiration = datetime.now(tz=UTC) + InstallationToken.GITHUB_TOKEN_LIFETIME

        return token, expiration

    def __init__(
        self,
        jwt_token: JWTToken,
        installation_id: int,
        installations_token_map_name: Path,
        installation_token: None | tuple[str, datetime] = None,
    ) -> None:
        self.jwt_token = jwt_token
        self.installation_id = installation_id
        self.installations_token_map_name = installations_token_map_name

        if installation_token is None:
            self.token, self.expiration = InstallationToken._generate_token(
                self.jwt_token, self.installation_id
            )
            self._save()
        else:
            self.token, self.expiration = installation_token

    def get(self) -> str:
        self.verify()
        return self.token

    def get_as_secret(self) -> str:
        return f"%(secret:github-token-{self.installation_id})"

    def verify(self) -> None:
        if datetime.now(tz=UTC) - self.expiration > self.GITHUB_TOKEN_LIFETIME * 0.8:
            self.token, self.expiration = InstallationToken._generate_token(
                self.jwt_token, self.installation_id
            )
            self._save()

    def _save(self) -> None:
        # of format:
        # {
        #   123: {
        #     expiration: <datetime>,
        #     token: "token"
        #   }
        # }
        installations_token_map: dict[int, Any]
        if self.installations_token_map_name.exists():
            installations_token_map = json.loads(
                self.installations_token_map_name.read_text()
            )
        else:
            installations_token_map = {}

        installations_token_map[self.installation_id] = {
            "expiration": self.expiration.isoformat(),
            "token": self.token,
        }

        atomic_write_file(
            self.installations_token_map_name, json.dumps(installations_token_map)
        )
