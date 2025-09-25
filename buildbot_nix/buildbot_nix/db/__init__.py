"""Database components for buildbot-nix."""

from .failed_builds import FailedBuild, FailedBuildsConnectorComponent
from .failed_status import FailedStatusConnectorComponent

__all__ = [
    "FailedBuild",
    "FailedBuildsConnectorComponent",
    "FailedStatusConnectorComponent",
]
