# NRD4 容量 FPS 长窗测量

`--fps-window-secs` 是显式 opt-in 的测量版本。未设置该参数时，
`evaluation/stress_slots.py` 保持既有 health FPS minimum、strict native、
queue、drops 记录和 RK RGA 判据；旧 raw 不会被重新判定。

该窗口规则是本次容量测量的冻结具体化，不能表述为 NRD4 原文已经规定
120 秒窗口、native completed counter 或 BASE-1 §10.1 对 NRD4 的直接授权。
默认运行目标为 `--fps-window-secs 120`，容量运行仍要求 poll 与 native
覆盖至少 600 秒。

长窗模式的 FPS 计算为每路、每 shard、同一 native generation 的
`processed_frames` 增量除以实际 `stats_monotonic_ms` 增量。窗口起点取首个
fresh snapshot；边界取首个 native timestamp 达到起点加窗口长度的 fresh
snapshot，使用真实 elapsed，不能用 nominal 120 秒作分母。重复
`(pid, restart generation, shard, stats_monotonic_ms)` 快照只保留一次；同一
timestamp 的 processed 或 dropped counter 内容冲突即 integrity failure。

每路至少要有三个完整 120 秒窗口，且每个完整窗口都须达到
`0.9 * target_fps`。全程 native 平均也须达到该阈值。poll/native 覆盖不足
600 秒、无完整窗口、generation/PID/clock/count reset、缺 expected IDs、
non-running、HTTP 失败和其他既有 strict integrity failure 均拒绝通过。

最后一个完整窗口之后不足 120 秒的尾段不会丢弃：它会合并到最后一个
coverage window，coverage window 使用从该完整窗口起点到最终 fresh endpoint
的实际 elapsed，并同时记录尾段自己的起止、elapsed、counter delta 和 rate。
完整窗口是额外的严格检查，coverage windows 覆盖全程；两组允许最后窗口
重叠，不能把重叠误报成额外独立窗口。窗口数量、实际边界和 tail diagnostic
都写入 JSON。

health FPS、逐样本 queue、drops（全程及逐窗）和完整 RGA 日志仍保留为
diagnostic；capacity opt-in 只移除 health FPS minimum 作为 FPS reject gate，
不移除 queue、RGA、HTTP、运行状态或 native 完整性拒绝。

输出包含 `measurement_version`、`timebase`、`fps_window_secs`、
`aggregation`、`target_fps`（CLI）、`native_revision` 和 `collector_sha`。
revision/SHA 若输入样本没有提供则写 `null`，不会伪造来源信息。

诊断字段还包括 fresh completed-counter plateau 的起止与 duration、最大
native sampling gap、最大 poll sampling gap、缓存重复数量及最长连续重复、
每路 health FPS 为零的 timestamp/count，以及 poll/native 的 wall-clock gap。
不同设备的 monotonic clock 不直接相减；没有可比 poll monotonic 数据时对应
字段为 `unavailable`。这些 snapshot 诊断不能排除两个 fresh snapshot 之间
已经发生并恢复的 sub-snapshot stall；本版本不增加 hard stall threshold。
