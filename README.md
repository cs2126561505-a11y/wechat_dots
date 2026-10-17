# wechat_dots

将现有 ChatGPT dot 接入指定的普通微信群。目标是通过程序化读写接口完成群消息触发、理解和回复。核查日期：2026-10-17。

## 当前选型

当前决策是保留微信 `4.1.15.13`，优先审查 `wechatauto-replica 1.2.6.1`（源码提交 `62503d7`），先验证 `test-group` 收发。已实现默认关闭的读取探针原型与离线测试；候选尚未通过实机准入。

日常电脑同时保存主账号与测试账号的数据。源码审查发现，按账号选目录不能限制取密钥时的进程内存扫描范围。因此当前电脑不进行取密钥或解密；所有者已选择专用 Windows 虚拟机或备用电脑，仅登录测试账号。代码中的隔离门禁在验证环境之前无条件阻止 `bind`、`watch` 和底层读取入口，命令行与环境变量均不能绕过。详见[候选审查与隔离结论](docs/replica-audit.md)。

WeChatFerry 上游于 2026-07-10 归档，最新发布页列出 `v39.5.2` 配套微信 `3.9.12.51`。Wechaty 的 PadLocal 具有个人微信与群操作接口文档，其凭据申请入口在 2026 年有失效报告。两者当前都处于准入验证阶段。[WeChatFerry 发布页](https://github.com/lich0821/WeChatFerry/releases/latest) · [PadLocal](https://github.com/wechaty/puppet-padlocal) · [入口问题报告](https://github.com/wechaty/puppet-padlocal/issues/358)

```text
普通微信群 → 微信适配器 → 权限与消息网关 → MCP Events
          → 原 ChatGPT dot → 回复草稿 → 所有者审核 → 原群
```

OpenAI 提供 dots 的 MCP Events 接入机制，网关负责实现事件订阅与受限读写工具。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## 接下来验证什么

先准备专用 Windows 测试环境，核对文件、进程和共享资源的隔离，再审阅是否允许只读探针运行。真实群 ID、真实 @、手打假 @ 和自身消息验证通过后，才研究固定回复的稳定目标绑定及发送确认。当前原型没有发送功能。迁移与测试步骤见[探针使用说明](docs/probe-guide.md)，完整停止条件见[通道验证计划](docs/transport-validation.md)。

## 仓库内容

[方案筛选](docs/ecosystem.md) 记录候选与证据；[架构与接口](docs/architecture.md) 定义消息链路；[聊天配置与验收](docs/setup-and-safety.md) 给出群规、dot 指令和权限要求；[验证记录](docs/validation.md) 区分离线结果与实机状态。

[群策略](config/group-policy.example.json) 与[消息事件](examples/message-event.json) 使用 `0.2` 版项目契约，范围固定为普通微信群。默认接入关闭、白名单为空、草稿模式。[工具契约](contracts/tools.json) 保留读取、草稿和审核后发送的设计。

当前交付包括文档、配置、Schema、隔离门禁、只读探针原型和离线测试。运行中的网关、真实群收发及 dot 事件唤醒尚未验证。正式策略和工具契约保持关闭；探针不是新增的生产通道。仓库使用合成数据，私密状态、候选源码缓存与虚拟环境均排除在版本控制之外。

## 离线检查

使用 Python 3.11 或更高版本，在隔离环境中执行：

```sh
python -m venv .venv
# 激活环境：Linux/macOS 用 source .venv/bin/activate；Windows 使用相应激活脚本。
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试覆盖配置、Schema、工具输入约束、隔离门禁、合成群身份、候选 @ 解析和持久化去重；无需微信、密钥或真实数据库。运行诊断需要的额外依赖见 `requirements-probe.txt` 和[探针使用说明](docs/probe-guide.md)。实机上线标准见[验收表](docs/setup-and-safety.md#上线验收)。
