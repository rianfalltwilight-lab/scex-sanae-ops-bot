$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

$env:LLBOT_API = 'http://127.0.0.1:3000'
$env:LLBOT_GROUP = 'YOUR_GROUP_ID'
$env:LLBOT_MANAGEMENT_GROUP = 'YOUR_MANAGEMENT_GROUP_ID'
$env:SANAE_BOT_QQ = 'YOUR_BOT_QQ'
$env:LLBOT_SOURCE_IPS = '127.0.0.1'
$env:SCE_SERVER_REGISTRY = Join-Path $PSScriptRoot '..\servers.json'
$env:SANAE_BRIDGE_PORT = '18790'

$env:SOCIAL_LITE_ENABLED = '1'
$env:SOCIAL_LITE_GROUP = $env:LLBOT_GROUP
$env:SOCIAL_LITE_COOLDOWN = '12'
$env:SOCIAL_LITE_SPONTANEOUS = '0'
$env:SOCIAL_LITE_AMBIENT_WEIGHT = '0.1'
$env:SOCIAL_STICKER_MODE = 'auto'
$env:SOCIAL_STICKER_COOLDOWN = '45'

$env:DEEPSEEK_TEXT_MODEL = 'deepseek-v4-flash'
$env:DEEPSEEK_REASONING_EFFORT = 'low'
$env:SANAE_TEXT_PROVIDER = 'deepseek' # 改为 gemini 时走原生 generateContent
$env:GOOGLE_GEMINI_BASE_URL = 'https://generativelanguage.googleapis.com'
$env:GEMINI_MODEL = 'gemini-3.8-flash'
$env:GEMINI_THINKING_LEVEL = 'low'
# GEMINI_API_KEY 不要写入此文件；生产环境请存入私有 secrets.json 的 gemini_api_key。
$env:ZHIPU_VISION_MODEL = 'glm-4.5v-flash'
$env:RELAY_G2S = '1'
