# 单项控制、AFG 与已保存数据


| 目的 | 工具及关键参数 |
| --- | --- |
| 设备/选件 | scope_identify、scope_doctor、scope_capabilities |
| 当前配置/截图 | scope_status、scope_generator_status、scope_screenshot |
| 通道 | scope_configure_channel(channel, settings, confirmed)：enabled、probe_ratio、impedance=1M/50、coupling、scale_v_div、offset_v、bandwidth_limit=OFF/20M/250M |
| 时基 | scope_configure_timebase(settings, confirmed)：scale_s_div、offset_s |
| 边沿触发 | scope_configure_trigger(channel, level_v, slope=POS/NEG/RFAL, sweep=AUTO/NORM/SING, confirmed) |
| 脉宽触发 | scope_configure_pulse_trigger(channel, level_v, condition=greater/less/between, width_s 或 lower_s/upper_s, polarity=POS/NEG, sweep, confirmed) |
| 采样设置 | scope_configure_acquisition(settings, confirmed)：type=NORM/AVER/PEAK/HRES、averages、memory_depth（整数点数或 AUTO） |
| 运行控制 | scope_control(action=run/stop/single, wait_s, confirmed)，wait_s 仅 single 使用 |
| 测量 | scope_measure(channels, measurements)，可用 frequency、period、vpp、vmax、vmin、mean、rms、duty、negative_duty、rise_time、fall_time、positive_width、negative_width |
| 双通道时差/相位 | scope_measure_timing(channel_a, channel_b, measurement=rr_delay/rf_delay/fr_delay/ff_delay/rr_phase/rf_phase/fr_phase/ff_phase) |
| AFG 基础设置 | scope_generator_configure(channel, settings, confirmed)：waveform=SIN/SQU/RAMP/DC/NOIS、frequency_hz、amplitude_vpp、offset_v、phase_deg、load=highz/50、duty_percent 或 symmetry_percent |
| AFG 输出 | scope_generator_output(channel, enabled, expected_settings_hash, max_open_circuit_v, confirmed) |
| AFG 相位同步 | scope_generator_sync_phase(confirmed)；命令完成不等于物理相位测量通过 |
| 导出现有波形 | scope_waveform_export(channels, mode=NORM/RAW, points, start, chunk_points, stop_before_capture, run_after_capture, max_bytes, confirmed) |
| 一次触发并导出 | scope_capture_single(channels, mode, points, wait_s, confirmed)，同一锁内操作，结束保持 STOP |
| 本机取回产物 | scope_artifact_read(capture_id, name, offset, size)，块大小至多 262144 字节 |

设置结果中检查 verified、requested、actual 和 mismatches。verified=false 必须作为未通过处理，不能只看 MCP 请求已返回。读取失败不自动重试写操作。

## AFG 规则

先确认该路输出关闭，再修改参数；不会自动关闭他人正在使用的输出。配置或状态读回中的 settings_hash 用于开启前确认信号设置未变化，不是凭据。

开启输出时必须提供当前 settings_hash 及本次任务允许的 max_open_circuit_v（最坏开路绝对峰值）。负载为 50 欧时以显示电压的两倍计算开路峰值；高阻显示按原值计算。固定源阻抗为 50 欧，菜单负载设置只改变幅度显示约定。

持续输出任务完成后可保持输出；临时输出测试结束关闭本次开启的输出，保留本轮参数和示波器设置，避免无意义的恢复切换。断线时报告状态未知，重连不自动开启输出。AM/FM 等调制控制、任意波上传及伯德图仍待后续阶段，不能用基本工具假称已支持。

## 波形与产物

- NORM 最多 1000 点，RAW 读指定停止记录；当前是 little-endian uint16 WORD 编码，用 preamble 换算电压与触发时间。
- 连贯导出要求 STOP；若当前运行，明确设置 stop_before_capture=true。run_after_capture=true 只在本工具停止了原运行记录时恢复 RUN。
- 多通道在同一冻结记录中读取，校验水平参数一致。外部面板/其他主机仍可能干扰，不声称全局独占。
- 大波形保存 chN.bin 和 metadata.json；小波形附 CSV。预览点只是抽样，min/max/mean 由实际读取的全部点计算。失败文件会标为 partial，不是完整记录。
- MCP 返回摘要与图片；使用 resource 或 scope_artifact_read 将数据取回本机项目，并核对文件 SHA-256。大数组不要直接放进对话。
- OpenClaw bridge 可能仅转发文字，服务器文件路径不等于用户已收到图片；不要因此虚构视觉分析或发送目标。
- 当前没有 PLA2216，数字通道硬件未验收。没有无人循环或自动续传；预约采集使用有明确到期时间的短时所有者进程。

依据与已验证编码见 [P2 参考](p2-queries.md)。

## 已保存数据：服务器端分析

这组工具在仪器服务主机执行，调用设备只取结果，不需要安装 NumPy/Matplotlib。它们不会配置或启动示波器；生成分析文件是用户分析请求的一部分，无须 confirmed。

- scope_captures_list：按 serial、channel、kind、status、created_after/created_before 查询；limit<=100，使用 next_cursor 翻页。无时区的日期按 UTC。
- scope_capture_info(capture_id)：列出原始和派生产物；查询列表中的哈希未经重新计算，分析工具会重新验证原文件。
- scope_waveform_plot(capture_id, channel, start_s, end_s)：绘图或放大；时间均相对触发点。大波形用分箱最小/最大值保留尖峰，图上的包络不是测量输入。
- scope_waveform_analyze：原始点上的 min/max/Vpp/均值/RMS、频率、占空比、完整脉宽和 10–90% 边沿时间；同时生成 PNG、JSON、Markdown。可传 threshold_v、hysteresis_v、low_level_v/high_level_v。export_csv=true 仅限<=100k选中点。
- scope_waveform_search：kind=pulse_width/edge_gap 时 minimum/maximum 为秒，kind=voltage_range 时为伏；搜索严格低于下界或高于上界的事件。positive 指高脉冲/上升沿或低脉冲/下降沿。limit<=200，查看 match_count 和 truncated。

默认 5%/95% 分位值估计低高电平；罕见窄脉冲可能无法自动分出电平，需要调用方提供已知低/高电平。无法支持的测量返回 null；检查 quality、完整周期数、未分辨边沿数。两个采样间隔以下的边沿不作精确时间结论。频率是通过质量门限的边沿周期估计，无效时返回null并说明原因，不是 FFT；不能据单份记录排除混叠。

每次分析最多 2M 选中点，超过时选择时间窗口，不会悄悄降采样再测量。来源哈希、算法、阈值和窗口保存在 JSON 中。原文件不被改写；所有客户端可通过 scope_artifact_read 或 scope://files/{capture_id}/{name} 获取派生文件。分析结果明确属于主机计算，不能写成仪器直接测量或硬件精度校准。

0.5新增 [质量判定及离线双通道时差/相位](offline-quality-timing.md)。大记录的分块、时间和字节预算见[深记录](deep-records.md)。

## 任务交还规则（0.5.1）

测量过程中按需保留设置；最终释放仪器调用scope_release(confirmed=true)，默认两路AFG关闭、存储深度AUTO，保留其他配置。仅有明确持续使用任务时在keep_generator_outputs中保留对应路数。低功耗先显式scope_wake；已保存数据分析不需要唤醒或释放。

0.6增加scope_trigger_options/configure_trigger_options和prepared系列工具，见[触发选项](trigger-options.md)及[预约采集](prepared-capture.md)。边沿接口也接受coupling/noise_reject/holdoff_s，脉宽接口接受noise_reject/holdoff_s。
