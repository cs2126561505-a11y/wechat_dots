# 实验网关

`wechat_gateway` 已实现带认证的文本事件与回复草稿服务。ChatGPT 网页授权与原 dot 的真实工具调用通过；事件订阅及微信回复待验收。部署配置中的采集、群绑定和发送保持关闭。

## 接口与授权

`/mcp` 使用协议 `2026-07-28`，实现发现、工具调用和事件订阅方法。服务逐次核对版本、客户端能力与 HTTP 请求头；监听环回地址，由反向代理提供 TLS。[MCP Events](https://developers.openai.com/plugins/build/mcp-events) · [MCP HTTP 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)

dot 通过授权码和 S256 PKCE 连接。当前授权服务针对单个所有者，使用本地保存的口令；回调采用精确白名单，令牌绑定固定发行者与资源。刷新令牌单次轮换，重复使用撤销该授权系列。订阅绑定授权系列，撤销后停止投递。[OpenAI 认证要求](https://developers.openai.com/plugins/build/auth)

`base_url` 是浏览器来源与接口地址的基准，`issuer_url` 是逐字比较的 OAuth 标识，可明确配置为同一 HTTPS 根地址带或省略末尾斜杠的形式。发现文档、授权服务器列表和回跳 `iss` 始终使用同一值；持久化状态绑定这个精确值，配置变更会拒绝启动。现有实验部署的规范形式变更经过无有效授权检查、私密备份和旧授权失效处理。

四项工具分别是连接状态 `wechat_get_connection`、事件读取 `wechat_get_event`、上下文读取 `wechat_get_context` 和回复草稿 `wechat_propose_reply`。发送工具从 MCP 目录及调用中排除。适配器凭据绑定主体、账号和群，所有者审核使用独立凭据；dot 的令牌只允许使用 MCP 入口。

## 持久化与发送边界

身份匹配、真实 @ 明确为真、自身与已知机器人标志为假、时间有效的消息才入队。未知 @ 保持未知并跳过触发。服务端根据主体、通道、账号、群和消息标识生成事件 ID，拒绝内容冲突的重复消息。

回调先验证挑战再启用。密钥加密保存，连接时验证公网地址并固定目标 IP，使用原主机名验证 TLS，关闭重定向。瞬时错误最多尝试五次，事件 ID 保持一致，签名每次更新；410 与 413 停止重试。2xx 表示回调接收，dot 后续处理单独验证。

每个事件只有一个回复槽，重复请求须匹配幂等键和正文。批准绑定正文哈希、原事件、账号、群、有效期和策略。适配器领取后先保存 `sending`；未确认的发送在重启后转为 `delivery_unknown`，暂停该群继续发送。实际桌面目标核对、回读及未知结果的人工核验入口仍待实现。合成回执仅验证状态转换。

## 运行配置

安装 `requirements-gateway.txt`，以独立环境和单个进程执行 `python -m wechat_gateway --config /private/gateway.json`。[配置示例](../config/gateway.example.json) 对应实际服务；`bindings` 绑定主体、账号和群，`policy` 对应代码中的 `Policy`，空对象使用关闭默认值。原 `group-policy.example.json` 仍是规划契约，运行服务尚未直接加载它。

`key_file` 保存回调加密密钥，`state_dir` 保存 SQLite 状态。私密配置、凭据和数据库均排除在公开仓库之外。Linux 部署使用独立服务账号，状态目录为 0700，配置及密钥由 root 管理并限服务组读取。

内容到期后逻辑清空正文和回执，保留去重和未知发送所需标识。SQLite 页面、WAL、磁盘快照与外部备份的物理清除，以及长期状态容量管理，仍需补齐。

## 当前证据

本地 79 项测试通过，其中 28 项覆盖网关、9 项覆盖新增来宾事件投递；Linux 部署包中的同样 28 项网关测试通过，依赖检查通过。真实环回 HTTP 测试完成 OAuth、订阅、模拟消息入队、模拟 dot 草稿、独立审核、适配器领取与合成确认。公网 HTTPS 检查完成授权码交换及工具、事件目录读取，自动检查使用的测试令牌已撤销，真实用户授权保留。

实际连接曾报告 `Origin rejected.`。检查发现授权页采用 `no-referrer`，按照浏览器 Fetch 规范，同源表单 POST 也会产生 `Origin: null`。现已改为 `same-origin`，保留同源提交校验，并在跨站回调时省略 Referer。用户重试后，日志确认同源授权提交返回 303，原拒绝已经越过；随后检查发现 `form-action 'self'` 还会限制 Chromium 的跨站回跳，已将配置内的精确 OAuth 回调加入表单策略。新增回归覆盖 CSRF、空来源与外站拒绝及回调策略；请求诊断仅记录固定接口、方法、状态码和来源类别。原 HTTPS 检查手动填入 Origin，并未驱动浏览器，因此没有覆盖这些行为；实际 ChatGPT 最终连接仍待确认。[Fetch 来源处理规范](https://fetch.spec.whatwg.org/#append-a-request-origin-header) · [浏览器表单与回跳策略](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action)

完整验收仍需实际 dot 唤醒、专用来宾采集、可靠群目标确认、经批准的测试回复及接收端确认，以及断线恢复和同名会话对照。接口当前仅处理文本，ZIP 文件传递尚未实现。网关测试使用合成消息，日常主机微信保持隔离。

实际浏览器已完成口令校验并回到 ChatGPT，客户端随后报告发行者不匹配。网关公开元数据与返回值核对一致；将发行者统一为带末尾斜杠的表示后，用户重连仍报告同一错误，因此该对照尚未解决问题。该次请求记录只有授权页读取和成功的 303 回跳，未出现新的发现文档读取或令牌交换。现有证据无法确定客户端保存的预期发行者值，也无法把错误归因于末尾斜杠。

授权配置更新后，普通页面刷新不能作为客户端已重读配置的证据。官方流程是在连接详情内选择 Refresh 并核对元数据变化。本轮保留原连接、新建独立测试连接作对照，并通过发现文档读取、注册、回跳和令牌交换的阶段记录核验结果。诊断保留严格发行者匹配，排除授权参数和凭据内容。[刷新连接元数据](https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata)

新建连接已重新读取发现文档并完成客户端注册，在桌面回跳中仍出现同一错误。随后使用普通浏览器的 ChatGPT 网页端连接成功，服务端确认授权码交换与认证后的 MCP 请求均成功；本会话也通过真实插件调用 `wechat_get_connection`，返回 `enabled=false`、`sending_enabled=false`、`rooms=[]`。这将差异定位到桌面授权回跳链路，具体缺陷仍待定位。当前保留已连通的网页授权，发行者配置保持稳定；原有 dot 的事件订阅与完整微信回路仍待验收。

所有者已在原有 dot 中调用同一工具并确认相同结果，原 dot 的工具使用通过。新的来宾事件投递代码已完成离线回归，尚待部署与实测。
