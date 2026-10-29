# SCEX 早苗：Minecraft QQ 群服运维与拟人机器人

> 本项目的大部分重构、实现、测试和公开版整理由 **OpenAI Codex** 在服主监督与真实生产
> 反馈下完成。详见 [AI 参与开发声明](AI-GENERATED.md)。

这是为 **Space Creator EX（SCEX）服务器** 特化的 Minecraft Java 服务端 QQ 机器人。
它把 OneBot v11、RCON、服务端事件监控、模组查询与一个低占用的“群友式早苗”
组合成同一条链路：平时像群友一样选择性接话，需要时又能查 TPS、在线玩家、配方、模组、
服务器状态，并在严格权限下执行运维命令。

基础运维框架和部分便携实现来自
[`i0czf/minecraft-server-ops-kit`](https://github.com/i0czf/minecraft-server-ops-kit)，
本仓库是面向 SCEX 真实生产环境的特化与重写版本，不是上游官方发行版。

最新源码同步：[2026-10-29 更新说明](docs/UPDATE-20261029.md)。

- 想知道早苗能做什么：读[能力说明](docs/CAPABILITIES.md)，按使用场景列出例子、权限和数据限制。
- 想了解或继续开发：读[技术实现](docs/ARCHITECTURE.md)，从 QQ 消息到工具执行、记忆和回复逐层说明。
- 想自己部署：看下方快速开始、[配置示例](examples/env.example.ps1)和[安全说明](SECURITY.md)。

能力说明参考了早苗在群里的自我介绍，并逐项对照源码核实。公开文档不收录原聊天、玩家坐标或部署凭据，也不把角色的自述当成能力证明。

## 它解决什么问题

传统群服机器人往往只有两种：纯命令机器人太硬，所有消息都交给大模型又耗资源、容易刷屏，
还会把高危服务器权限暴露给自然语言。本项目把三层能力拆开：

1. 本地规则层负责消息过滤、权限、回声防护、冷却、去重和命令确认；
2. RCON/OneBot/BlueMap 等受限工具层负责真正执行；
3. AI 只在值得回复或需要理解自然语言时介入。

因此它可以长期挂在小型 Windows 主机上，不需要新增 QQ 账号，也不依赖常驻 agent。

## 主要能力

| 场景 | 能做什么 | 关键条件 |
| --- | --- | --- |
| 群友式陪伴 | 短句接话、连续消息合并、引用追问、适时沉默、表情回应；闲聊与技术回答使用不同节奏 | 角色、触发范围、冷却与自动接话按部署配置；短消息最多 8 条是上限 |
| 记忆与理解 | 按群和用户保存个人记忆、持续事件、说话心得；识别“用户说的”和“自己以前说的” | 有来源与遗忘入口；不等于模型训练或自主改代码 |
| QQ 与媒体 | 本群历史/转发阅读、图片和音视频理解、可选静默看图、群文件/公告/相册/荣誉、戳戳与消息回应 | 依赖 OneBot 实际支持的接口；禁言、撤回、精华和相册写入核验 QQ 群身份 |
| 搜索与知识 | 联网搜索/网页正文、B 站公开信息、模组安装与来源、配方用途和机器加工索引 | 来源受限且可能过时；本地索引不保证包含所有运行时动态配方 |
| 服务器诊断 | TPS/MSPT、维度负载、实体与机器统计、强加载、主机指标、网络诊断、玩家流量和 JFR 数据包采样 | 需要对应服务端探针及主机读取权限；采样有成本，统计不自动证明卡顿原因 |
| 玩家与世界 | 生涯统计、任务和研究进度、成就排行、神秘时代世界数据、可选定时位置轨迹 | 区分实时数据与最近存档；玩家坐标、轨迹等敏感查询限制权限 |
| 游戏经济 | SI 市场历史、货币发行销毁、成交及委托、卖出模拟、管理员经济审计和反应堆记录 | 依赖对应 SI 命令/存档格式；不是现实金融接口 |
| 受控运维 | 多服路由、只读日志/配置镜像、备份状态、问题台账、管理通知、受限 RCON 和确认后执行 | 普通查询、低风险写入、高风险确认分开；通用文件写入工具已停用 |

上表描述代码具备的能力，不表示公开示例已经启用全部功能。示例关闭聊天转服、服内事件监控、
静默看图、玩家轨迹采集等可选项；TPS 和状态查询仍能单独使用。完整例子、依赖与边界见
[能力说明](docs/CAPABILITIES.md)。

## 安全边界

- 默认示例不包含任何真实凭据、QQ/群号、服务器地址或玩家数据。
- RCON 密码从独立文件读取；请只让 RCON 监听本机或受控内网。
- 高危命令必须配置 owner/admin，并保留确认流程。
- 贴纸、聊天归档、黑话、记忆、反馈、绑定和遥测都属于本地状态，不应提交。
- 本项目不会分发 LLBot、QQ 客户端、Minecraft、整合包、模组、世界或 BlueMap 瓦片。
- AI 输出不能替代权限检查；不要删除确定性安全门控。

详见 [SECURITY.md](SECURITY.md) 和 [公开发布审计](PUBLIC-RELEASE-AUDIT.md)。

## 快速开始（Windows）

需要 Python 3.11+、一个可用的 OneBot v11 HTTP/上报端，以及启用了 RCON 的 Minecraft
服务端。QQ/LLBot 程序需自行安装。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .\examples\secrets.example.json .\secrets.json
Copy-Item .\examples\servers.example.json .\servers.json
Copy-Item .\examples\qq-admins.example.json .\qq-admins.json
Copy-Item .\examples\env.example.ps1 .\env.ps1
Copy-Item .\start.example.ps1 .\start.ps1
```

然后填写本机配置：

- `secrets.json`：按需填 AI、CurseForge、受限文件服务 token；
- `servers.json`：服务器目录、RCON 主机/端口、密码文件、BlueMap 本机 URL；
- `qq-admins.json`：owner 和管理员；
- `env.ps1`：OneBot API、目标群、机器人 QQ 和开关。

文本模型支持 DeepSeek、原生 Gemini 流式及 `generateContent` 接口，和 `gpt6luna` 命名的 OpenAI 兼容适配。
模型标识需匹配所用服务。切换 Gemini 时把
`SANAE_TEXT_PROVIDER` 设为 `gemini`，配置 `GOOGLE_GEMINI_BASE_URL`、`GEMINI_MODEL` 和
`GEMINI_THINKING_LEVEL`，并只在未跟踪的 `secrets.json` 中保存 `gemini_api_key`。代码不会在
未配置时跨 provider 回落。显式设置 `SANAE_TEXT_FALLBACK=deepseek` 后，符合条件的首次调用
失败可回落到 DeepSeek；已经执行工具的轮次不从头重放。OpenAI 兼容适配使用 `GPT6LUNA_BASE_URL`、
`GPT6LUNA_MODEL` 和私有 `gpt6luna_api_key`。

多群、记忆、静默看图和远程服务的基础配置见[前次说明](docs/UPDATE-20261025.md)，
上次新增开关见[10 月 26 日说明](docs/UPDATE-20261026.md)，本次新增配置见[10 月 29 日说明](docs/UPDATE-20261029.md)。

先在测试群和测试服验证，再运行：

```powershell
.\start.ps1
```

测试不需要连接真实 QQ 或 Minecraft：

```powershell
$env:SCE_BOT_TEST_MODE = '1'
python scripts/test-offline.py
```

## 目录说明

- `llbot_bridge.py`：OneBot 上报入口和总路由；
- `sanae_ai.py`：早苗角色卡、模型调用、自然语言工具与视觉路径；
- `social_*.py`、`sticker_catalog.py`：轻量社交状态与贴纸；
- `conversation_*.py`、`expression_memory.py`、`reply_lessons.py`：对话轮次、个人记忆和回复经验；
- `sanae_identity.py`、`sanae_persona.py`、`silent_vision.py`：身份、角色和静默图片观察；
- `server_registry.py`、`rcon_*.py`：多服注册表与 RCON 安全执行；
- `legacy_monitor.py`、`advancement_localization.py`：SCEX 怀旧服事件与成就本地化；
- `ops_*.py`：运维快照、遥测和确定性查询；
- `server_insight.py`、`player_progress.py`、`world_status.py`、`world_records.py`：服务、玩家及世界查询；
- `server_network.py`、`server_traffic.py`、`player_trail.py`、`market_audit.py`：网络、流量、轨迹和游戏经济审计；
- `qq_interact.py`、`lookup_tools.py`、`backup_status.py`：QQ 互动、资料检索和备份状态；
- `remote_*reader.py`、`remote_mods_mirror.py`、`server_watch.py`：已配置远程服务的采集与观察；
- `bug_reports.py`、`maintenance/`：问题台账、配置审计与备份维护工具；
- `tests/`：离线安全、路由、去重、UTF-8、学习与运维测试。

## 上游、AI 与许可

上游来源、审阅版本与许可边界见 [UPSTREAM-NOTICE.md](UPSTREAM-NOTICE.md)。上游当前
README 的许可证状态与生产快照记录存在差异，因此本仓库保守采用
**PolyForm Noncommercial 1.0.0**：允许非商业使用、修改和再分发，不允许商业用途。

角色卡与轻量社交层借鉴了
[`Derpyu520/qq-bridge`](https://github.com/Derpyu520/qq-bridge) 的群友仿真思路，但没有接入
其 agent/DSH 运行架构。第三方服务和 Minecraft/QQ 相关组件各自遵循其许可证与服务条款。

这是一份真实生产系统的脱敏公开版，仍可能包含只适合 SCEX 的假设。欢迎提交 Issue 和
非商业用途的改进，但请勿附带任何真实 token、QQ 信息、玩家信息或服务器日志。
