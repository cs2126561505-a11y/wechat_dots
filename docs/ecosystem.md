# 普通微信群接入方案筛选

核查日期：2026-10-17。目标固定为现有普通微信群与原 ChatGPT dot。允许适配器封装数据库与桌面操作，对 dot 继续提供受限工具。本轮清单用于安排验证顺序。

## 结论

当前优先审查与现有微信 `4.1.15.13` 对应的新候选 `wechatauto-replica`，不降级日常客户端。由于日常主机同时保存主账号数据，实测移至仅登录测试账号的专用 Windows 虚拟机或备用电脑。源码审查与离线探针已落地，隔离和完整收发尚未通过。[审查结论](replica-audit.md)

WeChatFerry 与 Wechaty + 具体 Puppet 继续保留为备选。Wechaty 的底层若选择 XP，同样依赖 Windows 客户端；选择 PadLocal，才转向 Pad 协议服务路线。[Wechaty Puppet 定义](https://wechaty.js.org/docs/specs/puppet) · [XP](https://github.com/wechaty/puppet-xp)

| 验证对象 | 已核查证据 | 第一项验证 | 当前判断 |
| --- | --- | --- | --- |
| wechatauto-replica 1.2.6.1 / 62503d7 | 维护者记录微信 4.1.15.13；已审查账号回退、缓存、内存扫描和桌面写入路径 | 专用环境隔离，随后测试真实群与成员 ID、@ 元数据 | 当前优先审查，实时读取和发送均未准入 |
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

wxauto 属于 Windows 桌面 UIAutomation 路线。给它加 HTTP 或 MCP 包装，仍保留桌面控件和窗口状态依赖。此次允许底层桌面操作后，优先审查对象固定为上述 replica 提交，不把其他包或版本的能力混作本候选的验证结果。[wxauto 项目说明](https://github.com/cluic/wxauto)

MCP Events 保留为 dots 侧的共同接入层。通道确定以后，两个候选都接入同一套网关和事件工具契约。[OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events)

## 决策方式

当前先完成 replica 的隔离环境审查，再决定是否进行只读群消息测试。未通过则保留结论并调查备选，不自动切换账号、降低隔离要求或降级日常客户端。任何通道完整通过群收发后才连接 dot，详细标准见[通道验证计划](transport-validation.md)。
