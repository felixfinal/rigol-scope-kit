# P2 查询和控制依据

官方 MHO900 编程手册 PGA46002-1110：采集第 3.3 节、通道第 3.6 节、测量第 3.17 节、AFG 第 3.25 节、时基第 3.26 节、触发第 3.27 节、波形第 3.28 节。

MHO954 实机在 NORM/RAW 模式验证：WORD 为小端无符号 16 位，电压=(code-yorigin-yreference)*yincrement。时间=(全局零基采样索引-xreference)*xincrement+xorigin。改变读取窗口不会改变已验证实机的 xorigin；每块仍核对 preamble，不直接将手册适配推及所有固件。

每个 AFG 输出为固定 50 欧源阻抗；高阻/50 欧负载显示约定影响允许幅度与偏置。AFG100 正弦最高100MHz、方波20MHz、锯齿2MHz；AFG50 分别50/10/1MHz。高阻最大20Vpp（<=50MHz）或10Vpp（>50MHz），50欧时减半。偏置和幅度总峰值需共同满足限制。NOIS/DC 有独立边界。

官方资料：https://www.rigol.com/zh_CN/products/oscilloscope/MHO900.html

实机测量 PDUT/NDUT 返回比例（例如 0.5），对外工具换算为百分数 50%，并保留 raw 原始响应。
