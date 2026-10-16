# wechat_dots

将真正的 ChatGPT dot 接入微信消息通道的可行性研究与接口设计。核查日期：2026-10-16。

## 结论

优先采用「微信适配器 → 权限与消息网关 → MCP Events → dot → 受限写工具 → 原会话」路线，不以浏览器操作为主方案，也不把使用 OpenAI API 的另一个聊天机器人称为原来的 dot。

OpenAI 已公开面向 dots 的 MCP Events 文档：插件可以提供事件订阅和读写工具。因此，dots 一侧有官方接入路径，但仍需部署和连接自己的服务，并通过账户及工作区权限检查。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

难点在微信一侧。腾讯的 `openclaw-weixin` 已提供微信收发协议，但当前 OpenClaw 文档只声明私聊能力，不能将其当作现有普通微信群的官方接入方案。保留普通微信群时，Wechaty 的具体协议适配器或 WeChatFerry 只能作为待实测的实验候选；若接受改用机器人支持的企业微信会话，则优先考虑 WeCom 官方智能机器人 SDK。[腾讯插件](https://github.com/Tencent/openclaw-weixin) · [微信通道范围](https://docs.openclaw.ai/channels/wechat) · [WeCom SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk)

## 当前交付状态

本仓库交付调研、架构、配置示例、JSON Schema、工具契约及离线契约检查，不是已经部署的微信群机器人。尚未接入微信账号、验证目标群、部署 MCP 服务、创建真实事件订阅或发送群消息。所有示例标识均为虚构数据。

本次 ChatGPT 插件目录检索 `WeChat Weixin 企业微信 微信` 未返回可直接安装的匹配项。该检索不穷尽整个插件生态；不能据此断言所有微信插件都不存在。

## 阅读入口

[方案与生态比较](docs/ecosystem.md) 解释普通微信群、微信私聊和企业微信的能力边界。[架构与接口](docs/architecture.md) 定义收发链路、授权、去重和投递状态。[接入、聊天规则与上线验收](docs/setup-and-safety.md) 给出配置方式、dot 指令草案和安全边界。

[群策略示例](config/group-policy.example.json) 默认禁用接入、空白群白名单、草稿模式。[消息 Schema](contracts/message-event.schema.json)、[工具定义草案](contracts/tools.json) 与[合成事件](examples/message-event.json) 是待实现网关的项目契约，不是可以直接导入 ChatGPT 就自动生效的原生配置。

## 离线检查

使用 Python 3.11 或更高版本，在隔离环境中安装开发依赖并运行：

```sh
python -m venv .venv
# 激活环境：Linux/macOS 用 source .venv/bin/activate；Windows 使用相应激活脚本。
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

检查范围仅限配置与 JSON Schema 一致性和若干负面输入，不能证明 MCP 协议互通、微信登录、自动唤醒或实际发送成功。详细记录见[验证记录](docs/validation.md)。

## 安全边界

群消息不是账户所有者的授权。不得把个人 dot 已连接的邮件、文件、GitHub 或浏览器权限转授给群成员。dot 的记忆和插件连接可能跨入口共享，提示词和桥接层的 `room_id` 都不能建立模型内部的硬隔离。保留个人 dot 时，第一阶段只生成待审核草稿；需要自动服务多人时，应先验证可用的独立身份和权限隔离方案。[OpenAI 隐私与权限说明](https://help.openai.com/en/articles/20001529-dots-privacy-security-and-safety-faqs)

仓库为公开仓库。不要提交真实群消息、登录二维码、账号令牌、OAuth 密钥、webhook 签名密钥或订阅回调地址。
