# 双路 AFG 同步与时差

## 已验证范围

MHO954：两路各通过10×探头接入两个模拟通道，输入1 MΩ、DC；1 kHz、1 Vpp、0 V偏置，高阻幅度显示约定。正弦/方波基本回接和0°/90°/180°相位变化经过实测。接线是每次任务输入，不把历史通道号当默认物理接线。

## 调用流程

1. scope_generator_status 确认两路原输出关闭，读取通道现状，仅改变需要的示波器设置。配置探头倍率必须匹配物理拨档。
2. 分别 scope_generator_configure 设置两路同频信号和 phase_deg。只在输出关闭时改发生器参数。
3. 用各自新 settings_hash 和当前任务允许的 max_open_circuit_v 开启两路输出。
4. scope_generator_sync_phase 使两路相位同步。其 verified 只证明命令完成与设置读回，物理相位由以下测量确认。
5. 以参考输入通道触发，scope_capture_single 同时保存两路原始记录；scope_measure_timing(channel_a, channel_b, measurement=rr_delay/rr_phase) 读取仪器值。保留截图、原始数据和无效响应。
6. 临时测试结束关闭本次开启的输出，保留发生器和示波器设置；无需恢复旧量程/阻抗/通道。

## 数值解释

实测 GII 设置+90°相对于 GI，在对应输入a=GI、b=GII时，仪器 rr_delay 约-250us、rr_phase约-90°。即b的对应边沿更早，不能将其符号直接当成发生器 phase_deg。

180°附近可表示为+180°或-180°，按周期等价比较，不能将359°差值判为严重错误。

当前已测设置下，0°近重合的正弦场景中，rr_delay/rr_phase 返回9.9e37无效值；运行中预先查询再采集仍未消除。必须报告 valid=false/null，不能把它当成0或据此宣称同步失败。90°和180°的仪器读数已实测有效。根因未确认，不定性为固件缺陷；按用户约定列为非阻塞已知问题，暂不继续排查，需要精确近零时差的实际任务出现时再处理。

本轮另用同一记录完整样本、已知1kHz的正弦最小二乘拟合核对相位，0°/90°/180°约为0.28°/90.28°/180.28°。0.5已提供正式scope_waveform_timing的sine_fit与edge_delay方法，详见[离线分析](offline-quality-timing.md)。结果不能混称为仪器读数，也不是探头/通道延迟精度校准。

## 任务交还规则（0.5.1）

测量过程中按需保留设置；最终释放仪器调用scope_release(confirmed=true)，默认两路AFG关闭、存储深度AUTO，保留其他配置。仅有明确持续使用任务时在keep_generator_outputs中保留对应路数。低功耗先显式scope_wake；已保存数据分析不需要唤醒或释放。
