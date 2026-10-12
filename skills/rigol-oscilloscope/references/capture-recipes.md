# 采集场景

所有示例先查询当前合同/profile，不硬编码摘要。数值是示例参数，不是设备默认值。

## 周期方波 / PWM

已知 1 kHz、0–3.3 V 时，可用 CH1 正边沿、1.5 V 阈值和 500 us/div 观察多个周期。选相符的探头 profile，确认显示上下限足够；采集后用原始点分析 frequency、duty、脉宽。罕见窄脉冲不能依赖分位数自动确定电平，可显式传 low_level_v/high_level_v。

若目标转为上升沿质量，任务 Agent 根据首轮结果缩短时基、调整深度并再采一次。高采样率不证明模拟带宽或探头足够。

## 脉宽条件触发

在 overrides.trigger 中设置 kind=pulse、channel、level_v、polarity，并明确：

- condition=less / greater：提供 width_s。
- condition=between：提供 lower_s 和 upper_s，前者必须小于后者。

例如校准方波约 500 us 高脉冲，less + width_s=0.0006 可用于基本验收。抓窄异常应按任务给出的正常宽度和异常定义设阈值，同时调整时间窗和采样分辨率；不能照抄校准阈值。

## 没有触发 / 波形质量不足

查看失败 stage、trigger_timeout、实际触发配置和 STOP 清理状态。没有匹配触发只表明有限等待内未抓到符合条件的事件；不等于信号或故障不存在。调用方可选择改变条件、延长有限等待（受合同上限约束）或检查接线。

若成功抓到但削顶/周期不足/事件分辨率不足，保留原 capture_id，明确建议调整哪个参数；下一轮由任务 Agent 决定。不要用 AUTO 强制出的旧/无匹配画面宣称目标已触发。

## 已保存记录 / 尚未支持的触发

已有 capture_id 直接绘图、选窗分析或搜索脉宽/电压异常，不重新配置仪器。搜索保存数据不能替代尚未实现的硬件 runt、timeout、setup-hold、协议触发。硬件许可证全开也不代表 MCP 工作流已实现。

外部 MCU 单次发包等需“先armed、再刺激”的任务，使用[预约采集](prepared-capture.md)的真实WAIT握手，不用猜测延时代替ready。
