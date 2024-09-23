import dbm
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class FailedBuildsError(Exception):
    pass


class FailedBuild(BaseModel):
    derivation: str
    time: datetime
    url: str = Field(
        default=f"build unknown. Please delete {Path("failed_builds.dbm").resolve()} to get a build url"
    )


class FailedBuildDB:
    def __init__(self, db_path: Path) -> None:
        self.database = dbm.open(str(db_path), "c")

    def close(self) -> None:
        self.database.close()

    def add_build(self, derivation: str, time: datetime, url: str) -> None:
        self.database[derivation] = FailedBuild(
            derivation=derivation, time=time, url=url
        ).model_dump_json()

    def check_build(self, derivation: str) -> FailedBuild | None:
        if derivation in self.database:
            # TODO create dummy if deser fails?
            return FailedBuild.model_validate_json(self.database[derivation])
        return None

    def remove_build(self, derivation: str) -> None:
        del self.database[derivation]
