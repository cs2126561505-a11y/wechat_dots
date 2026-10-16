# 方案与生态比较

核查日期：2026-10-16。这里区分文档已支持、项目声称支持、目标账号实测三个证据层级。本次完成前两层的桌面核查，没有微信登录或目标群实测。

## 先区分三个目标

现有普通微信群、微信私聊、企业微信机器人可用会话不是同一种入口。产品写着「支持微信」并不能证明它支持原有个人微信群，更不能证明能读取全部聊天历史。

本项目优先保留原来的 ChatGPT dot，不将另一个调用模型 API 的机器人当作它的替身。消息通道应只负责传输，dot 才是执行和回答的一侧。

## 初步筛选

| 候选 | 已核查能力与边界 | 对本项目的判断 |
| --- | --- | --- |
| OpenAI MCP Events + MCP 工具 | 官方文档明确支持 dots 的事件订阅，受账户与工作区控制 | dots 侧首选；须自行实现、部署并测试微信适配服务 |
| WecomTeam 智能机器人 SDK | 提供 WebSocket 消息事件、回复及主动发送方法 | 可迁移到受支持企业微信会话时首选；不等于原普通微信群直连 |
| Tencent/openclaw-weixin | 官方微信通道插件和后端协议；OpenClaw 文档标明能力元数据仅声明私聊 | 微信私聊可优先研究；不能据此承诺普通微信群 |
| Wechaty | SDK 抽象提供群和消息操作，底层依赖具体 Puppet | 普通群的实验候选；先验证具体非浏览器适配器，不预先推荐付费供应商 |
| WeChatFerry | Windows 微信 Hook，提供 Python 和多种 HTTP 封装 | 普通群收发原型的条件性候选；客户端版本、登录和风险检查不过就停止 |
| LangBot / CowAgent | 有消息通道、插件及模型编排生态 | 可参考或复用通道层，但默认运行的是其自身 Agent，不是原 dot |

[MCP Events](https://developers.openai.com/plugins/build/mcp-events) · [WeCom Python SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk) · [WeCom Node SDK](https://github.com/WecomTeam/wecom-aibot-node-sdk) · [腾讯微信插件](https://github.com/Tencent/openclaw-weixin) · [OpenClaw 微信范围说明](https://docs.openclaw.ai/channels/wechat) · [Wechaty](https://github.com/wechaty/wechaty) · [WeChatFerry](https://github.com/lich0821/WeChatFerry) · [LangBot](https://github.com/langbot-app/LangBot) · [CowAgent](https://github.com/zhayujie/CowAgent)

## 普通微信群：保留目标，但不承诺即装即用

Wechaty 的接口方便做统一适配，真正决定可用性的是 Puppet 的协议和服务。项目 README 同时存在多代适配器与弃用项。列在目录中不代表今天仍能登录，也不代表稳定性或平台授权。采购前应要求对方在目标群演示收消息、识别真实成员 ID、识别真实 @、回复原群、重连及重复消息处理。不要把聊天正文里的昵称当成身份，也不要提交二维码或 token 到公开仓库。[Wechaty 协议与群接口](https://github.com/wechaty/wechaty)

WeChatFerry 的 README 有 Windows 注入结构、接收消息和发送文本的说明；其可见版本记录包含微信 3.9.12.51 的适配。这证明存在程序化接口，不证明该版本目前仍能登录。我的判断是只在用户明确接受非官方通道风险、隔离测试账号与主机，并验证客户端匹配后开展原型；不为主账号或长期稳定性背书。接收不到合法消息时，不继续堆叠 MCP 或模型框架。[项目与版本记录](https://github.com/lich0821/WeChatFerry)

## 企业微信：工程上优先，产品目标需要变化

官方 SDK 可监听 `message.text`，通过 `reply_stream` 回复，也提供 `send_message(chatid, body)`。应先在管理端核对机器人允许加入的会话类型及回调范围，再做目标群测试。企业内部群、含微信联系人的外部群、普通微信群不能互相替代。拿到发送能力也不能推断得到任意群的读取能力。[SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk)

本次未取得足以确认目标外部群适用性的官方证据，因此不写「企业微信可以直接接现有普通微信群」。群里未 @ 机器人的消息是否可见，也必须单独验证。

## 生态适配，不要接错“大脑”

`@tencent-weixin/openclaw-weixin` 是 OpenClaw 插件，不是安装到 ChatGPT 就能用的同名插件。可以研究腾讯公开后端协议并编写适配器，但应复核适用条款和鉴权要求，不把后端 URL 当作免授权接口。[腾讯 README](https://github.com/Tencent/openclaw-weixin)

`zhayujie/chatgpt-on-wechat` 当前重定向到 `zhayujie/CowAgent`。它和 LangBot 的价值在通道、管理面板与独立 Agent 生态。接本项目时必须改成事件与工具桥接，禁用重复的默认回复链；仅填 OpenAI API Key 不会继承你现有 dot 的身份、上下文和权限。[CowAgent](https://github.com/zhayujie/CowAgent) · [LangBot](https://github.com/langbot-app/LangBot)

浏览器和桌面点击路线不作为默认方案：它们额外依赖登录状态、界面与可操作环境。dot 拥有云电脑是已记录的产品能力，但不能据此推出微信网页版一定可登录，或云电脑可充当长期公开服务主机。[dot 的电脑与应用](https://learn.chatgpt.com/docs/dots/computers-and-apps)

## 插件目录核查

本次通过 ChatGPT 的插件管理搜索检索 `WeChat Weixin 企业微信 微信`，结果为空。这个结果仅代表本次可见搜索没有匹配项，目录检索并不穷尽全部插件。没有据此安装替代插件，也没有将邮件、日历或浏览器插件误当成微信读写接口。

## 选型决策

在「不迁移现有普通微信群」约束下，先做一个隔离的非官方传输可行性测试，成功后才实现 MCP 适配；Wechaty 必须落实到具体可验证 Puppet，WeChatFerry 必须通过客户端与登录检查。在「可迁移企业微信」条件下改用官方 SDK。在「可先接受私聊」条件下研究腾讯微信插件。若只能得到独立模型机器人而无法触发真正的 dot，应明确报告目标未达成，而不是换名交付。
