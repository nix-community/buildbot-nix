from __future__ import annotations

import typing
from typing import Any

import requests
from buildbot.www.oauth2 import OAuth2Auth
from twisted.logger import Logger

from buildbot_nix.common import http_request
from buildbot_nix.errors import BuildbotNixError

if typing.TYPE_CHECKING:
    from buildbot_nix.models import OIDCConfig, OIDCMappingConfig

log = Logger()


class OIDCAuth(OAuth2Auth):
    faIcon = "openid"  # noqa: N815
    mapping: OIDCMappingConfig

    def __init__(self, oidc_config: OIDCConfig) -> None:
        self.name = oidc_config.name
        self.mapping = oidc_config.mapping
        self.authUriAdditionalParams = {"scope": " ".join(oidc_config.scope)}

        try:
            # Trying our best to follow https://openid.net/specs/openid-connect-discovery-1_0-15.html#ProviderConfig
            config = http_request(url=oidc_config.discovery_url, method="GET").json()

            self.authUri = config["authorization_endpoint"]
            self.tokenUri = config["token_endpoint"]
            self.resourceEndpoint = config["userinfo_endpoint"]
        except Exception as e:
            message = f"Failed to fetch {oidc_config.discovery_url}"
            raise BuildbotNixError(message) from e

        super().__init__(oidc_config.client_id, oidc_config.client_secret)

    def getUserInfoFromOAuthClient(  # noqa: N802
        self, c: requests.Session
    ) -> dict[str, Any]:
        response = c.get(self.resourceEndpoint)

        if not response.ok:
            message = f"Failed to fetch user info from {self.name}: {response.text}"
            raise BuildbotNixError(message)

        user = response.json()

        info_obj = {
            "username": user[self.mapping.username],
            "email": user[self.mapping.email],
            "full_name": user[self.mapping.full_name],
        }

        if self.mapping.groups is not None:
            info_obj["groups"] = user.get(self.mapping.groups or "groups", [])

        return info_obj

    def createSessionFromToken(  # noqa: N802
        self, token: dict[str, Any]
    ) -> requests.Session:
        s = requests.Session()
        s.headers = {
            "Authorization": "Bearer " + token["access_token"],
        }
        s.verify = self.ssl_verify
        return s
