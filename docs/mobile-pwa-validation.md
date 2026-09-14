# 默认入口更新

当前默认方案为 [电脑作为服务端](mobile-computer-server.md)：电脑提供 `/mobile/` 和同源 HTTPS API，扫码流程在桌面设置中直接展示。旧 `/phone/` 继续兼容，下面的旧路径示例仍可使用；WebRTC 已收纳为独立实验，不是默认产品路线。

---

# PWA 最小链路验证

## 电脑同源 HTTPS 方案的结论

本文保留电脑自行提供可信 HTTPS 的兼容验证步骤。用户已拒绝 GPT / Sites 托管交付，明确要求项目自带入口。项目现通过 `/mobile/` 提供随包分发的移动端；详见 [项目移动端入口](mobile-static-peer.md)。本次不因交付方式修正重做界面。

移动端改为 PWA 优先。已实现浏览器配对、同 session 访问、离线已读历史、每台电脑独立 VAPID 密钥、通知订阅、电脑手动发送测试通知、Service Worker 展示与接收回传。

**尚未达到 iPhone 真机可用的验收结果。** 当前用户没有测试域名和证书。自动化测试使用临时证书和测试专用的浏览器信任参数，没有修改系统信任库，没有实际联系 Apple / Google 推送服务，也没有证明 iOS 添加到主屏幕、锁屏显示或国内 Android 覆盖。

下面的手工证书配置仅面向电脑同源方案的开发验证，不能作为普通用户的最终安装流程。此前部署的私有测试入口已从产品方案中撤下（远端尚未删除）；没有购买域名、开放路由器端口、安装根证书、部署通信中继或修改用户实际运行配置。

## 技术路径与既有契约

- 同源入口：电脑 `https://实际域名:19705/phone/` 提供 PWA 及手机 API；桌面管理 API 仍只在原来的本机端口。
- 配对链接：一次性邀请放 URL fragment，加载后移除；会话凭据由电脑生成，以 `__Host-`、Secure、HttpOnly、SameSite=Strict Cookie 保存。JavaScript 不获取原始凭据。
- 写请求要求精确匹配配置的 Origin 和自定义请求头；Cookie 请求检查 Host，拒绝跨站请求。没有开启通用 CORS，也没有向手机开放整个桌面 API。
- 会话仍读取原始 SessionStore，发送仍复用电脑 Agent 执行和请求 ID 去重。没有移动、改写或删除原始历史。
- Service Worker 仅缓存手机页面资源。已读取的历史与草稿按电脑 ID 缓存在本机；不缓存 API 响应、不离线自动发送、不靠后台常驻连接接收通知。
- 推送只覆盖用户手动触发的固定测试通知；**定时任务自动推送、通知 outbox / 补投尚未实现**。原有任务通知行为未改变。
- 原生原型保留用于兼容验证；PWA 不依赖它，也不能继承它的 Keychain / Keystore 凭据。

## 有可信地址后的真机步骤

开发验证需要一个手机能访问的稳定地址，以及手机系统信任、名称匹配的证书。DNS 指向电脑并不意味着外网可达；端口、防火墙与网络路径要分别满足。证书私钥留在电脑，不分发共用私钥给所有用户。

在实际配置文件中填写以下字段后重启电脑服务：

```yaml
mobile:
  enabled: true
  host: "0.0.0.0"
  port: 19705
  public_origin: "https://替换为实际域名:19705"
  tls_certfile: "/实际路径/fullchain.pem"
  tls_keyfile: "/实际路径/privkey.pem"
```

这三个新增字段必须同时提供。启动会检查 HTTPS 地址、端口、证书有效期、SAN 名称和证书/私钥匹配，失败时不降级为不可信连接。服务端检查通过不代表手机已信任证书。

1. 手机 Safari 打开该地址，无证书警告；加入主屏幕后从桌面图标打开。
2. 电脑“设置 → 连接手机”生成二维码。浏览器可复制配对信息；Safari 和主屏幕应用可能使用不同存储，不能假定配对自动转移。主屏幕应用缺少信息时需粘贴，过期则重新生成。
3. 手机申请连接，在电脑核对六位数字并批准。
4. 在手机继续原来的会话，确认电脑看到相同消息。
5. 手机“连接 → 验证锁屏通知 → 开启测试通知”，明确授予通知权限。
6. 锁定手机，在电脑对应设备旁点击“发送测试通知”。
7. 必须实际观察锁屏通知，点击后回到同一 PWA；然后分别测试离线、重启、撤销。

Cookie 和站点数据仍受浏览器清理策略影响。清除站点数据或更换 Origin 可能需要重新配对；稳定 Origin 是安装身份的一部分。证书续期保持同一 Origin 和电脑数据目录，不应更换电脑 ID 或 VAPID 密钥。

## 如何解释测试状态

| 状态 | 能说明什么 |
| --- | --- |
| 已保存订阅 | 手机已将订阅交给电脑，尚未证明送达 |
| 推送服务已接受 | 推送接口已接受请求，尚未证明手机收到 |
| 手机已处理并回传 | Service Worker 的显示调用完成，并成功回传电脑；不能证明用户看见锁屏通知 |
| 没有回传 | 可能未送达，也可能通知已到而手机无法连接电脑；不能据此判定推送失败 |
| 订阅失效 | 服务返回 404 / 410，电脑删除失效订阅，需要重新开启 |

测试通知有效期 60 秒，每个设备两次发送至少间隔 30 秒，无自动重试。只允许预先列出的 Apple、FCM、Mozilla 推送端点，拒绝任意 URL 和重定向。载荷采用标准 Web Push 加密，只包含固定测试文案和测试 ID。VAPID 授权按推送服务缓存一小时。

`data/mobile/vapid.pem`、`push.sqlite3` 仅保存在电脑可写数据目录，权限 0600。不要把它们当成构建资源或随意删除。推送结果是独立测试状态，不修改原来的通知记录。

## 自动化证据与边界

同源 PWA 阶段结果（后续直连验证见上方文档）：后端全量 993 项通过；安装后的只读 wheel 中 30 项手机/PWA 检查通过；前端 24 + 8 项通过；PWA 浏览器 2 项、既有浏览器回归 2 项通过。源码归档已排除 Android 构建缓存、APK 和生成资产，wheel 包含 PWA 资源。

```bash
pnpm --dir apps/mobile build
pnpm --dir apps/web build
uv run pytest tests/test_mobile_access.py tests/test_mobile_pwa.py tests/test_hatch_build.py
pnpm --dir apps/mobile test:pwa
```

Python 测试验证 Cookie 配对、审批、重启、撤销、原历史一致性、跨站和 Host 拒绝、推送端点限制、真实 Web Push 加解密、过期订阅清除及 TLS 配置拒绝。

浏览器测试使用实际 `fetch` / Cookie / HTTPS / Service Worker 和离线缓存；仅模拟系统订阅及推送服务，再把已解密载荷通过 Chrome 调试协议送入真实 Worker。它不测试 Apple 推送链路、不测试 iPhone 系统界面。另一个浏览器测试不使用信任参数，确认临时证书会被拒绝。

测试使用临时数据和确定性 Agent，不调用真实模型、不触发用户的真实任务、不运行持久测试服务器。wheel 必须包含同样的 PWA 静态资源，安装目录保持只读。

## 恢复与下一步

关闭电脑“手机连接”会停掉移动入口；会话数据不变。移除浏览器配对会撤销该设备并移除其订阅；已展示的通知和手机已缓存的数据不能远程收回。若要退回原生开发入口，可移除新增的三个 TLS 配置字段并重启；原生客户端需要重新核对恢复后的证书。

下一步的产品问题是：如何让普通用户获得可信、稳定的访问入口，而不自行管理域名/证书。该问题未解决前，不能宣称“一扫码即可跨平台安装、外网直连并可靠接收后台通知”。真机链路通过后再接入定时任务自动推送。

## 已确认的静态入口边界（2026-09-10）

用户已明确允许统一 HTTPS 静态站点。静态站点只分发页面，不存储会话、不运行 Agent。本次新增独立静态构建和 WebRTC DataChannel 直连验证；没有增加公共信令、STUN 或 TURN 服务。用户电脑自备域名/证书的同源方案保留为兼容入口。

| 候选 | 解决的问题 | 仍需解决的问题 |
| --- | --- | --- |
| 用户电脑同时提供 PWA 与 API | 已有同源 Cookie / HTTPS 验证实现；没有公共页面托管 | 普通用户的稳定地址、证书签发和续期；手机能否直达电脑 |
| 产品统一 HTTPS 静态站点，数据直连电脑 | 用户安装入口可以共用，无需每人提供网页域名 | 跨来源连接不能直接沿用当前 Cookie 接口；需要重新验证安全配对、传输、重连和电脑发现 |

第二种候选可研究 WebRTC DataChannel：其 DTLS 传输本身加密，但还需要双向交换连接描述及可靠身份验证。WebRTC 不替产品定义信令方式，不能因为它叫 P2P 就承诺无需连接引导或外网一定直连。本次 WebRTC 实现使用人工交换 offer/answer，尚未增加信令或中继服务。[WebRTC 连接说明](https://webrtc.org/getting-started/peer-connections)、[DataChannel 加密](https://developer.mozilla.org/en-US/docs/Web/API/WebRTC_API/Using_data_channels)。当前实现和验证边界见 [静态 PWA 直连验证](mobile-static-peer.md)。

参考：[WebKit 主屏幕 Web Push](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)、[Apple Web Push 接入](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers)、[W3C Service Worker 安全来源](https://github.com/w3c/ServiceWorker/blob/main/explainer.md)、[RFC 8292](https://www.rfc-editor.org/rfc/rfc8292)、[pywebpush](https://github.com/web-push-libs/pywebpush)。参考机制仅映射到电脑作为发送端、手机作为订阅端，不引入参考项目可能使用的业务云服务器。
