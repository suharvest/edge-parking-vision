"""CCPD 文件名解析测试（NRD-3 §7 / §8.1 P4）。

3 个样例均为 GPU x86 工作站上 CCPD2020 ccpd_green/train 的真实文件名（2026-10 提取）。
字符索引表对全量 11776 张的实证核对见 training/ccpd_to_coco.py 模块 docstring。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "training"))

from ccpd_to_coco import PROVINCES, ADS, PLATE_LETTERS, decode_plate, parse_ccpd_name  # noqa: E402

# 真实样例 1：0,0,3,24,28,24,31,33 → 皖 A D 0 4 0 7 9（新能源 D 牌）
SAMPLE_D = "00360785590278-91_265-311&485_406&524-406&524_313&520_311&485_402&489-0_0_3_24_28_24_31_33-117-16.jpg"
# 真实样例 2：0,0,3,26,25,31,33,32 → 皖 A D 2 1 7 9 8
SAMPLE_D2 = "00373372395833-90_96-276&514_387&548-387&548_276&547_276&516_384&514-0_0_3_26_25_31_33_32-157-19.jpg"
# 真实样例 3：0,0,3,24,25,30,31,31 → 皖 A D 0 1 6 7 7
SAMPLE_D3 = "00378472222222-90_268-291&442_400&477-396&476_292&477_291&442_400&442-0_0_3_24_25_30_31_31-125-83.jpg"

SAMPLES = [SAMPLE_D, SAMPLE_D2, SAMPLE_D3]
EXPECTED_PLATES = ["皖AD04079", "皖AD21798", "皖AD01677"]
EXPECTED_BBOXES = [(311, 485, 406, 524), (276, 514, 387, 548), (291, 442, 400, 477)]
EXPECTED_VERTICES = [
    ((406, 524), (313, 520), (311, 485), (402, 489)),
    ((387, 548), (276, 547), (276, 516), (384, 514)),
    ((396, 476), (292, 477), (291, 442), (400, 442)),
]


def test_parse_fields():
    for name, bbox, verts in zip(SAMPLES, EXPECTED_BBOXES, EXPECTED_VERTICES):
        info = parse_ccpd_name(name)
        assert info.bbox == bbox
        assert info.vertices == verts
        assert info.brightness > 0 and info.blur >= 0
        assert len(info.indices) == 8
    info = parse_ccpd_name(SAMPLE_D)
    assert info.area == (91, 265)
    assert info.brightness == 117 and info.blur == 16
    info = parse_ccpd_name(SAMPLE_D2)
    assert info.area == (90, 96)
    assert info.brightness == 157 and info.blur == 19


def test_decode_plate():
    for name, plate in zip(SAMPLES, EXPECTED_PLATES):
        assert parse_ccpd_name(name).plate == plate
    # 直接解码
    assert decode_plate((0, 0, 3, 24, 28, 24, 31, 33)) == "皖AD04079"
    # 蓝牌 7 位（CCPD2019 到位后同一路径）
    assert decode_plate((0, 1, 24, 25, 26, 27, 28)) == "皖B01234"


def test_char_tables():
    # 31 个省份简称（PROVINCES 前 29 + 警学等特殊位）与 GA 36 序号约束
    assert len(PROVINCES) == 35
    assert PROVINCES[0] == "皖"
    assert "I" not in PLATE_LETTERS and "O" not in PLATE_LETTERS
    assert len(PLATE_LETTERS) == 24
    # 序号表：字母在前（D=3、F=5），数字在后（'0'=24、'9'=33）——新能源 D/F 位实证
    assert len(ADS) == 34
    assert ADS[3] == "D" and ADS[5] == "F"
    assert ADS[24:34] == "0123456789"


def test_bad_names():
    import pytest

    for bad in [
        "not-a-ccpd-name.jpg",
        "1-2-3-4-5-6.jpg",  # 字段值格式错
        "1-1_2-3-0_0_3_99_24_28_24_31_33-1-1.jpg",  # ads 索引越界
    ]:
        with pytest.raises(ValueError):
            parse_ccpd_name(bad)
