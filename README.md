# NodeSeek 关键词监控

监控 NodeSeek 论坛 RSS，命中关键词即推送到 Telegram。纯面板操作，聊天界面始终干净。

## 功能

- 关键词 + 排除词过滤，不区分大小写，支持标题和内容
- 多板块订阅（trade / daily / tech / info / review / dev …）或全站模式
- 内联按钮控制面板，所有操作原地刷新，不刷屏
- 输入框左下角持久化菜单按钮，一点即开
- 白名单保护（`ALLOWED_USER_IDS`），非授权用户无法操作
- 配置和去重队列持久化到磁盘，重启不丢

## 最低要求

- **1C512M** 可跑，建议 **1C1G** 起步更舒服
- 任何支持 Docker 的 Linux 发行版
- 一个 Telegram Bot Token（找 [@BotFather](https://t.me/BotFather) 创建）
- 你的 Telegram user_id（找 [@userinfobot](https://t.me/userinfobot) 查）

## 快速部署

```bash
# 1. 建目录
mkdir -p nodeseek-monitor && cd nodeseek-monitor
mkdir -p data

# 2. 拉配置文件
wget https://raw.githubusercontent.com/merlin-node/ns_monitor/main/docker-compose.yml
wget https://raw.githubusercontent.com/merlin-node/ns_monitor/main/.env.example -O .env

# 3. 编辑 .env, 填入你的 TG_BOT_TOKEN 和 ALLOWED_USER_IDS
nano .env

# 4. 启动
docker compose up -d
docker compose logs -f
```

启动成功后在 Telegram 打开你的 bot，发送 `/menu` 或点输入框左边的菜单按钮即可。

## 环境变量

|变量                |必填|默认                         |说明                   |
|------------------|--|---------------------------|---------------------|
|`TG_BOT_TOKEN`    |✅ |-                          |Telegram Bot Token   |
|`ALLOWED_USER_IDS`|✅ |-                          |允许使用的 user_id，多个用逗号分隔|
|`TG_CHAT_ID`      |❌ |-                          |推送目标（留空则首次启用时自动记录）   |
|`KEYWORDS`        |❌ |-                          |初始关键词，逗号分隔           |
|`EXCLUDES`        |❌ |-                          |初始排除词，逗号分隔           |
|`BOARDS`          |❌ |`trade`                    |初始订阅板块，逗号分隔，留空=全站    |
|`INTERVAL`        |❌ |`120`                      |轮询间隔（秒），最小 10        |
|`RSS_URL`         |❌ |`https://rss.nodeseek.com/`|RSS 源                |
|`HTTPS_PROXY`     |❌ |-                          |HTTP 代理（大陆服务器可能需要）   |

所有配置启动后都可以在面板里修改，`.env` 只是初始值。

## 板块代号

|代号     |中文 |代号         |中文 |
|-------|---|-----------|---|
|trade  |交易 |promotion  |推广 |
|daily  |日常 |life       |生活 |
|tech   |技术 |photo      |贴图 |
|info   |情报 |expose     |曝光 |
|review |测评 |meaningless|无意义|
|dev    |Dev|sandbox    |沙盒 |
|carpool|拼车 |           |   |

## 匹配规则

- 标题和正文都会搜索，命中任一关键词即推送
- 排除词优先级更高，命中任一排除词的帖子直接丢弃
- 所有匹配不区分大小写
- 首次启动不推送历史帖子，只推送启动后的新帖

## 面板说明

进入 bot 发送 `/menu` 即可看到控制面板：

- **🔔 开启 / 关闭提醒** — 总开关
- **📋 关键词管理** — 添加、删除、清空
- **🚫 排除词管理** — 同上
- **📑 板块订阅** — 勾选订阅的板块，支持全站模式
- **⚙️ 间隔设置** — 预设或自定义秒数
- **📖 说明书** — 使用帮助

所有操作在面板里点按钮完成，文字命令已废弃。

## 数据存储

数据挂载在 `./data/` 目录：

- `config.json` — 运行时配置（关键词、排除词、板块、开关、间隔）
- `seen.json` — 已推送帖子的 ID 队列（最多 500 条）

删除这两个文件即可重置状态。

## 更新

```bash
docker compose pull
docker compose up -d
```

## License

MIT
