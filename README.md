# 7risha 自动签到

> 基于 GitHub Actions 的 [www.7risha.com](https://www.7risha.com) 每日任务奖励自动领取工具，支持多账号与多通道推送（PushDeer / Server酱）。

## 原理

站点是 WordPress + LightSNS 主题，签到与任务奖励都是普通 AJAX 接口，脚本按顺序执行三个动作：

| 顺序 | 动作 | 接口 | 表单 |
|---|---|---|---|
| 1 | 每日签到 | `POST /wp-content/themes/LightSNS/module/action/sign.php` | `sign=1&ticket=&randstr=` |
| 2 | 任务一 | `POST /wp-content/themes/LightSNS/module/action/task.php` | `task_id=task007&type=day` |
| 3 | 任务二 | 同上 | `task_id=task009&type=day` |

```json
{"code":1,"msg":"签到成功","sign_c":45,"text":"今日已签到"}
{"code":1,"msg":"奖励领取成功！","task":35}
```

> 这两个字段都不参与展示：`task` **不是**奖励额度（奖励是固定的），`sign_c` 是累计签到天数（只写日志）。脚本只取 `code` 与 `msg` 做判定。

脚本做的事就是：**带上你浏览器里的登录 Cookie，按「签到 → 007 → 009」的顺序各 POST 一次，每次间隔 5 秒**。没有模拟点击，没有浏览器自动化，纯 HTTP 请求。

动作顺序、任务 ID、间隔都是代码里的常量（`Config.SIGN_ENABLED` / `Config.TASKS` / `Config.ACTION_INTERVAL`），不需要也没法用环境变量覆盖。

登录态来自 WordPress 的 `wordpress_logged_in_<hash>` cookie——它是 `用户名|过期时间|token|HMAC` 的 URL 编码形式，服务端验签通过即视为已登录。所以**这条 Cookie 等价于账号密码**。

## 与原 GLaDOS 版的差异

| 维度 | GLaDOS 版 | 本项目 |
|---|---|---|
| 登录态 | `koa:sess` + 签名 | `wordpress_logged_in_<hash>` |
| 签到动作 | 单个 `/api/user/checkin` | 多个 `task_id` 逐个领取 |
| 站点 | 双域名 `glados.cloud` / `railgun.info` | 单站点 `www.7risha.com` |
| 成功返回码 | `code == 0` | `code == 1` |
| 附加请求头 | 设备 UA 需匹配 | 需 `X-Requested-With` + `Origin`/`Referer` |
| 积分兑换 | 支持 `plan100/200/500` | 无此概念，已移除 |
| 设备自适应 | `device-mismatch` 换 UA 重试 | 无此机制，已移除 |

## 文件结构

```shell
│  checkin.py          # 签到主脚本
│  logging_config.py   # 日志配置（北京时间、[I/W/E] 短标签）
│  requirements.txt    # Python 依赖
│  .gitignore
│
└─.github
   └─workflows
           rishaCheck.yml   # Actions 配置文件
```

## 食用方式

### 1. Fork 本仓库

### 2. 配置 Secrets

跳转至自己仓库的 `Settings` → `Secrets and variables` → `Actions`，添加 `repository secret`：

#### 2.1 COOKIES（必填）

1. 浏览器登录 <https://www.7risha.com>
2. 按 `F12` → `Network` 面板 → 刷新页面
3. 点任意一条 `www.7risha.com` 的请求 → `Request Headers` → 复制 `Cookie` 整行

> **必须包含 `wordpress_logged_in_` 开头的字段**，否则脚本会在启动阶段直接判定登录态无效并跳过。
>
> 其它字段（`X_CACHE_KEY`、`invite`、`history-search`、`server_name_session` 等）带上无妨，脚本会原样透传。

- 多账号用 `&` 连接，例如：`cookie1&cookie2&cookie3`

#### 2.2 VERBOSE（非必填）

设为 `true` 时输出每次请求的详细结果，排查问题时使用。

#### 2.3 推送（非必填）

**任一后端配置后即会推送，支持同时配置多个（都发）**：

- **PushDeer**：`PUSHDEER_SENDKEY`
- **Server酱**：`SERVERCHAN_SENDKEY`

### 3. 启用 Actions

仓库 `Actions` 标签页 → 左侧 `risha checkin` → `Enable workflow`，可先手动 `Run workflow` 验证一次。

定时为**北京时间 00:17 与 08:17**：零点后立刻领一次，早上再兜底一次，同时避开 GitHub 整点排队高峰。

## 环境变量一览

只保留 4 个变量，且都**不带项目前缀**：

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `COOKIES` | ✅ | — | 登录 Cookie，多账号用 `&` 分隔 |
| `VERBOSE` | | `false` | 详细日志 |
| `PUSHDEER_SENDKEY` | | — | PushDeer 推送密钥 |
| `SERVERCHAN_SENDKEY` | | — | Server酱 SendKey |

任务 ID、`type=day`、5 秒间隔都是代码里的常量（`Config.TASKS` / `Config.TASK_TYPE` / `Config.ACTION_INTERVAL`），不需要也没法用环境变量覆盖。

## 状态与文案

脚本内部状态与站点返回码解耦，避免被远端码值绑架。推送里只输出精简后的短语：

| 站点返回 | 内部状态 | 推送显示 |
|---|---|---|
| `code == 1` | 签到成功 / 领取成功 | `✅ 签到成功`、`✅ 领取成功  task007` |
| `msg` 含"已签到" | 已签到 | `🔄 已签到` |
| `msg` 含"已领取/已完成/重复/领取过" | 重复领取 | `🔄 重复领取  task010  · 已领取` |
| `msg` 含"未完成/没有完成" | 领取失败 | `🔴 领取失败  task009  · 任务未完成` |
| `msg` 含"登录/登陆/无权限/未授权" | 登录失效 | `🔴 登录失效`，并**中断该账号剩余动作** |
| `msg` 含"非法请求/非法操作/请求异常" | 请求被拒 | `🔴 请求被拒`，同上中断 |

> 站点原句（如"该任务你还没有完成！"）只写进控制台日志，不进推送，避免手机上刷屏。

## 日志示例

```
2026-10-20 00:17:03 - [I] ℹ️ 站点: www.7risha.com
2026-10-20 00:17:03 - [I] ℹ️ 共加载了 1 个 Cookie。
2026-10-20 00:17:03 - [I] ℹ️ 动作顺序: 签到, task007, task009，间隔 5 秒
2026-10-20 00:17:04 - [I] 🚀 ========== 开始处理账号 1 ==========
2026-10-20 00:17:05 - [I] 🍪[1] 🎯[签到] ✅ 签到成功
2026-10-20 00:17:05 - [I] 🍪[1] 🎯[签到] ⏳ 间隔 5 秒后继续
2026-10-20 00:17:11 - [I] 🍪[1] 🎯[task007] ✅ 领取成功
2026-10-20 00:17:11 - [I] 🍪[1] 🎯[task007] ⏳ 间隔 5 秒后继续
2026-10-20 00:17:17 - [I] 🍪[1] 🎯[task009] 🔴 领取失败 · 任务未完成
2026-10-20 00:17:17 - [I] 🏁 ========== 签到总结 ==========
7risha 签到 · 成功 2 · 重复 0 · 失败 1
#1  ✅  签到成功
#2  ✅  领取成功  task007
#3  🔴  领取失败  task009  · 任务未完成
```

## 本地调试

不想等定时任务，可以本地跑一次（先 `pip install -r requirements.txt`）：

```shell
# Linux / macOS
COOKIES='粘贴你的 Cookie' SERVERCHAN_SENDKEY='SCT...' python checkin.py

# Windows PowerShell
$env:COOKIES='粘贴你的 Cookie'; $env:SERVERCHAN_SENDKEY='SCT...'; python checkin.py
```

加 `VERBOSE=true` 可以看到每次请求的原始响应，排查接口字段变化时很有用。

## 常见问题

**Q：日志提示「Cookie 中缺少 wordpress_logged_in_*」**
复制 Cookie 时漏了登录字段，或浏览器已登出。重新登录后按 2.1 重抓。

**Q：提示「登录态无效」**
Cookie 过期了。WordPress 登录态有有效期，重新登录刷新后更新 `COOKIES`。

**Q：提示「请求被拒 · 非法请求！(1700001)」**
这是站点对无效会话的返回。两种可能：Cookie 已过期，或者复制时漏了 `wordpress_logged_in_*` / `server_name_session` 等字段。请回 2.1 完整复制一次。

**Q：某个项目返回「领取失败 · 任务未完成」**
说明该项目有前置条件（比如需要发帖/评论），脚本只能领奖励、不能替你完成任务。两个签到项目是写死在 `Config.TASKS` 里的，如果某个项目长期不可领，把对应的 `task_id` 从该列表删掉即可。

**Q：为什么领取的 POST 不做自动重试？**
自动重发一个"发放奖励"的请求存在重复请求风险。网络抖动导致的失败由早上 08:17 那次定时任务兜底，比冒险重发更稳。

**Q：日志出现「接口返回 30001（服务端记账异常），消息应已送达，不重发」**
`30001` 是 Server酱 写入 `wxstatus` 字段时的数据库错误。**它发生在消息推送之后**——消息能正常收到，只是接口回了错误码。所以脚本把它按"已送达"处理并明确**不重发**，否则你会收到两条一模一样的推送。

> 早期版本曾尝试用 `channel=9` 兜底重发，实测该渠道返回 `SUCCESS` 却并不投递，属于假成功，已移除。

**Q：会不会被判定为异常请求？**
脚本复用了完整的浏览器请求头（`Origin`/`Referer`/`X-Requested-With`/真实 UA），频率为每天 2 次、每次 2 个请求且间隔 5 秒，与正常用户手动领取一致。但任何自动化都存在风险，请自行评估。

## 声明

- 本项目仅供学习 Python 网络请求与 GitHub Actions 自动化，请勿用于商业用途。
- Cookie 泄露等于账号被完全接管，**建议使用私有仓库**。
- 站点接口与页面结构可能随时变更，本项目不保证持续可用。
