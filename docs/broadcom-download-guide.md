# Broadcom 资料审核与 Workstation 下载指导

更新日期：2026-10-17。你已完成账号注册和登录，目前停留在 Broadcom 支持首页。本指导根据你提供的页面地址及导航文字定位入口，后续审核步骤依据 Broadcom 官方文档；账号的具体审核状态以登录后的提示为准。

## 从当前首页进入下载页面

在已登录的浏览器里打开[免费软件下载页](https://support.broadcom.com/group/ecx/free-downloads)，搜索 `VMware Workstation Pro`，展开产品和版本，选择适用于 Windows 的 `.exe` 安装包。

也可以沿门户菜单进入：`My Downloads` → `Free Software Downloads available HERE` → Workstation。你看到的 Products、Solutions、Support and Services、Company、How to Buy 属于网站顶部导航；直接打开上面的链接，可以更快到达本次任务的入口。[官方免费软件下载说明](https://knowledge.broadcom.com/external/article/397417/downloading-free-software-from-the-broad.html)

本次使用 Basic 账号的免费软件资格。若进入了要求 Site ID 的企业授权页面，返回免费软件下载页继续。Site ID 对应企业合同下的产品和服务授权。[账号注册与企业授权说明](https://knowledge.broadcom.com/external/article/145581/register-for-an-account-on-the-broadcom.html)

## 打开资料审核表单

在安装包页面，先点击 `Terms and Conditions` 阅读协议，再回到下载页勾选同意，随后点击下载图标。出现包含 `additional verification` 的提示时，选择继续，进入资料审核表单。协议勾选框仍为灰色时，刷新页面后重新检查。[官方下载流程](https://knowledge.broadcom.com/external/article/397417/downloading-free-software-from-the-broad.html)

审核页通常名为 `Trade Compliance and Download Conditions`。按页面字段填写完整、真实的姓名和实际地址，采用英文或拼音，并遵循各字段的格式提示。[官方审核资料说明](https://knowledge.broadcom.com/external/article/368588/)

| 信息 | 填写要点 |
| --- | --- |
| First Name / Last Name | First Name 填名，Last Name 填姓，采用完整拼写。 |
| 实际地址 | 写明街道和门牌，按表单拆分城市、省或州、国家及邮编。 |
| 国家或地区 | 选择与真实地址一致的项目。 |
| 姓名格式错误 | 从右上角账号菜单进入 `My Profile`，使用 `Edit` 修正姓名，再回到审核页。 |

姓名修改入口已在官方资料中更新为可由用户自行编辑。提交前检查字段是否完整、拼写是否一致，再点击 `Submit`。[姓名编辑说明](https://knowledge.broadcom.com/external/article/368737/)

## 判断审核进度

看到 `Account verification is pending`，表示账号验证仍待处理。先核对姓名、地址与字符格式；资料完整且已提交时，等待审核，再回到同一个安装包页面检查。审核队列可能持续数周，具体进度由 Broadcom 决定。[待审核提示处理](https://knowledge.broadcom.com/external/article/368588/) · [审核时长说明](https://knowledge.broadcom.com/external/article/368734/download-desktop-hypervisor-workstation.html)

下载图标可用，并能开始取得所选安装包，是本轮确认下载权限开通的直接依据。审核失败或表单持续报错时，保存错误原文，通过[官方支持入口](https://knowledge.broadcom.com/external/article/206567)查询账号资料或审核问题。

## 今天遇到登录异常时

你提供的[支持首页](https://support.broadcom.com/web/ecx/home)列出登录服务维护窗口：2026 年 10 月 17 日 05:00–09:00（PDT），对应北京时间 10 月 17 日 20:00 至 10 月 18 日 00:00。

若遇到登录后跳回首页、重复登录或间歇性报错，可在维护结束后重试。这个公告说明登录服务可能受到影响；账号审核进度仍需查看下载页的具体提示。

## 用 IDM 下载并保存安装包

权限开通后，从已登录浏览器点击官方安装包的下载按钮。本次保存位置设为 `E:\WeChatLab\media`。

IDM 自动接管时，核对文件名、保存位置和任务状态。若下载仍由内置浏览器处理，可继续使用该下载任务；希望使用 IDM 时，可在已启用官方 `IDM Integration Module` 的 Edge 或 Chrome 中登录同一账号，再发起下载。浏览器集成方式见 [IDM 官方说明](https://www.internetdownloadmanager.com/register/new_faq/edge_extension.html)。

IDM 负责文件传输与续传，Broadcom 负责账号审核及下载资格。下载完成后，将安装包文件名和保存位置发给我，我会核验签名、版本及可用的官方校验值，再继续 E 盘虚拟机安装。
