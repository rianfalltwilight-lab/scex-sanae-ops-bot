# SCEX 早苗：Minecraft QQ 群服运维与拟人机器人

> 本项目的大部分重构、实现、测试和公开版整理由 **OpenAI Codex** 在服主监督与真实生产
> 反馈下完成。详见 [AI 参与开发声明](AI-GENERATED.md)。

这是为 **Space Creator EX（SCEX）服务器** 特化的 Minecraft Java 服务端 QQ 机器人。
它把 OneBot v11、RCON、服务端事件监控、BlueMap、模组查询与一个低占用的“群友式早苗”
组合成同一条链路：平时像群友一样选择性接话，需要时又能查 TPS、在线玩家、配方、模组、
地图和服务器状态，并在严格权限下执行运维命令。

基础运维框架和部分便携实现来自
[`i0czf/minecraft-server-ops-kit`](https://github.com/i0czf/minecraft-server-ops-kit)，
本仓库是面向 SCEX 真实生产环境的特化与重写版本，不是上游官方发行版。

## 它解决什么问题

传统群服机器人往往只有两种：纯命令机器人太硬，所有消息都交给大模型又耗资源、容易刷屏，
还会把高危服务器权限暴露给自然语言。本项目把三层能力拆开：

1. 本地规则层负责消息过滤、权限、回声防护、冷却、去重和命令确认；
2. RCON/OneBot/BlueMap 等受限工具层负责真正执行；
3. AI 只在值得回复或需要理解自然语言时介入。

因此它可以长期挂在小型 Windows 主机上，不需要新增 QQ 账号，也不依赖常驻 agent。

## 主要能力

- **群服互联**：群消息按注册表转发到一个或多个服务器；逐服 best-effort，一服失败不拖住其他服。
- **服内事件**：玩家加入/离开、当前人数、死亡、成就与挑战中文本地化、崩溃摘要及去重。
- **RCON 运维**：`list`、TPS、规则、版本、地址、天气、种子、存盘、备份和受控 `cmd`；高危操作按身份与确认流程限制。
- **自然语言运维**：可以把“给某玩家 OP”“查一下服务器 TPS”解析到受限命令目录，而不是直接执行任意模型输出。
- **BlueMap 截图**：按服务器和玩家定位，检查在线列表、网页 HTTP、PNG 与 OneBot 发送结果。
- **模组与配方**：Modrinth、CurseForge、百科检索、服务端命令目录与配方索引。
- **拟人闲聊**：早苗角色卡、短句/分条节奏、被点名可靠回复、低概率环境回复、可选择沉默。
- **轻量学习闭环**：黑话候选、长期主题记忆、反馈记录、聊天活动日志和有限容量状态。
- **贴纸策略**：按开心、无语、拒绝、震惊、道歉、撒娇等语义选择素材，带冷却与单次上限；素材本身不随仓库分发。
- **媒体理解**：解析 OneBot 图片/引用消息，可选视觉模型描述；文件大小、来源与下载路径受限制。
- **UTF-8 通路**：JSON `ensure_ascii=False`、UTF-8 字节与 BOM 兼容，避免中文经过嵌套 PowerShell 命令后乱码。

详细数据流见 [架构说明](docs/ARCHITECTURE.md)。

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

先在测试群和测试服验证，再运行：

```powershell
.\start.ps1
```

测试不需要连接真实 QQ 或 Minecraft：

```powershell
$env:SCE_BOT_TEST_MODE = '1'
python -m unittest discover -s tests -v
```

## 目录说明

- `llbot_bridge.py`：OneBot 上报入口和总路由；
- `sanae_ai.py`：早苗角色卡、模型调用、自然语言工具与视觉路径；
- `social_*.py`、`slang_learner.py`、`sticker_catalog.py`：轻量社交状态；
- `server_registry.py`、`rcon_*.py`：多服注册表与 RCON 安全执行；
- `legacy_monitor.py`、`advancement_localization.py`：SCEX 怀旧服事件与成就本地化；
- `ops_*.py`：运维快照、遥测和确定性查询；
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

