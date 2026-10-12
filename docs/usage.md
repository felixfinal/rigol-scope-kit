# 使用流程

先完成 [uv 安装](uv.md)和身份绑定。下列实机步骤只有在当前任务授权、接线明确时才执行。

## 新测量任务

1. 读取 `scope_contract(section="all")`，查询 `scope_profiles_list` / `scope_profile_get`。
2. 对照物理探头、输入终端和目标选 profile；复制返回的 id/version/digest，不能猜摘要。
3. 设置 `channels`、必要 overrides 和 expected_signal；用 `scope_measurement_preview` 检查计划。
4. 调用 `scope_measurement_capture`，`confirmed=true` 表示当前任务已经授权变更。
5. 检查 verified、实际读回、质量、触发状态和产物，再决定是否调整。
6. 任务结束调用 `scope_release(confirmed=true)`，检查结果；有明确持续输出任务才显式 keep 对应输出。

profile 数值和字段以当前返回为准。默认不恢复旧通道/触发/量程，减少无意义继电器切换。
生成本轮请求模板可运行：

```bash
uv run --frozen --no-sync python examples/request_template.py > request.json
```

该示例只使用内置 profile 和合同，不访问仪器；生成的采集条件必须由调用方按实际任务调整。
可将请求文件用于 CLI 的 `--params`，其顶层包含 request/confirmed，CLI 的 device 在独立 `--device` 参数指定。
未完成接线确认前只生成模板，不执行采集。

## 先等待，再刺激

`scope_prepare_capture` → 确认 ready 且 WAIT → 刺激 → `scope_prepared_collect`。
不再使用时 `scope_prepared_cancel`，并检查 cleanup_complete。
租约 5–120 秒，ready 等待 0.1–10 秒，导出截止时间 5–900 秒，均需符合任务预算。
私有 reservation_token 保留在同一调用方运行中，不能交给刺激 Agent。
内部发生器启用使用 `scope_prepared_generator_output`；普通工具在预约期间会拒绝设备占用。
详见 [预约流程](../skills/rigol-oscilloscope/references/prepared-capture.md)。

## AFG 输出

先确认实际 GI/GII 接线、通道倍率、输入阻抗和允许电压。
读取/配置发生器后获得新鲜 settings hash，再按工具 schema 提供电压界限开启输出。
GI/GII 对应发生器 1/2 路，与示波器 CH1/CH2 的接线没有固定对应关系。
50 Ω 同轴直连可配 50 Ω 终端，但需确认源电压和功耗；物理 10× 探头使用 10× 倍率及 1 MΩ 输入。
任务结束 release 默认关闭两路未用输出。

## 已保存数据

`scope_captures_list` → `scope_capture_info` → 选择 channel/start/count → 绘图/分析/搜索/时差。
这些工具不会唤醒或访问仪器；服务配置仍用于定位状态和 profile 目录。
产物用 resource 或 `scope_artifact_read` 取回，核对 sha256；只有远端路径不能宣称已拿到图片。
最大深记录先取需要的窗口，双通道分析每路最多 1M 点。

## MCP 无仪器连通检查

```bash
uv run --frozen --no-sync python examples/mcp_discovery.py
```

该例经过真实 stdio 初始化、list_tools、合同/profile/Skill resource 读取；使用文档保留地址，不发 SCPI。
实机只读检查脚本在 `scripts/`：运行前必须显式提供配置/启动命令及输出目录。
它们不会配置仪器，但会连接示波器并截图，不应放进无人 CI。
