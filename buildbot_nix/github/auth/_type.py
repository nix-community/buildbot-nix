from dataclasses import dataclass
from pathlib import Path


@dataclass
class AuthType:
    pass


@dataclass
class AuthTypeLegacy(AuthType):
    token_secret_name: str = "github-token"


@dataclass
class AuthTypeApp(AuthType):
    app_id: int
    app_secret_key_name: str = "github-app-secret-key"
    app_installation_token_map_name: Path = Path(
        "github-app-installation-token-map.json"
    )
    app_project_id_map_name: Path = Path("github-app-project-id-map-name")
    app_jwt_token_name: Path = Path("github-app-jwt-token")
