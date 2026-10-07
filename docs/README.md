# 设计与维护说明（docs）

本目录放**面向维护者**的简短说明；面向使用者的完整手册在仓库根目录的 [README.md](../README.md)。

---

## 1. 职责边界（最重要）

> **插件只负责把消息告诉群聊聊天器。** 回不回、回什么，由 chatter 决定。

- ✅ 属于插件：轮询发现、频道/版块/昵称等 ID 的自动获取、去重水位线、会话路由、
  **出站落点定位**（回到哪条帖子/评论）、能力开关、并发与限流保护。
- ❌ 不属于插件：判断「这条要不要回」、生成回复内容、批量决策、插件内直调 LLM。
- ⚠️ 灰区：`notice_types` / `max_per_poll` / `page_num` / `skip_self` / `max_age_hours`
  是「少搬一点」（输入筛选），可以保留；但**不得**演变成「按内容判断该不该搬」。

改代码前先问：这是**搬运**，还是**替 chatter 做决定**？后者不该写进本插件。

参考（同工作室的 foxzone / QQ 空间插件）：它走的是「插件自己批量调 LLM 决策 + `reply=null` 跳过」，
与本插件边界相反，**不要照搬它的架构**（且它注册的 `qzone_chatter` 在 1.0.0 里是死路径）；
边界内可借鉴的只有搬运层机制（DND 勿扰、发送节流、每帖接力上限、间隔用分钟）。

## 2. 消息流

```
轮询（通知 / 新帖 / 评论，三条独立路径）
  → 归一化 notice（notice_mapping）
  → 去重（水位线 + 跨路径内容指纹）
  → 注入（envelope_for_notice，一条 notice 一条消息）
  → chatter 决策 → 出站 envelope
  → 出站落点（待回复队列按 latest/oldest 取；空则回退本会话最近目标）
  → do-reply（楼中楼）/ do-comment（帖内评论）/ push-group-dm-msg（私信）
```

轮询是**单主循环**（`adapter._poll_loop`）按各路径的到期时间调度，避免多循环叠加慢调用
（历史上同工作室的 foxzone 因在循环里串行慢调用撞上 EventBus 5s 超时，最后改成了绕开事件总线的写法）。

## 3. 注入文本（决定回复行为的核心）

注入给聊天器的内容是**一行「某人说了一句话」** —— 没有平台标签、没有元数据块。
昵称与时间由框架的消息行提供（`【时间】[平台ID] 昵称:名字 [msg_id]： 内容`，
昵称来自 envelope 的 `user_info.user_nickname`，见 `notice_mapping.envelope_for_notice`）：

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

三条硬规则（按踩坑顺序）：

1. **写成「人说的话」**：早期格式是 `【QQ频道·收到评论】<昵称>：<正文>（评论了你的帖子）` +
   `所属帖子ID：…` + `时间：…`。**决策子代理**会把它判成「QQ频道系统的通知消息，并非直接对机器人
   发起的对话」而拒绝回复（真实日志：`08:17:09 default_chatter | 子代理决策: …属于系统通知而非互动消息
   (respond=False)`；判定准则里的「机器博弈/系统消息」「话题无关」两条正对应这种形态）。
2. **评论的注入文本不带帖子标题/正文** —— 否则帖名里的点名（如「XX，看到请回复」）会让该帖下
   每条评论都被判成在叫它；
3. **不带催促语**（「这条在跟你说话」会让它句句都接），只保留 `（未提及你）` / `（提到了你）`
   这类**事实**说明。

判定与携带方式：

- `directed_at_self`：`@` 我 / 楼中楼父评论作者是我 / 通知箱语义（评论了我的帖子、回复了我、私信）；
- `mentioned_self`：只拿**本条自己**的正文比对机器人昵称（`channel.bot_name` + 探测到的昵称）；
- 两者随 envelope 的 `extra.notice_directed` / `extra.notice_mentioned` 传给 chatter，供其自行分档。

## 4. 自动获取（QQ 客户端里看不到的东西不让人填）

| 项 | 来源 | 失败时 |
|---|---|---|
| 真实频道 ID | 频道号 → `manage get-my-join-guild-info`（纯数字原样用） | 日志列出该账号的频道 |
| 版块 ID | `channel.section_name`（名字）→「全部」版块 → 第一个版块 | 警告 + 每轮重试；只影响发帖/评论 |
| 本账号 tiny_id | 按昵称在频道里搜成员（同名唯一时采用） | 警告，可手工填 `channel.self_tiny_id` |
| `bot_id` | `bot_id` → `bot_qq` → 登录账号 tiny_id → 占位 | — |

## 5. 两种调用方式

| 模式 | 说明 |
|---|---|
| `cli.mode="cli"`（默认） | 调本机 `tencent-channel-cli`（Node）。凭证在该 CLI 的机器级密钥链里 |
| `cli.mode="gateway"` | 直接 POST QQ AI Connect 网关，省掉 node 启动开销、不读系统密钥链；多实例/多账号共用一台机器时更干净 |
| `cli.mode="auto"` | 有 CLI 用 CLI，否则回落网关 |

## 6. 测试

在插件目录的**上一级**执行（插件内部一律相对导入，因此必须按包来跑）：

```bash
python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"
```

`tests/` 覆盖：manifest 与代码一致性、注入文本规则、频道号/版块解析（含网关 base64 形态）。
跑测试不需要安装 Neo-MoFox。测试会顺带清理 `__pycache__`/`.mypy_cache`/`.ruff_cache` 等可再生缓存，
因此可以当作打包前的门禁。

## 7. 发布

```bash
mpdt plugin check --fix --level info
mpdt market publish --owner <GitHub 用户名> --release-notes "..."
```

硬性要求：`categories` 只能取 `tool`/`chat`/`fun`/`information`/`moderation` 且只能一个；
import 了 `*_api` 模块就必须写 `api_version`（本插件只用 `service_api`）；发布包不要带 `__pycache__`。
`configs` 必须写成**普通赋值**（`configs = [XxxConfig]`）—— 带类型注解会被工具链判成「未定义配置类」。
插件内部导入必须用**相对导入**（`from .x import y`），绝对导入（`from tencent_channel.x import y`）会被 ImportValidator 判为错误。

### 有意保留的检查警告

`mpdt plugin check` 结果为 **0 error**，以下 warning 是刻意保留的，改动前请先确认不影响运行：

| 警告 | 位置 | 为什么保留 |
|---|---|---|
| `B006` 可变默认参数 | 各 Action 的 `image_paths: list[str] = []` 等 | 这是 LLM 工具/动作的**参数签名**，改成 `None` 会改变暴露给模型的 schema |
| `RUF012` 可变类属性 | `ClassVar` 未标注的类级列表 | 同上，部分属性由框架按约定读取 |
| `BLE001` 盲捕获 `Exception` | 各轮询/解析处（原本带 `# noqa: BLE001` 说明） | 单个通知解析失败不应影响整轮；这些位置都只记日志并继续 |
| `from_platform_message` 返回注解 | `adapter.py` | 插件按 mofox_wire 约定返回 **dict** envelope（不是 `MessageEnvelope` 对象），两个核心实测均可用 |
| `See ... missing-imports`（mypy） | `_compat.py` | 在 Neo-MoFox **之外**跑检查时，核心模块本来就不存在；`_compat.py` 已有 `# type: ignore` |

> 本节原为 README 的「12. 维护提示」，为避免面向用户的 README 过长而移到 docs。
> 内容与代码强相关（改了配置模型 / 组件清单 / 能力清单 / 发布方式时要同步这里）。

## 8. 维护提示（2026-10 从 README 移入；面向改代码的人）

- **职责边界**：见 §1（含 foxzone 的对照）—— 这一条是判断「某功能该不该写在本插件里」的依据。
- **改配置字段**：只改 `config.py`（节 docstring = 分组标题，`description` = 一行注释，`hint` = WebUI 提示），
  然后同步 §5 的表格；**不要手改 `config.toml`** —— 框架每次加载都会按模型签名重渲染它。
- **改配置键名/搬家**：在 `config.py` 的 `_LEGACY_KEYS` 里补 `(旧节, 旧键) → (新节, 新键)`，
  否则老配置里的值会被静默丢弃（框架只按新签名重渲染，不会搬运旧键）。
- **多键并一键**（如 `guild_id` + `guild_number` → `guilds`）：`_LEGACY_KEYS` 只做一对一搬迁，
  这种要另写归并函数（见 `config.py` 的 `_merge_legacy_guilds`）并挂进 `_migrate_legacy_config`；
  解析/拆分逻辑放 `guild_resolver.split_guild_refs`，配置迁移与运行时共用同一实现。
- **自动获取（QQ 里看不到的东西一律别让人填）**：真实频道 ID = `guild_resolver.resolve_guild`；
  版块 ID = `adapter._ensure_section_id`（`manage get-guild-channel-list`，优先「全部」版块）；
  本账号 tiny_id = `adapter._probe_tiny_id_from_members`（昵称候选见 `_nickname_candidates`）；
  `bot_id` = `bot_qq` → 登录账号 tiny_id → 占位值。
  新增这类「平台不可见 ID」时请沿用同一套路：**留空自动取 + 失败只警告不阻断**。
- **加/改嵌套配置节**：`@config_section("notices")` **只能写叶子名**（父节名由 TOML 渲染器
  和 WebUI schema 提取各拼一次）。若写成 `"channel.notices"`，TOML 与插件运行都不受影响，
  但 WebUI 会拼出 `channel.channel.notices`，**在编辑器里保存该插件配置时报
  `Extra inputs are not permitted`（`channel.channel`）**。顶层节名仍写完整名（`channel` / `cli`）。
- **读配置**：统一用 `cli_service.cfg_get(config, "channel.notices", "poll_interval", 600.0)`
  （`section` 支持点号路径）；`token_login._cfg_get` 是保持可独立加载的等价副本。
- **WebUI 兼容（配置页显示/保存）**：`Field(input_type=...)` 只能用前端控件表里的值 ——
  `text` / `textarea` / `number` / `slider` / `switch` / `select` / `multiselect` / `list` /
  `dict` / `object` / `json` / `password` / `email` / `url` / `boolean`。
  **不要用 `"file"`**：前端 `de()` 没有这个分支，会兜底成 TextField，而 TextField 把 `input_type`
  直接写进 `<input type=...>` → 变成浏览器的「选择文件」控件（路径无法回显，选中文件还会把
  `FileList` 写进配置）。路径类字段（`state_path` / `cli.path` / `token_file` …）请用 `"text"`。
  改完建议核对一遍：`extract_schema` 出的节名要等于 TOML 表名，且每个字段的 `input_type` 都在上表里。
- **多实例源码一致性**：改完插件后跑一次
  `python plugins/tencent_channel/sync_instances.py --source <A> --target <B>`（路径必填；
  `--check` 只检查、退出码即结论）。
  它只动插件目录，**不会**碰 `config/plugins/tencent_channel/` 下的配置与令牌；脚本自身也在插件目录里，
  所以两边始终是同一份工具。想彻底不漂移也可以把其中一个插件目录换成 junction（见该脚本 docstring 里的代价说明）。
- **发布到插件市场**：`mpdt plugin check --fix --level info` → `mpdt market publish --owner <GitHub 用户名>`。
  manifest 的硬性要求：`categories` 只能取 `tool`/`chat`/`fun`/`information`/`moderation` 且**只能一个**；
  import 了 `*_api` 模块就必须写 `api_version`（本项目只用到 `service_api`）；发布包不要带 `__pycache__`。
  另外：`configs` 必须写**普通赋值**（`configs = [XxxConfig]`，带注解会被判成「未定义配置类」）；
  插件内部导入一律用**相对导入**（`from .x import y`），绝对导入会被 ImportValidator 判为错误；
  组件名/描述用 `name`/`description`（`service_name`/`config_name` 是核心 legacy 别名，会被告警）。
  检查结果为 0 error，其余 warning（`B006`/`RUF012`/`BLE001`/`from_platform_message` 注解/mypy 缺核心桩）
  都是刻意保留的，逐条理由见上文 §7。
- **离线测试**：`python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"`
  （在插件**上一级**目录执行；`tests/` 全部零框架依赖，跑完会顺手清理可再生缓存）。
- **改组件**：`manifest.json` 的 `include`、`plugin.py` 的 `get_components()` 与 docstring 三处必须一致。
- **升级 CLI**：重跑 `gen_capability_spec.py`，再检查 `capabilities.py` 里的 `KIND_OVERRIDES`（新写命令默认按 sensitive）。
- **新增能力分类/开关语义**：只改 `capabilities.py`（人工可审），不要手改 `capability_spec.py`（生成物）。
- **跨核心兼容**：引用核心枚举/事件时**不要写死**（如 `EventType.BEFORE_TOOL_FILTER`）——
  老核心没有该成员会在 **import 阶段**直接让插件加载失败；用 `getattr`/名字探测 + 降级
  （见 `lifecycle.py` 的 `_resolve_tool_filter_event`）。改完至少在两个核心版本上各跑一次加载。
