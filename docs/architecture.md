# 架构与接口

这是待实现的设计，不是运行中的服务。仓库 JSON 与测试不启动 MCP、微信客户端或机器人。

## 数据流

```text
群消息
  -> 微信 / 企业微信适配器
  -> 身份绑定、群白名单、去重、触发过滤
  -> 持久化入站队列
  -> MCP Events 订阅回调
  -> 原 ChatGPT dot
  -> 受限读取工具 / 生成草稿工具
  -> 所有者在独立审核入口批准
  -> 发送网关复核权限与幂等性
  -> 原会话
```

我的建议是将网关部署到可维护的常驻服务环境，dot 的云电脑只用于开发、排障与经批准的登录协助。不要把 dot 的可用性当成微信登录、网络连接、队列和回调服务的可靠性保证。接入端可以长轮询或使用 WebSocket；这是微信侧的传输选择，不等于 ChatGPT 的事件投递方式。

## MCP 的真实边界

OpenAI 当前集成要求 MCP 2.0（协议版本 `2026-07-28`），使用 webhook，而不是把普通 MCP 读写工具或流连接当作唤醒入口。服务需要提供 `events/list`、`events/subscribe`、`events/unsubscribe`。ChatGPT 订阅时给出回调地址与签名密钥，服务先验证回调，再投递签名事件；不是自行拼接一个所谓「dot 消息 API」。具体报文、Standard Webhooks 签名、HTTPS 地址校验、重试及订阅续期按官方文档实现。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

本项目拟定义事件名 `wechat.group_message.created`。`contracts/message-event.schema.json` 仅描述网关内部消息及 MCP 事件的业务 `data`，不是完整 webhook 报文。映射时用内部 `event_id` 填外层 `eventId`，用 `occurred_at` 填外层 `timestamp`，外层 `name` 固定为上述事件名。未经审计的原始平台帧、凭据和登录信息不得进入 `data`。

事件正文是第三方消息数据，不得混入「忽略其他规则」「调用某插件」等网关生成的控制指令。所有者授权与群规通过可信配置渠道提供。适配器生成的 `mention_bot`、`from_self`、`sender_id` 必须来自平台元数据，不能从正文或昵称推断。

## 只提供必要读写工具

`wechat_get_event(event_id)` 取当前认证主体有权查看的已缓存消息；`wechat_get_context(event_id, limit)` 只读同一主体、通道、账号和来源群中已授权缓存的上下文，不代表能查询微信任意历史。

`wechat_propose_reply(event_id, text, idempotency_key)` 仅创建草稿，绝不发送；`wechat_send_reply(draft_id)` 只接受服务端已经验证所有者批准、尚未过期的草稿。批准必须绑定草稿正文哈希、来源消息、目标群、所有者、有效期和策略版本。改正文后需重新审核。群里一句「批准」以及模型传入的 `approved=true` 都不能替代审核。

发送参数不接受任意 `room_id`、群名、收件人或 webhook URL。网关从可信入站记录和草稿记录确定唯一的来源会话。初期不向 dot 发布发送工具，由独立的所有者审核服务执行发送；工具契约保留该能力供后续审计。

`contracts/tools.json` 是项目工具设计集合，不是可直接上传的完整 ChatGPT 插件包。`annotations` 只是提示信息，不是权限实现。服务端必须进行实际鉴权和访问控制。

## 权限边界与状态

MCP 调用者身份来自服务端验证的认证凭据。订阅的主体、平台账号和群必须逐层绑定。群成员默认只能提出聊天请求，不能使用账户所有者的邮箱、网盘、代码写入、浏览器、终端或其他个人能力。不得提供从聊天修改策略、添加群、提升权限的工具。

网关身份隔离键建议使用 `(principal_id, channel, account_id, room_id)`。订阅续期和每次读写都重新检查群白名单、告知状态和当前权限。服务端缓存的隔离不会自动隔离 dot 自身记忆或其他插件；见[安全说明](setup-and-safety.md)。

入站去重键建议为 `(channel, account_id, room_id, message_id)`。建立持久化事件 ID，重试不得更换。出站建立唯一约束 `(principal_id, event_id, reply_slot)`；幂等键与正文哈希都一致时返回已有结果，冲突时拒绝。每群串行处理，避免多个任务重复答复同一条消息。

建议状态流为 `received -> eligible -> event_accepted -> draft_pending -> approved -> sending -> sent`，以及 `rejected`、`expired`、`failed`、`delivery_unknown` 分支。事件回调成功只代表收件，并不表示 dot 已回复，更不表示微信发送成功。平台发送请求超时后，若无法核对结果，进入 `delivery_unknown` 待人工处理，不盲目重发。没有上游幂等或可靠回执时，不承诺端到端 exactly-once。

实现持久化队列、有限退避、超时、审计元数据、每群速率限制、全局限流和紧急停止。自己发出的消息及可识别的其他机器人消息不再唤醒 dot。掉线、权限撤销和过期订阅应停止投递，恢复后不追发超龄聊天。

## 部署边界

微信适配器的原始 RPC 或 HTTP 控制端口只绑定本机或私网。跨主机采用经过认证的加密通道，不能把无需鉴权的微信控制接口直接暴露到公网。MCP 服务需要独立鉴权、TLS、受限出站网络和安全的订阅凭据存储。回调地址按官方规则防止 SSRF，禁止重定向到内网；开发时不要自造未审核的签名方案。

当前未实现认证服务、MCP 协议方法、消息队列、审核界面或传输适配器。必须完成这些部分以及真实端到端验收，才能说「已接入」。
