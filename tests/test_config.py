"""C3 config 测试（§8.1 C3 行）：app.options 解析校验 + 预设/fixture 双向校验。"""

from __future__ import annotations

import json
import sys
import copy
from pathlib import Path

import pytest

from core_parking.config import (ConfigError, effective_options,
                                 parse_app_options, validate_counting)

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def _counting_options() -> dict:
    return {
        "site_id": "demo-lot",
        "mqtt": {"host": "localhost", "port": 1883, "username": "u",
                 "password": "", "keepalive_s": 30},
        "http": {"port": 8080},
        "classes": [2, 3, 5, 7],
        "spool": {"max_events": 50000},
        "preview_frame_stride": 15,
        "counting": {
            "line": {"a": [0.10, 0.60], "b": [0.90, 0.60]},
            "in_side": "positive",
            "count_directions": ["in", "out"],
            "band": 0.03, "segment_margin": 0.05, "min_track_frames": 3,
            "min_travel": 0.06, "reborn_window_s": 2.0, "reborn_dist": 0.08,
            "capacity": 120, "initial_occupancy": 0, "publish_interval_ms": 1000,
        },
    }


# --------------------------------------------------------------- parse OK/NG

def test_parse_ok_returns_copy():
    opts = _counting_options()
    out = parse_app_options(opts)
    assert out["site_id"] == "demo-lot"
    out["site_id"] = "changed"       # 浅拷贝：顶层替换不影响原 dict
    assert opts["site_id"] == "demo-lot"


def test_unknown_key_reports_path():
    opts = _counting_options()
    opts["foo"] = 1
    with pytest.raises(ConfigError, match=r"app\.options\.foo"):
        parse_app_options(opts)


def test_unknown_nested_key_reports_path():
    opts = _counting_options()
    opts["mqtt"]["foo"] = 1
    with pytest.raises(ConfigError, match=r"app\.options\.mqtt\.foo"):
        parse_app_options(opts)


def test_unknown_counting_key_reports_path():
    opts = _counting_options()
    opts["counting"]["foo"] = 1
    with pytest.raises(ConfigError, match=r"app\.options\.counting\.foo"):
        parse_app_options(opts)


@pytest.mark.parametrize("mutate,match", [
    (lambda o: o.pop("mqtt"), "app.options.mqtt: required"),
    (lambda o: o.pop("http"), "app.options.http: required"),
    (lambda o: o.pop("spool"), "app.options.spool: required"),
    (lambda o: o.pop("site_id"), "app.options.site_id: required"),
    (lambda o: o.update(site_id="Demo_Lot"), "site_id"),
    (lambda o: o.update(site_id="x" * 33), "site_id"),
    (lambda o: o["mqtt"].pop("host"), "app.options.mqtt.host: required"),
    (lambda o: o["mqtt"].update(port=70000), "app.options.mqtt.port"),
    (lambda o: o["http"].update(port=0), "app.options.http.port"),
    (lambda o: o["spool"].update(max_events=0), "app.options.spool.max_events"),
    (lambda o: o.update(classes=[1]), "app.options.classes"),
    (lambda o: o.update(classes=[]), "app.options.classes"),
    (lambda o: o.pop("classes"), "app.options.classes"),
    (lambda o: o.pop("preview_frame_stride"), "app.options.preview_frame_stride"),
    (lambda o: o.update(preview_frame_stride=0), "app.options.preview_frame_stride"),
    (lambda o: o.pop("counting"), "app.options"),
    (lambda o: o.update(events_out=42), "app.options.events_out"),
])
def test_parse_errors_carry_field_path(mutate, match):
    opts = _counting_options()
    mutate(opts)
    with pytest.raises(ConfigError, match=match):
        parse_app_options(opts)


def test_counting_branch_requires_classes_and_stride():
    opts = _counting_options()
    del opts["classes"]
    with pytest.raises(ConfigError, match="app.options.classes"):
        parse_app_options(opts)


# ---------------------------------------------------------------- counting

def test_validate_counting_ok_normalizes_line_to_float():
    d = validate_counting({"line": {"a": [0, 1], "b": [1, 0]},
                           "in_side": "negative", "count_directions": ["in"],
                           "band": 0, "segment_margin": 0, "min_track_frames": 1,
                           "min_travel": 0, "reborn_window_s": 0,
                           "reborn_dist": 0, "capacity": 0,
                           "initial_occupancy": 0, "publish_interval_ms": 1})
    assert d["line"]["a"] == [0, 1]


@pytest.mark.parametrize("counting,match", [
    ({"in_side": "positive"}, "counting.line: required"),
    ({"line": {"a": [0.1, 0.6]}, "in_side": "positive",
      "count_directions": ["in"], "band": 0, "segment_margin": 0,
      "min_track_frames": 1, "min_travel": 0, "reborn_window_s": 0,
      "reborn_dist": 0, "capacity": 0, "initial_occupancy": 0,
      "publish_interval_ms": 1}, "counting.line.b: required"),
    (None, "counting: 必须为对象"),
])
def test_validate_counting_errors(counting, match):
    with pytest.raises(ConfigError, match=match):
        validate_counting(counting)


@pytest.mark.parametrize("a", [[-0.1, 0.5], [1.5, 0.5], [0.5], [0.5, 0.5, 0.5],
                               ["x", "y"], [True, 0.5]])
def test_validate_counting_rejects_bad_coords(a):
    counting = _counting_options()["counting"]
    counting["line"]["a"] = a
    with pytest.raises(ConfigError, match=r"counting\.line\.a"):
        validate_counting(counting)


def test_validate_counting_rejects_bad_in_side_and_directions():
    counting = _counting_options()["counting"]
    counting["in_side"] = "west"
    with pytest.raises(ConfigError, match="in_side"):
        validate_counting(counting)
    counting = _counting_options()["counting"]
    counting["count_directions"] = []
    with pytest.raises(ConfigError, match="count_directions"):
        validate_counting(counting)


# ------------------------------------------------------- effective_options

def test_effective_options_injects_state_dir():
    opts = effective_options({"site_id": "s"}, "/data/edge-parking")
    assert opts["state_dir"] == "/data/edge-parking"
    assert effective_options({"state_dir": "x"}, "/d")["state_dir"] == "x"


# ----------------------------------------------- 预设 / fixture 双向校验

def _load(name: str) -> dict:
    return json.loads((REPO / name).read_text(encoding="utf-8"))


# tests/fixtures/config.file.json 里有两处必须机器相关的值（§S2 要求
# native.binary 是绝对路径；file:// 流要能真的指向文件），提交的是占位符，
# 加载前在这里物化，否则这个 fixture 只在最初那台机器上能用。
def _materialize(name: str, tmp_path) -> Path:
    raw = (REPO / name).read_text(encoding="utf-8")
    raw = raw.replace("__REPO__", str(REPO))
    # 任何绝对可执行文件都能通过校验；用解释器自身，避免依赖某个构建目录
    raw = raw.replace("__VB_RUNTIME__", sys.executable)
    out = tmp_path / Path(name).name
    out.write_text(raw, encoding="utf-8")
    return out


@pytest.mark.parametrize("name", [
    "platforms/cpu/presets/counting.json",
    "tests/fixtures/config.file.json",
])
def test_configs_parse_as_parking_options(name, tmp_path):
    cfg = _load(name) if "__" not in (REPO / name).read_text() else json.loads(
        _materialize(name, tmp_path).read_text(encoding="utf-8"))
    app_options = copy.deepcopy(cfg["app"]["options"])
    out = parse_app_options(app_options)
    # 顶层 state_dir 注入后仍应通过（state_dir 是注入键，不在 schema 内）
    assert effective_options(out, cfg["state_dir"])["state_dir"] == cfg["state_dir"]


@pytest.mark.parametrize("name", [
    "platforms/cpu/presets/counting.json",
    "tests/fixtures/config.file.json",
])
def test_configs_validate_against_schema(name, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "contracts" / "app-options.schema.json")
                        .read_text(encoding="utf-8"))
    cfg = json.loads(_materialize(name, tmp_path).read_text(encoding="utf-8")) \
        if "__" in (REPO / name).read_text() else _load(name)
    jsonschema.validate(cfg["app"]["options"], schema)


@pytest.mark.parametrize("name", [
    "platforms/cpu/presets/counting.json",
    "tests/fixtures/config.file.json",
])
def test_configs_load_in_vision_base(name, tmp_path):
    from vision_base.config import load
    cfg = load(str(_materialize(name, tmp_path)))
    assert cfg.app["module"] == "core_parking.config:ParkingApp"
    # §S2：app.options.mqtt 与顶层 mqtt 的 host/port/username/password 一致
    top, app_m = cfg.mqtt, cfg.app["options"]["mqtt"]
    for k in ("host", "port", "username", "password"):
        assert top[k] == app_m[k], k
    assert cfg.mqtt["topic_root"] == \
        f"{cfg.app['options']['site_id']}/parking/{cfg.device_id}"


def test_fixture_stream_is_file_url_and_exists(tmp_path):
    cfg = json.loads(_materialize("tests/fixtures/config.file.json",
                                  tmp_path).read_text(encoding="utf-8"))
    url = cfg["streams"][0]["url"]
    assert url.startswith("file://")
    assert Path(url[len("file://"):]).exists()
    assert cfg["app"]["options"]["events_out"]
