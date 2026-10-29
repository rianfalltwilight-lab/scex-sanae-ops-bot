$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

$env:LLBOT_API = 'http://127.0.0.1:3000'
$env:LLBOT_GROUP = 'YOUR_GROUP_ID'
$env:LLBOT_MANAGEMENT_GROUP = 'YOUR_MANAGEMENT_GROUP_ID'
$env:SANAE_BOT_QQ = 'YOUR_BOT_QQ'
$env:SANAE_GROUPS = $env:LLBOT_GROUP # 多群用逗号分隔
$env:SANAE_RELAY_GROUPS = $env:LLBOT_GROUP # 允许转发到服务器的群
# $env:SANAE_HISTORICAL_BOT_QQ = 'OLD_BOT_ID,OTHER_OLD_BOT_ID'
# 如 OneBot 启用鉴权，在本机私有配置中设置 LLBOT_API_TOKEN / LLBOT_CALLBACK_TOKEN。
$env:LLBOT_SOURCE_IPS = '127.0.0.1'
$env:SCE_SERVER_REGISTRY = Join-Path $PSScriptRoot 'servers.json'
$env:SANAE_BRIDGE_PORT = '18790'

$env:SANAE_MENTION_ONLY = '1'
$env:SANAE_NAME_CALL_ENABLED = '0'
$env:SANAE_SILENT_VISION = '0'
$env:SANAE_ENABLE_BACKUP_WATCH = '0'
$env:SANAE_ENABLE_NAST_SYNC = '0'
$env:SANAE_ENABLE_LEGACY_MONITOR = '0'
$env:SANAE_EXPRESSION_LEARNING = '0'
$env:SANAE_PRIVATE_FORWARD = '0'
$env:FUN_MUTE_AI = '0'

$env:SOCIAL_LITE_ENABLED = '1'
$env:SOCIAL_LITE_GROUP = $env:LLBOT_GROUP
$env:SOCIAL_LITE_COOLDOWN = '12'
$env:SOCIAL_LITE_SPONTANEOUS = '0'
$env:SOCIAL_LITE_AMBIENT_WEIGHT = '0.1'
$env:SOCIAL_STICKER_MODE = 'auto'
$env:SOCIAL_STICKER_COOLDOWN = '45'

$env:DEEPSEEK_TEXT_MODEL = 'deepseek-flash'
$env:DEEPSEEK_REASONING_EFFORT = 'low'
$env:SANAE_TEXT_PROVIDER = 'deepseek' # deepseek / gemini / gpt6luna
$env:GOOGLE_GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com'
$env:GEMINI_MODEL = 'YOUR_GEMINI_MODEL'
$env:GEMINI_THINKING_LEVEL = 'high'
$env:GEMINI_STREAM = '1'
$env:GEMINI_RETRIES = '2' # 瞬时服务错误的额外重试，仍受整轮超时限制
$env:SANAE_INTERIM_DELAY = '20' # 较慢请求才发中间等待提示（秒）
# Optional cross-provider fallback; requires a separately configured DeepSeek key.
# $env:SANAE_TEXT_FALLBACK = 'deepseek'
# GEMINI_API_KEY 不要写入此文件；生产环境请存入私有 secrets.json 的 gemini_api_key。
# OpenAI 兼容适配：使用你的服务实际支持的模型；密钥放 secrets.json 的 gpt6luna_api_key。
# $env:GPT6LUNA_BASE_URL = 'https://api.openai.com'
# $env:GPT6LUNA_MODEL = 'YOUR_MODEL_ID'
$env:SANAE_VISION_PROVIDER = 'zhipu' # zhipu / deepseek
$env:ZHIPU_VISION_MODEL = 'glm-5.3-flash'
$env:RELAY_G2S = '0' # 状态/TPS 查询仍可用；按需显式开启聊天转发

# Optional local media tools; leave unset when unused.
# $env:LLBOT_BIN = "<LLBot bin directory>"
# $env:SANAE_FFMPEG = "<ffmpeg executable>"
# $env:SANAE_NODE = "<Node executable>"
# $env:SANAE_SILK_MODULE = "<silk-wasm entry module>"

# Optional server routing and service adapters; see docs/UPDATE-20261025.md.
# $env:SCE_SERVER_EXTRA_REGISTRY = Join-Path $PSScriptRoot 'servers-extra.json'
# $env:SCE_SERVER_GROUP_DEFAULTS = 'YOUR_GROUP_ID:legacy,OTHER_GROUP_ID:remote'
# $env:LEGACY_MONITOR_GROUPS = $env:LLBOT_GROUP
# $env:SANAE_LEGACY_BACKUP_PIPELINE = '<backup pipeline script>'
# $env:SANAE_MSLX_CONTROL_SCRIPT = '<MSLX control script>'
# $env:SANAE_SHOP_MONITOR_ROOT = '<shop monitor data directory>'
# $env:LEGACY_BACKUP_TOOL_DIR = '<backup tool state directory>'

# Player names/coordinates remain private local state; collection is opt-in.
$env:PLAYER_TRAIL_ENABLED = '0'
# $env:PLAYER_TRAIL_SERVER = 'legacy' # 必须是已配置的服务器 ID
# $env:PLAYER_BINDINGS_SERVER = 'legacy'

# Private archives: successful bot replies and incoming messages remain local.
# $env:SOCIAL_REPLY_ARCHIVE = Join-Path $PSScriptRoot 'logs/reply-archive'
$env:SOCIAL_REPLY_ARCHIVE_DAYS = '365'
$env:SOCIAL_ARCHIVE_DAYS = '365'
# Optional monitor destinations (empty/unset means no corresponding notification).
# $env:LEGACY_CRASH_NOTIFY_GROUPS = $env:LLBOT_MANAGEMENT_GROUP
# $env:LEGACY_LAG_ALERT_GROUPS = $env:LLBOT_MANAGEMENT_GROUP
# $env:LEGACY_UPDATE_NOTIFY_GROUPS = $env:LLBOT_MANAGEMENT_GROUP
