from abc import abstractmethod


class RepoToken:
    @abstractmethod
    def get(self) -> str:
        pass

    @abstractmethod
    def get_as_secret(self) -> str:
        pass
