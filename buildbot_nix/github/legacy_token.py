from .repo_token import RepoToken


class LegacyToken(RepoToken):
    token: str

    def __init__(self, token: str) -> None:
        self.token = token

    def get(self) -> str:
        return self.token

    def get_as_secret(self) -> str:
        return "%(secret:github-token)"
