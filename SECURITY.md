# 安全与隐私

请不要在 Issue、日志或提交中包含真实 QQ 号、群号、OpenID、玩家 UUID、服务器地址、
RCON 密码、AI/API token、OneBot token、崩溃报告或聊天历史。

生产配置默认不进入 Git：`secrets.json`、`servers.json`、`qq-admins.json`、`state/`、
`logs/`、`backups/` 均已忽略。建议 RCON 只监听本机或受控内网，并用独立密码文件；
BlueMap 保持本机访问，不要为了截图功能直接暴露到公网。

高危 RCON 操作应只授予 owner/admin，保留确认码和审计。公开版没有附带 LLBot、QQ
客户端、Minecraft 服务端、整合包、世界存档、模组文件或贴纸素材。

发现可能泄密的问题时，请先私下联系仓库维护者，不要直接发布包含敏感样本的 Issue。

