# tencent_channel — 腾讯频道（QQ 频道）

把 Neo-MoFox 接入腾讯频道：轮询 **@ / 评论 / 回复 / 私信** 并交给聊天器处理，
同时提供发帖、评论、楼中楼回复、点赞、私信与频道查询工具。

> 本插件只负责**把频道的消息搬给聊天器**（含去重、会话路由、出站落点定位）。
> 回不回、回什么由 chatter 决定 —— 所以「回复积极性」这类问题请调 chatter 的提示词/兴趣值，而不是来这里找开关。

**环境要求**：Neo-MoFox 核心 `>= 1.2.0-rc.1`；`tencent-channel-cli`（Node ≥ 18，**可选**，用网关模式就不需要）。

---

## 1. 安装（三步）

1. **准备通道**（二选一）
   - 装 CLI：`npm install -g tencent-channel-cli`（默认 `cli.mode="auto"`，有 CLI 就用 CLI）
   - 或**免装 node**：配置 `[cli] mode = "gateway"`（直连 QQ AI Connect 网关，见 §3）
2. **放插件**：把本目录整个复制到 MoFox 安装目录的插件目录下 ——
   `<MoFox 安装目录>\plugins\tencent_channel\`
   （是安装目录下的 `plugins\`，不是 `_internal\plugins\`）
3. **重启**，然后填配置（下一节；也可以在 WebUI 的插件配置页里填）

启动日志出现下面这些行就说明起来了：

```
tencent_channel.adapter | 腾讯频道适配器已加载（platform=tencent_channel，CLI=...）
tencent_channel.adapter | 频道号已解析：pd12345678 → guild_id=12345678901234567
tencent_channel.adapter | 默认发帖版块：全部（12345678901）；本频道共 3 个版块 —— …
tencent_channel.adapter | 适配器就绪：频道=…，版块=…，账号=「…」，tiny_id=…
tencent_channel.adapter | 通知轮询已启动：1 个频道=…，间隔=10min（600s）…
```

---

## 2. 配置：最小只要 3 项

配置文件：`config/plugins/tencent_channel/config.toml`（或 WebUI → 插件配置 → 腾讯频道）。

```toml
[channel]
guilds   = "pd12345678"        # ① 频道号：QQ 客户端里能看到的那串 pd…
bot_name = "你的bot昵称"        # ② bot 在频道里显示的昵称
bot_qq   = "123456789"         # ③ bot 的 QQ 号（仅用于 bot_id 与日志）
```

**其余「在 QQ 客户端里看不到」的 ID 都会自动获取**，不用你填：

| 自动获取 | 来源 | 拿不到时 |
|---|---|---|
| 真实频道 ID | 频道号 → `manage get-my-join-guild-info`（填纯数字则原样使用） | 日志会列出该账号的频道 |
| 版块 ID | `manage get-guild-channel-list`，优先「全部」版块 | 只影响发帖/评论；可手填 `channel.section_name` |
| 本账号 tiny_id | 用 `bot_name` 在频道里搜成员（同名唯一时采用） | 可手填 `channel.self_tiny_id` |
| `bot_id` | 依次用 `bot_qq` → 登录账号 tiny_id → 占位值 | — |

> 小提示：`bot_qq` 只作标识 —— 频道接口**不返回 QQ 号**，登录身份由 `[cli.login]` 的令牌决定。

---

## 3. 常用配置速查

| 想做什么 | 怎么配 |
|---|---|
| **发帖到指定版块** | `channel.section_name = "闲聊"`（按**名字**填，无需查 ID）；临时换版块可让 bot 调 `channel_publish_feed` 时传 `channel_id = "版块名"` |
| **整个频道当一条群聊会话** | `channel.channel_as_group = true`（推荐配合下面的评论区轮询） |
| **监听别人发的新帖** | `[channel.feeds] enabled = true` |
| **监听评论区的对话**（含别人之间的） | `[channel.comments] enabled = true`（默认关） |
| **轮询频率** | 三处 `poll_interval`，默认 **600 秒（10 分钟）**；单位是**秒**，别把「10 分钟」填成 10 |
| **只注入部分互动** | `[channel.notices] types = ["at","comment","reply","dm"]`（可加 `like`/`top`/`favorite`） |
| **防刷屏** | `[channel.notices] max_per_poll`、`[channel.comments] page_num` / `max_age_hours` |
| **免装 node（网关模式）** | `[cli] mode = "gateway"`；多实例/多账号同机时它还能避开共享的系统密钥链 |
| **扫一次码以后免扫** | `python export_login_token.py --from-config config/plugins/tencent_channel/config.toml my_token.json`，再把路径填进 `[cli.login] token_file` |
| **只要工具、不轮询** | `channel.poll = false`（整体停用则 `[plugin] enabled = false`） |
| **不提供写操作** | `[capabilities] disabled = ["feed.publish-feed", …]`；`expose_generic_write = false` 隐藏通用写入工具 |
| **改提示词** | `[prompts]` 各字段（WebUI 里可直接编辑，重启生效） |
| **日志噪音** | `[log] poll_summary = false`、`raw_notices = false` |

多频道：`guilds = ["pd111", "pd222"]`（或逗号分隔字符串）——第一个解析成功的作为默认频道。

---

## 4. 常见问题（FAQ）

**Q1. 怎么登录？扫码还是令牌？**
三种方式，任选：
① 手动：`tencent-channel-cli.cmd login --json` 拿链接/二维码 → 扫码 → `login poll-token --json`；
② 自动：`[cli.login] auto_qrcode = true`，启动发现未登录就打印授权链接 + 二维码字符画并后台等扫码；
③ **令牌直登**：扫过一次后导出令牌（见 §3），填 `token_file`，之后重启（含清凭证后）免扫码。

**Q2. 日志在哪？**
`C:\Users\<你>\AppData\Local\MoFox Code\logs\`；关键行见 §1。

**Q3. 提示「频道号已解析」失败 / 看不到我的频道？**
先手动核对：`tencent-channel-cli.cmd manage get-my-join-guild-info --json`。
日志会把该账号的频道列出来 —— 直接把**真实频道 ID**（纯数字）填进 `guilds` 也能用。

**Q4. 发帖发到了「全部」版块，我想发到别的版块？**
版块 ID 在 QQ 里看不到，所以按名字配：`channel.section_name = "闲聊"`；
只想临时换，让 bot 发帖时传 `channel_id = "闲聊"`（先调 `channel_sections` 看有哪些版块）。

**Q5. 它太爱回话了 / 不该接的话也接？**
插件侧已经做到「只写事实、不带催促语、评论不夹带帖子标题」；剩下的取舍在 **chatter**（兴趣值/提示词）。
想更安静：`[channel.notices] types` 只留 `["at","dm"]`、调小 `[channel.comments] page_num`，或直接关掉评论轮询。

**Q6. 有通知但一条都没回？**
看日志：① 有没有「已注入通知…」；② 出站是否报「没有待回复的出站目标」。
后者说明该会话从没有过可回退的目标（例如重启后收到旧会话消息）—— 属安全保护，不会乱发。

**Q7. 一个回合说了三句，只发出去一句？**
已修：现在队列空时会回退到「本会话最近一次成功的目标」，一回合多条都会发出（日志会打 WARNING 说明）。

**Q8. 同机跑两个实例 / 两个账号会不会打架？**
CLI 的凭证存储是**全机器共享**的（`qq-cli:token`）。稳妥做法：其中一个实例用 `[cli] mode = "gateway"`
（不读系统密钥链），另一个用 CLI；或者拆到不同 Windows 用户/机器。每个实例都要各自配 `token_file`。

**Q9. 报 `retCode=8004 … strconv.ParseUint: parsing "": invalid syntax`？**
这是**空字符串 ID** 被塞进网关请求导致的（旧版 bug，已修：空值整个省略）。
如果仍出现，配一下 `channel.channel_id` 或 `channel.section_name` 让它有明确版块。

**Q10. 收到 `153` / 调用过于频繁？**
1.1.0 起走**进程级指数退避熔断**：任一次真实调用触发 153，全部调用（轮询/出站/工具）
一起进入冷却（`cli.rate_limit_sleep` 起步，按 `cli.rate_limit_multiplier` 指数增长，
封顶 `cli.rate_limit_ceiling`，默认 30 分钟）；冷却期内调用直接快速失败、不再出网，
冷却结束后自动恢复，无需重启。同时把 `poll_interval` 调大（默认 600 秒已经很保守）。
注意：**单位是秒**，四个路径都别填成 10。

**Q11. 楼中楼回复变成了帖内评论？**
说明没拿到本账号 tiny_id（楼中楼需要它）。启动日志有没有「已按昵称…探测到本账号 tiny_id」？
没有就把频道里显示的名字填进 `channel.bot_name`；同名多人时用手工值 `channel.self_tiny_id`。

**Q12. WebUI 里改了配置不生效 / 保存报错？**
框架每次加载都会按模型签名**重渲染** `config.toml`，所以不要手写注释指望保留。
如果保存时报 `Extra inputs are not permitted`（`channel.channel`）—— 那是旧版本插件的嵌套节名 bug，升级到 1.0.0+ 即可。

**Q13. 支持发图片/视频吗？**
支持上传（发帖图文/视频、评论与楼中楼各 1 张图）；**识别**别人的图片/视频暂不支持。

---

## 5. 详细文档

| 文档 | 内容 |
|---|---|
| [docs/REFERENCE.md](docs/REFERENCE.md) | **完整手册**：全部字段与默认值、CLI 能力清单、功能模式、三层去重、验证清单、安全边界、开发与测试 |
| [docs/README.md](docs/README.md) | **维护者文档**：职责边界、消息流、注入文本格式、测试与发布流程 |

## 6. 许可

GPL-3.0（见 [LICENSE](LICENSE)）。使用前请自行确认符合腾讯频道相关服务条款。
