# KOOK TTS 语音欢迎

> 成员进入 KOOK 语音频道时，机器人自动加入、**按用户昵称实时合成欢迎语音（TTS）**并播放，播放完毕后立即离开。

适配 **KOOK（开黑啦）** 语音频道的 AstrBot 插件。与"播放固定欢迎音频"的方案不同，本插件按进房用户的昵称现场合成语音——每个人听到的是"欢迎 XX 进入语音频道"。

## 功能

| 能力 | 说明 |
|------|------|
| 昵称 TTS 欢迎 | 进入语音频道 → 查 KOOK 昵称 → 渲染文案 → TTS 合成 → 推流播放 |
| 文案模板 | `{nickname}` 昵称 / `{username}` 用户名 / `{user_id}` / `{channel}` 频道名 / `{guild}` 服务器名 / `{mute_type}` 静音类型，各状态独立模板 |
| 多人冲突策略 | merge 合并播报（窗口可配）/ interrupt 后进打断 / ignore 忽略后进，设置里三选一 |
| 文字频道同步 | 进出动作同步到文字频道，**每个服务器各自绑定**；不配也行，自动识别服务器默认文字频道 |
| 指定用户定制 | 定制**文案**（`/语音欢迎定制`）或定制**音频**（`/语音欢迎定制音频`，本地文件或链接），优先于全局模板与 TTS |
| 多状态播报 | 进入 / 退出有网关事件；静音 / 闭麦 / 解除静音 / 开麦通过轮询服务器静音名单识别（见「已知限制」） |
| 排除自己 | 机器人自身进出语音频道一律不播报；被移出频道时立刻中止推流 |
| TTS 引擎自选 | 优先用 AstrBot 里配置的语音合成提供商（Edge-TTS / OpenAI TTS / GSVI…），没配置则用 edge-tts 兜底 |
| 结果缓存 | 同一句合成文本只合成一次（按文本 + 音色哈希缓存）；昵称 / 频道名 / 服务器信息查询带 TTL 缓存 |

## 工作原理

```
成员进入语音频道
        │
        ▼
KOOK 网关系统消息（type=255, extra.type=joined_channel）
        │  user_id / channel_id 从 extra.body 取出
        ▼
排除自己（bot_id）→ 频道过滤 → 防抖 / 冷却 / 队列 → 预取昵称（并行）
        │
        ├─────────────── 并行 ───────────────┐
        ▼                                    ▼
GET /user/view → 昵称                  POST /voice/join
        │                                    │
        ▼                                    │
渲染文案 → TTS 合成（带缓存）                 │
        │                                    │
        ▼                                    │
尾部静音拼接 → 时长校验                        │
        └─────────────── 汇合 ───────────────┘
        │
        ▼
入房后至少 500 毫秒（准备工作更久则自动吸收）→ FFmpeg Opus RTP 推流
        │                               （尾部拼 2 秒静音，单条 RTP 流）
        ▼
POST /api/v3/voice/leave  →  冷却 3 秒，处理下一次事件
```

延迟优化：音频准备（昵称 / TTS / 拼接）与加入语音频道**并行**执行；入房到开播的
最短间隔默认 500 毫秒（可在设置中精确到毫秒调整），且会被 TTS 等准备工作耗时
自动吸收，因此事件到开播的整体延迟约等于 TTS 合成时间（同一句话命中缓存后
接近 1 秒内）。

日志里的两个时长含义：`开始播报（音频时长：语音 X 秒 + 尾部静音 Y 秒，共 Z 秒）`
是**音频文件时长**（含结尾静音，不是进房后的等待时间）；`播报完成（FFmpeg 推流
墙钟用时 N 秒）` 是推流进程的实际运行时间。

## 环境要求

| 项目 | 要求 |
|------|------|
| AstrBot | ≥ 4.16，KOOK 适配器已启用并在线 |
| FFmpeg | 已安装，需包含 `libopus` 编码器（启动时自动检测） |
| TTS | AstrBot 已配置语音合成提供商，**或**安装 `edge-tts`（`pip install edge-tts`） |
| 网络 | 服务器可访问 KOOK 开放平台，并允许向语音服务器发送 UDP 数据 |

## 安装

1. 将 `astrbot_plugin_kook_tts_welcome/` 放入 AstrBot 的 `data/plugins/`；
2. `pip install edge-tts`（或在 AstrBot 管理面板配置好语音合成提供商）；
3. 安装 FFmpeg（Debian/Ubuntu：`sudo apt install ffmpeg`；Windows：`winget install Gyan.FFmpeg`）；
4. 重启 AstrBot 或在插件管理中重载。

## 配置

在管理面板 → 插件管理 → KOOK TTS 语音欢迎：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 启用语音欢迎 | 开 | |
| 目标语音频道 ID | 空 | 留空自动跟随事件频道 |
| 进入播报文案 | `欢迎 {nickname} 进入 {channel}` | 留空关闭进入播报 |
| 退出播报文案 | `再见，{nickname}，离开 {channel}` | 需同时打开「播放退出语音」 |
| 播放退出语音 | 关 | |
| 多人同时进房策略 | `merge` | `merge` 合并 / `interrupt` 打断 / `ignore` 忽略 |
| 合并窗口 | 2000 毫秒 | 仅 merge 策略生效 |
| 兜底文字频道 ID | 空 | 所有服务器共用；**留空推荐**，交给自动识别 |
| 自动识别服务器默认文字频道 | 开 | 按「服务器默认频道 → 第一个文字频道」找；找不到就不发文字 |
| 文字播报进入 / 退出文案 | 见设置 | 占位符同语音模板，留空关闭对应通知 |
| 静音 / 闭麦轮询间隔 | 0（关闭） | 大于 0 才轮询服务器静音名单；最小 3 秒 |
| 静音 / 解除静音 / 闭麦 / 开麦 文案 | 空 | 留空 = 不播报；填了才会出声 |
| edge-tts 音色 | `zh-CN-XiaoxiaoNeural` | 仅 edge-tts 兜底时使用 |
| 播报音量 | 100 | 0 - 100，设置中可调，试听指令即时生效 |
| 入房到开播的最短间隔 | 500 毫秒 | 精确到毫秒；准备工作更久时自动吸收 |
| 播放冷却 / 尾部停留 | 3s / 2s | |
| FFmpeg 路径 | `ffmpeg` | 未进 PATH 时填绝对路径 |

### 文字播报频道怎么定

多服务器场景不需要填任何频道 ID。解析顺序：

1. **每服务器手动绑定**（`/语音欢迎文字频道 当前`，存 `notify_channels.json`）；
2. **兜底配置**（设置里的「兜底文字频道 ID」，所有服务器共用）；
3. **自动识别**：`GET /guild/view` 的 `default_channel_id`（服务器默认频道），必须是文字频道才用；否则取频道列表里第一个非分类文字频道。

三级都没有 → **该服务器不发文字**，只写诊断日志，绝不会把消息发到别的服务器去。

## 指令

前缀以 AstrBot 全局设置为准（下表以 `/` 为例）。管理员指 AstrBot 配置的管理员。

| 指令 | 权限 | 说明 |
|------|------|------|
| `/语音欢迎` | 所有人 | 查看状态（含本服务器解析到的文字频道） |
| `/语音欢迎帮助` | 所有人 | 帮助 |
| `/语音欢迎模板 <状态> <文案>` | 管理员 | 状态：`enter` `exit` `mute` `unmute` `mic_off` `mic_on`；填「清空」关闭该状态 |
| `/语音欢迎定制 <用户ID> enter\|exit <文案>` | 管理员 | 指定用户定制文案 |
| `/语音欢迎定制音频 <用户ID> <状态> <路径或链接>` | 管理员 | 指定用户直接播放这段音频，不走 TTS |
| `/语音欢迎定制删除 <用户ID>` | 管理员 | 删除该用户的定制文案与音频 |
| `/语音欢迎定制音频删除 <用户ID> [状态\|全部]` | 管理员 | 只删定制音频 |
| `/语音欢迎文字频道 当前\|<频道ID>\|自动\|关闭` | 管理员 | **只对当前服务器生效**；绑定后立刻发测试消息验证发言权限 |
| `/语音欢迎文字频道 列表` | 管理员 | 查看各服务器的绑定 |
| `/语音欢迎文字频道 全局 <频道ID>\|全局 关闭` | 管理员 | 设置所有服务器共用的兜底频道 |
| `/语音欢迎静音播报 开 [秒]\|关\|状态` | 管理员 | 静音 / 闭麦轮询开关与间隔 |
| `/语音欢迎试听 [文案]` | 管理员 | 合成并试听 |
| `/语音欢迎重载` | 管理员 | 重载配置、绑定与定制文案 |

示例：

```
/语音欢迎模板 enter 欢迎 {nickname} 大佬闪亮登场
/语音欢迎模板 mic_off {nickname} 在 {guild} 闭麦了
/语音欢迎定制 123456789 enter 欢迎舰长 {nickname} 回港
/语音欢迎定制音频 123456789 enter https://example.com/vip.mp3
/语音欢迎文字频道 当前
/语音欢迎静音播报 开 5
/语音欢迎试听 欢迎 小明 进入语音频道
```

> **从旧版本升级**：如果配置里的文案还是 v0.1.x 的默认值（`欢迎 {nickname} 进入语音频道` /
> `再见，{nickname}`），插件启动时会自动升级为带 `{channel}` 的新默认文案，日志里会记录
> 「已升级默认文案」。你自己改过的文案不会被动。

## 已知限制（重要）

**KOOK 网关只推送「进入 / 退出语音频道」两类事件**（`joined_channel` / `exited_channel`），body 里只有 `user_id` / `channel_id` / 时间戳。**没有任何静音 / 闭麦事件**。

所以静音 / 闭麦是这样做的：

- KOOK 唯一暴露的是**服务器级**静音名单：`GET /api/v3/guild-mute/list?guild_id=&return_type=detail`，返回 `mic`（被服务器闭麦的人）和 `headset`（被服务器静音的人）；
- 插件按「静音 / 闭麦轮询间隔」轮询该名单，快照比对后派发 `mic_off` / `mic_on` / `mute` / `unmute` 事件，走和进出事件完全相同的播报管线；
- **只能识别「被服务器静音 / 闭麦」**。用户在客户端自己点的静音、自己关的麦，KOOK 不对外暴露，任何第三方机器人都拿不到——这不是本插件的取舍，是平台能力缺口；
- 默认关闭（轮询间隔 0），开启后只在最近 15 分钟有语音活动的服务器上查询，避免无意义请求；
- 静音事件本身不带频道 ID，插件用「该成员最近一次进入的语音频道 → 该服务器最近一次语音活动频道」来定位；都定位不到就跳过，不会乱进频道。

「退出」与「进/出切换」：目前 `exited_channel` 一律视为退出播报，KOOK 不区分「主动退出」与「换频道」。

## TTS 引擎说明

合成优先级：

1. **AstrBot 语音合成提供商**（管理面板 → 服务提供商 → 文本转语音）——用你已配好的 Edge-TTS / OpenAI TTS / GPT-SoVITS / Fish Audio 等；
2. **edge-tts 兜底**——免费无需 Key，音色由 `edge_voice` 配置（默认 `zh-CN-XiaoxiaoNeural`，更多音色见 `edge-tts --list-voices`）。

合成结果缓存在 `data/plugin_data/astrbot_plugin_kook_tts_welcome/tts_cache/`，同一句话不会重复合成；清空该目录即强制重新合成。

## 诊断日志

`data/plugin_data/astrbot_plugin_kook_tts_welcome/logs/kook_tts_welcome.log`（路径见 `/语音欢迎`）。单文件超 1 MB 自动轮转；不写 Bot Token。

常见排查：

- **没播报** → 看日志有没有「收到语音事件」；是否被冷却/播放中忽略；文案是否为空；
- **有播报没声音** → `ffmpeg -encoders | grep opus` 确认 libopus；确认服务器 UDP 出方向放行；
- **合成失败** → 看日志是 AstrBot TTS 还是 edge-tts 失败；edge-tts 需要能访问微软服务。

## 开发

```
astrbot_plugin_kook_tts_welcome/
├── main.py           # 入口：生命周期、事件分发、指令
├── constants.py      # 常量与状态定义
├── utils.py          # 类型转换、子进程、ffprobe
├── kook_api.py       # KOOK HTTP 客户端（语音 / 用户 / 服务器 / 静音名单 / 下载）
├── ffmpeg.py         # RTP 推流命令拼装
├── tts.py            # 昵称解析、文案渲染、TTS 合成与缓存、定制音频
├── notify.py         # 文字频道解析（每服务器绑定 / 兜底 / 自动识别）与发送
├── mute.py           # 静音 / 闭麦轮询与状态派发
├── config.py         # 配置读取与诊断日志
├── hooking.py        # KOOK 适配器事件挂接
├── playback.py       # 播报队列与推流流程
├── resources.py      # Token / FFmpeg 解析
├── _conf_schema.json # WebUI 配置模式
└── metadata.yaml
```

数据目录 `data/plugin_data/astrbot_plugin_kook_tts_welcome/`：

```
├── tts_cache/            # TTS 合成缓存（可安全删除）
├── stream_cache/         # 音频+尾部静音 合成缓存（可安全删除）
├── custom_audio/         # 定制音频（URL 下载的副本）
├── user_templates.json   # 指定用户定制文案与定制音频
├── notify_channels.json  # 服务器 → 文字播报频道 绑定
└── logs/
```

## 致谢

- 语音推流链路（事件挂接 / keep-alive / 尾部静音单流推流等）参考了 [astrbot_plugin_kook_audio_welcome](https://github.com/Hanbaobei/astrbot_plugin_kook_audio_welcome) 的设计，在此致谢；
- 感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot)、[KOOK 开放平台](https://developer.kookapp.cn/doc)、[FFmpeg](https://ffmpeg.org/)。

> 本插件仅供学习与交流使用，请遵守 KOOK 开放平台的服务条款与社区规范。
