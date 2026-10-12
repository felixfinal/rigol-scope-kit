# 测量合同与默认配置

## 获取与调用

`scope_contract(section="all")` 返回合同版本、请求/profile JSON Schema、软件能力和错误约定。`scope_profiles_list` 列出 profile，`scope_profile_get(profile_id)` 的 `profile` 字段提供完整默认值、assumptions、version 和 digest。

示例调用形状（版本和摘要必须从服务端获取，不照抄占位符）：

```json
{
  "device": "bench-scope",
  "confirmed": true,
  "request": {
    "contract_version": "从合同读取",
    "profile_id": "从 profile 读取",
    "profile_version": "从 profile 读取",
    "profile_digest": "从 profile 读取",
    "channels": [1],
    "overrides": {
      "timebase": {"scale_s_div": 0.0005},
      "trigger": {"kind": "edge", "channel": 1, "level_v": 1.5}
    },
    "expected_signal": {
      "objective": "检查校准方波",
      "channels": {"1": {"frequency_hz": 1000, "voltage_min_v": 0, "voltage_max_v": 3.3}}
    }
  }
}
```

以上数值仅演示校准场景。`request` 是嵌套对象，不能将它的字段平铺到工具参数。

`scope_measurement_preview` 使用同一个 request，不带 confirmed。它只读现场，返回 effective、origins、估算和 plan_digest。可将 plan_digest 作为采集工具的 preview_digest；预览不预约仪器，相关配置已变化则拒绝执行。

## 默认值来源

- `overrides.channel_defaults` 作用于本次选中通道；`overrides.channels["1"]` 等为逐通道覆盖。未选通道保留原设置，但其开启状态会影响可用采样率。
- `passive-10x` 是物理 10x 无源探头基线，未指定量程/时基/触发电平时读取当前值。
- `calibration-ch1-10x` 仅适用 MHO954 的 CH1 校准接线。两者都是项目基线，不是声称出厂默认值。
- profile 可以带脉宽触发的备用边界，只有相应触发种类启用时才应用。从 edge 改 pulse 时应明确提供 width_s 或 lower_s/upper_s，不能把备用边界当作目标脉宽。
- 服务端从配置文件旁的 profiles 目录加载自定义 JSON，也可通过 profiles_dir 指定。profile 修改属于维护操作，没有远程任意编辑工具。分发软件时保留该目录；不要覆盖同名内置 profile。
- 版本用于显式升级，digest 用于检测同版本内容变化。过期引用在连接仪器前拒绝；客户端重新读取并审视差异后再提交。

## 返回与失败

成功收据包含请求/profile 快照、各字段来源、configuration_before_arm、actual_settings、capture、screenshot、quality 和 receipt_uri。`verified` 仅说明配置读回及采集步骤成功；`application_verdict=caller_decides` 表示业务判断由调用方承担。

实际采样率以采集后读回为准，exported 样本间距以导出记录为准。quality 提示窗口、电压范围、预期事件分辨率等限制，不是全面模拟/混叠证明。

已知无效字段、过期 profile 或预览漂移会在写仪器前返回结构化错误及 writes_sent=0。进入执行后的失败收据报告 stage、已执行步骤、mismatches 或错误、STOP 清理结果。配置可能已部分改变；不自动回滚或重试。无新采集时 capture 为 null，不能将旧波形冒充本轮结果。

通过 `scope://measurements/{operation_id}/receipt.json` 获取持久化执行收据；成功波形 metadata.json 也保存 measurement_context。预检失败未进入执行，不保证有持久化收据。

同一运行版本的 Skill 可通过 scope://skill/SKILL.md 读取，引用的参考文件通过 scope://skill/references/{name} 读取，无须本地复制数值默认值。

## 大记录预算

工具顶层operation_timeout_s可显式设5～900秒，未指定仍沿用设备默认；request.capture中的wait_s仅为有限触发等待。最大记录需同步指定capture.max_bytes及合适chunk_points，详见[深记录](deep-records.md)。内置默认深度不因软件支持500M而自动增大。

## 外部刺激

0.6提供[预约采集](prepared-capture.md)，复用MeasurementRequest，触发耦合/噪声抑制/Holdoff也参与读回和参数来源记录。普通capture仍为阻塞式；prepared流程的返回值须分别检查ready、capture_complete及cleanup_complete。
