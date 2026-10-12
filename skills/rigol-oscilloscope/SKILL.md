---
name: rigol-oscilloscope
description: Capture and analyze signals with a RIGOL MHO900 oscilloscope through rigol-scope MCP. Discover versioned defaults and contracts, configure triggers and acquisition, retrieve waveforms, analyze saved records, and operate dual AFG outputs in bounded interactive tasks.
---

# RIGOL 示波器

任务 Agent 提供预期信号、测量目标和接线，决定初始设置及下一轮调整。示波器侧验证、执行一次、读回并返回证据；不自行反复调参，也不替调用方判断电路/固件正确性。

主要入口是调用设备通过 SSH stdio MCP 连接仪器服务主机，或在服务主机本地调用。采用 OpenClaw 专职 Agent 集成时，由 oscilloscope Agent 统一操作，其他内部 Agent 委派测量任务，结果回当前父级；普通 MCP 客户端不依赖 OpenClaw。

## Fast Path Contract

- 新采集：调用 `scope_contract` 查看当前合同，再用 `scope_profiles_list` / `scope_profile_get` 获取默认值、适用条件、版本和摘要；按需 `scope_measurement_preview`，然后 `scope_measurement_capture`。
- 外部单次刺激改用scope_prepare_capture；确认ready及质量后再发刺激，最后scope_prepared_collect/cancel。私有凭证留在同一调用方运行中，详见预约采集参考。
- 合同和 profile 查询不访问仪器。数值默认值以服务端 profile 为准，不在 Skill 或客户端复制一份；请求固定合同版本及 profile 的 id/version/digest。
- 参数优先级为本轮显式覆盖 > profile > 当前现场读回。探头倍率必须匹配物理探头；探头倍率、输入阻抗、线缆特性阻抗是不同参数。
- 选择触发、时间窗口、幅值范围和采样条件，先获得适合目标的波形，再分析。`expected_signal` 是上下文及质量检查输入，不会自动变成触发/时基设置。
- 原子采集在同一设备锁内配置、核对、SINGLE、导出和截图。检查 `verified`、实际读回、质量提示和失败阶段；MCP 调用返回不代表测量成功。结束保持 STOP，保留本轮设置。
- 已保存记录：直接用 `scope_captures_list` / `scope_capture_info` 和绘图、分析、搜索工具，无须先 identify/doctor 或上电。
- 标准客户端使用原始工具名，默认 device=`bench-scope`。OpenClaw 用 `mcp_call` 的 server=`rigol-scope`；原生投影名以当前工具列表为准。
- `confirmed=true` 表示当前用户任务已授权配置/采集，不要求每步再次问用户。AFG 接线和允许输出范围仍须来自任务上下文。

## 保留设置与减少切换

- 默认保留本轮通道、量程、阻抗、时基和触发配置，不在每次测试结束恢复旧设置；只改当前任务需要变化的项目，减少继电器切换。用户明确要求恢复时才恢复。
- AI完成任务、交还仪器前调用 `scope_release(confirmed=true)`：关闭不再使用的发生器输出，把存储深度设为AUTO，其他测量配置和发生器参数保留。存在明确持续输出任务时，才用keep_generator_outputs列出必须继续开启的路数；默认不保留任何输出。
- AUTO是空闲/交还仪器时的采样深度规则。测量时仍可根据目标显式选深度；原子RAW合同暂不接受AUTO，采集完成后再release回AUTO。
- 低功耗时可用scope_power_status只读检查；已授权的实机任务用scope_wake(confirmed=true)唤醒并检查verified。离线分析不唤醒仪器。低功耗不保留旧RAM波形，唤醒不能恢复旧记录。
- 通道配置工具先完成倍率/输入联动及读回，再设置量程；相同设置不重复写入。物理 10× 探头配 1 MΩ 输入和10×倍率，同轴线直连另行确定终端。

## 按任务读取

- 触发耦合、噪声抑制、Holdoff：[触发基础参数](references/trigger-options.md)。
- 必须先ready再由其他设备刺激的单次事件：[预约采集](references/prepared-capture.md)。
- 新调用方、默认配置或请求字段：[测量合同](references/measurement-contract.md)。
- 把预期信号转为采集条件、评估返回证据：[采集流程](references/capture-workflow.md)。
- 周期/PWM、窄脉冲、未触发等场景：[采集示例](references/capture-recipes.md)。
- 存储深度选择、最大500M能力和导出预算：[深记录](references/deep-records.md)。自动化按目标选较小够用深度，不默认拉满。
- 噪声/尖峰质量判断及正式离线双通道分析：[保存数据质量与时差](references/offline-quality-timing.md)。
- 双路发生器同步、相位符号和近零相位读数限制：[AFG 相位流程](references/afg-phase.md)。
- 单项控制、AFG、文件获取、已有记录分析：[工具参考](references/tool-reference.md)。

## Normal-Path Source Ban

正常调用通过 Skill、合同和工具 schema 获取能力，不查驱动源码、历史会话或维护脚本，不以原始 SCPI 绕过接口失败。出现错误时报告实际状态、已应用步骤及下一步建议；不自动重放写操作。

## 释放与唤醒边界

释放是任务完成时的显式工具调用，不是每个MCP请求结束，也不是进程退出钩子；CLI、远程客户端和OpenClaw调用方均需在任务收尾执行。工具持设备锁并逐项读回，但不是跨主机会话租约。断线/异常退出不能保证清理完成；结果不确定时说明状态未知，不能宣称输出已关。release在低功耗时不暗中唤醒，应先显式wake。休眠时包括身份在内的查询可能超时；wake仅向已绑定地址发送一次唤醒命令，随后核验序列号及电源状态。

AUTO的深度查询可能返回自动计算的实际点数而非AUTO字符串。工具检查命令完成、新SCPI错误队列和有效点数；不把数值读回误报为设置失败，也不声称独立读到了模式锁存位。

## 操作边界

- 当前原子流程支持 edge/pulse 触发；其他高阶触发即使硬件有许可，也不能当作 MCP 已支持。以合同中的 software_support 和实机选件分别判断。
- 同主机锁保护通过本服务协作的调用，不能锁住前面板或其他主机。原子流程不是失败回滚，旧采样记录不能恢复。
- 普通原子采集等待持续/自然事件；外部单次刺激用scope_prepare_capture，确认ready后刺激，再scope_prepared_collect。占用凭证留在同一调用方运行中，不能交给刺激Agent。结束、取消或到期检查cleanup_complete。
- 没有无人循环或自动续传；预约使用有明确到期时间的短时所有者进程，清理后退出。PLA2216 未接入；两路 AFG 经 10× 探头、1 MΩ 输入的低频正弦/方波回接，以及 GII 经 50 Ω 同轴线、50 Ω 输入的低频回接已通过；高频精度未验收。完整范围见项目验收文档。
- 文件路径不是已交付图片。通过 MCP resource 或 `scope_artifact_read` 取回产物并核对哈希；未看到图片不得声称已视觉检查。
