> 本文是**完整参考手册**（全部字段、能力清单、内部机制、验证清单、安全边界、开发与测试）。
> 想快速上手请看仓库根目录的 [README.md](../README.md)；维护者向说明见 [docs/README.md](README.md)。

# tencent_channel — Neo-MoFox 腾讯频道（QQ 频道）插件

通过 `tencent-channel-cli` 把腾讯频道的互动通知接进 MoFox 标准消息管线，让 AI 自动回复评论 / 楼中楼 / 私信，
同时把 CLI 的 **61 条可用命令**包成 LLM 的只读工具与写入动作（含可选的多频道、群聊式上下文、免 CLI 网关直连）。

```
feed get-notices（互动通知轮询）
feed get-guild-feeds（新帖轮询，可选 channel.feeds.enabled）
feed get-feed-comments（评论区轮询，可选 channel.comments.enabled）
        ↓ notice_mapping：归一化 + 去重键 + 水位线
   MessageEnvelope(dict) → CoreSink → 默认 Chatter(LLM)
        ↓ MessageSender（按 platform=tencent_channel 命中适配器）
   adapter._send_platform_message → 按 reply_target_policy 取「待回复目标」
        ↓ reply_sender
   feed do-comment / feed do-reply / manage push-group-dm-msg
```

> 本文档与 `manifest.json`（17 个组件）及 `capability_spec.py`（CLI 1.0.10，61 条可用能力）同步。

## 1. 组件清单

| 类型 | 名称 | 说明 |
|---|---|---|
| config | `config` | 插件配置（总开关、频道、CLI、回复、日志、能力开关、提示词） |
| adapter | `tencent_channel_adapter` | 三条轮询 + 出站回复（`platform = tencent_channel`），依赖 `channel_cli` |
| service | `channel_cli` | CLI 调用封装，供其他组件复用（Service **不是单例**，每次 `get_service` 新建实例） |
| tool | `channel_notices` | 查看互动通知 |
| tool | `channel_search_feeds` | 频道内搜索帖子 |
| tool | `channel_feed_detail` | 帖子详情 |
| tool | `channel_guild_info` | 频道信息 |
| tool | `channel_members` | 按昵称搜成员（取 `tiny_id`，@人必须要它） |
| tool | `channel_guilds` | **本账号加入了哪些频道**（名称 / 频道号 / 频道 ID） |
| tool | `channel_sections` | **频道里有哪些版块**（板块名称 + 版块 ID） |
| tool | `channel_read` | **通用只读**：执行能力清单里任意一条只读命令 |
| action | `channel_publish_feed` | 发帖（短贴/长贴、纯文本/Markdown、图片、视频、话题、@、文字链接） |
| action | `channel_comment_feed` | 评论帖子（可带 1 张图） |
| action | `channel_reply_comment` | 楼中楼回复评论（可带 1 张图，字段不全自动降级为帖内评论） |
| action | `channel_like` | 给帖子 / 评论 / 楼中楼回复点赞或取消自己的赞 |
| action | `channel_send_dm` | 发私信 |
| action | `channel_write` | **通用写入**：执行能力清单里任意一条写命令（含敏感/破坏性，按开关 + `confirm` 放行） |
| event_handler | `shutdown_logout` | Bot 优雅关机（`ON_STOP`）时按配置 `login logout` 清凭证 |
| event_handler | `capability_gate` | 在 `BEFORE_TOOL_FILTER` 里按 `[capabilities]` 开关把关闭的工具从提示词里摘掉（核心 < 1.3.0 无此事件，退化为工具自检） |

### 1.1 核心版本兼容

| 能力 | 核心 ≥ 1.3.0（如 `1.3.0-alpha.0`） | 核心 1.2.x（如 `1.2.0`） |
|---|---|---|
| 插件加载与全部功能 | ✅ | ✅ |
| `[capabilities]` 关闭的工具**从提示词里隐藏** | ✅ 通过 `BEFORE_TOOL_FILTER` 事件（`capability_gate`） | ⚠️ 该核心的 `EventType` 没有这个成员：工具仍出现在提示词里，但 `execute` 会直接拒绝（`tools.py` 里的能力自检），**行为上一样不会执行** |

- `manifest.json` 的 `min_core_version` 是 `1.2.0-rc.1`（1.2.x 起可用）。
- `lifecycle.py` **不写死** `EventType.BEFORE_TOOL_FILTER`，而是按名字探测：
  缺少该事件的核心只会打一条 warning，不会在 **import 阶段**让插件加载失败
  （历史上正是因为写死它，1.2.0 上插件直接 `AttributeError: BEFORE_TOOL_FILTER` 加载失败）。

## 2. CLI 能力清单与分类

能力清单由 `gen_capability_spec.py` 从 `tencent-channel-cli schema -j` 生成到 `capability_spec.py`（当前为 CLI **1.0.10**），
`capabilities.py` 在其上补**分类**（read / write / sensitive / danger）、**开关判定**与**提示词**。

统计：spec **66** 条 → 可用 **61** 条（只读 24 / 写入 6 / 敏感 24 / 破坏性 7）；另有排除名单 12 条
（其中 5 条命中 spec 的交互式命令被剔除，另 7 条是 CLI 运维口令 `cli.login*` 等，本就不在清单里，LLM 调不到）。

**只读（24 条，默认开）**
```
feed.get-guild-feeds、feed.get-channel-timeline-feeds、feed.get-feed-detail、feed.get-feed-comments、
feed.search-guild-feeds、feed.get-feed-share-url、feed.get-notices、feed.get-next-page-replies、
manage.get-guild-info、manage.get-my-join-guild-info、manage.get-user-info、manage.get-guild-member-list、
manage.guild-member-search、manage.get-guild-channel-list、manage.search-guild-content、
manage.get-join-guild-setting、manage.get-guild-share-url、manage.get-share-info、
manage.notices-status、manage.check-notices、manage.check-new-notices、manage.get-recent-notices、
cli.version、cli.doctor
```

**写入（6 条，默认开）**
```
feed.publish-feed、feed.do-comment、feed.do-reply、feed.do-like、feed.do-feed-prefer、
manage.push-group-dm-msg
```

**敏感（24 条，默认关）**
```
feed.alter-feed、feed.top-feed、feed.set-feed-essence、feed.push-essence-feed、feed.move-feed、
feed.latest-feeds-detail、feed.hot-feeds-detail、
manage.update-guild-info、manage.modify-guild-number、manage.create-guild-role-group、
manage.modify-guild-role-group、manage.add-role-members、manage.remove-role-members、manage.join-guild、
manage.create-channel、manage.modify-channel、manage.upload-guild-avatar、
manage.create-theme-private-guild、manage.update-join-guild-setting、
manage.notices-on、manage.notices-off、manage.subscribe-notices、manage.unsubscribe-notices、manage.deal-notice
```

**破坏性（7 条，默认关；开启后执行还需 `confirm=true`）**
```
feed.del-feed、manage.kick-guild-member、manage.modify-member-shut-up、manage.delete-channel、
manage.add-admin、manage.remove-admin、manage.leave-guild
```

**被排除的命令（不交给 LLM）**：`cli.login` / `cli.login.poll-token` / `cli.login.logout` / `cli.completion` /
`cli.help` / `cli.schema` / `cli.logs`（运维口令，登录态由插件自己管理）、`feed.quick-publish` /
`feed.search-and-comment` / `feed.delete-and-mute` / `manage.search-and-join`（交互式命令，改用等价组合）、
`manage.notify-daemon`（常驻进程，插件自己轮询）。

> ⚠️ 分类小坑：内置只读提示词里推荐的 `feed.latest-feeds-detail` / `feed.hot-feeds-detail` 目前被归为
> **敏感（默认关）**，直接调会被拒；需要它们时请写进 `[capabilities] enabled`。

## 3. 前置条件

1. **安装 CLI**（Node ≥ 18）：`npm install -g tencent-channel-cli`，用 `where.exe tencent-channel-cli` 确认路径。
2. **登录**（三选一）：
   - 手动：`tencent-channel-cli.cmd login --json` → 用返回的 `verification_uri` / `qrcode_path` 扫码 →
     `tencent-channel-cli.cmd login poll-token --json`（默认插件**不代跑**登录，也不读取/打印凭证）；
   - 或打开 `cli.login.auto_qrcode = true`，启动发现未登录时插件会打印授权链接 + 二维码字符画并后台等扫码；
   - 或**令牌直登**：扫码过一次后跑 `python plugins\tencent_channel\export_login_token.py <输出路径>`，
     把路径填进 `cli.login.token_file`，此后重启（含 `cli.login.logout_on_startup` 清凭证后）自动恢复登录，免扫码。
3. **频道与 Bot**：**最小配置只要 3 项**（都在 `[channel]`）：

   ```toml
   [channel]
   guilds   = "pd12345678"          # ① 频道号：QQ 客户端里能看到的那串 pd…
   bot_name = "示例Bot"      # ② bot 在频道里显示的昵称
   bot_qq   = "123456789"           # ③ bot 的 QQ 号（用于 bot_id 与日志）
   ```

   **其余「在 QQ 客户端里看不到」的东西都会自动获取**：

   | 自动获取项 | 怎么拿到的 | 失败时怎么办 |
   |---|---|---|
   | 真实频道 ID（`guild_id`） | 频道号 → `manage get-my-join-guild-info` 换算（纯数字则原样使用） | 日志会列出该账号的频道，可把真实 ID 填进 `guilds` |
   | 版块 ID（`channel_id`） | `manage get-guild-channel-list` 取「全部」版块（没有就叫第一个） | 日志警告；读操作不受影响，发帖/评论需手动填 `channel.channel_id` |
   | 本账号 `tiny_id` | 用 `bot_name`（及接口返回的昵称/频道昵称）在频道里搜成员，**同名唯一**时采用 | 同名多人时日志警告，需手工填 `channel.self_tiny_id` |
   | `bot_id` | 留空时用 `bot_qq`，其次用登录账号 `tiny_id` | 最后回落占位值 |

   > `bot_qq` 只作标识/日志用：QQ 频道接口**不返回 QQ 号**（`get-user-info` 只有昵称与 tiny_id），
   > 所以它不能用来反查账号 —— 登录身份由 `[cli.login]` 的令牌决定。

   想在配置前手动核对频道列表：`tencent-channel-cli.cmd manage get-my-join-guild-info --json`。

**Windows 提示**：裸 `tencent-channel-cli` 会走 `.ps1`，执行策略受限时会**无交互永久卡住**。
插件已内置路径自动发现：`cli.path` 默认的 `tencent-channel-cli.cmd` 若不可用，
会自动在 `%APPDATA%\npm`（以及 Program Files 下的 npm 目录）里找
`node_modules/**/tencent-channel-cli.exe`（优先）或 `tencent-channel-cli.cmd`，并把调用方式切到 `exe`/`cmd`
（启动日志会打印 `已自动发现 tencent-channel-cli：...`）。所以通常**不需要**手写路径；
要固定时推荐直接指向平台包 exe 并设 `cli.mode = "exe"`：

```
%APPDATA%\npm\node_modules\tencent-channel-cli\node_modules\
    tencent-channel-cli-win32-x64\bin\tencent-channel-cli.exe
```

## 4. 安装

1. 把本目录 `tencent_channel\` 整个复制到 MoFox 安装目录的插件目录下：

   ```
   <MoFox 安装目录>\plugins\tencent_channel\
   ```

   （注意是安装目录下的 `plugins\`，不是 `_internal\plugins\`；启动日志会打印它扫描的目录。）
2. 重启 MoFox Code（启动阶段会 `ON_ALL_PLUGIN_LOADED` 自动启动适配器）。
3. 首次加载后框架会在 `C:\Users\<你>\AppData\Local\MoFox Code\config\plugins\tencent_channel\config.toml`
   **自动生成**默认配置；也可以先把本仓库的 `config\plugins\tencent_channel\config.toml` 复制过去再改。
   `channel.guilds` 必须填（至少要有一个频道），否则适配器只提供工具、不轮询。

## 5. 配置说明（全部字段）

配置按**嵌套节**分组，共 11 个节：

```
[plugin]                  插件总开关
[channel]                 目标频道 + 会话形态
[channel.notices]         互动通知轮询
[channel.feeds]           新帖轮询
[channel.comments]        评论区轮询
[cli]                     CLI / 网关调用方式
[cli.login]               登录凭证生命周期
[reply]                   自动回复策略
[log]                     日志与排障
[capabilities]            逐能力开关
[prompts]                 LLM 提示词
```

> ⚠️ **框架会按 `config.py` 的模型签名重渲染 `config.toml`**：手改注释/排版下次启动就会被覆盖。
> 要改说明请改 `config.py`（节 docstring 渲染成分组标题，`Field(description=…)` 渲染成字段上的一行注释），
> 详细解释写在 `Field(hint=…)`（只进 WebUI，不进 TOML）。旧键迁移见 [5.8](#58-旧键迁移与文件渲染规则)。

### 5.1 `[plugin]` 总开关

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 插件总开关：false 时整体停用 —— 不轮询、不登录，工具与动作也**不进** LLM 的可用组件 |

### 5.2 `[channel]` 目标频道与会话

前 3 项是**最小配置**（其余都可留空，见 [§3](#3-前置条件) 的自动获取表）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `guilds` | `[]` | **① 监听频道**：列表、逗号/顿号分隔字符串，或单个字符串都行；每项填频道号（`pd20589127`，自动解析）或真实 ID（纯数字）。第一个解析成功的作为默认频道；留空则不轮询 |
| `bot_name` | QQ频道助手 | **② bot 昵称**：用于自动探测本账号 `tiny_id`（也作为 `get_bot_info` 的显示名） |
| `bot_qq` | "" | **③ bot 的 QQ 号**：仅用于 `bot_id` 与日志（接口不返回 QQ 号） |
| `channel_id` | "" | 版块 ID：**留空自动获取**（优先 `section_name`，其次「全部」版块） |
| `section_name` | "" | **默认版块名**（如「全部」「闲聊」）：按名字填即可，无需查 ID；与 `channel_id` 同时填时以 `channel_id` 为准 |
| `self_tiny_id` | "" | `do-reply` 的 `replier_id`：**留空自动探测**（按昵称搜成员，同名唯一时采用） |
| `bot_id` | "" | 预留覆盖：**留空自动**用 `bot_qq`，其次用登录账号 `tiny_id` |
| `poll` | true | 是否轮询频道（false = 只提供工具/动作，不拉通知/帖子） |
| `channel_as_group` | false | **把整个频道当成一个群**：所有帖子与评论进同一条会话流 |
| `reply_target_policy` | latest | 频道级会话下回复落在哪条待回复消息上：`latest`（推荐）/ `oldest`（非法值回落 latest） |
| `dm_source_guild_id` | "" | 私信发送的来源频道（空则用主频道；同样支持频道号） |
| `state_path` | "" | 水位线持久化文件（空 = 仅内存；重启后重新建立基线，宁可漏也不重复回复） |

### 5.3 三条轮询路径：`[channel.notices]` / `[channel.feeds]` / `[channel.comments]`

| 字段 | 默认 | 说明 |
|---|---|---|
| notices.`enabled` | true | 是否轮询互动通知 |
| notices.`poll_interval` | 600.0 | 通知轮询间隔秒 = **主循环节拍**（默认 600 = 10 分钟，可自行调整；代码下限 5） |
| notices.`page_num` | 20 | `feed get-notices --page-num` |
| notices.`max_per_poll` | 20 | 单轮最多注入条数，超出只注入**最新** N 条，防历史积压刷屏 |
| notices.`types` | comment/reply/at/dm | 值得注入的通知类别（可加 `like`/`top`/`favorite`，默认忽略点赞类） |
| notices.`inject_history` | false | 首轮是否注入历史通知（false = 只建立基线） |
| feeds.`enabled` | false | **新帖子监听**：帖子不产生互动通知，开启后额外轮询帖子列表并注入（可自动评论别人的新帖） |
| feeds.`poll_interval` | 600.0 | 新帖轮询间隔秒（默认 600 = 10 分钟；下限 15），仅 `feeds.enabled=true` 时生效 |
| feeds.`page_num` | 10 | 每次拉取的帖子条数（`feed get-guild-feeds --count`） |
| feeds.`skip_self` | true | 跳过自己发的帖子，避免机器人评论自己 |
| comments.`enabled` | false | **评论区轮询**：把房间里别人之间的评论/回复也注入，像群聊一样有上下文 |
| comments.`poll_interval` | 600.0 | 评论轮询间隔秒（默认 600 = 10 分钟；下限 15），仅 `comments.enabled=true` 时生效 |
| comments.`page_num` | 5 | 参与评论轮询的最新帖子条数（每条帖子一次 CLI 调用） |
| comments.`reply_list_num` | 1 | 每条评论预加载的回复数（`--reply-list-num`，代码钳制 0–10） |
| comments.`max_age_hours` | 72.0 | 只注入此时间内的评论/回复（0 = 不限） |
| comments.`skip_self` | true | 跳过本账号自己发的评论/回复，避免自问自答 |

### 5.4 `[cli]` 调用方式 + `[cli.login]` 登录凭证

| 字段 | 默认 | 说明 |
|---|---|---|
| cli.`mode` | auto | `auto` / `node` / `cmd` / `python` / `exe` / **`gateway`**（免 CLI 直连 MCP 网关） |
| cli.`path` | tencent-channel-cli.cmd | CLI 路径；填默认值时插件会自动发现 `.cmd`/`.exe`（优先 exe） |
| cli.`gateway_endpoint` | `https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp` | `mode=gateway` 的端点（一般不用改） |
| cli.`node_path` / cli.`python_path` | "" | `mode=node` / `python` 用的解释器路径（留空用 PATH / 当前解释器） |
| cli.`timeout` | 60.0 | 单次调用超时秒（含 node 启动开销） |
| cli.`rate_limit_sleep` | 70.0 | 触发 retCode 153 后**进程级冷却**的首档秒数（文档要求 70s） |
| cli.`rate_limit_multiplier` | 2.0 | 连续限流时冷却时长按此倍数指数增长（70s → 140s → 280s …） |
| cli.`rate_limit_ceiling` | 1800.0 | 指数退避的最大冷却秒数（默认 30 分钟） |
| cli.`dry_run` | false | 只构造命令不执行（CLI `--dry-run`；gateway 模式返回构造好的 tool/arguments） |
| login.`token_file` | "" | 令牌文件（**优先于** `token`）：JSON `{"token":…,"device_id":…}` / `QQ_AI_CONNECT_TOKEN=…` 行 / 纯文本；支持 `~` 与环境变量 |
| login.`token` | "" | 访问令牌直登（明文，不推荐）；CLI 未登录时写回系统密钥环恢复登录，不覆盖已有有效登录 |
| login.`device_id` | "" | 与令牌配套的 `device-id`（文件里也能带 `device_id` 字段） |
| login.`capture_keychain` | true | 配置未填令牌时，在启动清凭证**之前**先抓取本机凭据存储里的现存令牌用于回注；false = 严格的一次性会话语义 |
| login.`auto_qrcode` | false | 启动发现未登录时打印授权链接 + 二维码（字符画 + PNG 路径），后台等扫码，成功后自动继续启轮询 |
| login.`wait_seconds` | 600.0 | 打印二维码后等待扫码时长秒（下限 30） |
| login.`poll_interval` | 10.0 | 等待扫码期间轮询 `login poll-token` 的间隔秒（下限 3） |
| login.`logout_on_startup` | false | 启动时先清上次残留凭证再进未登录流程（强杀/断电的唯一可靠兜底） |
| login.`logout_on_shutdown` | false | Bot **优雅关机**（`ON_STOP`）时执行 `login logout --yes` 清凭证 |

### 5.5 `[reply]` / `[log]`

| 节 | 字段 | 默认 | 说明 |
|---|---|---|---|
| reply | `enabled` | true | 自动回复总闸：false = 只注入通知、不回复 |
| reply | `max_length` | 1000 | 回复正文最大字符数，超出截断并追加省略号（发帖正文超限会**拒绝**而不是截断） |
| reply | `reply_to_comment` | true | 评论级通知优先楼中楼；false 或字段不全时退化为帖内评论 |
| reply | `enrich_comment_context` | true | `do-reply` 前用 `get-feed-detail` / `get-feed-comments` 补齐必填字段 |
| log | `raw_notices` | false | 打印原始通知 JSON（排障，截断输出） |
| log | `poll_summary` | true | 每轮打印通知统计摘要（拉取/新增/注入条数） |

### 5.6 `[capabilities]` 逐能力开关

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `[]` | 显式**开启**的能力名 / 类别名 / 域名 / `*` |
| `disabled` | `[]` | 显式**关闭**（优先级最高） |
| `read_default` | true | 只读能力的默认开关 |
| `write_default` | true | 内容写入（发帖/评论/回复/点赞/私信）的默认开关 |
| `sensitive_default` | false | 敏感能力（改频道资料/版块/身份组/帖子状态/通知订阅）默认关 |
| `danger_default` | false | 破坏性能力（删除/踢人/禁言/权限变更/退出频道）默认关，开启后执行时还要 `confirm=true` |
| `expose_generic_read` | true | 是否暴露通用只读工具 `channel_read`（覆盖全部只读能力） |
| `expose_generic_write` | true | 是否暴露通用写入动作 `channel_write`（覆盖全部写能力） |
| `allow_yes_for_danger` | false | 破坏性命令（`confirm=true` 时）是否附加全局 `--yes` 跳过 CLI 的人工确认；**默认关**，关着时 CLI 若要求确认可能因等待输入而超时 |

开关判定：`disabled` > `enabled` > 类别默认值；三个名单里都可以写
**能力名**（`feed.del-feed`）、**类别名**（`read`/`write`/`sensitive`/`danger`）、
**域名**（`feed`/`manage`/`cli`）或 `*`。
升级 CLI 后若出现未显式分类的新写命令，一律按 `sensitive`（默认关）处理，不会悄悄放开。

### 5.7 `[prompts]` 提示词

LLM 看到的提示词都在这里（留空用内置默认，**改完重启生效**）。

| 字段 | 说明 |
|---|---|
| `read_tool` | 通用只读工具 `channel_read` 的提示词，`{capabilities}` → 当前开启的只读清单 |
| `write_action` | 通用写入动作 `channel_write` 的提示词，`{capabilities}` → 当前开启的写清单（含敏感/破坏性） |
| `capability_hints` | 逐能力提示词覆盖，元素形如 `"feed.del-feed=删除帖子（不可恢复，慎用）"` |
| `notices` / `search_feeds` / `feed_detail` / `guild_info` / `members` / `guilds` / `sections` | 7 个专用只读工具的提示词（`guilds` = 频道列表、`sections` = 版块列表） |
| `publish_feed` / `comment_feed` / `reply_comment` / `like` / `send_dm` | 5 个专用写入动作的提示词 |

### 5.8 旧键迁移与文件渲染规则

- **旧版扁平键自动迁移**：`[channel] watch_new_feeds` 这类旧键在加载时会被搬到新布局
  （迁移表见 `config.py` 的 `_LEGACY_KEYS`，共 28 条），日志里会出现一条
  `检测到 N 个旧版配置键，已自动迁移到新布局…` 的 warning，文件随后按新签名重渲染。
  主要映射：

  | 旧写法 | 新写法 |
  |---|---|
  | `[channel] guild_id` / `guild_number`（与 `guilds` 并存） | `[channel] guilds`（三者**合并成一个列表**，去重保序） |
  | `[channel] enabled` | `[channel] poll` |
  | `[channel] poll_interval` / `poll_page_num` / `max_notices_per_poll` / `notice_types` / `inject_history_on_first_poll` | `[channel.notices] poll_interval` / `page_num` / `max_per_poll` / `types` / `inject_history` |
  | `[channel] watch_new_feeds` / `feed_poll_interval` / `feed_page_num` / `feed_skip_self` | `[channel.feeds] enabled` / `poll_interval` / `page_num` / `skip_self` |
  | `[channel] watch_comments` / `comment_poll_interval` / `comment_page_num` / `comment_reply_list_num` / `comment_max_age_hours` / `comment_skip_self` | `[channel.comments] enabled` / `poll_interval` / `page_num` / `reply_list_num` / `max_age_hours` / `skip_self` |
  | `[channel] enable_reply` | `[reply] enabled` |
  | `[cli] auto_login_qrcode` / `login_token` / `login_token_file` / `login_device_id` / `login_capture_keychain` / `login_wait_seconds` / `login_poll_interval` / `logout_on_startup` / `logout_on_shutdown` | `[cli.login] auto_qrcode` / `token` / `token_file` / `device_id` / `capture_keychain` / `wait_seconds` / `poll_interval` / `logout_on_startup` / `logout_on_shutdown` |
  | `[log] log_raw_notices` / `log_poll_summary` | `[log] raw_notices` / `poll_summary` |

- **不要手改 `config.toml` 的注释**：框架每次加载都按模型签名重渲染（每字段固定输出
  「描述注释 + 值类型/默认值注释 + 键值 + 空行」），注释与排版都会被覆盖。
  想改说明：改 `config.py` 里对应节的 docstring（分组标题）或字段的 `description`（一行注释）。
- **字段的 WebUI 元数据**（`label` / `hint` / `input_type` / `choices` / `rows`）只影响 WebUI 配置表单，
  不会写进 TOML。
- **新增/删除配置字段**时：只改 `config.py`，框架会自动把新字段写进文件、把未定义字段清掉；
  若改的是键名，记得在 `_LEGACY_KEYS` 里登记旧键，并同步本节表格与 §6/§8 的相关说明。

## 6. 功能模式

### 6.1 「频道当群聊」模式（可选）

默认是「一条帖子 / 一条评论 = 一条会话」，只喂「指向机器人」的互动通知。想让它像 QQ 群一样，把这三个开关打开（需重启）：

| 开关 | 效果 |
|---|---|
| `channel.feeds.enabled = true` | 别人发新帖（哪怕没 @ 你）也会注入 → 可以自动评论 |
| `channel.comments.enabled = true` | 帖子里**所有人**的评论/楼中楼回复都注入 → 机器人能"听到"别人之间的对话 |
| `channel_as_group = true` | 所有帖子与评论进**同一条会话流**（`tcf\|<频道ID>`）→ 上下文连续，像群里一样 |

开启后的 CLI 调用量（每轮一次 node 进程，约 1~3s；下列次数需**按频道数**翻倍）：

```
通知轮询  1 次 / notices.poll_interval(默认 600s = 10min)
新帖轮询  1 次 / feeds.poll_interval(默认 600s = 10min)
评论轮询  最多 comments.page_num 次 / comments.poll_interval(默认 600s = 10min)   ← 只对有评论的帖子，无评论的帖子跳过
```

> 三个间隔都可自行改（`[channel.notices]` / `[channel.feeds]` / `[channel.comments]` 各自的 `poll_interval`）。
> 其中 `notices.poll_interval` 同时是**主循环节拍**：轮询线程每轮都醒来一次，到期的那条路径才真正发起调用。
> 想更及时就调小（代价是更频繁地拉起 node / 走网关，且平台对频繁调用有限流：`153` 会触发**进程级指数退避冷却**，见 6.8）。

两个已内置的防护：`comments.skip_self=true` 跳过机器人自己的评论（避免自问自答），
`comments.max_age_hours` 只处理近期内容（避免重启后翻旧账）。

### 6.2 单频道与多频道监听

只监听一个频道时，`channel.guilds` 直接写一个字符串即可（最省事）：

```toml
[channel]
guilds = "pd12345678"          # 频道号，启动时自动解析成真实 ID
```

要同时监听多个就写成列表（或逗号/顿号分隔的字符串）：

```toml
[channel]
guilds = ["pd11111111", "1234567890"]
# guilds = "pd11111111, pd22222222"   # 等价写法
```

- 每一项都支持真实 ID（纯数字，不查询）或频道号（`pd…`，启动时换算），
  逐项解析，**解析失败的项跳过并告警**，不会拖垮其他频道；
- 第一个解析成功的作为「主频道」：工具/动作不显式传 `guild_id` 时用它；
- 通知、新帖、评论三条轮询路径都会遍历全部频道；通知里缺失的 `guild_id` 会按本轮频道补齐（去重键、会话路由、出站目标都因此带上正确频道）；
- 私信来源频道 `dm_source_guild_id` 同样支持频道号，留空则用主频道；
- 解析结果进程内缓存，`guilds` 里重复写同一个频道会自动去重。

### 6.3 凭证生命周期：开机扫码 / 令牌直登 / 退出清凭证

| 开关 / 手段 | 时机 | 行为 |
|---|---|---|
| `cli.login.logout_on_startup = true` | 启动（早于 poll/guild 检查） | 先 `login logout` 清掉上次残留凭证。**这是强杀环境下唯一可靠的清理点** |
| `cli.login.auto_qrcode = true` | 启动发现未登录 | 调 `login --json` 拿授权链接与二维码；日志打印**授权链接**、**PNG 路径**和**二维码字符画**（`▀▄█`，手机可直接扫），随后按 `cli.login.poll_interval` 轮询 `login poll-token`，最多等 `cli.login.wait_seconds` |
| `cli.login.token` / `cli.login.token_file` | preflight 发现未登录 | 把令牌写回 CLI 的凭证存储（Windows 凭据管理器 `qq-cli:token` / `qq-cli:device-id`；Linux `secret-tool`、macOS `security`，不可用时降级 `~/.qqcli/.env`），再用 `login status` 复验；**绝不覆盖一个已经有效的登录**（如刚扫码产生的新令牌） |
| `cli.login.capture_keychain = true`（默认） | 启动、清凭证之前 | 先抓一份本机凭据存储里的令牌留着回注 → 配合 `cli.login.logout_on_startup` 得到「扫过一次码的机器此后免扫码」 |
| `cli.login.logout_on_shutdown = true` | 优雅关机（`ON_STOP`） | 执行 `login logout --yes`，尽早清掉凭证（密钥链 + `.env` + 二维码缓存） |

要点与坑：

- 除 `cli.login.capture_keychain` 外，上述开关**默认都是 false**，插件默认仍是「不代跑登录、不主动清凭证」。
- **`ON_STOP` 只在优雅关机时触发**：Ctrl+C、正常退出、启动器走"优雅停止"才会发。**强杀 / 断电 / 启动器直接结束进程不会触发**，
  所以**只靠 `cli.login.logout_on_shutdown` 是不够的**：那次退登根本没机会执行，凭证会留到下次启动。
- 想要「退出即失效」，请用 **`cli.login.logout_on_startup`**（+ 可选 `cli.login.auto_qrcode`，或 + `cli.login.token_file` 免扫码）：
  每次启动都先清一次，无论上次是优雅退出还是被强杀，效果一致。
- 两个退登调用都带 **15 秒短超时**，不会拖住启动或关机流程；失败只打 warning，不影响运行。
- 二维码字符画依赖 Pillow（**未在 `manifest.json` 的 `python_dependencies` 里声明**）；渲染失败时仍可用日志里的授权链接或 PNG 路径完成登录。
- 本地 `login logout` **不等于**账号侧解除授权：服务端那条设备绑定仍在，要彻底解除得去 QQ 的授权/设备管理里撤销（CLI 没有远程 revoke 命令）。
- 令牌文件**等同登录凭证**（能以你的账号身份操作频道），务必妥善保管、不要进仓库或外发；日志只打印掩码形态。

#### 同机多实例 / 多账号（重要）

CLI 的凭证存储（Windows 凭据管理器里的 `qq-cli:token` / `qq-cli:device-id`）是**全机器共享**的，
而且 CLI 每次调用都要去读它 —— 也就是说**同一台机器上，同一时刻只能有一个 CLI 登录身份**。

插件对此的处理：

- **配置了 `cli.login.token` / `token_file` 时，启动会把本实例的身份"钉"成配置的令牌**：
  若共享存储里是**别的**令牌（典型：同机另一个 bot 实例刚刚登录过），会替换成配置的，
  替换后登不上则**回滚**原凭证（不会把原本可用的登录弄坏）。日志里能看到
  `本机凭据存储里是**另一个**令牌… 将改用配置的令牌` 与 `当前登录账号：昵称「…」`。
- **没配置令牌时**（`capture_keychain=true` 的兜底路径）：会沿用机器上已有的登录，
  并打一条 WARNING 提醒 —— 这时本实例可能是**别的 bot 的账号**。

由此得到的结论：

| 场景 | 可行做法 |
|---|---|
| 一台机器只跑一个 bot 实例 | CLI 模式即可；给它配 `token_file`，启动会确保身份正确 |
| 一台机器跑**多个不同账号**的实例，且**同时在线** | CLI 模式**做不到**（两个实例抢同一份凭据存储）。给其中一个（或全部）改成 `cli.mode = "gateway"` —— gateway 直接用自己的令牌走 HTTP，不经过共享存储；或者分到不同 Windows 用户 / 不同机器上跑 |
| 多实例但**错开**运行（不重叠） | CLI 模式即可：每个实例启动时都会把共享存储换成自己配置的令牌 |

> gateway 模式的代价：它是 QQ AI Connect 的**内部** MCP 口子（非公开 API，理论上有风控可能），
> 且首次获取令牌仍需用 CLI 扫码一次（之后 `export_login_token.py` 导出即可）。

### 6.4 免 CLI 网关模式（`cli.mode = "gateway"`）

不拉起 CLI 子进程，直接对 `graph.qq.com/mcp_gateway/...` 发 JSON-RPC `tools/call`（无状态、`Authorization: Bearer <token>`）。
参数整形与响应归一化是按真机捕获实现的（读 6 + 写 6 条已验证），插件上层解析逻辑**完全复用** CLI 模式：

- 优点：省掉每次调用的 node 启动开销，机器上无需安装 CLI；
- 令牌来源与 token 直登一致（`cli.login.token` / `cli.login.token_file` 优先，其次读本机凭据存储里 CLI 登录过的令牌）；
- **不做扫码登录**（设备授权协议在 CLI 里）：首次拿令牌仍需用 CLI 扫码一次，再 `export_login_token.py` 导出；
- `login status` 在网关模式下用最便宜的读命令（`get_interact_notice`，`pageNum=1`）做令牌探针；
- 注意：这是 QQ AI Connect 的**内部** MCP 口子、非公开 API，Header/UA 与 CLI 有差异，理论上有被风控的可能；
  **CLI 模式仍是默认**。

### 6.5 会话路由与三层去重

**会话 key**（编码进 `group_id`，重启后仍可从出站 envelope 反解目标）：

| 场景 | key |
|---|---|
| 帖子级（默认） | `tcf\|<guild_id>\|<feed_id>` |
| 评论级（默认） | `tcf\|<guild_id>\|<feed_id>\|<comment_id>` |
| 频道级（`channel_as_group=true`） | `tcf\|<guild_id>` |
| 私信 | 走私聊 stream，`user_id = <对方 tiny_id>` |

**出站落点**：频道级会话里同一条流有多个候选落点（多条帖子/多条评论），插件为每条注入的消息登记一个「待回复目标」，
出站时按 `reply_target_policy` 取一条：`latest`（默认，回复锚定刚刚触发它的那条）/ `oldest`（严格先来后到）。
**队列空时回退到本会话最近一次成功回复的目标**（打 WARNING）：一个回合里 LLM 连发多条消息
（多角色 bot 各说一句很常见）时，一条通知只对应一个目标，第 2 条起就走这个回退；
只有连「最近目标」都没有时才拒绝发送（宁可不发也不发错位置）。
评论级目标自动走 `do-reply`（楼中楼），帖子级走 `do-comment`，私信走 `push-group-dm-msg`。

**三层去重**：

1. **水位线**：统一去重键 `canonical_notice_id`（`reply|<feed>|<id>` / `comment|<feed>|<id>` / `feed|<feed>` / 通知自带 id），
   配合 `NoticeWatermark`（已见集合 + 时间水位线，最多记 2000 条），可按 `state_path` 落盘；
2. **跨路径同内容指纹**：互动通知不带 `comment_id`，合成键与评论轮询键天然不同，同一条回复会被两条路径各注入一次；
   这里用「帖子 + 规范化正文」（**含 `guild_id`**，多频道下不误杀）在 600s 窗口内跨路径去重；
3. **出站 `message_id` 去重**：120s 窗口，避免 `MessageSender` 与 CoreSink outgoing 回调双路径重复发送。

### 6.6 注入文本格式（直接决定「回不回」）

注入的是**一行「某人说了一句话」**：不带平台标签、不带元数据块、不重复昵称 ——
昵称与时间由框架的消息行给出（`【时间】[平台ID] 昵称:名字 [msg_id]： 内容`，
昵称来自 envelope 的 `user_info.user_nickname`）。

```
在你的帖子下留了言：今天的图真好看～      ← 通知箱：评论了我的帖子
回复了你的留言：说得好                                  ← 通知箱：回复了我的评论
在频道里 @ 了你：@示例Bot 来看看
私信你：在么
在频道里发了新帖：<正文摘要>                            ← 新帖轮询
在频道里留了言（未提及你）：大家晚上好                  ← 评论轮询：别人之间的对话
在楼中楼里说了话（未提及你）：我也觉得
给你的帖子或留言点了赞                                  ← 点赞 / 收藏 / 顶帖
```

三条刻意为之的规则（按踩坑顺序）：

1. **括号里只写事实**，不写任何催促语 —— 历史上写过「这条在跟你说话」，实测会让 LLM 对频道里
   **每一条**别人之间的对话都抢着回话；
2. **帖子标题/正文不进评论的注入文本** —— 曾经把 `帖子：小A小B小C…看到请回复，一定要回我呀，我求你们`
   附在每条评论后面，结果那张强提及帖下的**每一条**评论都被当成在叫它；
3. **整体改写成「人说的话」** —— 早期形态 `【QQ频道·收到评论】<昵称>：<正文>（评论了你的帖子）` +
   `所属帖子ID：…` + `时间：…` 会被**决策子代理**判成「QQ频道系统的通知消息，并非直接对机器人发起的
   对话」而拒绝回复（真实日志 `08:17:09 default_chatter | 子代理决策: …属于系统通知而非互动消息
   (respond=False)`；判定准则里「机器博弈/系统消息」「话题无关」两条正对应这种形态）。

| 情形 | 注入文本 | 日志标签 | 在对机器人说话 |
|---|---|---|---|
| 通知箱：评论了机器人的帖子 | `在你的帖子下留了言：<正文>` | 收到评论 | ✅ |
| 通知箱 / 评论轮询：@ 了机器人 | `在频道里 @ 了你：<正文>` | 被@ | ✅ |
| 通知箱 / 评论轮询：回复机器人的评论 | `回复了你的留言：<正文>` | 收到回复 | ✅ |
| 私信 | `私信你：<正文>` | 私信 | ✅ |
| 评论轮询：别人之间的一级评论 | `在频道里留了言（未提及你）：<正文>` | 频道评论 | ❌ |
| 评论轮询：别人之间的一级评论（**本条正文点名了机器人**） | `在频道里留了言（提到了你）：<正文>` | 频道评论 | ❌（但值得看一眼） |
| 评论轮询：别人之间的楼中楼 | `在楼中楼里说了话（未提及你）：<正文>` | 频道楼中楼 | ❌ |
| 评论轮询：别人之间的楼中楼（本条正文点名了机器人） | `在楼中楼里说了话（提到了你）：<正文>` | 频道楼中楼 | ❌（但值得看一眼） |
| 新帖监听 | `在频道里发了新帖（提到了你／未提及你）：<正文>` | 新帖子 | ❌ |
| 点赞 / 收藏 / 顶帖 | `给你的帖子或留言点了赞` / `收藏了你的内容` / `顶了你的帖子` | 点赞 / 收藏 / 顶帖 | ❌ |

> 「日志标签」（`type_label`）只出现在插件日志里（如 `已注入通知：收到评论 → 会话 …`），不进注入文本。

判定规则：

- **「在对机器人说话」（`directed_at_self`）**：`@` 了本账号；或楼中楼回复的**父评论作者是本账号**
  （接口不返回「这条回复在回谁」，只能用父评论作者近似，`at_users` 能兜住真正的 @）；
  或通知箱里本来就是「回复了我 / 评论了我的帖子」。
- **「正文点名了机器人」（`mentioned_self`）**：只拿**本条内容自己**的正文去比对机器人昵称
  （`channel.bot_name` + 探测到的全局昵称/频道昵称），**不含帖子标题**。

出站 envelope 的 `extra` 带 `notice_category` / `notice_directed` / `notice_mentioned`，
chatter 侧要按「是否在叫我 / 是否点名了我」分档处理兴趣值，可以直接用它们。

> 想进一步降低刷屏/插话：`channel.notices.types`（默认只注入 comment/reply/at/dm）、
> `channel.notices.max_per_poll`（单轮注入上限）、`channel.comments.page_num`（只轮询最新 N 条帖子的评论）、
> `channel.comments.max_age_hours`（只注入多久内的评论），或直接关掉 `channel.comments.enabled`。

### 6.7 频道与版块（板块）：能看见什么、发帖发到哪

三个问题一次说清（均已真机验证）：

| 问题 | 答案 | 用什么 |
|---|---|---|
| bot 能看见有哪些频道吗？ | 能 | `channel_guilds` → 返回「名称 / 频道号 `pd…` / 真实频道 ID」；也可以用通用工具 `channel_read` + `manage.get-my-join-guild-info` |
| 能看见频道里有哪些版块吗？ | 能 | `channel_sections` → 返回「版块名 + 版块 ID」；也可以走 `manage.get-guild-channel-list` |
| 能控制发帖发到哪个版块吗？ | 能 | `channel_publish_feed` 的 `channel_id` 参数：**版块名（如「闲聊」）或版块 ID 都行**；`channel_write` 的 `feed.publish-feed` 同理。不填则用下面的默认版块 |

默认版块的确定顺序（发帖、评论、楼中楼都用它）：

```
channel.channel_id（版块 ID）→ channel.section_name（版块名）→ 自动取「全部」版块 → 自动取第一个版块
```

- **想换默认发帖版块**：写 `channel.section_name = "闲聊"` 即可 —— 不需要知道任何数字 ID
  （版块 ID 在 QQ 客户端里看不到，插件按名字换算）。
- **只想临时发到别处**：让 bot 调 `channel_publish_feed` 时传 `channel_id = "闲聊"`，
  插件会自动换算成该版块的 ID；填错了会返回「频道里没有叫 X 的版块，可选：…」。
- 启动日志会把当前频道的版块清单打出来（`默认发帖版块：全部（12345678901）；本频道共 3 个版块 —— …`），方便核对。

### 6.8 限流治理：进程级指数退避冷却（retCode 153）

背景：平台对 AI Connect 通道有**配额级**频率上限（`153`「接口调用已超过申请的频率上限」）。
实测配额窗口较长（小时级），本地「sleep 70s 重试一次」只会持续撞墙——1.0.5 及之前
CLI/网关两条 transport 各自重试且互不感知，三条轮询路径叠加曾形成连续数小时的重试风暴。

1.1.0 起，所有调用（轮询三路径、出站回复、补昵称回查、工具/动作）共享一份**进程级冷却时钟**
（`cli_client.RateLimitGovernor`，模块级单例）：

- 任一次**真实网络调用**触发 153 → 进入冷却：首档 `cli.rate_limit_sleep`（70s），连续触发按
  `cli.rate_limit_multiplier` 指数增长（70 → 140 → 280 → …），封顶 `cli.rate_limit_ceiling`（默认 30 分钟）；
- 冷却期内**所有** CLI/网关调用直接快速失败（零网络请求），日志形如「限流冷却中（剩余 Ns），跳过调用：…」；
- 轮询主循环在冷却 ≥ 一个 `poll_interval` 时整轮跳过（日志「限流冷却中：轮询暂停到冷却结束」），
  **冷却结束自动恢复，无需重启**；
- 任何一次成功调用即同时清零冷却与连续计数；冷却自然过期后的下一次调用就是恢复探测；
- 例外：`cli.dry_run` 不受冷却拦截（演练模式本就不出网，排障时需要看到完整命令形态）。

配套减负（同样为了少触发 153）：三条轮询路径**错峰**（基线轮后新帖/评论各让出 1/3、2/3 间隔，
顺延加 ±10% 抖动）；补昵称的帖子详情/评论回查带 120 秒 TTL 缓存，冷却期内直接跳过不出网。

## 7. 验证清单

启动日志（`C:\Users\<你>\AppData\Local\MoFox Code\logs\`）应依次出现：

```
plugin_loader    | 发现插件文件夹: ...\plugins\tencent_channel
plugin_manager   | 注册组件: tencent_channel:adapter:tencent_channel_adapter
plugin_manager   | 注册组件: tencent_channel:service:channel_cli
plugin_manager   | ✅ 插件加载成功: tencent_channel v0.1.0
tencent_channel.plugin   | 腾讯频道插件已加载：适配器 1 个、CLI 服务 1 个、工具 8 个、动作 6 个；提示词已刷新 N 个；能力开关：…
adapter_manager  | 适配器启动成功: tencent_channel:adapter:tencent_channel_adapter
tencent_channel.adapter | 腾讯频道适配器已加载（platform=tencent_channel，CLI=...（mode=...））
tencent_channel.adapter | 频道号已解析：pd20589127 → guild_id=1234567890        ← 填了频道号时才有
tencent_channel.adapter | 多频道监听已启用：共 2 个频道 → ...                    ← channel.guilds 多于 1 项时才有
tencent_channel.adapter | tencent-channel-cli 版本信息：1.0.10
tencent_channel.adapter | 当前登录账号：昵称「xxx」，tiny_id=...                 ← 身份自检
tencent_channel.adapter | 默认发帖版块：全部（12345678901）；本频道共 3 个版块 —— 闲聊(12345678902)、公告(12345678903)、全部(12345678901)；可用 channel.section_name 指定默认版块…
tencent_channel.adapter | 已按昵称「xxx」探测到本账号 tiny_id=...（do-reply 的 replier_id）
tencent_channel.adapter | 适配器就绪：频道=1234567890，版块=12345678901（全部），账号=「xxx」，tiny_id=...，bot_qq=...
tencent_channel.adapter | 通知轮询已启动：1 个频道=1234567890，间隔=10min（600s）（新帖=10min（600s），评论=10min（600s）），单轮最多注入 20 条，监听类型=comment,reply,at,dm
```

运行期：

```
tencent_channel.adapter | 首次轮询建立基线：记下 N 条历史通知（默认不注入…）
tencent_channel.adapter | 轮询统计：1 个频道，拉取 N 条，新增 M 条，注入 K 条，忽略 J 条
tencent_channel.adapter | 新帖子轮询：拉取 N 条，新增 M 条，注入 K 条
tencent_channel.adapter | 评论轮询：检查 N 条帖子，新增 M 条，注入 K 条
tencent_channel.adapter | 已注入通知：收到评论 → 会话 帖子 ...
tencent_channel.adapter | 自动回复成功：已楼中楼回复评论（帖子：...，评论：...）
tencent_channel.adapter | 跳过跨路径重复内容（notice 已投过 → comment）：...
tencent_channel.lifecycle | 按 [capabilities] 开关隐藏工具：feed.latest-feeds-detail, ...
```

token 直登 / 凭证清理（开启相关开关时）：

```
tencent_channel.adapter | 已启用 token 直登：token=abcd…（共 N 位）（来源：token_file:...）
tencent_channel.adapter | 当前登录账号：昵称「xxx」，tiny_id=...
tencent_channel.adapter | 本机凭据存储里是**另一个**令牌（共 N 位），与 cli.login 配置的令牌（abcd…）不一致 —— 将改用配置的令牌…
tencent_channel.adapter | 已把访问令牌写入系统密钥链：qq-cli:token, qq-cli:device-id，正在验证登录…
tencent_channel.adapter | 已改用配置的令牌登录（token 直登验证通过）
tencent_channel.adapter | token 直登验证通过：已用配置的访问令牌登录（未扫码）
tencent_channel.adapter | 已清除上次残留的 CLI 登录凭证（cli.login.logout_on_startup=true）
tencent_channel.lifecycle | 已退出腾讯频道 CLI 登录（本机凭证已清除）
```

真机联调：在频道里评论一条帖子 → 日志出现「已注入通知」→ AI 在评论区回复；
再回复该评论 → 验证楼中楼（若字段不足会看到「do-reply 缺字段 …，已降级」并按帖内评论发出）。

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志「tencent-channel-cli 不可用」 | 未安装 / 自动发现没找到 / 只有 `.ps1` 被策略拦 | `npm install -g tencent-channel-cli`；确认 `%APPDATA%\npm` 下确实有 `.cmd` 或平台包 exe；必要时把 `cli.path` 写成绝对路径并设 `cli.mode="exe"` |
| 日志「CLI 未登录或鉴权失败」 | 未登录 / token 过期 | 手动 `login --json` → 扫码 → `login poll-token --json` 后重启；或配置 `cli.login.token_file` 走令牌直登（令牌过期就重跑 `export_login_token.py`） |
| 日志里的账号昵称 / `tiny_id` 是**别的** bot | CLI 凭据存储是全机器共享的：同机另一个实例登录过，本实例的 `login status` 直接用上了它的账号 | 给本实例配 `cli.login.token` / `token_file`（启动会换成自己的身份，日志会打印`已改用配置的令牌登录`）；同机同时跑多个账号请改用 `cli.mode="gateway"`（见 §6.3） |
| 两个实例同时在线时，回复会以对方账号发出 | 同上：每个 CLI 调用都读同一份凭据存储，最后写入者胜 | 给其中一个实例改用 `cli.mode="gateway"`，或把它们分到不同 Windows 用户 / 机器；也可以错开运行 |
| 日志「channel.guilds 未配置，跳过通知轮询」 | 没填监听频道 | 在 `channel.guilds` 填频道号（可直接写 `"pd20589127"`）或真实频道 ID，支持多个 |
| 日志「频道号 xxx 在「我的腾讯频道」里没有匹配项」 | 频道号写错 / 该账号未加入此频道 | 按日志里列出的可选频道改配置；没加入就先 `manage search-and-join --keyword "频道名" --json` |
| 日志「频道列表里没有「频道号」字段，无法自动把 xxx 换成真实 ID」 | CLI 版本差异（列表不带频道号） | 按日志列出的频道，把真实 `guild_id`（纯数字）填进 `channel.guilds` |
| 日志「频道需先加入才能互动」(20006) | 账号未加入该频道 | `manage search-and-join --keyword "频道名" --json` |
| 日志「没有待回复的出站目标」，回复被丢弃 | 该会话**从未成功回复过**（没有可回退的最近目标），或出站 envelope 反解不到本地上下文（如重启后收到旧会话回复） | 属安全保护（宁可不发也不发错位置）；检查 `reply_target_policy`，并设置 `state_path` 提高重启后连续性 |
| bot 回复积极性过高（连别人之间的对话都要插话） | ①注入文本把别人之间的评论/楼中楼也标成「收到回复」并附「（这条在跟你说话）」；②评论的注入文本里还带着**帖子标题**，帖名里的强提及（如「示例Bot…看到请回复」）把它下面每条评论都触发了提及判定 | 已修：只有在「@了我」或「回复挂在**我的**评论下」时才按「被@ / 收到回复」渲染，其余是「在频道里留了言（未提及你）」；评论注入文本**不带帖子标题/正文**，提及只按本条正文判定（见 §6.6）。若仍嫌话多，再收紧 `channel.comments.page_num` / `max_age_hours` / `channel.notices.types` |
| bot 该回却不回（决策子代理判成「系统通知」） | 早期注入文本带平台标签 + 元数据块（`【QQ频道·收到评论】…（评论了你的帖子）` / `所属帖子ID：…` / `时间：…`），子代理把它当成系统消息而非真人发言 | 已修（v1.0.2 之后）：注入文本改成「某人说了一句话」（`在你的帖子下留了言：<正文>`），平台标签与元数据块全部去掉（见 §6.6） |
| 日志「待回复队列为空…回退到本会话最近一次的目标」 | 一个回合里连发了多条消息，而一条通知只登记一个目标（多角色 bot 常见） | 属预期，消息会正常发出并落在同一条帖子/评论下；不想看到可把回复合并成一条 |
| 回复变成帖内评论而不是楼中楼 | `do-reply` 必填字段不全（常见 `replier_id`） | 先看启动日志有没有「已按昵称「xxx」探测到本账号 tiny_id」：没有就说明昵称对不上 —— 把频道里显示的名字填进 `channel.bot_name`（同名多人时会放弃自动探测，此时才需要手工填 `channel.self_tiny_id`，用 `manage guild-member-search --guild-id <频道ID> --keyword <昵称>` 取 `tinyid`） |
| 日志「版块 ID 未配置且自动获取失败」/「接口没返回任何版块」 | 没填 `channel.channel_id`，自动获取也没成功（网络抖动或多频道场景） | 读操作不受影响；发帖/评论需要版块 ID —— 用 `manage get-guild-channel-list --guild-id <频道ID> --json` 查出来填进 `channel.channel_id`（插件每轮会再试一次自动获取） |
| 日志 `retCode=8010 … 字段 createTime 格式不正确` | 接口只认**秒级时间戳**，而通知里给的是 `2026-10-01 10:19:38` 这种人类可读时间 | 已在 `reply_sender.to_epoch_seconds()` 统一归一，并用 `get-feed-detail` 的 `create_time_raw` 校正；若仍出现，确认 `reply.enrich_comment_context=true` |
| 别人发帖但没 @ 我，机器人不理 | 帖子**不产生互动通知**，默认轮询看不到新帖 | 打开 `channel.feeds.enabled = true`（需重启）；新帖会以「在频道里发了新帖：<正文>」注入，回复会作为该帖子的评论发出 |
| 别人在评论区聊天（没 @ 机器人），机器人完全不知道 | 默认只注入「指向机器人」的互动通知 | 打开 `channel.comments.enabled = true`：按 `comments.poll_interval` 轮询最近 `comments.page_num` 条有评论的帖子，把**所有人**的评论/楼中楼都注入（自动跳过自己的、超龄的） |
| 想让频道像 QQ 群一样连续对话 | 默认按帖子/评论分会话 | 打开 `channel.channel_as_group = true`：所有帖子与评论进同一条会话流（`tcf\|<频道ID>`），落点由 `reply_target_policy` 决定 |
| 多频道时回复/工具用错了频道 | 工具/动作不传 `guild_id` 时用主频道（第一个解析成功的） | 调用时显式传 `guild_id`，或调整 `channel.guilds` 顺序 |
| 敏感/破坏性命令被拒（提示"已被配置关闭"） | 这两类默认 `false` | 在 `[capabilities] enabled` 里加能力名（如 `feed.set-feed-essence`）或打开 `sensitive_default` / `danger_default` |
| 破坏性命令仍报失败/等待输入超时 | CLI 对高风险命令可能要求人工确认 | 执行时传 `confirm=true`；必要时再打开 `capabilities.allow_yes_for_danger`（会更危险，谨慎） |
| 提示词里推荐的 `feed.latest-feeds-detail` 调用被拒 | 该命令被归为**敏感（默认关）** | 把 `feed.latest-feeds-detail` / `feed.hot-feeds-detail` 写进 `[capabilities] enabled`，或改用 `feed.get-guild-feeds` |
| 升级 CLI 后新命令调不到 / 参数不认识 | 能力清单是生成快照 | 重跑 `python gen_capability_spec.py --schema <schema.json> [--params <params.json>]` 后重启 |
| 通用工具 `channel_read` / `channel_write` 不在提示词里 | `expose_generic_read` / `expose_generic_write` 关了，或该类别没有任何开启的能力 | 打开 `expose_generic_*`，并确保对应类别至少有一条能力开启 |
| 发帖/评论报「文件不存在」 | 本地图片/视频路径写错；相对路径按**进程工作目录**解析 | 用绝对路径，或确认文件确实存在 |
| `gateway` 模式启动即鉴权失败 | 没有可用令牌 | 配置 `cli.login.token_file`（用 CLI 扫码一次后 `export_login_token.py` 导出），或先在本机完成一次 CLI 扫码登录 |
| `gateway` 模式日志 `retCode=8004 … strconv.ParseUint: parsing "": invalid syntax` | 某个 ID 字段被填成了**空串**（旧版把空的 `channel_id` 拼进 `channelSign`，而网关的 proto 是 uint64，空串直接解析失败） | 已修为「空值整个省略」；同时建议填 `channel.channel_id`（版块 ID，`manage get-guild-channel-list` 可查）—— 发帖/评论/楼中楼也需要它 |
| 发帖发到了「全部」版块，想发到别的版块 | 默认版块未指定，自动取的是「全部」 | 配置 `channel.section_name = "版块名"` 换默认；只想临时换就让 bot 在 `channel_publish_feed` 里传 `channel_id = "版块名"`（不知道有哪些版块就先调 `channel_sections`） |
| 开机日志里出现一段块字符画的二维码 / 提示「请用手机 QQ 扫码」 | 开启了 `cli.login.auto_qrcode` 且当时未登录 | 属预期：扫码即可，插件会自动完成登录并开始轮询；不想看到就关掉该开关 |
| 关机后发现要重新扫码登录 | 开启了 `cli.login.logout_on_shutdown`（这是它的设计目的） | 关掉该开关，或配合 `cli.login.token_file` 免扫码 |
| 明明开了 `cli.login.logout_on_shutdown`，关机后凭证还在 | **强杀/断电/启动器直接结束进程不会发 `ON_STOP`** | 改用 `cli.login.logout_on_startup = true`（强杀也能兜住） |
| `login logout` 之后账号侧仍显示已授权 | 本地退登只清本机凭证，不等同于服务端解绑 | 去 QQ 的授权/设备管理里撤销（CLI 没有远程 revoke 命令） |
| 日志 `@我` 通知里显示「未知用户」 | 通知 payload 本身不带昵称 | 插件会在注入前用 `get-feed-detail`（帖子级）或 `get-feed-comments`（评论级）补昵称，带 120 秒缓存；仍是「未知用户」说明详情里也没有昵称字段 |
| `@我` 通知回复成了「帖内评论」而不是楼中楼 | `@` 通知本身不带评论 ID，只能评论帖子 | 属预期行为；评论/回复类通知才会走 `do-reply` 楼中楼 |
| 收到 153 | 调用过于频繁 | 已触发**进程级指数退避冷却**（全部调用快速失败、不出网，冷却结束自动恢复）；把 `channel.notices.poll_interval` 调大，参数见 `cli.rate_limit_*` |
| 私信回复失败 100707 | 对方未回复前只能发 1 条 | 等对方回复 |
| 日志出现 `setup_hint` / `subscribe_hint` | CLI 建议开启频道通知 | 插件只提示不代跑；非 OpenClaw 环境无法自动推送，本插件走主动轮询，**无需** `notices-on` |

## 9. 安全边界

- **默认只做只读与内容写入**：只读 24 条 + 写入 6 条默认开；敏感 24 条、破坏性 7 条默认关，必须在 `[capabilities]` 显式开启。
- **破坏性能力双重闸门**：不仅要配置开启，执行时还必须 `confirm=true`；只有 `allow_yes_for_danger=true` 时才会附加全局 `--yes`
  （默认**永不**附加）。唯一无条件的 `--yes` 是 `cli.login.logout_on_shutdown` / `cli.login.logout_on_startup` 触发的 `login logout --yes`，属于本机凭证管理。
- **枚举参数被钉死**（`SAFE_GUARDS`）：`do-comment.comment_type` / `do-reply.reply_type` 只允许 `1`（发表；0/2 是删除），
  `do-like.like_type` 只允许 `3/4/5/6`（点赞或取消自己的赞），`do-feed-prefer.action` 只允许 `1/3`，`publish-feed.feed_type` 只允许 `1/2`。
  越界值会被自动收敛并在结果里说明。
- **交互式与运维命令不交给 LLM**：`cli.login*`、`cli.logs`、`notify-daemon` 等 12 条在排除名单里，
  `channel_read` / `channel_write` 直接拒绝执行；登录态由插件自己管理。
- **`content_file` 有目录限制**：只允许读实例 `data/` 目录下的 `.txt` / `.md`，避免被频道里的陌生人诱导读任意本地文件。
  （注意：`image_paths` / `video_paths` / `image_path` 只校验「存在且是文件」，未限制目录。）
- **凭证零泄漏**：日志只输出命令的参数名与结果码，令牌只以掩码形态（`abcd…（共 N 位）`）出现；
  推荐用 `cli.login.token_file` 而不是明文 `cli.login.token`；令牌文件等同登录凭证，注意保管。
- **出站绝不猜位置**：找不到目标上下文或待回复队列为空时直接丢弃并报错。
- **出站去重**：按 `message_id` 120 秒窗口幂等，避免 `MessageSender` 与 CoreSink outgoing 回调双路径重复发送。
- **网关模式的风险自述**：`mode=gateway` 直连的是非公开内部 MCP 端点，Header/UA 与厂商 CLI 有差异，理论上有风控可能；默认仍是 CLI 模式。

## 10. 开发与测试

无框架依赖、可直接导入/单测的模块：`cli_client.py`、`notice_mapping.py`、`reply_sender.py`、`guild_resolver.py`、
`capabilities.py`、`gateway_client.py`、`token_login.py`、`qr_ascii.py`（`capabilities.py` 只依赖 `capability_spec.py` + stdlib）。

```powershell
# 能力清单（升级 CLI 后重跑；schema 来自 `tencent-channel-cli schema -j`）
python gen_capability_spec.py --schema <cli_schema_all.json> [--params <cli_command_params.json>]

# 令牌导出（默认读本机密钥链；不落盘可加 --stdout）
python export_login_token.py [输出路径] [--force]

# 多实例机器：从**本实例**的 config.toml 导出（别读共享密钥链，否则会导出别的实例的令牌）
python export_login_token.py --from-config config/plugins/tencent_channel/config.toml my_token.json

# 多实例部署：校验/同步两份插件源码（--source/--target 必填）
python plugins/tencent_channel/sync_instances.py \
  --source "D:\bots\instance-a\plugins\tencent_channel" \
  --target "D:\bots\instance-b\plugins\tencent_channel" --check   # 退出码 0 = 一致，1 = 有差异
# 不加 --check 则把 source 同步到 target 并自动复核；--mirror 连目标侧多余文件一起清掉；--reverse 对调方向
```

> ⚠️ `sync_instances.py` **只同步插件目录**：各实例的 `config/plugins/tencent_channel/config.toml`
> 与 `login_token.json` 是各自的配置与凭证，**绝不在同步范围内**。
> 该脚本自身也放在插件目录里，所以两边永远持有同一份工具，不会自我漂移。
> 路径必须显式指定（脚本不猜本机目录）；想彻底不漂移也可以把其中一个目录换成 junction
> （见该脚本 docstring 里的代价说明）。
> 另外它是 Python 而不是 `.ps1`：部分 Windows 上 PowerShell 执行策略是 `Restricted`，跑 `.ps1` 会被直接拒绝。

> ⚠️ 默认导出读的是 **全机器共享** 的密钥链 `qq-cli:token`：同机另一个实例登录过，导出的就是**它**的令牌。
> 一台机器跑多个账号时一律加 `--from-config`（脚本会在输出里打印令牌来源，便于核对）。

> 本工作副本内**未包含** `tests/` 与 `scripts\smoke-cli.ps1`（旧版 README 曾引用这两处），
> 因此 `python -m unittest discover -s tests -t .` 在当前副本里无法运行。
> 若你的副本包含 `tests/`（含 `fake_cli.py` 假 CLI 与 `test_plugin_static.py` 静态校验），
> 在插件目录下执行上面那条命令即可；受限沙箱禁止捕获子进程输出时，CLI 层测试会自动 skip 而不是失败。

设计取舍与 CLI 契约细节见仓库 `docs/cli-contract.md`；已核实的命令 schema 见 `docs/cli-schema/`（同样不在本副本内）。

## 11. v1 范围外

推送模式（`notices-on` + OpenClaw daemon）、`--ref` 编号回复、WebSocket 实时推送、多账号、
图片/视频**识别**（上传已支持：发帖图文/视频、评论与楼中楼各 1 张图）、
私信批量群发、正文自动切分与多分片、附件解析、
以及 `gateway` 模式下的扫码登录（首次获取令牌仍需 CLI 一次）。
