"""契约 fixture 测试（§8.1 C0）：

- contracts/fixtures/*.valid.json 必须在 validate_payload.py 与 jsonschema 下都通过；
- contracts/fixtures/*.invalid-*.json 必须在两者下都被拒；
- 即标准库实现与 jsonschema（CI 交叉校验）结论一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import referencing

import validate_payload

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
FIXTURES = CONTRACTS / "fixtures"

KIND_BY_PREFIX = {
    "crossing": "crossing-event.schema.json",
    "occupancy": "occupancy.schema.json",
    "app-options": "app-options.schema.json",
    "plate-event": "plate-event.schema.json",
    "plate-crop": "plate-crop.schema.json",
    "gate-event": "gate-event.schema.json",
    "whitelist": "whitelist.schema.json",
    "gate-config": "gate-config.schema.json",
    "slots": "slots.schema.json",
}


def _registry() -> referencing.Registry:
    resources = []
    for schema_file in CONTRACTS.glob("*.schema.json"):
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        resource = referencing.Resource.from_contents(
            schema, default_specification=referencing.jsonschema.DRAFT202012
        )
        resources.append((schema["$id"], resource))
    return referencing.Registry().with_resources(resources)


_REGISTRY = _registry()


def _jsonschema_valid(kind: str, payload: dict) -> bool:
    schema = json.loads((CONTRACTS / KIND_BY_PREFIX[kind]).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, registry=_REGISTRY, format_checker=jsonschema.FormatChecker()
    )
    return validator.is_valid(payload)


def _kind_and_polarity(name: str) -> tuple[str, bool]:
    for prefix in KIND_BY_PREFIX:
        if name.startswith(prefix + "."):
            return prefix, ".invalid-" not in name
    raise AssertionError(f"无法从文件名推断 kind: {name}")


def _all_fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


@pytest.mark.parametrize("path", _all_fixtures(), ids=lambda p: p.name)
def test_fixture_stdlib_validator(path: Path) -> None:
    kind, expect_valid = _kind_and_polarity(path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_payload.validate(kind, payload)
    if expect_valid:
        assert errors == [], f"{path.name} 应通过，实际错误: {errors}"
    else:
        assert errors, f"{path.name} 应被拒，实际通过"


@pytest.mark.parametrize("path", _all_fixtures(), ids=lambda p: p.name)
def test_fixture_jsonschema_agrees(path: Path) -> None:
    kind, expect_valid = _kind_and_polarity(path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    stdlib_errors = validate_payload.validate(kind, payload)
    jsonschema_ok = _jsonschema_valid(kind, payload)
    assert (not stdlib_errors) == jsonschema_ok, (
        f"{path.name}: validate_payload 与 jsonschema 结论不一致 "
        f"(stdlib errors={stdlib_errors!r}, jsonschema valid={jsonschema_ok})"
    )
    assert jsonschema_ok == expect_valid, f"{path.name}: 期望 valid={expect_valid}"


def test_each_kind_has_valid_and_three_invalid() -> None:
    for prefix in KIND_BY_PREFIX:
        assert (FIXTURES / f"{prefix}.valid.json").exists(), prefix
        invalid = sorted(FIXTURES.glob(f"{prefix}.invalid-*.json"))
        assert len(invalid) >= 3, f"{prefix} 的 invalid fixture 不足 3 个: {invalid}"
