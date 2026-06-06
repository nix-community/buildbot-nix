"""Regression tests for NixEvalJob parsing."""

from __future__ import annotations

from buildbot_nix.models import NixEvalJobModel, NixEvalJobSuccess


def test_impure_job_with_null_out_parses() -> None:
    """nix-eval-jobs emits outputs with null values for impure / CA
    derivations that have no statically-known output path. We must accept
    such jobs instead of crashing the entire eval step.

    See: https://buildbot.thalheim.io/api/v2/logs/671634/raw_inline
    """
    raw = {
        "attr": "aarch64-linux.pytest-impure",
        "attrPath": ["checks", "aarch64-linux", "pytest-impure"],
        "cacheStatus": "notBuilt",
        "neededBuilds": [],
        "neededSubstitutes": [],
        "drvPath": "/nix/store/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-pytest-impure.drv",
        "inputDrvs": {},
        "name": "pytest-impure",
        "outputs": {"out": None},
        "system": "aarch64-linux",
    }

    job = NixEvalJobModel.validate_python(raw)

    assert isinstance(job, NixEvalJobSuccess)
    assert job.outputs == {"out": None}
    # Downstream code does `job.outputs["out"] or None`; ensure that still works.
    assert (job.outputs["out"] or None) is None
