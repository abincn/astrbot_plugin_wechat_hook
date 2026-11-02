# 微信 Hook 连接插件

独立的 AstrBot 插件：连接现有 PC WeChat Hook，接收群消息，持久化 Inbox，隔离群会话，并把 AstrBot 回复保存为草稿。

**当前为草稿模式原型，不会真实发送微信。** 即使把 `send_enabled` 改为 `true`，也不会调用微信发送接口。
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

```json
{
  "id": "wechat_pc_hook",
  "type": "wechat_pc_hook",
  "enable": false,
  "hook_url": "http://127.0.0.1:30001",
  "hook_token": "YOUR_HOOK_TOKEN",
  "self_wxid": "YOUR_WXID",
  "allowed_rooms": ["YOUR_ROOM@chatroom"],
  "inbox_db": "data/wechat_hook_inbox.sqlite3",
  "poll_interval": 2,
  "send_enabled": false,
  "capture_enabled": false
}
```

用实际值替换占位内容后再启用：

- `self_wxid` 是微信内部账号 ID，不是昵称或手机号。
- `allowed_rooms` 必须是数组；每项是完整群 ID，结尾为 `@chatroom`，不是群名。
- `hook_token` 必须与 Hook 服务的访问令牌一致；HTTP 401 表示鉴权被拒绝。
- `hook_url` 目前仅接受本机回环 HTTP 地址，Hook 应与 AstrBot 运行在同一台电脑。
- 默认不更改 Hook 的采集配置或回调地址。先用现有 Hook 控制方式确认采集已启用。
- 为此独立实例使用自己的数据库，不与生产接收器共用游标。

日志中的 `Prepare to send` 只表示 AstrBot 进入回复流程。本插件把结果写入 Inbox 数据库的
`outbound` 表，状态为 `draft_only`，不代表微信已经收到回复。

## 与 Agent 插件配合

连接插件不导入 Agent、不注册任务页面、不创建任务或启动 Worker。
它保留按群文档和默认空工具策略，以及一个供可选 Agent 插件使用的版本化绑定接口：
`get_task_scope()`、`attach_task_runtime()`、`detach_task_runtime()`。
停止平台会停止已绑定的 Worker。平台重载后需完整重启独立实例来重绑。

原平台 ID、Inbox 路径和消息状态继续使用，不做数据库迁移。Agent 的授权配置另见其仓库说明。

## 打包下载

**Actions → Build plugin package** 的成功构建提供安装 ZIP。
下载 artifact 后先解压 GitHub 外层包，再解压其中的插件 ZIP 到 `data/plugins/`。

本地构建：

```powershell
python tools/package_plugin.py
```

输出 `dist/astrbot_plugin_wechat_hook.zip`。构建使用明确文件清单，不打包数据、凭据、缓存或 Agent 兼容别名。

## 当前验证范围

迁移自已拆分的插件版本 v0.2.0，保留原业务行为。迁移只进行语法检查、静态核对和 ZIP 构建，
没有编写测试代码或运行本地测试，也没有部署到生产或连接真实微信/模型。
真实发送、处理回执、多进程恢复与实机升级验收仍未实现或未验证。
