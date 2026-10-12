# GitHub 发布步骤

## 公共源码

公开发布内容为代码、测试、通用配置/脚本、Skill/profile、文档、CI、许可证和 uv.lock。
工具按白名单导出，目的目录必须尚不存在：

```bash
uv run --frozen --no-sync python tools/export_public.py /new/path/rigol-scope-kit
```

不会导出本机维护/部署脚本、旧 dist、真实配置、状态、厂商 PDF 或第三方实验目录。
公共源码包还由构建配置独立限制内容。导出快照与维护源需在发布前重新生成保持一致。

[本轮审核记录](review-0.6.1.md)

## 发布验证

在公共导出目录运行：

```bash
uv sync --frozen
uv lock --check
uv run --frozen --no-sync python -m unittest discover -s tests -q
uv run --frozen --no-sync python examples/mcp_discovery.py
uv run --frozen --no-sync python tools/verify_public.py
uv build --no-build-isolation --out-dir dist
uv run --frozen --no-sync twine check dist/*
uv run --frozen --no-sync python tools/verify_public.py --dist dist
```

另在独立环境安装 wheel，核对 CLI、MCP discovery、Skill 导出和 profile 可用；离线迁移按 [uv 文档](uv.md)实测缓存重建。
更新[验证记录](release-validation-0.6.1.md)，写明静态/离线结果、历史实机范围和本次未重跑项。
GitHub Actions 不访问仪器；只有实际成功运行后才能称该次 CI 通过。

## 仓库与 release

仓库为 [felixfinal/rigol-scope-kit](https://github.com/felixfinal/rigol-scope-kit)，默认分支 main。
审核并提交本地源码后，使用已有 Git 凭据推送；保留远端已有历史，不强制覆盖。
建议 tag `v0.6.1`，标题 `rigol-scope-kit 0.6.1`，说明可基于 [release notes](release-notes-0.6.1.md)。
GitHub 自动产生源码归档；release 可另外附 wheel、sdist、含 uv.lock 的源码 ZIP、SHA256SUMS 和验证记录。
公开许可证为 Apache-2.0；保留 NOTICE，勿转载厂商 PDF。
