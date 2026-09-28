"""NRD-4 车位状态包（§6.1/§6.2）：事件级钩子 `hooks_slots`。

判定与去抖在基座 `slot_coverage`（BASE-1 M4.1，§8.0：本仓库不实现）；
本包只有事件级汇总：slot_change 事件 → 车位状态表 → retained `…/slots`
payload；断流超时置 unknown；恢复 running 回到最后已知状态。
"""
from .hooks_slots import SlotHooks, SlotsOptions, slot_coverage_config

__all__ = ["SlotHooks", "SlotsOptions", "slot_coverage_config"]
