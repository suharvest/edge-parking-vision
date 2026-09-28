"""COCO 车辆类别映射（规格 §6.1、§S2 `app.options.classes`）。

`line_cross` 事件 `fields.class_id`（§S13 B4）是 COCO 类别 id；
`parking.crossing/1` 的 `vehicle_class` 只取 `car|motorcycle|bus|truck`。
"""
from __future__ import annotations

__all__ = ["CLASS_NAMES", "CLASS_IDS", "vehicle_class"]

# COCO id → parking.crossing/1 vehicle_class
CLASS_NAMES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# 默认下发给 line_cross 的 classes 过滤（§S2 counting 预设）
CLASS_IDS: tuple[int, ...] = tuple(CLASS_NAMES)


def vehicle_class(class_id: int) -> str:
    """COCO class_id → vehicle_class；未知 id 抛 KeyError（调用方决定跳过还是报错）。"""
    return CLASS_NAMES[int(class_id)]
