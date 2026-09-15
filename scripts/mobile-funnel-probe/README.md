# Funnel 连接组件与隔离验证工具

本目录同时用于构建 Python wheel 内置的连接组件和开发验收工具；当前实现尚未发布。普通用户通过项目“设置 → 连接手机”操作，见 [手机连接](../../docs/mobile-managed-connection.md)。
独立运行默认只准备状态，显式 `--connect` 后默认只提供固定测试文字。显式传入 `--mobile-bridge-config` 才进入 PWA 接入模式；该模式只转发移动端路径。
用户不需要安装 Tailscale 客户端；账号登录和 Funnel / HTTPS 授权仍由用户在供应商页面完成。

## 构建与检查

当前验证平台为 macOS；状态锁实现也支持 Linux。Windows 尚未实现状态锁，会明确拒绝运行。
固定使用 Go 1.27.1 和 `tailscale.com v1.102.4`；模块校验由 `go.sum` 和默认 Go checksum database 完成。
在本目录运行：

```sh
go test -race ./...
go build -trimpath -o /absolute/private/path/compass-funnel-probe .
```

只准备状态，不联网：

```sh
/absolute/private/path/compass-funnel-probe --state-dir /absolute/private/path/probe-state
```

连接验证时，由开发者使用项目的进程管理器管理以下命令：

```sh
/absolute/private/path/compass-funnel-probe --state-dir /absolute/private/path/probe-state --connect
```

独立验收的状态目录必须放在仓库、已安装包和正式业务数据目录之外。项目托管模式则使用配置的 `data_dir/mobile/funnel/`；两种方式均使用专用私有可写目录。
不能启动两个实例共享状态；操作系统锁在退出或崩溃时释放，身份文件保留。
不会启动系统级 Tailscale、修改系统 DNS、安装根证书、设置出口节点或添加路由。
关闭自动端口映射和 Tailscale 诊断日志上传；这不消除供应商正常运行所需的设备、账号与连接元数据。

## 授权与检查

- `status.json` 为私有文件，`action_url` 可能包含登录授权链接；不得上传、提交或放到公共页面。
- `diagnostics` 字段只包含本进程的控制状态、固定错误类别和有界事件；不保存原始日志或凭据。它不证明公网可用，也不会触发自动重启。见[诊断边界与验收方法](../../docs/mobile-funnel-diagnostics.md)。
- 首次启动会进入 `needs_login_or_network`。如果有 `action_url`，在本机浏览器打开并登录。
- `needs_funnel_https_permission` 表示仍缺公网访问或 HTTPS 权限。组件通过官方 `QueryFeature("funnel")` 获取授权入口，不自行编辑 tailnet 策略。
- `preparing_certificate` 表示正在准备公开域名证书；45 秒内未取得有效证书 / 私钥对则停止，不接受浏览器连接。
- 如果供应商没有返回可用的操作链接，应记录失败，不要求普通用户改策略或执行命令来掩盖流程缺口。
- `listening_public_access_unverified` 仅表示监听成功。使用手机蜂窝网络打开 `origin`，核对固定测试文字和浏览器证书，才能记录外部可达。
- `/` 和 `/healthz` 是唯一成功响应路径，只允许真实 TLS 的 GET / HEAD；Host 必须匹配本节点域名。
- 默认测试模式不提供会话；两种模式都不提供桌面管理、任意文件、非本机 HTTPS 上游、CORS 放开或证书跳过验证选项。
- 状态或权限异常时关闭监听；检测到公开来源变化时停止并要求调查，避免静默替换 PWA 安装地址。

`identity.json` 保存随机节点名及首次公开来源；`tsnet/` 包含节点身份及证书状态。
正常重启保留整个目录，不使用 ephemeral 节点。日志只输出阶段名称，不输出账号、授权 URL 或请求内容。
停止进程会关闭监听与连接；供应商账号中仍可能保留已注册设备和已批准的 tailnet 权限。
删除供应商中的测试设备属于测试结束的清理操作，不能通过删除本地目录假装已经撤销远端身份。

## 边界

当前开发流程已完成一台手机的公网 PWA 连接、真实会话和任务通知跳转；72 小时稳定性及国内外多平台覆盖仍未验收。
HTTP handler 和状态文件的测试不等同于真实 Funnel 中继验证。
当前已集成界面与进程生命周期，发布前仍须完成 [安全审查与验收记录](../../docs/mobile-managed-access-review.md) 中剩余的验收。

## 显式 PWA 接入验证

完成固定文字与手机蜂窝网络测试后，开发者可传入绝对路径的私有 JSON 配置：

```json
{"origin":"https://your-node.your-tailnet.ts.net","upstream":"https://127.0.0.1:19705"}
```

`origin` 必须与节点实际来源完全一致。`upstream` 只能是带明确端口的 loopback IP HTTPS 地址，不接受主机名、路径、凭据、查询参数。
配置文件权限为 0600，不能放入仓库或公开资产目录。此模式仍是开发验证，不是普通用户需要填写的产品设置。

电脑的专用移动端监听器配置 `mobile.tls_relay: true`、loopback 地址以及同一公开域名的证书 / 私钥。
只有此显式模式允许本机端口与公开 443 不同；证书域名、有效期、私钥匹配和实际 HTTPS 检查仍然生效。
Go 转发对本机的 TLS 连接使用系统 CA 和公开域名校验，不读取环境 HTTP 代理，不信任客户端的转发头。
公网仅允许 `/mobile`、`/mobile/…`、`/phone`、`/phone/…`；根地址跳转到 `/mobile/`。
请求体上限 64 KiB，只允许 GET / HEAD / POST；失败响应不包含上游诊断，不自动重放请求。
监听状态为 `listening_mobile_access_unverified`；它不能代替真实配对、历史一致性和撤销验收。

上述独立桥接命令仅用于开发验收。项目托管模式由 Python 主程序创建子进程，自动传入每次启动唯一的 `--instance`、`--wait-for-mobile` 和 `--parent-stdin`：先准备证书并发布 `waiting_for_mobile`，再由 Python 启动本机 HTTPS 手机监听器并写入本次桥接配置。父进程管道关闭即取消连接；旧进程的状态不能使新一轮启动误报就绪。普通用户不需要准备桥接 JSON 或管理子进程。

在仓库根目录执行 `python scripts/build_mobile_helper.py` 构建四平台压缩组件、完整性清单和依赖许可证；`--version` 只报告组件名与协议版本，不联网或初始化节点。Python 在可写数据目录释放并校验组件，不修改已安装包。

Python 的显式 `tls_relay` 模式每 30 秒检查证书更新，通过私有快照校验后切换后续握手，保持原电脑身份、授权和会话。
不完整或无效的新材料不会替换有效证书；旧证书到期后拒绝新握手，直至有效材料恢复。独立 CA 的实际 HTTPS 轮换测试已通过，供应商真实续期仍未实测，不能据此作为长期正式交付。
停止移动端监听器后转发只能失败，不能退回桌面端口或降低证书验证。
