"""Test that all Pydantic models can be instantiated."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import buildbot_nix


def find_all_pydantic_models() -> list[type[BaseModel]]:
    """Dynamically discover all Pydantic BaseModel subclasses in buildbot_nix."""
    models = []

    # Get the package path
    package_path = Path(buildbot_nix.__file__).parent

    # Iterate through all modules in the package
    for _importer, modname, _ispkg in pkgutil.walk_packages(
        path=[str(package_path)],
        prefix="buildbot_nix.",
        onerror=lambda _x: None,
    ):
        # Skip modules that require environment variables or special setup
        if modname.endswith(".worker"):
            continue
        module = importlib.import_module(modname)

        # Find all classes in the module
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            # Check if it's a Pydantic BaseModel subclass
            if (
                issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__ == modname  # Only models defined in this module
            ):
                models.append(obj)

    return models


# Collect all models at module load time
ALL_PYDANTIC_MODELS = find_all_pydantic_models()


def _handle_ref_field(
    model_class: type[BaseModel], field_info: dict[str, Any]
) -> Any | None:
    """Handle reference to other models."""
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
    """Handle anyOf (union types)."""
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
    """Generate a minimal valid value for a field based on its type information."""
    # Check if it's a datetime field
    if "format" in field_info and field_info["format"] == "date-time":
        return datetime.now(tz=UTC)

    # Try reference field
    if (value := _handle_ref_field(model_class, field_info)) is not None:
        return value

    # Try union field
    if (value := _handle_union_field(model_class, field_name, field_info)) is not None:
        return value

    # Handle basic types
    return type_map.get(field_info.get("type", "string"))


@pytest.mark.parametrize("model_class", ALL_PYDANTIC_MODELS)
def test_pydantic_model_can_be_instantiated(
    model_class: type[BaseModel],
) -> None:
    """Test that each Pydantic model can be instantiated with minimal valid data."""
    # Get the model's schema to understand required fields
    schema = model_class.model_json_schema()

    # Build minimal valid data for required fields
    required_fields = schema.get("required", [])
    minimal_data: dict[str, Any] = {}
    properties = schema.get("properties", {})

    for field in required_fields:
        field_info = properties.get(field, {})
        minimal_data[field] = get_minimal_value_for_field(
            model_class, field, field_info
        )

    # Try to create an instance with minimal required data
    instance = model_class(**minimal_data)
    assert instance is not None

    # Verify the model can be serialized and deserialized
    # Use by_alias=True to ensure proper serialization for models with aliases
    json_data = instance.model_dump_json(by_alias=True)
    restored = model_class.model_validate_json(json_data)
    assert restored is not None
