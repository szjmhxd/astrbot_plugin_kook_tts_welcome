"""全局常量与默认值。"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 插件标识
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "astrbot_plugin_kook_tts_welcome"
PLUGIN_VERSION = "v0.1.1"

# 诊断日志单文件上限，超过后轮转为 .log.1
LOG_MAX_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# KOOK 接口
# --------------------------------------------------------------------------- #

KOOK_API_BASE = "https://www.kookapp.cn/api/v3"
# KOOK 网关系统消息的 type 固定为 255，具体事件名在 extra.type 字段里
KOOK_SYSTEM_MESSAGE_TYPE = 255

# 语音频道状态。KOOK 网关只推送进入 / 退出两类事件；
# 静音、闭麦等状态变更官方没有对应事件（见 README「已知限制」）。
STATE_ENTER = "enter"
STATE_EXIT = "exit"
STATE_MUTE = "mute"
STATE_UNMUTE = "unmute"
STATE_MIC_OFF = "mic_off"
STATE_MIC_ON = "mic_on"

KOOK_EVENT_TO_STATE = {
    "joined_channel": STATE_ENTER,
    "exited_channel": STATE_EXIT,
}
# 具备网关事件的状态（其余状态保留模板与分发骨架，等待 KOOK 开放对应事件）
STATES_WITH_EVENTS = frozenset({STATE_ENTER, STATE_EXIT})

# --------------------------------------------------------------------------- #
# 语音推流时序
# --------------------------------------------------------------------------- #

# KOOK 官方要求：长时间断流会回收端口资源，建议每 45 秒调用一次 keep-alive
VOICE_KEEP_ALIVE_INTERVAL = 40
# /voice/leave 之后需要等待 2-3 秒才能重新加入
STALE_VOICE_RELEASE_WAIT = 3
# 加入频道后到开始播放之间的最短间隔（秒）。听众客户端需要一点时间建立
# 语音接收；若 TTS 等准备工作耗时更久，这段时间会被自动吸收，不再额外等待。
DEFAULT_START_DELAY = 0.5
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
# TTS 结果缓存目录
TTS_CACHE_DIR_NAME = "tts_cache"
# 昵称查询缓存有效期（秒）
NICKNAME_CACHE_TTL = 3600
# 每个用户缓存的昵称数量上限（防止无限增长）
NICKNAME_CACHE_MAX = 2048

# 默认文案模板。占位符：{nickname} 服务器昵称、{username} 用户名、{user_id}
DEFAULT_ENTER_TEMPLATE = "欢迎 {nickname} 进入语音频道"
DEFAULT_EXIT_TEMPLATE = "再见，{nickname}"

# edge-tts 兜底音色
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"

# --------------------------------------------------------------------------- #
# 数据目录名
# --------------------------------------------------------------------------- #

USER_TEMPLATES_FILE_NAME = "user_templates.json"
