# 准备采集 → 外部刺激 → 取数

适用于MCU启动、单次发包、外部切换等必须先让示波器等待的任务。持续/自然事件仍可直接scope_measurement_capture。

## 分工与授权

测量任务与刺激设备的操作授权、接线、电压范围应在准备前明确。任务Agent决定条件和刺激；示波器只配置、等待、取数、返回证据。OpenClaw内部仍由oscilloscope专职Agent持有仪器权限；本机/其他设备可直接使用SSH stdio MCP。

`reservation_token`为本次占用的私有凭证，留在取得它的同一个调用方/Agent运行中，只传给prepared系列工具；不写用户报告、记忆、任务交接或发给刺激Agent。调用封装须对此字段做日志脱敏，不要将prepare原始输出直接重定向进项目日志。协调方可转交不含凭证的ready信息，让另一个已授权硬件Agent执行刺激。负责示波器的同一运行须覆盖准备到取数/取消的生命周期；不要通过旧会话或历史日志找回凭证。

## 调用流程

1. 从scope_contract、scope_profile_get取得合同和profile版本/摘要，构造与普通采集相同的request。触发sweep使用NORM或SING；显式给出符合接线的通道、量程、阈值、时基和深度。内置AFG用作刺激时，先在输出关闭状态配置并保留settings_hash及电压限额。
2. `scope_prepare_capture(request, confirmed=true, lease_s=60, ready_wait_s=5, export_timeout_s=30)`。
3. 仅当返回`status=ready`且`ready=true`才发刺激。检查stimulus_wait_remaining_s、ready_sample_rate_sa_s和quality_before_stimulus，确认当前条件满足目标后再继续。这意味着本次SINGLE后实际观察到WAIT；等待期间已提前触发、未ready或过期时不要继续发刺激。用外部刺激设备自身的工具，不靠猜测sleep当作就绪。
4. 外部设备不使用示波器占用凭证。若用本机AFG，调用`scope_prepared_generator_output`，携带本次凭证、通道、当前settings_hash、max_open_circuit_v及confirmed=true；它只在WAIT时允许开启输出。不要绕过占用去调用普通scope_generator_output。
5. `scope_prepared_collect(reservation_id, reservation_token, wait_s=3)`。尚未完成时返回capture_complete=false，可在剩余占用期内有限查询；不会自动重置/重启SINGLE。完成时返回status=collected、measurement（采集收据）、cleanup。检查capture_complete、measurement.verified和cleanup_complete，再判断是否命中目标。
6. 不再等待时调用`scope_prepared_cancel(..., confirmed=true)`。导出中取消可能先返回cancel_requested，须查看后续status/cleanup_complete，不能把请求取消当成已清理。

`scope_prepared_status`读取所有者状态和最终收据，不抢硬件锁。终态重复collect只返回同一份已保存结果，不重新触发或导出。reservation_receipt_uri可读取不含凭证的终态收据，供后续报告使用；无需为后续查询保存私有token。该资源不公开进行中的占用凭证/IPC信息。

## 时间与设备占用

- 等待刺激的lease_s为5～120秒，从真实ready开始计时，不续租；ready_wait_s最多10秒。
- 取数开始后使用单独的export_timeout_s（5～900秒），大记录需显式给足时间/字节预算。短时等待与长记录传输是两个预算。
- 一个短时本地所有者进程持有既有serial/endpoint文件锁，普通仪器调用返回busy；进程在成功/取消/到期清理后退出，不是长期守护或无人反复采集。
- 客户端离线/结束后，所有者仍按到期清理；结束STOP、关闭不再使用的AFG输出、深度AUTO，其他配置保留。需要持续输出的路数须在prepare中明确列入keep_generator_outputs。
- 采集收据中的actual_settings是捕获时设置，cleanup是交还后的结果，二者的深度可能不同。
- 连接故障或进程被强杀仍可能造成硬件状态未知。所有者被杀而占用尚未终结时，持有效凭证的显式cancel可在取得设备锁后恢复清理；若已终结但cleanup_complete=false，应报告未知状态，再按当前授权使用普通release或操作员恢复；禁止自动重放刺激或假称已安全关闭。永久丢失凭证且所有者被杀时需操作员介入，不自行删除锁文件冒充释放。

## 证据边界

STOP必须出现在本次已观察WAIT之后，旧STOP不能作为ready或采集成功依据。前面板及其他主机的操作仍无法被文件锁阻止；外部手动STOP可能干扰判断，必须核对波形内容。服务不保证多设备硬实时同步，也不保证外部刺激“恰好执行一次”。

lease提前超时会返回expired及清理结果，不将旧波形作为新结果。KI-001等已知仪器测量限制保持原处理。

本轮实机验证的是本机MCP客户端编排的完整闭环。OpenClaw跨Agent消息时序尚未端到端验收：如果无法在同一仪器owner运行中传递公开ready信息并完成刺激，应报告协调缺口；不能结束owner运行后新spawn去找回凭证。
