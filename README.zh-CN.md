# rigol-scope-kit

[English](README.md)

面向 RIGOL MHO900 的 Linux SCPI 服务、stdio MCP 和 Agent Skill。
先选合适的触发、时间窗口、幅值范围和采样条件，抓到可用波形，再分析。
任务 Agent 提供预期信号、接线和测量目标，选择初始配置，并根据返回证据决定下一轮调整。
服务端负责验证、执行一次、读回和保存证据，不自行循环调参。

**公开整理版 0.6.1；合同 1.0.0；41 个 MCP 工具；Apache-2.0。**
实机验收型号为 MHO954；MHO934/MHO984 仅有软件模型支持，未实机验收。
服务端要求 Linux、Python ≥3.11；远端设备通过 SSH stdio 调用，不必安装分析库。

## 用 uv 安装

先按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)安装 uv，再在仓库目录执行：

```bash
git clone https://github.com/felixfinal/rigol-scope-kit.git
cd rigol-scope-kit
uv python install 3.13
uv sync --frozen --no-dev
uv run --frozen --no-sync rigol-scope contract
cp config/devices.example.toml config/devices.toml
```

修改 `devices.toml` 的 IP、SCPI 端口和型号。示例中的 `192.0.2.10` 是文档保留地址，必须替换。
先读取身份，再把序列号填入配置，后续实机操作要求身份绑定：

```bash
uv run --frozen --no-sync rigol-scope --config config/devices.toml identify
uv run --frozen --no-sync rigol-scope --config config/devices.toml status
```

`contract` 无须配置且不访问仪器；`identify/status` 会访问仪器。
`uv.lock` 是公开版唯一依赖锁；`--frozen` 不改锁文件，`--no-dev` 排除构建/发布检查工具。
[uv 专项说明](docs/uv.md)包含开发、构建、更新依赖、离线缓存和跨设备迁移步骤。

## MCP 与 Skill

客户端配置采用[本地示例](config/mcp-local.example.json)或 [SSH stdio 示例](config/mcp-ssh.example.json)，替换绝对路径和 SSH 别名。
Python 环境、依赖、配置和原始记录集中在服务主机，客户端按需取 Skill、合同、窗口和产物。
MCP 每次启动直接使用已建立的环境，不执行安装或自动升级。

```bash
.venv/bin/rigol-scope-mcp --config /absolute/path/to/devices.toml
.venv/bin/rigol-scope-skill /new/path/rigol-oscilloscope
```

wheel 内含 [Skill](skills/rigol-oscilloscope/SKILL.md)、参考文件和默认 profile。
MCP 可读取 `scope://skill/SKILL.md` 和 `scope://skill/references/{name}`。
如客户端需要本地 Skill，导出到尚不存在的目录，再按该客户端的 Skill 目录约定放置。
OpenClaw 属于可选集成：内部 Agent 委派给示波器专职 Agent；普通 MCP 客户端直接调用同一接口。

## 当前能力

| 范围 | 能力 |
| --- | --- |
| 采集条件 | 四通道倍率、输入阻抗、耦合、带宽限制、量程/偏置、时基/位置、采集模式和深度 |
| 触发 | EDGE / PULSE，AUTO/NORM/SING，耦合、噪声抑制和 Holdoff |
| 工作流 | 默认 profile、合同、预览、原子单次采集、先 WAIT 后外部刺激的预约采集 |
| 信号发生器 | GI/GII 基本波形、参数/负载约定、受限输出开启、相位同步 |
| 数据 | 截图、测量、分块 RAW/WORD 导出、目录、资源读取、离线绘图/分析/搜索/双通道时差 |
| 交还 | 未用发生器输出关闭、深度 AUTO、其他设置保留；支持显式唤醒 |

[能力矩阵](docs/capabilities.md)列出支持范围；[验收说明](docs/validation.md)区分实机通过、局部验证和未验收。
单通道 500M 整份导出已通过，多通道最大深度目前只验收了部分窗口。
硬件选件全开并不代表驱动已覆盖：高阶/协议触发、总线解码、数字探头、AFG 调制/突发/任意波上传和无人任务尚未实现。

## 完整文档

- [架构与多 Agent 职责](docs/architecture.md)
- [使用流程和调用示例](docs/usage.md)
- [uv、环境和迁移](docs/uv.md)
- [验收证据范围](docs/validation.md)
- [已知问题](docs/known-issues.md)
- [开发约定](CONTRIBUTING.md)、[发布步骤](docs/releasing.md)、[变更记录](CHANGELOG.md)

公开版不附带厂商 PDF、真实设备配置、SSH 配置或采集数据。上游声明见 [NOTICE](NOTICE)，资料入口见[来源说明](docs/sources.md)。
