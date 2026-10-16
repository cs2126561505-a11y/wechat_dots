# wechat_dots

将现有 ChatGPT dot 接入指定的普通微信群。目标是通过程序化读写接口完成群消息触发、理解和回复。核查日期：2026-10-16。

## 当前选型

本轮验证集中在 WeChatFerry 与 Wechaty + 具体 Puppet。前者先检查 Windows 客户端版本与账号登录，后者先确认协议提供者、可用凭据和普通群能力。通过收发测试的通道，再接入 MCP Events 与 dot。

WeChatFerry 上游于 2026-07-10 归档，最新发布页列出 `v39.5.2` 配套微信 `3.9.12.51`。Wechaty 的 PadLocal 具有个人微信与群操作接口文档，其凭据申请入口在 2026 年有失效报告。两者当前都处于准入验证阶段。[WeChatFerry 发布页](https://github.com/lich0821/WeChatFerry/releases/latest) · [PadLocal](https://github.com/wechaty/puppet-padlocal) · [入口问题报告](https://github.com/wechaty/puppet-padlocal/issues/358)

```text
普通微信群 → 微信适配器 → 权限与消息网关 → MCP Events
          → 原 ChatGPT dot → 回复草稿 → 所有者审核 → 原群
```

OpenAI 提供 dots 的 MCP Events 接入机制，网关负责实现事件订阅与受限读写工具。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## 接下来验证什么

已有兼容 Windows 环境时，先验证 WeChatFerry 的登录、群消息接收、真实 @ 元数据与原群回复。Wechaty 路线先确认具体 Puppet 的服务可获得性，再用同一套群收发标准测试。完整步骤与停止条件见[通道验证计划](docs/transport-validation.md)。

## 仓库内容

[方案筛选](docs/ecosystem.md) 记录候选与证据；[架构与接口](docs/architecture.md) 定义消息链路；[聊天配置与验收](docs/setup-and-safety.md) 给出群规、dot 指令和权限要求；[验证记录](docs/validation.md) 区分离线结果与实机状态。

[群策略](config/group-policy.example.json) 与[消息事件](examples/message-event.json) 使用 `0.2` 版项目契约，范围固定为普通微信群。默认接入关闭、白名单为空、草稿模式。[工具契约](contracts/tools.json) 保留读取、草稿和审核后发送的设计。

当前交付为文档、配置、Schema 和离线测试；网关执行器、微信登录、群收发及 dot 事件唤醒均待实现或实测。仓库使用合成数据，真实凭据和聊天内容保存在所有者的私密环境。

## 离线检查

使用 Python 3.11 或更高版本，在隔离环境中执行：

```sh
python -m venv .venv
# 激活环境：Linux/macOS 用 source .venv/bin/activate；Windows 使用相应激活脚本。
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

测试范围为配置、Schema 和工具输入约束。实机上线标准见[验收表](docs/setup-and-safety.md#上线验收)。
