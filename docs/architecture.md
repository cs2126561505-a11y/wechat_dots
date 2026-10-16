# 架构与接口

本页为普通微信群网关的实现草案，运行服务与端到端接入待完成。

## 数据流与部署

```text
普通微信群
  → WeChatFerry / Wechaty + 已验证 Puppet
  → 身份绑定、群白名单、去重、触发过滤
  → 持久化入站队列
  → MCP Events 订阅回调
  → 原 ChatGPT dot
  → 读取工具 / 回复草稿工具
  → 所有者在独立审核入口批准
  → 发送网关复核权限与幂等性
  → 来源群
```

适配器和网关运行在可维护的常驻环境中。Windows Hook 路线需要配套客户端主机，协议服务路线需要对应的服务凭据与连接。dot 的云电脑可承担开发和排障，微信连接、队列及回调由网关单独监控。

## MCP Events

按 OpenAI 文档实现 MCP 2.0，协议版本为 `2026-07-28`。在服务发现中声明事件能力，提供 `events/list`、`events/subscribe`、`events/unsubscribe`，持久化订阅并验证 ChatGPT 提供的回调地址，随后通过 Standard Webhooks 投递签名事件。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

事件名拟定为 `wechat.group_message.created`。`contracts/message-event.schema.json` 定义业务 `data`；外层 `eventId`、`timestamp`、`name` 分别取内部 `event_id`、`occurred_at` 和上述事件名。`channel` 固定为 `wechat`。普通群身份由适配器根据平台元数据核验，Schema 负责字段约束。

`sender_id`、`room_id`、`mention_bot`、`from_self` 来自可信适配器获取的平台元数据。事件正文作为第三方内容保存；群规和所有者授权来自独立配置入口。投递数据采用允许字段清单，凭据、二维码和原始登录帧留在私密凭据域。

## 工具与审核

`wechat_get_event(event_id)` 读取当前认证主体有权访问的已缓存事件。`wechat_get_context(event_id, limit)` 读取同一主体、通道、账号和来源群的授权缓存，并应用时间、条数和字符上限。

`wechat_propose_reply(event_id, text, idempotency_key)` 仅创建草稿，返回 `draft_pending`。`wechat_send_reply(draft_id)` 由独立审核服务使用，默认从 dot 的工具目录排除。

批准记录绑定正文哈希、来源事件、目标群、所有者、有效期和策略版本。发送时重新鉴权，正文或绑定条件变化后重新审核。群消息和模型传入的 `approved=true` 不能代替所有者批准。

发送接口只接收 `draft_id`，目标群由服务端根据事件与草稿记录确定。`contracts/tools.json` 是工具设计集合，具体权限由服务端执行。

## 身份、队列与投递状态

MCP 调用主体由认证凭据确定。建议隔离键为 `(principal_id, channel, account_id, room_id)`，订阅续期与每次读写重新检查权限、群白名单和告知状态。网关缓存隔离与 dot 内部记忆隔离分别验收，详见[权限说明](setup-and-safety.md#个人-dot-与权限)。

入站去重键为 `(channel, account_id, room_id, message_id)`，持久化 `event_id` 并在重试中复用。出站建立唯一约束 `(principal_id, event_id, reply_slot)`，仅在幂等键和正文哈希均相符时复用结果，冲突则拒绝。每群串行处理。

状态流建议为 `received → eligible → event_accepted → draft_pending → approved → sending → sent`，另设 `rejected`、`expired`、`failed`、`delivery_unknown`。事件接受、草稿生成和微信发送成功分别记录。发送超时且结果无法核对时进入 `delivery_unknown`，暂停自动重试并交由所有者核验。上游缺乏幂等或可靠回执时，不能保证端到端 exactly-once。

队列采用有限退避、超时、限流和紧急停止。过滤自身与已识别机器人消息，掉线或权限撤销时暂停投递；恢复后仅处理有效期内的消息。

## 网络与密钥

微信适配器的原始控制端口绑定本机或私网，跨主机使用认证加密通道。公网 MCP 服务独立鉴权并启用 TLS。订阅回调在连接时核验目标地址，阻断内网地址和重定向，签名及密钥轮换依照官方规范实现。[回调安全要求](https://developers.openai.com/plugins/build/mcp-events)

上线实现包含适配器、认证、事件方法、持久化队列、审核入口和发送网关，按[验收表](setup-and-safety.md#上线验收)逐项验证。
