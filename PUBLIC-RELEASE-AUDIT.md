# Public release audit

本仓库由生产快照通过文件白名单导出，而不是从运行目录直接初始化 Git。

明确排除：

- `secrets.json`、RCON 密码和所有 token；
- `servers.json`、真实 QQ 管理员表、群号、QQ 号、服务器域名、内网地址和本机绝对路径；
- `state/`、`logs/`、`backups/`、聊天归档、学习记忆、反馈、绑定关系和运维遥测；
- 世界、模组、客户端、崩溃报告、玩家信息、白名单、OP 列表和封禁列表；
- QQ 登录数据、LLBot 程序、二维码、贴纸素材和下载缓存；
- Minecraft 原版完整语言资源。

发布前门禁：Python 语法检查、离线单元测试、绝对路径/IP/长数字扫描、常见 token
格式扫描、Git 暂存区复核。任何门禁失败都不得推送。

