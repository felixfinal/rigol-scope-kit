# uv：依赖、环境和迁移

## 文件职责

| 文件/目录 | 用途 | 是否提交 |
| --- | --- | --- |
| pyproject.toml | Python 要求、依赖范围、入口、构建和 dev 组 | 是 |
| uv.lock | 解析后的版本、Python/平台条件和包哈希 | 是 |
| .python-version | 仓库默认 Python 3.13 | 是 |
| .venv | 本机环境，由 uv 重建 | 否 |
| uv cache | 下载包和构建缓存，按平台准备 | 否 |
| devices.toml、state | 真实地址/身份与记录 | 否 |

公开版统一使用 uv.lock，不再发布 requirements.lock.txt。
本次验证使用 uv 0.11.28；CI 固定同一 uv 版本。Python 3.13 是部署基线，服务代码要求 ≥3.11。

## 初次安装与运行

[官方 uv 安装页](https://docs.astral.sh/uv/getting-started/installation/)提供 Ubuntu/Linux 的安装方式。
安装完成后在仓库根目录执行：

```bash
uv --version
uv python install 3.13
uv sync --frozen --no-dev
uv run --frozen --no-sync rigol-scope contract
```

`uv sync --frozen` 使用现有锁，不更新它；项目元数据/锁一致性另用 `uv lock --check` 校验。
`--no-dev` 只安装运行所需依赖，开发时去掉它。
`uv run --frozen --no-sync` 使用已建好的环境，避免执行前同步。
MCP 启动入口直接指定 `.venv/bin/rigol-scope-mcp` 的绝对路径，不能在握手时临时安装依赖。
生产版本可用 `uv sync --frozen --no-dev --no-editable` 安装为普通包；可编辑安装用于开发。

## 开发、构建与检查

```bash
uv sync --frozen
uv lock --check
uv run --frozen --no-sync python -m unittest discover -s tests -q
uv run --frozen --no-sync python tools/verify_public.py
uv build --no-build-isolation --out-dir dist
uv run --frozen --no-sync twine check dist/*
uv run --frozen --no-sync python tools/verify_public.py --dist dist
```

dev 组包括固定版本的 hatchling 和发布元数据检查工具 twine。
构建使用锁定的 dev 环境并关闭另建隔离构建环境；build-system 同样固定 hatchling 版本。
普通 wheel 的元数据仅记录依赖范围，不携带一套锁定的运行依赖。
需要可复现环境时，应迁移包含 uv.lock 的源码快照，而不只复制一个 wheel。

## 更新依赖

```bash
uv lock --upgrade-package mcp
uv sync --frozen
uv run --frozen --no-sync python -m unittest discover -s tests -q
```

审阅 pyproject/uv.lock 变更及兼容性后一起提交。
日常部署不使用 `--upgrade`，也不删除锁后重新解析。
本轮整理没有升级已有运行依赖，仅增加锁定的开发/发布检查依赖。

## 在线迁移

1. 复制同一版本的源码快照（含 uv.lock、Skill、profile），或检出同一 tag。
2. 在目标 Linux 主机安装 uv 和 Python 3.13，执行 `uv sync --frozen --no-dev --no-editable`。
3. 单独迁移设备配置、所需原始记录及自定义 profile；更新路径并核对仪器身份。
4. 改 MCP 客户端的 SSH 别名/绝对入口路径，先做合同与 Skill 发现，再按需实机只读检查。

每个版本单独一个目录和环境，切换入口时保留上一版可回退。
跨主机/架构不要复制 `.venv`。客户端通过 SSH 不需 uv 或科学计算依赖。

## 离线迁移

缓存不是跨平台完整发行包。必须在与目标相同的 OS/CPU/Python 环境先准备：

```bash
export UV_CACHE_DIR=/absolute/path/to/transfer-cache
uv sync --frozen --no-dev --no-editable
```

该步骤下载对应架构运行依赖及项目构建后端；保留缓存完整目录。
迁移源码和这个缓存，在已经准备好 Python 3.13 的目标机执行：

```bash
export UV_CACHE_DIR=/absolute/path/to/transferred-cache
uv sync --frozen --offline --no-dev --no-editable
uv run --frozen --no-sync rigol-scope contract
```

若需离线开发/构建，准备缓存时去掉 `--no-dev`，以覆盖开发依赖。
uv 管理的 Python 解释器不属于普通包缓存，需要提前安装或单独准备同架构解释器。
从桌面 x86_64 缓存不能推断 ARM64 主机安装可用；目标架构应单独暖缓存并实测离线重建。
[uv 同步语义](https://docs.astral.sh/uv/concepts/projects/sync/)与[缓存说明](https://docs.astral.sh/uv/reference/storage/)是维护依据。
