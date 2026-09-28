#!/usr/bin/env python3
"""停车 MQTT 事件载荷校验（标准库实现，设备上也能跑；§S3 / §8.1 C0）。

只用 Python 标准库，不 import jsonschema；CI 另用 jsonschema 交叉校验
（tests/test_contracts.py 断言两者对每个 fixture 的结论一致）。

支持的 JSON Schema draft 2020-12 关键字（即本仓库 contracts/*.schema.json 用到的子集）：
  type / required / properties / additionalProperties / pattern / format(uuid)
  enum / const / minimum / maximum / minLength / minItems / maxItems / uniqueItems
  items / allOf / oneOf / $ref（相对文件 与 #/$defs/<name>）/ $defs
  unevaluatedProperties（false；与 allOf 组合实现事件 schema 的字段封闭）
  $schema / $id / description 等注解忽略。

公开 API：
  validate(kind: str, payload: dict) -> list[str]   # 空列表 = 通过
  kind ∈ {crossing, occupancy, app-options, plate-event, plate-crop,
          gate-event, whitelist, gate-config, slots}

CLI：
  python contracts/validate_payload.py <kind> <file.json>
  校验失败打印错误并退出 1；JSON 解析失败同样退出 1。
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent

KINDS = {
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

_schema_cache: dict[str, dict] = {}


def _load_schema(name: str) -> dict:
    if name not in _schema_cache:
        with open(_SCHEMA_DIR / name, encoding="utf-8") as f:
            _schema_cache[name] = json.load(f)
    return _schema_cache[name]


def _is_int(value) -> bool:
    # bool 是 int 的子类，但 JSON Schema 里 boolean 不是 integer/number
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return _is_int(value)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported type {expected!r}")


def _resolve_ref(ref: str, base_file: str) -> tuple[dict, str]:
    """返回 (schema, 所在文件名)。支持 'envelope.schema.json' 与 '#/$defs/x'。"""
    if ref.startswith("#/"):
        node = _load_schema(base_file)
        for part in ref[2:].split("/"):
            node = node[part]
        return node, base_file
    if "#" in ref:
        file_part, frag = ref.split("#", 1)
        target = file_part or base_file
        node = _load_schema(target)
        if frag:
            for part in frag.lstrip("/").split("/"):
                node = node[part]
        return node, target
    return _load_schema(ref), ref


def _check(schema: dict, value, path: str, base_file: str, errors: list[str],
           evaluated: set | None = None) -> None:
    # evaluated：当前实例位置（同一个 object）已被 properties 评估的键集合，
    # 跨 allOf/$ref 共享，供 unevaluatedProperties 使用（draft 2020-12 语义）。
    if isinstance(value, dict) and evaluated is None:
        evaluated = set()
    if "$ref" in schema:
        target, target_file = _resolve_ref(schema["$ref"], base_file)
        _check(target, value, path, target_file, errors, evaluated)
        return

    if "allOf" in schema:
        for sub in schema["allOf"]:
            _check(sub, value, path, base_file, errors, evaluated)

    if "oneOf" in schema:
        matched = 0
        for sub in schema["oneOf"]:
            sub_errors: list[str] = []
            _check(sub, value, path, base_file, sub_errors, evaluated)
            if not sub_errors:
                matched += 1
        if matched != 1:
            errors.append(f"{path}: oneOf 期望恰好 1 个分支通过，实际 {matched}")

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {value!r}")
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 值 {value!r} 不在枚举 {schema['enum']!r} 内")

    expected = schema.get("type")
    if expected is not None and not _type_ok(value, expected):
        errors.append(f"{path}: 类型错误，期望 {expected}，实际 {type(value).__name__}")
        return

    if isinstance(value, dict):
        assert evaluated is not None
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: 缺少必需字段")
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in value:
                evaluated.add(key)
                _check(subschema, value[key], f"{path}.{key}", base_file, errors)
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: 不允许的额外字段")
        if schema.get("unevaluatedProperties") is False:
            for key in value:
                if key not in evaluated:
                    errors.append(f"{path}.{key}: 不允许的额外字段")

    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: 值 {value!r} 不匹配 pattern {schema['pattern']!r}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: 长度 {len(value)} < minLength {schema['minLength']}")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError, TypeError):
                errors.append(f"{path}: 值 {value!r} 不是合法 uuid")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(value)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: 元素数 {len(value)} > maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{path}: 元素不唯一（uniqueItems）")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _check(item_schema, item, f"{path}[{i}]", base_file, errors)
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: 值 {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: 值 {value} > maximum {schema['maximum']}")


def validate(kind: str, payload: dict) -> list[str]:
    """按 contracts/<kind>.schema.json 校验 payload，返回错误列表（空 = 通过）。"""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}，可选：{sorted(KINDS)}")
    if not isinstance(payload, dict):
        return [f"$: 顶层必须是 object，实际 {type(payload).__name__}"]
    schema_file = KINDS[kind]
    errors: list[str] = []
    _check(_load_schema(schema_file), payload, "$", schema_file, errors)
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"用法: {argv[0]} <{'|'.join(KINDS)}> <file.json>", file=sys.stderr)
        return 2
    kind, file_path = argv[1], argv[2]
    try:
        with open(file_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID: 无法读取/解析 {file_path}: {exc}", file=sys.stderr)
        return 1
    try:
        errors = validate(kind, payload)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print(f"OK: {file_path} 通过 {kind} 校验")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
