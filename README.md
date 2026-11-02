# 微信 Hook 连接插件

独立的 AstrBot 连接插件：连接现有 PC WeChat Hook，转换群消息、持久化 Inbox，并把 AstrBot 回复保存为草稿。
群 Agent 名单、按群指令和模型工具策略由 Agent 插件管理。

**默认只保存草稿。** 在平台设置中打开“允许真实发送”后，新产生的入站消息回复可以提交给 Hook 发送文本或群 @。
不支持主动/无来源消息、图片、文件、语音、原生引用或 @全体；历史草稿不会自动发送。
任务编排和任务工作台由另一个仓库提供：[微信 Agent 任务插件](https://github.com/abincn/astrbot_plugin_wechat_agents)。
只需要微信连接时无需安装 Agent 插件。

## 安装

在独立 AstrBot 后台的插件管理中，通过仓库地址安装：

```text
https://github.com/abincn/astrbot_plugin_wechat_hook
```

也可在本仓库 **Code → Download ZIP** 下载源码，解压后把插件目录命名为
`astrbot_plugin_wechat_hook`，放入 AstrBot 的 `data/plugins/`。`main.py`、`metadata.yaml`
应直接位于该目录下，不要多嵌套一层文件夹。Python 额外依赖是 `httpx`。

从原合并版本升级时，先退出独立 AstrBot，将旧插件目录移到 `plugins` 目录之外，再放入新目录。
直接覆盖会留下旧 `views/`，造成重复的任务界面。不要删除已有数据库、权限文件或 AstrBot 配置。

## 配置平台

重启独立 AstrBot 后，添加类型为 `wechat_pc_hook` 的消息平台。示例：

后台“启用连接”“允许真实发送”“自动开启 Hook 消息采集”均为布尔控件，不需要手工输入 true/false。
发送和采集默认关闭；以下 JSON 仅用于说明配置格式。

```json
{
  "id": "wechat_pc_hook",
  "type": "wechat_pc_hook",
  "enable": false,
  "hook_url": "http://127.0.0.1:30001",
  "hook_token": "YOUR_HOOK_TOKEN",
  "self_wxid": "YOUR_WXID",
  "inbox_db": "data/wechat_hook_inbox.sqlite3",
  "poll_interval": 2,
  "send_enabled": false,
  "capture_enabled": false
}
```

用实际值替换占位内容后再启用：

- `self_wxid` 是微信内部账号 ID，不是昵称或手机号。
- 不再配置 `allowed_rooms`。旧配置残留的该字段会被忽略；本连接器接收当前账号所有支持的入站群消息，暂不支持私聊。
- `hook_token` 必须与 Hook 服务的访问令牌一致；HTTP 401 表示鉴权被拒绝。
- `hook_url` 目前仅接受本机回环 HTTP 地址，Hook 应与 AstrBot 运行在同一台电脑。
- 默认不更改 Hook 的采集配置或回调地址。先用现有 Hook 控制方式确认采集已启用。
- 为此独立实例使用自己的数据库，不与生产接收器共用游标。

日志中的 `Prepare to send` 只表示 AstrBot 进入回复流程。发送关闭时写入 `draft_only`；
发送开启时，新回复会先落库，再用同一个已鉴权连接提交 `/SendTextMsg` 或 `/SendAtMsg_NoSrc`。

### 开启真实发送

在 **消息平台 → 微信 Hook 连接 → 编辑** 打开“允许真实发送”，保存并完整重启独立实例。
之后在群里触发 AstrBot 正常回复。无需安装 Agent 插件；回复条件仍由 AstrBot 自身配置决定。
若启用了引用/媒体回复，请先关闭这些暂不支持的组件；插件不会把不支持的内容伪装成已发送。

发送使用 Hook 的异步文本任务协议，状态保存在同一数据库：

| outbound 状态 | 含义 |
| --- | --- |
| `draft_only` | 仅草稿；不会被发送开关自动补发 |
| `send_requesting` | 已落库，正在提交请求 |
| `send_queued` | Hook 已受理，有 task_id；尚未确认本地提交 |
| `submitted_unverified` | Hook 报告本地提交完成；不代表对方收到 |
| `send_failed` | Hook 明确拒绝或任务失败；不自动重试 |
| `send_unknown` | 超时、断线、崩溃或结果不明；不自动重试 |

每个回复意图最多提交一次；请求前持久化 request_id 和参数，重复意图不再次 POST。
进程重启只恢复已受理任务的状态查询，不重发未确认请求。`outbound_send_attempts` 保留请求记录。
Hook 的 request_id 去重只有进程内有效，因此插件不会依赖 Hook 重启后的去重来自动重发。
认证失败或结果未知时，先人工核对，再用新的入站消息触发新回复。

## 与 Agent 插件配合

连接插件不导入 Agent、不注册任务页面、不创建任务或启动 Worker。
它仅提供连接、格式转换和消息来源标识，以及供可选 Agent 插件使用的版本化绑定接口：
`get_task_scope()`、`attach_task_runtime()`、`detach_task_runtime()`。
停止平台会停止已绑定的 Worker。平台重载后需完整重启独立实例来重绑。

原平台 ID、Inbox 路径和消息状态继续使用，不做数据库迁移。Agent 的授权配置另见其仓库说明。

Hook 本身不限制哪些群使用模型，不控制唤醒词、人设或工具。只安装连接器时，回复条件和工具行为遵循 AstrBot 原生配置，
并不表示每条消息都会被回复。安装 Agent 后，在 **插件 → 微信 Agent 任务 → 配置** 选择启用群；
只有这些群会应用 Agent 的按群指令、空工具策略和任务授权，名单外仍遵循 AstrBot 原生规则。

### 升级到 v0.3.0

同时更新连接和 Agent 插件，在 Agent 的配置页面填写原来的群 ID 列表，再完整重启独立实例。
不要依赖旧 Hook `allowed_rooms` 或任务文件中的 `rooms` 继续限制接收。
连接器现在会持久化更多群的入站消息；如需限制普通 AstrBot 回复，使用 AstrBot 自身的访问控制/回复配置。
会话标识和已有 Inbox 游标不变，不自动回放过去未接收的消息；升级不自动打开真实发送。
旧文本框保存的精确字符串 "true"/"false" 会规范为布尔值，其他非布尔输入被拒绝。

## 打包下载

**Actions → Build plugin package** 的成功构建提供安装 ZIP。
下载 artifact 后先解压 GitHub 外层包，再解压其中的插件 ZIP 到 `data/plugins/`。

本地构建：

```powershell
python tools/package_plugin.py
```

输出 `dist/astrbot_plugin_wechat_hook.zip`。构建使用明确文件清单，不打包数据、凭据、缓存或 Agent 兼容别名。

## 当前验证范围

v0.3.0 将群策略移到 Agent 插件，并接入可选真实文本发送与布尔控件。仅进行语法检查、静态核对和 ZIP 构建，
没有编写测试代码或运行本地测试，也没有部署到生产或连接真实微信/模型。
真实发送路径尚未实机验证；没有对端收件回执、自动重试、主动推送、私聊发送或多进程恢复。
