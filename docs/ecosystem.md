# 普通微信群接入方案筛选

核查日期：2026-10-16。目标固定为现有普通微信群与原 ChatGPT dot，优先使用程序化读写接口。本轮清单用于安排验证顺序；满足相同准入标准的新实现可另行加入。

## 结论

优先验证 WeChatFerry 与 Wechaty + 具体 Puppet。它们分属客户端接口实现和 SDK 适配层：Wechaty 的底层若选择 XP，同样依赖 Windows 客户端；选择 PadLocal，才转向 Pad 协议服务路线。[Wechaty Puppet 定义](https://wechaty.js.org/docs/specs/puppet) · [XP](https://github.com/wechaty/puppet-xp)

| 验证对象 | 已核查证据 | 第一项验证 | 当前判断 |
| --- | --- | --- | --- |
| WeChatFerry | 上游于 2026-07-10 归档；发布页列出 v39.5.2 配套微信 3.9.12.51；有收发消息接口 | 隔离 Windows 环境中的版本匹配、正常登录和普通群收发 | 有现成兼容环境时先做最小测试 |
| Wechaty + PadLocal | 文档列出个人微信、Pad 协议、文本收发及群操作；2026 年有申请入口失效报告 | 供应方确认服务与凭据可获得性，随后演示目标群收发 | 有可用凭据后进入代码验证 |
| Wechaty + XP | 版本表列出 puppet-xp 2.1.1 对应微信 3.9.10.27，属于本地 Windows Puppet | 对应版本的登录、群身份与 @ 元数据 | Windows 路线的备选，和 WeChatFerry 存在依赖重叠 |

来源：[WeChatFerry 项目](https://github.com/lich0821/WeChatFerry) · [发布页](https://github.com/lich0821/WeChatFerry/releases/latest) · [PadLocal 功能文档](https://wechaty.js.org/docs/puppet-providers/padlocal/) · [PadLocal 问题 #358](https://github.com/wechaty/puppet-padlocal/issues/358) · [XP 版本表](https://github.com/wechaty/puppet-xp#version-support)

## WeChatFerry：先验证客户端可用性

WeChatFerry 提供 Python 客户端与若干 HTTP 封装，底层是 Windows 微信 Hook。封装层方便连接网关，运行时仍依赖配套微信客户端。最新发布页标注的配套版本是 `3.9.12.51`，实际账号登录与持续群收发待实测。[项目说明](https://github.com/lich0821/WeChatFerry) · [发布页](https://github.com/lich0821/WeChatFerry/releases/latest)

本项目据此先做版本兼容性验证，后续维护来源单独核查。测试应从隔离账号、隔离主机和可追溯安装包开始。记录版本与包哈希，通过正常登录后再检查群消息、成员 ID、真实 @ 和原群回复。登录或关键群能力失败时暂停此版本，先寻找有实测证据的维护分支。

## Wechaty：把框架名落实到可运行的 Puppet

Wechaty 提供统一消息和群对象，实际收发由 Puppet 实现。PadLocal 文档列出个人微信群功能，并要求 Token；XP 文档列出具体 Windows 客户端版本。选型记录应包含 Puppet 名称、包版本、客户端或服务版本及群功能验证结果。[Puppet 目录](https://wechaty.js.org/docs/puppet-providers) · [PadLocal](https://github.com/wechaty/puppet-padlocal) · [XP](https://github.com/wechaty/puppet-xp)

PadLocal 的服务可获得性是当前主要疑问。问题 #358 于 2026-05-01 报告申请网站连接失败，#382 于 2026-08-22 继续报告入口问题。这些属于使用者报告；凭据发放、后端运行和支持响应仍需供应方确认。[#358](https://github.com/wechaty/puppet-padlocal/issues/358) · [#382](https://github.com/wechaty/puppet-padlocal/issues/382)

优先取得适用普通微信群的试用凭据、兼容版本清单和低频收发演示，再投入适配代码。

## 其余生态的定位

企业微信会话与私聊通道已移出本项目接入清单。腾讯微信插件当前文档声明私聊能力，因此本轮工作集中在具备普通群能力的适配器。[OpenClaw 微信能力说明](https://docs.openclaw.ai/channels/wechat)

wxauto 属于 Windows 桌面 UIAutomation 路线，可作为放宽底层实现约束后的备选。给它加 HTTP 或 MCP 包装，仍保留桌面控件和窗口状态依赖；本轮优先级低于协议或 Hook 接口。若仅要求 dot 通过工具调用、允许适配器操作桌面，则可将它加入下一轮验证。[wxauto 项目说明](https://github.com/cluic/wxauto)

MCP Events 保留为 dots 侧的共同接入层。通道确定以后，两个候选都接入同一套网关和事件工具契约。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## 决策方式

本轮进行两个准入检查：WeChatFerry 检查客户端与登录，Wechaty 检查具体 Puppet 与服务可获得性。先满足前置条件的一侧进入普通群收发测试。通过后再连接 dot，详细通过标准见[通道验证计划](transport-validation.md)。
