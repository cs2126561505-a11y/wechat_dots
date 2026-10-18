# wechat_dots

将现有 ChatGPT dot 接入指定的普通微信群。目标是通过程序化读写接口完成群消息触发、理解和回复。核查日期：2026-10-18。

## 当前选型

当前保留微信 `4.1.15.13`，使用固定的 `wechatauto-replica 1.2.6.1` 数据库模块（源码提交 `62503d7`）接入专用来宾中的 `test-group`。首次真实完整回路已通过：平台真实 @ 触发 MCP 事件，原 dot 自动读取消息与上下文并保存中文草稿，所有者批准后，适配器向来源群发送一次；数据库与接收端均确认成功且只有一条。ChatGPT 网页端 OAuth 正常，桌面授权回跳的兼容问题仍保留记录。

日常电脑同时保存主账号与测试账号的数据。源码审查发现，按账号选目录不能限制取密钥时的进程内存扫描范围。所有者已建立专用 Windows 虚拟机，仅登录测试账号；实时数据库操作限定在来宾内。代码在 `bind`、`watch` 和底层读取入口核验短时准入，绑定隔离环境、账号和微信进程；校验失败即停止读取。详见[候选审查与隔离结论](docs/replica-audit.md)和[验证记录](docs/validation.md)。

WeChatFerry 上游于 2026-07-10 归档，最新发布页列出 `v39.5.2` 配套微信 `3.9.12.51`。Wechaty 的 PadLocal 具有个人微信与群操作接口文档，其凭据申请入口在 2026 年有失效报告。两者当前都处于准入验证阶段。[WeChatFerry 发布页](https://github.com/lich0821/WeChatFerry/releases/latest) · [PadLocal](https://github.com/wechaty/puppet-padlocal) · [入口问题报告](https://github.com/wechaty/puppet-padlocal/issues/358)

```text
普通微信群 → 微信适配器 → 权限与消息网关 → MCP Events
          → 原 ChatGPT dot → 回复草稿 → 所有者审核 → 原群
```

OpenAI 提供 dots 的 MCP Events 接入机制，网关负责实现事件订阅与受限读写工具。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## 当前运行边界与下一阶段

当前为限时、单群、逐条审核的测试接口。网关和原 dot 的连接、授权群及订阅保留；本轮结束后关闭发送开关，临时采集与共享连接监控停止，读取许可撤销。恢复测试需重新核对来宾和限时许可，在生成新草稿前确定发送策略。普通消息和手打假 @ 缺少目标字段时保持未知并排除触发。见[实验网关](docs/gateway.md)、[探针说明](docs/probe-guide.md)和[验证记录](docs/validation.md)。

下一阶段再整理常驻启动、审核与停止入口，并验证断线、订阅刷新及长期稳定性。首次完整回路通过仅覆盖已记录的受控环境；日常主账号继续隔离，全面生产验收见[通道验证计划](docs/transport-validation.md)。

## 仓库内容

[方案筛选](docs/ecosystem.md) 记录候选与证据；[架构与接口](docs/architecture.md) 定义消息链路；[聊天配置与验收](docs/setup-and-safety.md) 给出群规、dot 指令和权限要求；[验证记录](docs/validation.md) 区分离线结果与实机状态。

[群策略](config/group-policy.example.json) 与[消息事件](examples/message-event.json) 使用 `0.2` 版项目契约，范围固定为普通微信群。默认接入关闭、白名单为空、草稿模式。[工具契约](contracts/tools.json) 保留读取、草稿和审核后发送的设计。

当前交付包括隔离读取探针、OAuth、MCP 事件订阅、持久化队列、审核状态机与合成 HTTP 回路测试。运行网关使用独立私密配置，示例见 `config/gateway.example.json`；原群策略文件仍是规划契约。仓库使用合成数据，私密状态、凭据、候选源码缓存与虚拟环境均排除在版本控制之外。

## 离线检查

使用 Python 3.11 或更高版本，在隔离环境中执行：

```sh
python -m venv .venv
# 激活环境：Linux/macOS 用 source .venv/bin/activate；Windows 使用相应激活脚本。
python -m pip install -r requirements-dev.txt
python -m pip install -r requirements-probe.txt -r requirements-gateway.txt
python -m unittest discover -s tests -v
```

测试覆盖配置、Schema、工具输入约束、隔离门禁、合成群身份、候选 @ 解析和持久化去重；无需微信、密钥或真实数据库。运行诊断需要的额外依赖见 `requirements-probe.txt` 和[探针使用说明](docs/probe-guide.md)。实机上线标准见[验收表](docs/setup-and-safety.md#上线验收)。
