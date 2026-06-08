"""JSON round-trip test for every Pydantic model in the package."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

import buildbot_nix


def find_all_pydantic_models() -> list[type[BaseModel]]:
    """Discover all BaseModel subclasses defined in buildbot_nix."""
    models = []
    for _importer, modname, _ispkg in pkgutil.walk_packages(
        path=list(buildbot_nix.__path__),
        prefix="buildbot_nix.",
        onerror=lambda _x: None,
    ):
        # The worker module needs environment setup at import time.
        if modname.endswith(".worker"):
            continue
        module = importlib.import_module(modname)
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__ == modname
            ):
                models.append(obj)
    return models


ALL_PYDANTIC_MODELS = sorted(
    set(find_all_pydantic_models()),
    key=lambda c: (c.__module__, c.__name__),
)

if not ALL_PYDANTIC_MODELS:
    pytest.skip("No Pydantic models discovered", allow_module_level=True)


def _handle_ref_field(
    model_class: type[BaseModel], field_info: dict[str, Any]
) -> Any | None:
    """Minimal data for a $ref to a nested model."""
    if "$ref" not in field_info:
        return None

    ref_path = field_info["$ref"]
    if not ref_path.startswith("#/$defs/"):
        return None

    ref_model_name = ref_path.split("/")[-1]
    schema = model_class.model_json_schema()

    if "$defs" not in schema or ref_model_name not in schema["$defs"]:
        return None

    ref_schema = schema["$defs"][ref_model_name]
    ref_data = {}
    ref_required = ref_schema.get("required", [])
    ref_properties = ref_schema.get("properties", {})

    for ref_field in ref_required:
        ref_field_info = ref_properties.get(ref_field, {})
        ref_data[ref_field] = get_minimal_value_for_field(
            model_class, ref_field, ref_field_info
        )
    return ref_data


def _handle_union_field(
    model_class: type[BaseModel], field_name: str, field_info: dict[str, Any]
) -> Any | None:
    """Minimal data for an anyOf field: first non-null option."""
    if "anyOf" not in field_info:
        return None

    for option in field_info["anyOf"]:
        if option.get("type") == "null":
            continue
        return get_minimal_value_for_field(model_class, field_name, option)
    return None


type_map = {
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
    "string": "test",
}


def get_minimal_value_for_field(
    model_class: type[BaseModel], field_name: str, field_info: dict[str, Any]
) -> Any:
    """Generate a minimal valid value for a field from its JSON schema."""
    if field_info.get("format") == "date-time":
        return datetime.now(tz=UTC)

    if (value := _handle_ref_field(model_class, field_info)) is not None:
        return value

    if (value := _handle_union_field(model_class, field_name, field_info)) is not None:
        return value

    return type_map.get(field_info.get("type", "string"))


@pytest.mark.parametrize(
    "model_class",
    ALL_PYDANTIC_MODELS,
    ids=lambda c: f"{c.__module__}.{c.__name__}",
)
def test_pydantic_model_round_trips(
    model_class: type[BaseModel],
) -> None:
    """Every model survives a JSON round trip: catches alias mismatches
    and serializers that drop or rename fields."""
    schema = model_class.model_json_schema()
    properties = schema.get("properties", {})
    minimal_data: dict[str, Any] = {
        field: get_minimal_value_for_field(
            model_class, field, properties.get(field, {})
        )
        for field in schema.get("required", [])
    }

    instance = model_class(**minimal_data)
    json_data = instance.model_dump_json(by_alias=True)
    restored = model_class.model_validate_json(json_data)
    assert restored.model_dump(by_alias=True) == instance.model_dump(by_alias=True)
