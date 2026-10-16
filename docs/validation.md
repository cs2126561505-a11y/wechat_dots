# 验证记录

核查日期：2026-10-16。目标范围为普通微信群与原 ChatGPT dot。

## 本轮变更

接入清单收敛到 WeChatFerry 与 Wechaty + 具体 Puppet，补充上游归档状态、PadLocal 凭据入口问题及[通道验证计划](transport-validation.md)。现有五份 Markdown 文档删除重复表述与范围外分支，聚焦选型、接口、聊天配置和验证结果。

事件与群策略契约升级到 `0.2`：事件 `channel` 固定为 `wechat`，群策略移除 `wecom_aibot`，配置和合成事件同步更新。原有 `0.1` 事件与策略须先审阅并迁移，工具契约继续使用独立的 `0.1` 版本。

## 离线验证

本轮从 GitHub 连接器读取基线文件。测试所需的七个文件在修改前，其 Git blob SHA 均与仓库基线 `0a3348f97234109c722b79a680527c0e9b20997f` 一致。基线上一轮记录为 14 项测试通过。

本轮离线执行环境为 Python 3.13.5、jsonschema 4.26.0，执行：

```sh
python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 17 tests in 0.025s

OK
```

保留原有 14 项检查，修正启用测试中的通道样例，并新增三项：接入候选范围约束、事件通道约束、`0.2` 版本要求。测试继续覆盖默认关闭、草稿模式、显式通道选择、必要身份字段、上下文上限、伪造批准和收件目标变更等输入。

测试副本的 Git blob SHA 如下，已与本轮 GitHub 写入结果和原有文件记录核对：

| 文件 | Git blob SHA |
| --- | --- |
| `config/group-policy.example.json` | `8545ae6c847c3c5fb6dcb7e6cdff4f1405d7383e` |
| `contracts/group-policy.schema.json` | `d8d66691b3bb7e53d887f74a1a88da02057fee7d` |
| `contracts/message-event.schema.json` | `f72cf93885702b92fe3cc27ce87067de3f5fd1c5` |
| `contracts/tools.json` | `e8f9428f4904e48e2db9f97f553bc2c57c78161a` |
| `examples/message-event.json` | `47cb32abf8a79299f12ed2426a22569127381ff1` |
| `tests/test_contracts.py` | `5dfc6969b4239c2138d591984113f80c2f46ae22` |
| `requirements-dev.txt` | `6e7e3a98c689e64dba53963f7df5bdd69fdecb6b` |

## 实机状态与下一步

微信账号登录、普通群收发、Puppet 凭据、网关执行器、MCP 订阅与 dot 唤醒均待实现或实测；GitHub Actions CI 待配置。当前结果证明项目契约的离线一致性。

认证、平台身份核验、成员告知、审批、运行时限流、网络防护及 dot 权限隔离通过后续服务与端到端测试验证。下一步从[通道准入](transport-validation.md)开始，通过后进行[上线验收](setup-and-safety.md#上线验收)。
