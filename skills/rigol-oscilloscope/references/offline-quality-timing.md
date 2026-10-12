# 保存波形的质量与双通道分析

所有工具仅分析指定capture_id的保存数据，不要求仪器上电；报告明确属于主机计算。

## 单通道质量

`scope_waveform_analyze` 保留原有统计，并返回：

- `vpp_v`：记录极值差，包含噪声、尖峰和过冲。
- `robust_span_v`：5%～95%分位差，不等于精确幅度；稀少脉冲可能不在该范围内。
- `stable_low_v/high_v`、`stable_level_difference_v`：只有高低平台占比支持双电平解释时才返回，正弦等波形通常为null。
- `quality.frequency_valid` 与 `timing_reasons`：噪声主导、低于显式幅度门限、周期采样不足、周期不稳定、ADC码触轨等会使频率/周期/占空比不可用，返回null而不是把噪声边沿当信号频率。
- `minimum_signal_vpp`：调用方可给出本任务的最低有效幅度；默认不使用固定毫伏门限，以免屏蔽真实小信号。
- `noise_sigma_estimate_v` 是相邻样本差的稳健估计，质量判断是保守启发式，不是模拟噪声校准或“不存在信号”的证明。

单次脉冲/不规则事件可以没有全局频率，仍可检查原始数据和明确门限的事件搜索。需要放宽噪声引发的假边沿时，由调用方明确选择hysteresis_v和阈值，并记录理由；服务端不自动调参重测。触轨只说明削顶可能，不能把所有平顶方波都判为削顶。

## 双通道 `scope_waveform_timing`

必须是同一个capture_id中的两个不同通道，选择相同时间窗口。工具验证两份原始SHA-256、元数据和时间网格；不接受不同采集之间拼接，也不自动插值对齐。单次两通道合计最多2M选中点。

### 已知频率的正弦

```json
{"capture_id":"<id>","channel_a":2,"channel_b":3,"method":"sine_fit","frequency_hz":1000}
```

对完整选中样本拟合共同已知频率的正弦和直流偏置，检查幅度与噪声、拟合残差/解释方差、至少3周期、至少10点每周期。频率错误、噪声、明显非正弦或点数不足会给出无效结果。该方法不测量frequency_hz本身，调用方应先确认频率。

返回 `timing.phase_lead_b_deg`：正值表示B领先A，范围[-180°,180°)。`delay_b_minus_a_s` 为等价主值延迟，正值表示B更晚。正弦无法区分相差整数周期的延迟，不能把它当绝对事件传播时间。

### 边沿时差

```json
{"capture_id":"<id>","channel_a":2,"channel_b":3,"method":"edge_delay","max_delay_s":0.0004,"edge":"rising","hysteresis_a_v":0.2,"hysteresis_b_v":0.2}
```

max_delay_s必须来自任务已知的配对范围。只有该范围内互相唯一的边沿配对才有效；多候选时返回歧义，不能悄悄选最近一个冒充已知事件。周期信号通常需小于半周期的合理范围，无法唯一配对时由调用方调整范围或选事件窗口。

返回匹配数量、未配对数量、延迟中位值、MAD及最多50个配对示例。只有两路存在稳定共同周期时才给相位，否则时差可有效而相位为null。

## 结果读取与限制

检查 `timing.valid`、`phase_valid` 和quality，工具正常返回/报告status=complete只说明计算完成。结果含两个通道的来源哈希、算法、参数、时间窗口、PNG/JSON/Markdown，通过现有resource和scope_artifact_read获取。

相位符号与仪器rr_phase可能相反：以本工具“B领先A为正”的定义为准。0°近同相的仪器无效读数仍按KI-001保留，不因离线分析能算出结果就宣布仪器问题已修复。
