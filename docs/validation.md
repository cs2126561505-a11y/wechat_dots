# 验证记录

核查日期：2026-10-17。目标范围为普通微信群与原 ChatGPT dot。

## 2026-10-17：主机重启完成，来宾首次启动受阻

环境方案收敛为本地 E 盘上的专用 Windows 虚拟机，仅登录测试账号。Broadcom 资料审核显示 `Account verification is Pending`，下载指导见[账户与下载说明](broadcom-download-guide.md)。完成兼容评估后，所有者明确确认先试 VirtualBox，并保留现有安全功能。VirtualBox、E 盘测试虚拟机与自动安装资料已配置，所有者已重启主机；首次后台启动在 VirtualBox 进程创建阶段失败，尚未进入 Windows 安装。主机 VBS、内存完整性及日常微信保持现状。

微软官方 Windows 11 企业评估版中文 x64 镜像已完成下载，文件大小为 `8548628480` 字节，SHA256 为 `b37a06d5121eee017826c0b133250bbd21bec694c2d607c60edd29090e667a84`，与官方发布值一致。镜像提供 90 天评估，尚未安装到来宾。[官方下载页](https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise) · [官方校验值](https://support.microsoft.com/en-us/servicing/os/windows/docs/2026/09/verify-the-authenticity-of-a-windows-11-enterprise-evaluation-iso-file)

以仓库提交 `34031493082ad088df4252d2e6749cf075f346d4` 为源快照，准备了固定上游数据库模块、23 个 Windows x64 / Python 3.12 离线依赖、逐文件哈希锁定清单、签名有效的 Python 3.12.10 与微信 4.1.15.13 安装包及来宾初始化脚本。资料包已制成 UDF 只读光盘镜像，重新挂载后核对 55 个文件全部通过。独立临时环境中离线安装和 `pip check` 通过，34 项合成测试通过，审查结果仍为实时读取、发送和 dot 关闭；这属于宿主上的迁移准备验证，尚未构成来宾测试。账号、数据库、密钥及主机运行缓存均未加入资料包。

腾讯官方 `WeChatWin_4.1.15.exe` 链接当前已被 `4.1.15.53` 覆盖，因此改从公开的 [4.1.15.13 历史归档](https://github.com/cscnk52/wechat-windows-versions/releases/tag/v4.1.15.13)获取固定版本安装器。文件大小为 `256624952` 字节，SHA256 为 `d5f3bb4232c0f172bce53141db90bf60f5f0a53eb137663ab8d32e74e04b9f4f`，与归档记录一致。Windows 签名验证确认安装器及内部 `Weixin.exe` 的腾讯签名均有效；解包后客户端 FileVersion 和 ProductVersion 均为 `4.1.15.13`，客户端 SHA256 为 `a832fb321ddb5f9701dd3a7bb1958804f09b620b9ad328f52e2e352651ab886c`。该安装器已加入来宾资料包；宿主仅作文件检查，未执行安装器或客户端。来宾安装后还需复核实际运行版本，当前结论仅覆盖文件来源、完整性及版本。

下一阶段先排查 VirtualBox 启动失败，恢复启动后监测主机内存并完成 Windows 来宾安装，再执行离线验收与测试账号绑定审查。实时入口仍保持关闭，普通群收发和 dot 接入均未验证。详细下载及校验记录仅保存在本地实验目录。

### 重启后的检查与启动诊断

系统启动时间确认更新为本地时间 2026-10-17 07:40:56，Windows Hypervisor Platform 已启用，VBS 与内存完整性继续运行。采样可用内存约 3.6–3.8 GiB，低于此前建议的约 6 GiB 操作余量。采用既定 4 GiB 来宾配置作一次受监测的启动尝试，计划在主机内存余量持续过低时保存来宾状态；实际在内存负载测试前已启动失败，因此尚无来宾性能结论。VS Code 和当前任务均保留。

本轮启动返回 `E_FAIL (0x80004005)`，完整性检查日志终止于 `supR3HardenedWinReSpawn`、`-104` 和 Windows 错误 `5`。对照 [VirtualBox 官方启动源码](https://github.com/VirtualBox/virtualbox/blob/master/src/VBox/HostDrivers/Support/win/SUPR3HardenedMain-win.cpp)，该提示位于创建下一层 VM 子进程失败的分支；它本身不足以确定驱动冲突、内存不足或 Hyper-V 性能问题。VirtualBox 主驱动正在运行，相关程序签名有效，虚拟磁盘仍为空白的 2 MiB 文件。发现两套游戏反作弊驱动正在运行，仅列为兼容性排查候选；尚未确认因果关系，也未停止驱动或修改安全功能。

Windows 界面工具连续两次返回 `trusted Node process exited unexpectedly`，目前无法代操作正常启动界面，所有者因此手动进行对照。打开 `.vbox` 文件产生已有 UUID 重复注册提示；管理器的 USB 枚举提示对应未安装的 Host USB Proxy Service，而本机测试配置已经关闭来宾 USB。随后点击正常启动生成了新的 `VirtualBoxVM.exe` 日志，复现与后台启动相同的子进程创建失败；虚拟机仍关机、磁盘仍为空白。正在征求所有者是否通过厂商提供的退出方式暂时退出游戏反作弊程序作对照，尚未执行。账号凭据保存在本机受限目录，完整诊断日志仅保存在本机。

内存来源检查发现 11 个 Node 进程，合计约 417 MiB 私有工作集；其中 10 个属于 Codex，约 395 MiB，另一个属于 Adobe。Codex 进程包括三组代码审查、应用工具、浏览器控制服务，以及一个文档产物服务。启动时间呈批次，与会话初始化触发服务的解释相符；缺少操作前后的连续记录，无法断言关闭预览页导致了这些进程，也无法据进程数量判断泄漏。相比重启前一次约 850 MiB 的采样，占用已经下降。当前 ChatGPT 主程序及子进程合计约 1.5 GiB；此处使用私有工作集，未把共享内存重复累加。

### 安装前：审核等待期间的虚拟机兼容评估

只读检查确认宿主为 Windows 11 家庭版 23H2（22631.6199）、i7-13650HX，物理内存可用容量 15.63 GiB，采样时剩余 2.89 GiB；E 盘剩余 114.92 GiB，C 盘剩余 22.36 GiB。系统报告 Microsoft hypervisor 已加载，VBS 与内存完整性正在运行；Windows Hypervisor Platform、Virtual Machine Platform、WSL 可选组件均关闭。家庭版缺少完整 Hyper-V 角色，与已有安全功能使用 hypervisor 属于不同层面。[微软 Hyper-V 版本要求](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v) · [VBS 状态解释](https://learn.microsoft.com/en-us/windows/security/threat-protection/device-guard/enable-virtualization-based-protection-of-code-integrity)

VirtualBox 7.2 官方共存方案要求 Windows Hypervisor Platform 已启用，并继续提示部分宿主可能明显降速；因此本机试用包含启用该组件、安装驱动及可能重启。Workstation 在宿主 VBS 开启时同样使用 Windows Hypervisor Platform 技术，当前硬件和系统满足其文档列出的共存最低条件，实际运行模式仍须安装后确认。两款软件在本机的性能均待实测；Broadcom 关于 17.x / Windows 24H2 的降速记录只能作为机制与风险参考，不能直接认定当前版本受同一故障影响。[Oracle 共存说明](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/AdvancedTopics.html) · [Workstation 共存条件](https://knowledge.broadcom.com/external/article/315420/) · [Broadcom 性能说明](https://knowledge.broadcom.com/external/article/417896/after-the-host-os-upgrade-to-version-win.html)

建议有限试用的起点为 2 个虚拟处理器、4 GiB 内存和 80 GiB 动态虚拟硬盘，启用来宾 EFI、TPM 2.0 与 Secure Boot。启动前可用内存达到约 6 GiB 属于本项目的操作余量目标，4 GiB 是 Windows 来宾最低配置，实际微信及更新负载可能需要更多内存。先观察 Windows 桌面、重启、输入与离线测试的响应及宿主内存压力；评估窗口设为系统初始化和更新完成后的约 30 分钟。持续换页、卡顿或启动失败时结束试用并关机。页面文件保持系统管理，VS Code 与正在执行的任务继续保留。[Windows 11 虚拟机要求](https://learn.microsoft.com/en-us/windows/whats-new/windows-11-requirements)

VirtualBox 自定义程序安装目录及其父目录存在专门权限要求，现有 E 盘实验目录使用继承的数据盘权限。低改动方案建议软件本体使用 C 盘默认程序目录，虚拟硬盘、快照和镜像放 E 盘；若要求软件本体也位于 E 盘，则另行设计专用受保护目录。首轮使用 NAT，主机文件夹共享、剪贴板、拖放和 USB 自动连接保持关闭，文件通过已校验的只读资料光盘进入来宾。保留 VBS 的方案主要涉及可卸载软件、可恢复的平台组件开关和可独立保留的虚拟机文件，属于中低复杂度试用；关闭主机安全功能的性能路线超出本轮推荐范围。后续改用 Workstation 时，复用系统与资料 ISO 重建来宾最稳妥，虚拟磁盘、TPM 状态和快照的跨产品迁移另需验证。[Oracle 安装目录与卸载说明](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/installation.html)

评估结论为：VirtualBox 值得作为审核等待期间的有限试验，前提是先解决内存余量并接受平台组件变更及重启；收益来自缩短等待和获得本机证据，尚无证据证明其 Hyper-V 性能风险已消失。本轮仅完成读取与评估，系统组件、目录权限、虚拟机软件和实时探针门禁均未修改。

### 确认试用后的安装结果

从 [Oracle 官方目录](https://download.oracle.com/virtualbox/7.2.20/)取得 `VirtualBox-7.2.20-175154-Win.exe`，大小为 `178192488` 字节。SHA256 为 `a81777d2b36380ce042a29e9c554cf032eb46a793f62e3cc82e7411e535c2c26`，与官方清单一致；Windows 验证 Oracle America, Inc. 数字签名有效。管理员安装步骤返回成功，命令行报告 `7.2.20r175154`。软件本体位于默认程序目录，虚拟机及安装资料位于 E 盘实验目录。

安装选项仅包含基础 `VBoxApplication`，Windows Hypervisor Platform 从 Disabled 变为 Enabled，并返回需要重启；过程禁止自动重启。安装前后 VBS 状态均为运行，内存完整性均为运行。所有者随后表示可在控制面板手动开启组件，已告知此前获确认的步骤已完成启用，接下来只需重启；后续涉及 Windows 功能的操作提前提示所有者。

已创建 `WeChatLab-Test`：2 个虚拟处理器、4096 MiB 内存、80 GiB 动态 VDI，配置 EFI、虚拟 TPM 2.0、Secure Boot、VBoxSVGA 与 NAT。主机文件夹共享为空，剪贴板、拖放、USB 与远程显示关闭。虚拟机目录使用仅当前用户、SYSTEM 和管理员可访问的权限。配置核查证明当前为关机状态；来宾启动、性能、运行时安全状态和账号隔离仍待验证。NAT 属于连接方式，单凭该配置不能证明来宾与宿主、局域网之间的网络隔离。

VirtualBox 对准备好的 Windows 镜像识别为 `Windows11_64` / `10.0.26300.9457` / `EnterpriseEval` / `zh-CN`，报告支持自动安装。已生成自动安装文件，移除上游模板中的 CPU、RAM、TPM、Secure Boot 和存储检查绕过项；初始自动登录限定于安装阶段，并准备首次登录时清除自动登录密码的步骤。生成后的 XML 可解析，检查绕过项为空，账号与计算机名正确。初始凭据及自动安装文件保存在本机受限目录，未输出凭据或提交仓库。安装光盘与资料光盘均已配置，系统镜像及资料包哈希重新核对通过；此处记录的是安装准备，首次登录处理及 Guest Additions 的实际结果待来宾运行后验证。

最后一次采样可用内存为 3.09 GiB。虚拟机保持关机，待所有者手动重启后重新评估内存，再执行来宾安装；VS Code、浏览器及正在执行的工作均保留。本阶段尚未运行微信安装器、登录测试账号或打开真实数据库入口。

## 2026-10-17：固定版本审查与离线探针

从基线 `5487ebce162dd19d7706a33b9d4a61f11536ac3f` 准备本地工作区，新增 `wechat_probe`、固定源码获取脚本与独立探针依赖。正式配置、事件 Schema、工具契约和原有 17 项测试保持不变。候选固定为 `wechatauto-replica 1.2.6.1` / `62503d76a0db069171cb4e42e87090ad285db489`，详细路径结论见[源码审查](replica-audit.md)。

离线执行环境为 Windows、Python 3.12.0、jsonschema 4.26.0。执行 `python -m unittest discover -s tests -v`，34 项测试通过。新增的 17 项测试覆盖精确唯一群身份、平台源字段与正文伪造的区分、未知元数据、合成标记过滤、真实成员标识、脱敏记录、跨分片去重、事务与重启、源码篡改、异常脱敏，以及隔离门禁在任何实时入口之前生效。SQLite 测试完全使用合成数据。

本机仅做版本与目录发现：一个主微信进程，客户端 `4.1.15.13`，发现两个账号数据目录。没有调用取密钥或内存读取函数，没有解密或查询真实聊天数据库，没有发送群消息。依赖安装于独立 `.venv-probe`，上游完整自动化包未安装；源码缓存不进入 Git。

所有者补充日常主账号也在此电脑使用，随后选择专用 Windows 虚拟机或备用电脑仅登录测试账号；目前正在了解虚拟机，尚未确定创建或部署。当前代码因此无条件禁止实时读取，无命令行或环境变量放行入口。本轮未创建虚拟机、变更系统功能或调整微信版本。

| 验收项目 | 本轮证据 | 结果 |
| --- | --- | --- |
| 固定源码与风险路径 | 提交、文件哈希、账号回退、缓存、扫描与桌面写入的源码位置 | 审查完成，完整候选未准入 |
| 当前电脑账号隔离 | 两个账号目录；无法用目录选择约束进程内存扫描 | 不满足，停止实时读取 |
| 隔离门禁 | 命令入口与直接函数调用均在数据库、进程读取与运行状态创建前停止 | 离线通过 |
| 群身份与真实 @ | 合成联系人表、SQLite 分片、候选源字段解析对照 | 离线通过，实机待验证 |
| 重复读取与恢复 | 合成 SQLite 事务、重复事件和状态重开 | 离线通过；真实断线恢复待验证 |
| 同名会话与发送确认 | 同名群拒绝的合成测试；桌面发送缺少稳定 ID 绑定 | 实机未验证，发送功能未实现 |
| 网关、MCP Events、dot | 原契约与默认关闭策略保留 | 未连接、未验证 |

下一步是等专用测试环境确定后审查其隔离与绑定证据，再实施读取放行。普通文本、真实 @、假 @、自身消息、断线恢复和唯一固定回复都不能据离线测试标为实机通过。

## 历史记录：2026-10-16

### 本轮变更

接入清单收敛到 WeChatFerry 与 Wechaty + 具体 Puppet，补充上游归档状态、PadLocal 凭据入口问题及[通道验证计划](transport-validation.md)。现有五份 Markdown 文档删除重复表述与范围外分支，聚焦选型、接口、聊天配置和验证结果。

事件与群策略契约升级到 `0.2`：事件 `channel` 固定为 `wechat`，群策略移除 `wecom_aibot`，配置和合成事件同步更新。原有 `0.1` 事件与策略须先审阅并迁移，工具契约继续使用独立的 `0.1` 版本。

### 离线验证

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

### 当时的实机状态与下一步

微信账号登录、普通群收发、Puppet 凭据、网关执行器、MCP 订阅与 dot 唤醒均待实现或实测；GitHub Actions CI 待配置。当前结果证明项目契约的离线一致性。

认证、平台身份核验、成员告知、审批、运行时限流、网络防护及 dot 权限隔离通过后续服务与端到端测试验证。下一步从[通道准入](transport-validation.md)开始，通过后进行[上线验收](setup-and-safety.md#上线验收)。
