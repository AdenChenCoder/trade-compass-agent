# 交易罗盘移动端

移动端是随源码和 Python wheel 分发的 PWA。会话、Agent 执行和任务结果保存在用户电脑，手机可以创建会话、继续已有对话并接收任务提醒。

## 连接手机

在电脑端进入 **设置 → 连接手机**，点击 **开启手机连接**。首次使用按页面引导完成 Tailscale 账号登录及授权，再生成配对二维码。手机扫码后输入电脑显示的六位配对码，即可进入会话；也可以添加到手机主屏幕使用。

完整操作和状态说明见 [手机连接指南](../../docs/mobile-managed-connection.md)。默认入口为 `/mobile/`，兼容旧 `/phone/` 路径。正常使用不需要单独安装手机 APK、配置路由器或填写域名和证书。

## 功能

- 始终可以新建会话；电脑和手机读取同一份历史，使用相同的 Agent 配置。
- 分页读取会话和消息，支持 Markdown、任务卡片及完整结果详情。
- 保存每段会话的草稿与已读取内容；恢复联网后补取历史。
- 发送请求具有持久请求标识，响应中断后查询执行状态，不自动重放工具操作。
- 手机通知和任务提醒分别开启；通知只包含通用提醒，点击后读取原始结果。
- 全新打开页面时检查并加载完整的新版本；使用中检测到更新时，点击 **确定** 后更新并保留草稿。
- 电脑可以撤销手机授权；关闭手机入口不会删除会话、配对记录或通知偏好。

## 开发与检查

在仓库根目录运行：

```sh
uv sync --extra dev
pnpm install --frozen-lockfile
pnpm --dir apps/web build
pnpm --dir apps/mobile build
python scripts/build_mobile_helper.py
```

连接组件的构建工具链和包内校验说明见 [手机连接指南](../../docs/mobile-managed-connection.md#开发分发与状态位置)。安装已有 wheel 后，运行时不需要 Go。

```sh
pnpm --dir apps/mobile test
pnpm --dir apps/mobile exec tsc --noEmit
pnpm --dir apps/mobile test:pwa
```

浏览器测试使用临时数据、真实 HTTPS 和确定性 Agent，不调用实际模型或操作用户会话。默认使用本机 Google Chrome，可通过 `CHROME_PATH` 指定测试浏览器。测试自动创建并关闭服务。

## 可选开发原型

`build:peer`、`test:peer` 和 `src/peer.ts` 保留 WebRTC 实验路径，输出到 `dist-peer/`。它不参与默认的 PWA 交付，详见 [静态 PWA 实验](../../docs/mobile-static-peer.md)。

`android/`、`ios/` 和 Capacitor 配置是早期原生原型，使用开发包 ID `com.tradecompass.mobile.dev`。需要这些原型时，可运行 `pnpm --dir apps/mobile sync`，再通过 `android` 或 `ios` 命令打开对应工程。默认 PWA 使用浏览器的同源 HTTPS 与 HttpOnly Cookie，不使用原生凭据桥接。

## 恢复与数据

- 电脑需要运行并联网才能执行任务和提供新结果；离线页面保留已读取的内容。
- 手机移除连接不会删除电脑上的记录；遗失手机时应在电脑撤销设备。
- 连接状态保存在配置的数据目录，包内资源保持只读。不要通过删除状态目录排查问题，以免丢失原节点身份。
- 配对、接口安全、开发证书和手动接入工具见 [接入开发说明](../../docs/mobile-access-development.md)；任务投递规则见 [自动任务提醒](../../docs/mobile-task-push.md)。
