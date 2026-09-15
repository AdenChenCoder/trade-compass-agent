# 手机直连：电脑端接入与协议验证

最新方向为 PWA 优先；本文保留原生接入协议。浏览器 Cookie 授权、可信 HTTPS 和手动 Web Push 测试见 [PWA 验证说明](mobile-pwa-validation.md)。尚未通过 iPhone 真机验收。

当前已实现电脑“设置 → 连接手机”页面，以及 `apps/mobile/` 下的 iOS / Android 测试版工程。支持扫码申请、电脑批准、撤销、原会话历史与分页、在同 session 发消息、应用内任务消息。两端通过定期读取原始记录同步，尚未实现 SSE 补放和后台系统推送。原生测试包的编译/真机状态见 `apps/mobile/README.md`。普通手机浏览器不能直接使用原生协议入口。

## 启用

推荐在更新后的电脑 Web UI 中打开“设置 → 连接手机”，点击“开启手机连接”。不需要手改配置或重启，它会立即启动独立 HTTPS 入口，并保存开关供服务下次启动使用。关闭按钮立即关闭入口。修改前的配置保存在旁边的 `.mobile-backup.yaml` 文件中。TLS 依赖已进入基础安装包，原有 mobile extra 仍兼容。

需要自定义地址/端口时，可在当前实际使用的配置文件里设置：

```yaml
mobile:
  enabled: true
  host: "0.0.0.0"
  port: 19705
```

重启原有 `trade-compass serve` / 受系统管理的服务后生效。手机接入口跟随它启动和停止，不另起 scheduler；本机 Web/API 仍保持原来的 loopback 地址和访问方式。不要把原来的 `serve --host` 改为公网监听。`mobile.host` 接受 IP 地址；IPv4/IPv6 分别绑定，不做地址发现、端口映射或防火墙修改。

新入口绑定失败、缺少 mobile 依赖、身份文件损坏或过期都会使启用了 mobile 的服务启动失败，不降级成 HTTP。修改配置或修复依赖后重启；也可以设置 `enabled: false` 恢复只使用桌面。

电脑的 `data_dir/mobile/identity.pem` 保存 TLS 身份，`devices.sqlite3` 保存配对授权。目录权限为 0700，文件为 0600。证书为每台电脑生成，有效期十年；不会自动替换身份导致手机误信另一台电脑。设备凭据在服务端只存 SHA-256 摘要。不要把这些文件提交、分发或当成普通缓存删除。原始会话和通知位置不变。

## 一次完整的开发验证

以下命令在源码环境运行。协议探针是开发工具，手机产品最终应通过 Keychain/Keystore 保存凭据，并实现相同的证书校验；探针使用权限为 0600 的状态文件。

1. 在电脑创建邀请。把示例 IP 替换为测试客户端实际可达的电脑地址；输出文件必须不存在。邀请有效五分钟，新邀请替代上一个尚未使用的邀请。

```bash
uv run --extra mobile python scripts/mobile_probe.py invite \
  --endpoint https://192.168.1.10:19705 --out /tmp/compass-invitation.json
```

2. 仅通过可信方式把邀请文件交给目标客户端。客户端先验证该邀请带来的 SHA-256 证书指纹，再在已验证的同一条 TLS 连接上发送申请。当前可在第二台有 Python 和本项目的设备上运行探针；同一电脑运行也能验证协议，但不能证明手机网络可达。

```bash
uv run --extra mobile python scripts/mobile_probe.py pair \
  --invitation /tmp/compass-invitation.json \
  --state /tmp/compass-device.json --name "我的测试设备"
```

客户端生成随机设备凭据，在发送申请前保存，以便响应丢失后用 `pairing/status` 恢复。申请返回设备 ID 和六位核对码。申请只消耗一次邀请；待批准设备不能读取历史。不要仅凭设备自行填写的名称批准。

3. 在电脑查看待批准设备，对照客户端显示的六位码，然后明确批准。

```bash
uv run python scripts/mobile_probe.py devices
uv run python scripts/mobile_probe.py approve --device-id DEVICE_ID --code SIX_DIGITS
```

待批准申请也有五分钟有效期。批准后保持授权，直到电脑端撤销；不按原来的配对到期时间失效。

4. 用同一个客户端状态读取电脑的原有记录。

```bash
uv run python scripts/mobile_probe.py read --state /tmp/compass-device.json
uv run python scripts/mobile_probe.py read --state /tmp/compass-device.json --session-id SESSION_ID
uv run python scripts/mobile_probe.py read --state /tmp/compass-device.json --session-id SESSION_ID --before 50
uv run python scripts/mobile_probe.py read --state /tmp/compass-device.json --resource notifications
uv run python scripts/mobile_probe.py read --state /tmp/compass-device.json --resource pairing/status
```

记录直接来自 `agent_sessions/` 和 `notifications.jsonl`，不存在移动端专用 session 副本。消息分页字段与桌面一致；只读请求不创建缺失会话、不迁移历史。任务消息只包含按现有通知开关、渠道与保留策略已经写入应用内通知的记录；当前读取不会补造未投递的任务结果。

5. 重启电脑服务，确认凭据继续有效。随后在电脑撤销，再次读取应返回 401。

```bash
uv run python scripts/mobile_probe.py revoke --device-id DEVICE_ID
```

撤销对后续请求生效，包括复用已建立的 HTTPS 连接；已完成响应和客户端已经缓存的数据不能远程收回。当前没有开放长连接事件流，后续接入 SSE 时还须实现撤销关闭订阅。

## 已实现的接口

本机管理接口在原有端口，继续受 Host/Origin 限制，并额外检查请求的实际来源为 loopback：

| 接口 | 行为 |
| --- | --- |
| `GET /api/mobile/status` | 接入口状态、电脑 ID 和证书指纹 |
| `POST /api/mobile/access` | 开启或关闭入口，并保存下次启动状态 |
| `POST /api/mobile/pairing/invitations` | 创建一次性邀请 |
| `GET /api/mobile/devices` | 待批准和已配对设备 |
| `POST /api/mobile/devices/{id}/approve` | 核对码匹配后批准 |
| `DELETE /api/mobile/devices/{id}` | 撤销设备 |

独立 HTTPS 端口仅开放以下接口。除申请接口外都需要 `Authorization: Bearer <device_secret>`；状态查询允许尚未批准的设备查询自身状态，其余只允许已批准设备。批准包含读取历史与发起 Agent 对话；电脑 UI 会明确展示这一授权范围。

| 接口 | 行为 |
| --- | --- |
| `POST /mobile/v1/pairing/claim` | 用邀请、设备名称、客户端随机凭据提出申请 |
| `GET /mobile/v1/pairing/status` | 查询当前设备的配对状态 |
| `GET /mobile/v1/info` | 协议版本、电脑 ID、当前能力 |
| `GET /mobile/v1/sessions?limit=20` | 与桌面一致的最近会话列表，最多 100 条 |
| `GET /mobile/v1/sessions/{id}/messages?limit=50&before=100` | 分页读取同一会话，最多 100 条/页 |
| `GET /mobile/v1/notifications?limit=30` | 与桌面一致的应用内消息，最多 500 条 |
| `POST /mobile/v1/turns` | 提交 `request_id`、既有 `session_id` 和消息，持久化接收回执后返回 202 |
| `GET /mobile/v1/turns/{request_id}` | 仅查询当前设备自己的请求状态 |

凭据不能放进 URL。默认不提供 CORS，也不接受浏览器 Origin；当前为待原生桥接的协议，而非 PWA 接入实现。请求体上限 64 KiB；错误响应不回显凭据。访问日志关闭，避免记录带秘密的误用 URL。业务接口不返回 API 密钥、配置、规则编辑、文件管理等本机管理能力。

当前列表仍是桌面的“最近 N 条”接口，历史分页仍使用原有位置游标。没有宣称全量 session 目录同步、稳定消息 ID、实时事件补放或断点同步；这些属于阶段 B。

## 后续验收

本阶段的自动化检查使用临时数据目录和真实本机 TLS socket。它能证明协议和服务生命周期，不能替代 iPhone、带 GMS Android、无 GMS Android 真机验收。

手机会话发送以持久化请求 ID 去重；网络失败不会自动重发。服务重启后未完成请求标记为 unknown，要求查看历史及实际执行结果，不能自动重复工具操作。电脑端原子登记保证两端同时发送时只接受一轮；切换页面不再隐式停止 Agent，显式停止操作保留。

原生层在 iOS 使用 URLSession + 证书固定 + Keychain，在 Android 使用 HTTPS 证书固定 + Android Keystore 加密凭据。原生生命周期、相机、局域网权限仍需真机验收。系统推送需沿方案分别验证原生发送授权与 Web Push。当前 `info.system_push` 为 `not_configured`，不会分发全局 APNs/FCM/OEM 私钥或引入产品云中心。仍未解决 Mac 睡眠执行、任意 NAT 穿透、PWA 可信 HTTPS 或各厂商后台推送覆盖。

回退只需关闭 `mobile.enabled` 并重启，不需要回滚会话或通知数据；身份/设备元数据保留可继续使用。若电脑身份泄露，先关闭入口并撤销授权，再更换身份、重新配对；不要把一次普通重启设计为身份重置。
