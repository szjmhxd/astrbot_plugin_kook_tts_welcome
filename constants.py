"""全局常量与默认值。"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 插件标识
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "astrbot_plugin_kook_tts_welcome"
PLUGIN_VERSION = "v0.4.3"

# 诊断日志单文件上限，超过后轮转为 .log.1
LOG_MAX_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# KOOK 接口
# --------------------------------------------------------------------------- #

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
# KOOK 网关系统消息的 type 固定为 255，具体事件名在 extra.type 字段里
KOOK_SYSTEM_MESSAGE_TYPE = 255

# 语音频道状态
STATE_ENTER = "enter"
STATE_EXIT = "exit"
# 静音 / 闭麦：网关事件 add/delete_guild_mute（服务器级），轮询 /guild-mute/list 兜底
# （KOOK 网关事件文档未列，Kook.Net 等实现已验证）
STATE_MUTE = "mute"  # 耳机静音（被服务器静音）
STATE_UNMUTE = "unmute"
STATE_MIC_OFF = "mic_off"  # 麦克风闭麦（被服务器闭麦）
STATE_MIC_ON = "mic_on"

KOOK_EVENT_TO_STATE = {
    "joined_channel": STATE_ENTER,
    "exited_channel": STATE_EXIT,
}
# 有网关事件直接推送的状态
STATES_WITH_EVENTS = frozenset({STATE_ENTER, STATE_EXIT})
# 需要轮询 /guild-mute/list 才能拿到的状态
STATES_WITH_POLLING = frozenset(
    {STATE_MUTE, STATE_UNMUTE, STATE_MIC_OFF, STATE_MIC_ON}
)
STATES_SUPPORTED = STATES_WITH_EVENTS | STATES_WITH_POLLING

# 服务器静音 / 闭麦的网关事件（官方文档未列，Kook.Net 等实现已验证）：
# extra.type = add_guild_mute / delete_guild_mute，body = {user_id, guild_id, type}
# body.type：1 = 麦克风闭麦，2 = 耳机静音
KOOK_MUTE_EVENT_ADD = "add_guild_mute"
KOOK_MUTE_EVENT_DELETE = "delete_guild_mute"
KOOK_MUTE_EVENT_TYPES = frozenset({KOOK_MUTE_EVENT_ADD, KOOK_MUTE_EVENT_DELETE})
KOOK_MUTE_TYPE_MIC = 1
KOOK_MUTE_TYPE_HEADSET = 2

# 静音 / 闭麦状态的中文说明，用于 {mute_type} 占位符
STATE_LABELS = {
    STATE_MUTE: "静音",
    STATE_UNMUTE: "解除静音",
    STATE_MIC_OFF: "闭麦",
    STATE_MIC_ON: "开麦",
}

# --------------------------------------------------------------------------- #
# 语音推流时序
# --------------------------------------------------------------------------- #

# KOOK 官方要求：长时间断流会回收端口资源，建议每 45 秒调用一次 keep-alive
VOICE_KEEP_ALIVE_INTERVAL = 40
# /voice/leave 之后需要等待 2-3 秒才能重新加入
STALE_VOICE_RELEASE_WAIT = 3
# 加入频道后到开始播放之间的最短间隔（毫秒）。听众客户端需要一点时间建立
# 语音接收；若 TTS 等准备工作耗时更久，这段时间会被自动吸收，不再额外等待。
DEFAULT_START_DELAY_MS = 500
# 音频末尾附加静音的时长（预拼进音频，保证整段只有一条 RTP 流）
DEFAULT_LEAVE_DELAY = 2
# FFmpeg 播放超时余量（秒）
PLAYBACK_TIMEOUT_MARGIN = 20
DEFAULT_PLAYBACK_TIMEOUT = 180

# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #

# 合成文本的最大长度，防止异常输入把 TTS 打挂
TTS_TEXT_MAX_CHARS = 120
# edge-tts 兜底合成的重试次数与重试基数（秒）；微软端点偶发抖动常见，一次失败不代表坏了
EDGE_TTS_MAX_ATTEMPTS = 3
EDGE_TTS_RETRY_DELAY = 1.5
# TTS 结果缓存目录
TTS_CACHE_DIR_NAME = "tts_cache"
# 昵称查询缓存有效期（秒）
NICKNAME_CACHE_TTL = 3600
# 每个用户缓存的昵称数量上限（防止无限增长）
NICKNAME_CACHE_MAX = 2048

# 默认文案模板。占位符：{nickname} 服务器昵称、{username} 用户名、
# {user_id} 用户 ID、{channel} 语音频道名、{guild} 服务器名
DEFAULT_ENTER_TEMPLATE = "欢迎 {nickname} 进入 {channel}"
DEFAULT_EXIT_TEMPLATE = "再见，{nickname}，离开 {channel}"

# 历史版本写入用户配置的旧默认文案。启动时若与之完全一致则自动升级为
# 上面的新默认（带 {channel}），用户改过的文案不受影响。
LEGACY_ENTER_TEMPLATE = "欢迎 {nickname} 进入语音频道"
LEGACY_EXIT_TEMPLATE = "再见，{nickname}"

# 文字频道同步播报的默认文案（留空模板 = 关闭对应通知）
DEFAULT_NOTIFY_ENTER = "{nickname} 进入了「{channel}」"
DEFAULT_NOTIFY_EXIT = "{nickname} 退出了「{channel}」"
# 静音 / 闭麦四态的文字播报默认文案。KOOK 只推送「被管理员服务器静音 /
# 闭麦」，自静音 / 自闭麦官方不暴露，文案里直接写明是管理员操作。
DEFAULT_NOTIFY_MUTE = "{nickname} 被管理员服务器静音"
DEFAULT_NOTIFY_UNMUTE = "{nickname} 的服务器静音已解除"
DEFAULT_NOTIFY_MIC_OFF = "{nickname} 被管理员服务器闭麦"
DEFAULT_NOTIFY_MIC_ON = "{nickname} 的服务器闭麦已解除"
LEGACY_NOTIFY_ENTER = "{nickname} 进入了语音频道 {channel}"
LEGACY_NOTIFY_EXIT = "{nickname} 退出了语音频道 {channel}"

# 静音 / 闭麦播报默认关闭（留空 = 不播报）。开启后需要轮询接口，
# 且每次状态变化都要进出语音频道，默认不打扰。
DEFAULT_TEMPLATE_MUTE = ""
DEFAULT_TEMPLATE_UNMUTE = ""
DEFAULT_TEMPLATE_MIC_OFF = ""
DEFAULT_TEMPLATE_MIC_ON = ""

# 静音 / 闭麦轮询间隔（秒），0 = 关闭
DEFAULT_MUTE_POLL_SECONDS = 5
# 服务器没有语音活动后多久停止轮询（秒）
MUTE_POLL_IDLE_SECONDS = 900
# 轮询间隔下限，防止配置过小打爆接口
MUTE_POLL_MIN_SECONDS = 3
# 记录「用户当前所在语音频道」的有效期（秒），用于给静音事件定位频道
USER_VOICE_CHANNEL_TTL = 3600

# 多人同时进房时的语音策略
POLICY_IGNORE = "ignore"  # 播报中 / 冷却中直接忽略后进的
POLICY_INTERRUPT = "interrupt"  # 后进的打断先进
POLICY_MERGE = "merge"  # 合并窗口内的人一起播报
MULTI_USER_POLICIES = (POLICY_IGNORE, POLICY_INTERRUPT, POLICY_MERGE)
DEFAULT_MULTI_USER_POLICY = POLICY_MERGE
DEFAULT_MERGE_WINDOW_MS = 2000

# edge-tts 兜底音色
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

# --------------------------------------------------------------------------- #
# 数据目录名
# --------------------------------------------------------------------------- #

USER_TEMPLATES_FILE_NAME = "user_templates.json"
# 服务器 → 文字播报频道 的映射（指令 /语音欢迎文字频道 写入）
NOTIFY_CHANNELS_FILE_NAME = "notify_channels.json"
# 映射表里表示「本服务器不发送文字」的哨兵值
NOTIFY_DISABLED = "off"
# 用户定制音频的本地副本目录
CUSTOM_AUDIO_DIR_NAME = "custom_audio"

# 服务器信息（名称 / 默认频道 / 频道列表）缓存有效期
GUILD_INFO_CACHE_TTL = 3600
# 「识别不到文字频道」的负缓存有效期，稍短一些，方便用户新建频道后自动生效
TEXT_CHANNEL_NEGATIVE_TTL = 300
