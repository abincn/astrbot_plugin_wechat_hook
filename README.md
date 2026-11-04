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
- `hook_url` 填 HTTP(S) 地址和端口，不附加 `/wxcx` 或接口路径。默认只允许回环地址；跨主机连接须开启“允许远程 Hook”并填写令牌。
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
  原名保留在 File.name；本地一律使用内容哈希加 .bin 存储，所有文件类型共用取回、大小/SHA校验流程，不识别、解码或执行正文。
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

接收到 Video 不等于模型看到了画面。连接器保留原视频组件及引用关系；v0.6.0 可选媒体模块提供画面抽帧和音轨转写。
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

### 原视频、内置 ffmpeg 与音轨处理（v0.6.0）

连接器接收、下载并校验原视频，构造 AstrBot `Video`；引用视频保留在 `Reply.chain` 中。
与其他平台连接器一样，这表示原视频已交给 AstrBot，不表示已经上传给大模型。
`Hook video ready` 是原视频接收成功的日志。

v0.6.0 按用户要求恢复独立媒体模块、平台布尔控件和模型请求钩子，群聊/私聊/引用视频共用。
不在群 Agent 里重复实现，不主动发起聊天模型请求，不自动上传整段原视频。
在消息平台的“微信 Hook 连接”中配置：

- **视频画面分析**（默认开）：约 0、3、9 秒最多三帧，最长边中的宽不超过 960 像素，每帧最多 1.5 MB、解码最多 12 秒，追加至当前请求的图片输入。需要视觉模型。
- **视频音轨识别**（默认开）：仅当 AstrBot 当前会话已启用语音转文字并配置 ASR 时执行。
  提取首条音轨的前最多 120 秒，生成 16 kHz/单声道/16-bit WAV（最多 4 MiB、解码 30 秒），
  调用该会话的 ASR（最多等待 60 秒），将最多 12000 字自动转写追加到当前请求。ASR 可能产生原服务的费用。

没有启用/配置 ASR、无音轨、解码失败、识别失败分别记录状态并提示模型，不假称听到了声音。
画面和声音独立处理：音轨失败不丢弃已有图片。成功日志分别是 `Hook video frames attached`、`Hook video audio transcribed`。
只有 `Hook video ready` 仍仅表示原文件已接收。

每次请求最多处理首个视频；只使用本连接器已验证的本地媒体目录，缓存位于 Inbox 同级的 `media/video_frames/`。
不修改群指令或工具策略。两个开关都关闭时，只交付原视频组件。接收视频本身仍无需执行 ffmpeg。

安装包分别内置 **Windows x64、Linux x64、Linux ARM64** 对应的 ffmpeg；选对架构后无需手工安装或配置 Path。
通过仓库安装时，`requirements.txt` 的固定版本 `imageio-ffmpeg==0.6.0` 提供对应平台可执行文件。
平台 ZIP 已自带 ffmpeg，不再重复下载该依赖。运行时依次查找插件内置程序、依赖中的程序、系统 Path。
打包仅下载/核对固定 SHA-256/复制二进制，不执行 ffmpeg。来源及许可证见 [FFmpeg 声明](docs/FFMPEG-NOTICE.md)。

普通微信语音（SILK）仍走 `Record` → AstrBot 自带 `silk-python` 解码 → 已配置 ASR 或音频模型，
不是用 ffmpeg 代替 SILK 解码器，也不重复调用 ASR。视频音轨通常是 AAC 等格式，走上述提轨转写模块。

如安装了旧 Agent v0.3.1，请更新到 v0.3.2 或以上，移除其群内重复抽帧。
完整重启 AstrBot 后生效，无需清空数据库或修改发送开关；已有视频和缓存不会自动删除。

### Linux / Docker 部署

AstrBot 和插件支持 glibc Linux x86_64 / aarch64，分别使用对应平台安装包；Alpine/musl 不在本次内置二进制支持范围。
Docker 应把正确的 Linux 包安装到 AstrBot 容器内；可执行文件所在挂载点须允许执行。插件会恢复 Linux 文件的可执行权限。
ffmpeg 在容器内运行，不能靠只给宿主机安装 Windows exe 或 ffmpeg 来替代。

微信 PC Hook DLL 仍运行在 Windows；Linux 不加载 DLL。媒体经 Hook HTTP 下载到 Linux 的本地 Inbox/媒体目录，无需共享 Windows 文件路径。
默认只允许回环地址，可将 Windows Hook 隧道转发到 Linux 的 `127.0.0.1:30001`。
例如从 Windows 发起反向 SSH 隧道（需已有 SSH 服务和登录权限）：

```text
ssh -N -R 127.0.0.1:30001:127.0.0.1:30001 linux_user@linux_host
```

随后 Linux 插件继续使用 `http://127.0.0.1:30001`，`hook_token` 与 Windows Hook 一致。
Docker 的回环地址属于容器自身；可使用 Linux 的 host 网络模式，或让隧道与 AstrBot 位于同一网络命名空间。
本仓库不自动建立隧道、不改变 Hook 监听地址或鉴权。配置持久化 Inbox、插件目录及 AstrBot ASR/模型配置后重启。

### 直接连接远程 Hook（v0.6.2）

Linux / Docker 需要直接连接另一台 Windows 时，在 **消息平台 → 微信 Hook 连接 → 编辑**：

1. 开启“允许远程 Hook”（`allow_remote_hook`，布尔控件，默认关闭）。
2. 地址填写 Windows 主机可达的 HTTP(S) 地址和端口，例如 `http://192.168.1.10:30001`。
3. 填写与该 Hook 一致的 `hook_token`；远程连接不接受空令牌。
4. 保存并完整重启 AstrBot。

地址仅含协议、主机及端口，不带 `/wxcx/messages` 等路径，不在地址中附加 token、用户名、密码或查询参数。
使用可信内网/VPN或HTTPS；HTTPS 保持证书校验，不自动忽略证书错误。
容器里的 `127.0.0.1` 仍指向容器本身，不能用它代替 Windows 的地址。
Windows Hook 必须已监听容器/服务器可达的地址，并允许该来源连接；此开关只放开插件的目标校验，
不修改 Windows 监听地址、防火墙或 Hook 端访问控制。若 Hook 仅监听回环或拒绝远端请求，请继续使用隧道。
插件继续使用 `X-Wxcx-Token` 请求头，不跟随重定向，不把令牌写入日志。收发、媒体及联系人查询共用该连接。

### 服务推送不自动回复（v0.6.1）

腾讯新闻等服务推送不能按普通私聊交给模型自动回复。
当前识别直接私聊来源 `newsapp`、`weixin`、`weixinreminder`、`qqmail`、`gh_` 公众号，
以及直接 `mmreader` / `msg/mmreader` XML 信封；不根据标题、昵称、链接域名或新闻正文猜测。
仅在明确入站且非群聊时应用；不扫描引用/转发 appmsg 内嵌 XML。
普通群聊、用户分享的常规链接/小程序卡片、引用/转发 appmsg 继续使用原来的接收规则。
未知来源且不符合上述结构的其他 App 推送暂不能保证识别，应根据实际事件结构再扩充分类。

匹配的消息仍在 Inbox 保存，状态为 `service_notice`，游标照常推进；
不创建媒体任务、不查询昵称、不送入聊天模型或 ASR，也不自动发回微信。
日志会显示 `Hook service notification retained; auto-reply skipped` 和分类原因。
更新后对尚未分发的 pending 通知也会再检查，发送入口另有同样保护；已发出的回复不自动撤回。
升级后完整重启 AstrBot，无需清空数据库。

### 联系人接口 404 与微信 4.x 昵称（v0.6.3）

当前 Hook 源码的 `/GetContact` 固定读取 `MicroMsg.db/Contact`，查库失败和联系人不存在都会返回 404。
因此 `contact_fields=[]` 不代表 AstrBot 丢失了字段，也不能靠把 ID 再填入 nickname 来修好。

批量联系人接口没有取得名字时，插件通过现有 `/QueryDB/GetAllDBName` 获取已打开的库名，
只选择联系人库及旧 MicroMsg.db，读取相关 contact 表结构，识别 `username`/`user_name`、`nick_name`/`NickName` 等字段。
随后按当前待分发消息的发送者 ID 定向 SELECT 昵称；库/表/字段名正确引用，账号值转义，不查询聊天正文或读取全部联系人。
仅使用 SELECT 和只读表结构 PRAGMA，不修改微信数据库或 Hook DLL，也不要求用户导出数据库。

昵称仍使用已有缓存和整个查询批次 3 秒等待上限；表结构命中缓存 10 分钟，失败后 60 秒再尝试。
日志 `Hook nickname schema selected` 表示发现兼容表，`Hook contact database nicknames resolved` 表示取得了昵称。
若 Hook 未开放查询接口、没有报告联系人库、库未缓存该成员或查询超时，仍显示“昵称未取得/账号ID”并保留诊断。
联系人昵称不保证等于群内自定义名片；群显示名仍依赖 Hook 提供的成员数据。

成功接收的媒体只保留实际媒体组件，不额外插入同名 Plain 占位，避免日志出现 `[图片] [图片]`。
模型请求用的 `message_str` 摘要仍保留；下载失败时仍输出缺附件文字。日志中的昵称/账号双字段由 AstrBot 格式化。
若入站消息没有昵称，使用 Hook 已有的 `/BatchGetContactBriefInfo` 获取联系人 `NickName`，
批量未命中时再调用单个 `/GetContact`，兼容 `NickName`、`nick_name`、`nickname` 等字段，
并从群 `/GetContact` 的成员信息补充群显示名；最多等待 3 秒，缓存最多 512 项，命中缓存不重复查询。
显示优先使用已取得的群显示名，再用入站昵称或联系人昵称；仍未取得时显示“昵称未取得/原账号ID”，
不把 ID 冒充昵称，并在 WARN 中记录 `Hook nickname not resolved` 及返回字段名，便于排查 Hook 数据兼容。
`[wechat_pc_hook(wechat_pc_hook)]` 是 AstrBot 的类型/实例 ID 格式，现有 ID 保持兼容；不代表重复接收。

文件 `resource_missing` 尚未在本轮修复：Hook 的文件打开失败会使用该错误码，可能涉及路径、权限或占用。
本轮未改动或替换 Hook DLL，不能把调整连接器职责当作文件资源问题已经解决。

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

v0.6.3 为旧联系人接口未命中增加已报告联系人库的只读昵称查询；其他连接/媒体能力保留。仅进行语法检查、静态核对和 ZIP 构建，
没有编写测试代码或运行本地测试，也没有部署到生产或连接真实微信/模型。
真实收发路径尚未实机验证；没有对端收件回执、自动重试、主动推送、媒体/卡片发送或多进程恢复。

## v0.6.5：通用文件交付和引用关联

v0.6.4 的文本识别/正文注入方案已撤回，请使用本版。

所有文件类型（PDF、Office、压缩包、源码、二进制、未知后缀、无后缀）共用 Hook file 媒体任务。原名称保留，缓存按内容哈希加 .bin 存放；大小上限24MiB与完整性校验仍保留，不按扩展名拒绝文件。

引用文件按当前账号、当前会话和原消息 MsgSvrID 关联，优先复用重新校验过的缓存，无缓存时按原消息ID取回。分别以普通 File 或 Reply.chain 中的 File 交付。

下游处理层可从 event.get_extra("wechat_attachments") 取得列表：kind、name、path、bytes、sha256、source_msgsvrid、source_talker、quoted。元数据不包含解码正文或推测的MIME。后续文件识别、文档提取及模型输入由应用层处理；连接插件不把文件正文塞入prompt，也不执行文件。

16项离线用例通过，覆盖各类二进制文件、普通/引用、私聊/群聊、缓存复用、任务至附件完整路径和校验隔离；其中使用614字节实际产物验证原字节不变。本次未部署运行中的AstrBot、调用模型或发消息。更新插件不需要替换/重启微信。

## v0.6.8：媒体任务容量回收

修复媒体取得后未释放Hook任务、累计128条后返回`media_capacity`的问题。附件保存并校验成功后释放任务，升级后也会回收插件数据库中已有的ready任务；AstrBot本地附件副本继续保留，清理失败会在后续轮询重试。

只有明确的HTTP429 `media_capacity`准入拒绝才每10秒等待重试，最多30次；已有task_id或结果未知的请求不自动重提。旧版已标为unknown的事件不会自动恢复。18项离线文件传输检查通过；更新插件需要重启AstrBot，微信DLL无需替换。

## v0.6.7：Responses 文件协议直传

Hook继续只交付原始附件。此版在AstrBot侧把当前消息的已验证附件作为原文件名+Base64字节发送，使用Responses input_file；不识别正文、不把代码转成prompt、不需要Moonshot或本机执行权限。原有24MiB附件上限继续保留。

兼容层绑定已存在的openai_responses提供商实例，保留原来的文本、图像、工具调用和推理转换；无文件请求直接交给原函数。其他类型提供商明确报告不支持直传，不静默丢弃文件或降级为正文。API可以拒绝不支持的格式，Hook取回成功不代表API已接受。

普通/引用附件都只从当前账号、当前会话、当前消息记录查找，发送前重新核对实际大小与SHA-256。文件缓存名可为.bin，线上filename保留原名称和后缀。模型API支持的格式以所用API/中转为准；此版不把任意二进制都宣称为模型可理解。

后台 WebChat 或现有管理员命令 `hook_file_probe`（按当前唤醒前缀，例如 `/ai hook_file_probe`） 通过现有模型连接发送一个不含私人内容的合成.S文件，并核对模型是否读回随机标记；不会读取或上传聊天附件，不发送微信消息，不导出API密钥。每次命令会产生一次实际模型请求；HTTP成功但未读回标记仍判为未验证。

更新后完整重启AstrBot服务/容器，以重建平台实例；微信无需重启。随后引用已收过的文件提问。准备日志会出现 `attachment prepared as Responses input_file`，此时只是准备完成，不冒充API接受。模型异常仍沿用AstrBot自身的错误处理。

离线验证：15项原附件交付测试及20项文件协议测试通过；另使用官方v4.29.4真实转换函数运行同20项协议测试通过。离线验证不代表你的中转接口已支持，需以合成文件实际验证结果及后续引用文件回复为准。
