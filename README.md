# 微信 Hook 连接插件

独立的 AstrBot 连接插件：连接现有 PC WeChat Hook，转换群聊及私聊消息、持久化 Inbox，并把 AstrBot 回复保存为草稿。
群 Agent 名单、按群指令和模型工具策略由 Agent 插件管理。

图片、视频和入站引用的能力边界见 [AstrBot / Hook / 连接插件对比表](docs/MESSAGE_CAPABILITIES.md)。

**默认只保存草稿。** 在平台设置中打开“允许真实发送”后，新产生的入站消息回复可以提交给 Hook 发送文本或群 @。
不支持主动/无来源消息、图片、视频、文件、语音、原生引用或 @全体的发送；历史草稿不会自动发送。
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
- 不再配置 `allowed_rooms`。旧配置残留的该字段会被忽略；本连接器接收当前账号的入站群聊和私聊。
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

### 图片、视频与引用消息接收（v0.3.1）

支持下载并校验图片（12 MiB）和视频（60 MiB），分别转换成 AstrBot `Image` / `Video`。
引用文字转换为 `Reply`；引用图片/视频使用原引用源的 ID 和发送者下载，放入引用链。
缺少有效引用 ID 时保留明确标记的文字回退，不伪造原消息 ID。

文件校验包括大小、SHA-256 与图片/视频格式；支持 JPEG/PNG/GIF/WebP 及 MP4/WebM。

### 语音、文件、卡片、表情与合并转发接收（v0.4.0）

- **语音**：按本次消息的原生 msgsvrid 从 `/DownloadVoice` 读取本地已下载的 SILK，
  检查返回 ID、长度、头部和帧边界，最多 4 MiB / 6000 帧，转换成 `Record`。
  复用 AstrBot 的 SILK 解码与语音处理，不单独调用模型或 `/Voice2Text`。
  语音能否转写/理解取决于 AstrBot 的 SILK 依赖、ASR 或音频模型配置；Hook 无本地数据时仅提示附件未取得。
- **文件**：复用现有媒体任务，最多 24 MiB（含空文件），核对大小、SHA-256 后输出 `File`。
  原名称仅用于显示；本地使用内容哈希命名，常见文档保留扩展名，其他格式存为 `.bin`，不打开或执行文件。
  `File` 表示附件已收到；正文分析使用 AstrBot 已有文件提取配置，不保证所有文件可直接被模型读取。
- **链接／小程序**：把 Hook 已解析的标题、描述、链接和小程序标识/路径交给 AstrBot；
  链接提供 `Share`，小程序提供 `Json`，同时保留文字摘要供模型使用。不抓取网页、不运行小程序。
- **表情**：仅处理原消息 XML 中带合法 MD5 的 `qpic.cn` CDN 地址，通过独立无令牌客户端访问 HTTPS，
  不跟随重定向；校验 MD5、大小（12 MiB）、图片头部后输出 `Image`。
  无 URL/MD5、其他域名、加密资源或下载失败时输出明确的表情附件未取得提示，不把微信表情 ID 冒充 QQ `Face` ID。
- **合并转发**：解析 type 49/subtype 19 消息自身携带的 `recorditem`，输出 `Nodes` 和文字摘要。
  最多 40 个原始节点（另加截断提示）、16000 字正文、单节点 2000 字；支持两层内嵌嵌套转发的文字展开。
  不请求远程节点或下载节点内附件，缺少内嵌原文时明确标记未展开。
  不完整/不支持的应用消息只输出明确提示，不把原始 XML 当作普通聊天文本交给模型。
- **名片**：保留微信 username、昵称、别名，输出文字与 `Json`。不使用尚为 TODO 且 ID 是整数的 `Contact` 冒充微信名片。
- **位置**：校验经纬度及取值范围，输出 `Location` 与地址/坐标文字；不访问地图、不执行导航。
- **@、系统及特殊消息**：映射显式 mentions 为 `At`，`notify@all` 为 `AtAll`；不根据文本猜测。
  文件传输通知保留阶段、状态、关联消息 ID。系统通知、撤回及其他应用/特殊消息输出最多 24 个叶子字段
  （单字段 500 字）和 `Json`，提供原始 `raw_message` 给其他插件；不自动撤回本地历史、批准好友或执行卡片动作。

XML 解析限制为每层 96 KB、1200 个元素、24 层深度，拒绝 DTD/实体声明。
新接入类型的附件下载失败时仍保留可用元数据，并明确说明内容未取得；
原生文件任务结果未知时不会自动重建任务，旧历史消息也不会自动回放。
本次仅调整连接插件；独立安装即可接收以上组件，不要求安装群 Agent。
失败或结果不确定的媒体不会交给模型，也不会自动重新创建下载任务。
升级后请发送新消息观察 `Hook image/video/file/voice/emoji ready` 日志；已有 held/unknown/failed 记录不自动回放。

接收到 Video 不等于模型看到了画面。v0.5.0 起，连接插件的通用媒体模块负责本地视频抽帧，
不再要求安装 Agent 或配置群名单；使用方法见下文。
资源与专用操作的剩余限制详见对比表。

### 私聊接入（v0.4.0）

明确的入站私聊转换为 `FRIEND_MESSAGE`，复用上述文字、媒体、引用、卡片等转换。
私聊会话为 `账号|dm|对方ID`；群会话仍为原来的 `账号|群ID`，历史群会话不变。
数据库的既有 `room` 列对私聊保存对方 ID，不需要迁移表结构。私聊不创建群 Agent 工作区，也不使用群名单。
私聊的回复规则、唤醒和访问权限由 AstrBot 控制；“允许真实发送”开启时可按原入站来源回复私聊文本，
目标必须与来源账号和会话一致，不支持私聊群 @。关闭发送时仍只存草稿，不主动推送、不回放历史草稿。

### 媒体下载修复与排查（v0.4.1）

修正 v0.4.0 把群成员 ID 当成媒体 `source_talker` 的错误。Hook 按会话定位消息表：
普通群媒体使用群 ID，私聊使用对方 ID；引用媒体使用有效的原会话 ID，缺少原会话信息时使用当前会话，
不使用引用发送者 ID 代替会话。原消息 ID 仍使用引用源的 msgsvrid。

插件下载日志接入 AstrBot 的插件日志，记录 received、task accepted、ready，以及失败的任务 ID 和 Hook 错误码。
在后台日志中查看“微信 Hook 连接”插件日志（如有过滤，请包含插件 INFO/WARN）。
成功下载应出现 `Hook image ready`、`Hook video ready` 或 `Hook file ready`，并有对应媒体组件。
`[文件传输通知]` 只表示通知到达；`[附件未取得]` 表示原文件不可用，并不表示收到文件内容。

新收到的图片/视频下载失败或结果未知时也会交给 AstrBot 明确的缺附件提示和原因，
不再一直隐藏在 held_media。引用失败仍保留本次回复正文和引用提示，不能据此猜测原画面。
常见原因包括 `source_not_found`（来源消息未找到）、`source_lookup_incomplete`（消息表定位不完整）、
`resource_missing`（原资源缺失）以及 `hook_http_401`（鉴权失败）；实际原因以新日志为准。

升级后完整重启独立 AstrBot，发送新的普通/引用图片、视频或文件观察日志。
不需要清空数据库；升级不会自动重投旧失败消息或重新创建结果未知的下载任务。

### 通用视频分析（v0.5.0）

在 **消息平台 → 微信 Hook 连接 → 编辑 → 视频抽帧分析** 使用布尔开关，默认开启。
同一处理链覆盖普通群聊、私聊、直接视频和引用视频，不依赖 Agent、Worker 或群名单。
它只补充 AstrBot 已经发起的模型请求，不绕过唤醒/访问控制，也不单独调用模型。

需要 AstrBot 运行环境的 PATH 可找到 `ffmpeg`，并配置支持图片输入的视觉模型。
每次请求最多处理首个视频，复用旧实现提取约 0、3、9 秒的最多三帧；宽不超过 960 像素，
每帧不超过 1.5 MB、单次解码不超过 12 秒。输入必须来自本连接器已验证的本地媒体目录。
抽帧缓存位于 Inbox 同级的 `media/video_frames/<视频内容哈希>/`，不放入群 Agent 工作区。
帧路径追加到当前请求的 `image_urls`，不改变人设、工具或回复策略；只依据可见画面，不分析音轨。

成功时日志出现 `Hook video frames attached: ... frames=...`；缺少 ffmpeg 或解码失败时出现
`Hook video frames unavailable: ... reason=...`，并告知模型画面尚不可见。
`Hook video ready` 仅表示原视频已取得，不能代替抽帧成功日志；模型仍须支持视觉输入。

如果安装了 Agent，须一并更新至 v0.3.2，避免旧版重复抽帧。旧 Agent 中的抽帧配置不再生效，
需要关闭分析时使用连接平台的开关。未安装 Agent 的用户仅更新连接器即可。
无需清空数据库或修改发送开关，升级后完整重启 AstrBot，用新的视频消息观察结果。

成功接收的媒体只保留实际媒体组件，不额外插入同名 Plain 占位，避免日志出现 `[图片] [图片]`。
模型请求用的 `message_str` 摘要仍保留；下载失败时仍输出缺附件文字。日志中的昵称/账号双字段由 AstrBot 格式化。
若入站消息没有昵称，使用 Hook 已有的 `/BatchGetContactBriefInfo` 获取联系人 `NickName`，
并从群 `/GetContact` 的成员信息补充群显示名；最多等待 3 秒，缓存最多 512 项，命中缓存不重复查询。
显示优先使用已取得的群显示名，再用入站昵称或联系人昵称；查询失败/无昵称时明确回退为原账号 ID。
`[wechat_pc_hook(wechat_pc_hook)]` 是 AstrBot 的类型/实例 ID 格式，现有 ID 保持兼容；不代表重复接收。

文件 `resource_missing` 尚未在本轮修复：Hook 的文件打开失败会使用该错误码，可能涉及路径、权限或占用。
本轮未改动或替换 Hook DLL，不能把迁移抽帧能力当作文件资源问题已经解决。

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

v0.5.0 提供独立于群 Agent 的通用视频抽帧；v0.4.1 修正媒体会话定位。仅进行语法检查、静态核对和 ZIP 构建，
没有编写测试代码或运行本地测试，也没有部署到生产或连接真实微信/模型。
真实收发路径尚未实机验证；没有对端收件回执、自动重试、主动推送、媒体/卡片发送或多进程恢复。
