# 触发耦合、噪声抑制和Holdoff

`scope_trigger_options`读取；`scope_configure_trigger_options(settings, confirmed=true)`修改指定字段并读回。边沿/脉宽配置工具和MeasurementRequest.trigger也接受适用字段；未指定值从现场读回，profile没有暗设一组新的触发默认值。

| 字段 | 取值 | 使用依据 |
|---|---|---|
| coupling | DC / AC / LFR / HFR | 仅模拟通道边沿触发。DC保留直流与交流；AC隔离直流；LFR抑制低频；HFR抑制高频。按目标选，不为波形“好看”过滤目标事件 |
| noise_reject | true / false | 降低噪声造成的误触发；目标幅度小或事件很窄时需核对实际捕获效果 |
| holdoff_s | 8e-9～10，秒 | 一次触发后暂不接受下一次触发；可帮助在脉冲串中锁定同一位置，设得过长也可能跳过目标事件 |

这是触发路径的耦合和抑制，不是输入通道coupling，也不是数据分析中的hysteresis_v。后两者不会自动随触发选项改变。

典型顺序：先确定事件类型、源、电平和极性；若边沿触发仍受干扰，再根据干扰频率选择coupling/noise_reject；脉冲串定位不稳定时按重复周期选择holdoff。调用方决定调整，服务端不自主反复重抓。

```json
{"settings":{"coupling":"DC","noise_reject":true,"holdoff_s":0.0002},"confirmed":true}
```

200us仅是演示值。0.6在MHO954验证了四种耦合、噪声抑制ON和200us Holdoff的读回，并在这些设置下完成GII→CH2采集；不是所有干扰条件下的效果保证。
