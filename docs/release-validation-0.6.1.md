# 0.6.1 整理版验证记录

日期：2026-10-12。uv 0.11.28，Linux x86_64。

| 检查 | 结果 |
| --- | --- |
| Python 3.11.15、3.13.14、3.14.4 | 各 117 项单元/loopback/子进程及发布边界测试通过 |
| uv.lock | frozen 安装与 lock --check 通过；原有运行依赖未升级 |
| 真实 stdio MCP（离线） | 41 个工具、合同、profile 和 Skill resource 可读 |
| wheel 独立安装 | 从 site-packages 加载；内置 Skill 和两份 profile 可用；不含 dev 依赖 |
| 离线重建 | 相同 x86_64 / Python 3.13 缓存下，frozen/offline/no-dev/no-editable 成功 |
| 发布内容 | 白名单源码与 wheel/sdist 的 Skill/profile/许可证、相对文档链接、设备信息排除通过 |
| 私有配置边界 | 真实 devices.toml 不导出；公共子目录意外 .env 拒绝导出 |
| Skill | frontmatter 与引用校验通过；主机相关措辞及 50 Ω 验收状态已校正 |
| CI | YAML 结构检查通过；Python 3.11/3.13/3.14 工作流的远端结果见仓库 Actions |

本轮只准备公开版，未部署到仪器服务主机，也未唤醒或操作示波器。
历史实机证据与待验收项目见 [验收范围](validation.md)。
ARM64 离线缓存和新设备/固件需在目标环境单独验证。发布前审核见[审核记录](review-0.6.1.md)。
