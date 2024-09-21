import dbm
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    database: None | dbm._Database = None
else:
    database: Any = None


class FailedBuildsError(Exception):
    pass


class FailedBuild(BaseModel):
    derivation: str
    time: datetime
    url: str = Field(default="unknown")


DB_NOT_INIT_MSG = "Database not initialized"


def initialize_database(db_path: Path) -> None:
    global database  # noqa: PLW0603

    if not database:
        database = dbm.open(str(db_path), "c")


def add_build(derivation: str, time: datetime, url: str) -> None:
    global database  # noqa: PLW0602

    if database is not None:
        database[derivation] = FailedBuild(
            derivation=derivation, time=time, url=url
        ).model_dump_json()
    else:
        raise FailedBuildsError(DB_NOT_INIT_MSG)


def check_build(derivation: str) -> FailedBuild | None:
    global database  # noqa: PLW0602

    if database is not None:
        if derivation in database:
            # TODO create dummy if deser fails?
            return FailedBuild.model_validate_json(database[derivation])
        return None
    raise FailedBuildsError(DB_NOT_INIT_MSG)


def remove_build(derivation: str) -> None:
    global database  # noqa: PLW0602

    if database is not None:
        del database[derivation]
    else:
        raise FailedBuildsError(DB_NOT_INIT_MSG)
