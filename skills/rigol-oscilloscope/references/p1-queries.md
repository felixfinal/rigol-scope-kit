# P1 查询依据

依据 MHO900 中文编程手册 PGA46002-1110（2026-02-26）。

| 工具能力 | 命令入口 | 印刷页 |
| --- | --- | --- |
| 身份 | `*IDN?` | 130 |
| 选件 | `:SYSTem:OPTion:STATus? <type>` | 270 |
| 模拟通道 | `:CHANnel<n>:DISPlay?`、`SCALe?`、`OFFSet?`、`IMPedance?` 等 | 75–81 |
| 采样与存储 | `:ACQuire:SRATe?`、`:ACQuire:MDEPth?` | 16–18 |
| 截图 | `:DISPlay:DATA? PNG` | 116 |
| AFG | `:SOURce<n>:OUTPut:STATe?`、`FUNCtion?`、`IMPedance?` 等 | 275–292 |

P1 不查询会消费错误队列的错误命令，不发送复位、停止、自动设置或输出开关命令。若固件不支持某查询，明确失败，保留证据，不将未返回视为功能关闭。

官方资料入口：https://www.rigol.com/zh_CN/products/oscilloscope/MHO900.html
