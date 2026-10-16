# 验证记录

核查日期：2026-10-16。交付为方案、配置与契约，不是已部署的机器人。

## 已完成

已通过 GitHub 连接器将文档、配置、契约、合成事件和测试脚本写入 `cs2126561505-a11y/wechat_dots` 的 `main` 分支，并读回仓库文件树确认路径。所有示例消息及身份均为合成数据，未写入真实群消息或凭据。

离线执行环境为 Python 3.13.5、jsonschema 4.26.0。容器直接克隆时无法解析 GitHub 域名，因此本地用已提交内容重建测试副本；测试脚本、5 个 JSON 文件与开发依赖文件共 7 个文件的 Git blob SHA 均与连接器返回的仓库文件树一致。核对基准提交为 `8b8adc7d665e62f84f6fe9b23368b23107bd6082`。不是仅检查了一份未提交的近似代码。

执行命令：

```sh
python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 14 tests in 0.016s

OK
```

这 14 项测试覆盖 JSON 解析、Schema 合法性、示例一致性、默认禁用状态、启用时的静态前置条件、非官方通道显式选择、第一阶段安全参数、事件必要字段与时间格式，以及读取/草稿/发送工具的输入约束。负例包括伪造批准字段、任意改变目标群、替换发送正文、超长或空白草稿、缺少幂等键、超量上下文和发布发送工具。

## 源文件核对

| 文件 | Git blob SHA |
| --- | --- |
| config/group-policy.example.json | fef7bb8befbbfe55229ee9b153ffe519bcdb60d2 |
| contracts/group-policy.schema.json | 5673f464e6e262f4d13fdb67f34a5e6302762bd8 |
| contracts/message-event.schema.json | e171dc37baa9c48d543ccac2938aef85d7995064 |
| contracts/tools.json | e8f9428f4904e48e2db9f97f553bc2c57c78161a |
| examples/message-event.json | 6721038d37155c66e63b89b5e3961c5f634a8d3c |
| tests/test_contracts.py | 1ca2a826e2bd467d790772c25a64b8502c8903a3 |
| requirements-dev.txt | 6e7e3a98c689e64dba53963f7df5bdd69fdecb6b |

## 没有验证的部分

未登录微信或企业微信，未测试目标群，未运行 Wechaty Puppet、WeChatFerry 或 WeCom SDK；未部署 MCP 服务，未在 dot 中安装本项目插件，未注册实际事件订阅、执行回调验证或发送群消息。没有运行 GitHub Actions CI。

JSON Schema 不能验证真实身份、群成员告知与授权、服务器鉴权、数据库唯一约束、审批真实性、运行时限流、SSRF 防护、提示注入防护或 dot 记忆隔离。工具描述中的行为都是后续实现要求，不应被当作已经生效的防护。

公开文档核查不等同于账号实测，也不证明服务的长期可用性。尤其不能把腾讯微信插件的私聊能力扩展解释成普通微信群能力，或把企业微信机器人适用会话扩展解释成任意微信外部群。

## 下一道验收门槛

先在获得必要授权的隔离测试群验证收发通道。普通微信群坚持不迁移时，必须先确认选定非官方适配器的实际登录和群能力；可迁移企业微信时核对管理端准入范围。然后用合成事件验证真正的 dot 订阅及草稿流程，最后进行[端到端验收](setup-and-safety.md)。没有通过这些测试前，不启用自动发送。
