#!/usr/bin/env python3
"""NodeSeek 交易区关键词监控 -> Telegram Bot
功能:
  - 标题 + 内容匹配, 多关键词 OR
  - 排除词过滤
  - TG 命令动态管理 (白名单保护)
  - 配置/去重持久化
"""
import os
import re
import json
import time
import html
import logging
import threading
from collections import deque
from pathlib import Path

import feedparser
import requests

# ========== 启动配置 (仅首次启动用, 之后以 config.json 为准) ==========
RSS_URL       = os.getenv("RSS_URL", "https://rss.nodeseek.com/")
CATEGORY      = os.getenv("CATEGORY", "交易")
TG_TOKEN      = os.getenv("TG_BOT_TOKEN", "").strip()
PROXY         = os.getenv("HTTPS_PROXY", "").strip()
DATA_DIR      = Path(os.getenv("DATA_DIR", "/data"))

# 白名单: 逗号分隔的 TG 用户 ID, 只有这些人能发命令
ALLOWED_IDS   = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

# 初始值
INIT_CHAT_ID  = os.getenv("TG_CHAT_ID", "").strip()
INIT_KEYS     = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
INIT_EXCLUDES = [k.strip() for k in os.getenv("EXCLUDES", "").split(",") if k.strip()]
INIT_INTERVAL = int(os.getenv("INTERVAL", "120"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE   = DATA_DIR / "seen.json"
MAX_SEEN    = 500

TG_API  = f"https://api.telegram.org/bot{TG_TOKEN}"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nsmon")

# ========== 配置管理 (线程安全) ==========
_lock = threading.Lock()

def _default_cfg():
    return {
        "chat_id":  INIT_CHAT_ID,
        "keywords": INIT_KEYS,
        "excludes": INIT_EXCLUDES,
        "interval": INIT_INTERVAL,
        "enabled":  True,
    }

def load_config():
    cfg = _default_cfg()
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("config.json 损坏, 使用默认: %s", e)
    return cfg

def save_config(cfg):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)

def load_seen():
    if SEEN_FILE.exists():
        try:
            return deque(json.loads(SEEN_FILE.read_text(encoding="utf-8")), maxlen=MAX_SEEN)
        except Exception:
            pass
    return deque(maxlen=MAX_SEEN)

def save_seen(seen):
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(seen), ensure_ascii=False), encoding="utf-8")
    tmp.replace(SEEN_FILE)

config = load_config()
seen   = load_seen()

# ========== Telegram ==========
def tg_call(method, **params):
    if not TG_TOKEN:
        log.error("未设置 TG_BOT_TOKEN")
        return None
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=70, proxies=PROXIES)
        data = r.json()
        if not data.get("ok"):
            log.warning("TG %s 失败: %s", method, data)
        return data
    except Exception as e:
        log.warning("TG %s 异常: %s", method, e)
        return None

def tg_send(chat_id, text, disable_preview=False):
    return tg_call(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=disable_preview,
    )

# ========== 匹配逻辑 ==========
TAG_RE = re.compile(r"<[^>]+>")

def clean_text(s):
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def match(title, content, keywords, excludes):
    """标题+内容做不区分大小写的 OR 匹配, 命中任一排除词则丢弃"""
    hay = (title + "\n" + content).lower()
    if excludes and any(ex.lower() in hay for ex in excludes):
        return False
    if not keywords:
        return False
    return any(kw.lower() in hay for kw in keywords)

def entry_in_category(entry, category):
    tags = entry.get("tags", []) or []
    for t in tags:
        term = (t.get("term") or "").strip()
        if term == category:
            return True
    # 兜底: 有些 feed 把分类放在 category 字段
    cat = entry.get("category")
    if isinstance(cat, str) and cat.strip() == category:
        return True
    return False

# ========== RSS 轮询 ==========
def poll_once():
    with _lock:
        cfg = dict(config)
    if not cfg["enabled"]:
        return
    if not cfg["chat_id"]:
        return  # 还没绑定接收者

    try:
        r = requests.get(RSS_URL, timeout=30, proxies=PROXIES,
                         headers={"User-Agent": "Mozilla/5.0 ns-monitor"})
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        log.warning("拉取 RSS 失败: %s", e)
        return

    new_hits = 0
    # RSS 通常按时间倒序, 反过来处理以便旧的先推
    for entry in reversed(feed.entries):
        pid = entry.get("id") or entry.get("link")
        if not pid or pid in seen:
            continue

        # 先占位, 即使后面没命中也不再重复检查
        seen.append(pid)

        if not entry_in_category(entry, CATEGORY):
            continue

        title   = clean_text(entry.get("title", ""))
        content = clean_text(entry.get("summary", "") or entry.get("description", ""))

        if not match(title, content, cfg["keywords"], cfg["excludes"]):
            continue

        link   = entry.get("link", "")
        author = entry.get("author", "未知")
        snippet = content[:200] + ("..." if len(content) > 200 else "")
        msg = (
            f"🔔 <b>NodeSeek · {CATEGORY}</b>\n\n"
            f"<b>{html.escape(title)}</b>\n"
            f"👤 {html.escape(author)}\n\n"
            f"{html.escape(snippet)}\n\n"
            f"🔗 <a href=\"{html.escape(link)}\">查看原帖</a>"
        )
        tg_send(cfg["chat_id"], msg)
        new_hits += 1
        time.sleep(0.5)  # 避免触发 TG 限流

    save_seen(seen)
    if new_hits:
        log.info("推送 %d 条新帖", new_hits)

# ========== TG 命令处理 ==========
HELP_TEXT = (
    "<b>可用命令</b>\n"
    "/help - 查看帮助\n"
    "/chat_id - 查看当前 chat_id\n"
    "/add 关键词 - 添加订阅关键词\n"
    "/del 关键词 - 删除关键词\n"
    "/keys - 查看关键词\n"
    "/clear_keys - 清空关键词\n"
    "/add_ex 排除词 - 添加排除词\n"
    "/del_ex 排除词 - 删除排除词\n"
    "/ex_keys - 查看排除词\n"
    "/clear_ex - 清空排除词\n"
    "/interval 秒数 - 设置轮询间隔\n"
    "/open - 开启提醒\n"
    "/close - 关闭提醒\n"
    "/status - 查看当前状态"
)

def fmt_list(items):
    return "、".join(items) if items else "(空)"

def handle_command(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    chat = msg["chat"]
    chat_id = chat["id"]
    user_id = msg.get("from", {}).get("id")

    # 白名单校验
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        log.warning("拒绝非白名单用户 %s: %s", user_id, text)
        tg_send(chat_id, "⛔ 你没有使用此机器人的权限")
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()  # 去掉 @BotName
    arg = parts[1].strip() if len(parts) > 1 else ""

    with _lock:
        cfg = config  # 直接改共享引用
        changed = False
        reply = None

        if cmd == "/help":
            reply = HELP_TEXT
        elif cmd == "/chat_id":
            reply = f"当前 chat_id: <code>{chat_id}</code>\n你的 user_id: <code>{user_id}</code>"
        elif cmd == "/add":
            if not arg:
                reply = "用法: /add 关键词"
            elif arg in cfg["keywords"]:
                reply = f"关键词已存在: {arg}"
            else:
                cfg["keywords"].append(arg)
                changed = True
                reply = f"✅ 已添加关键词: {arg}\n当前: {fmt_list(cfg['keywords'])}"
        elif cmd == "/del":
            if arg in cfg["keywords"]:
                cfg["keywords"].remove(arg)
                changed = True
                reply = f"✅ 已删除关键词: {arg}"
            else:
                reply = f"未找到关键词: {arg}"
        elif cmd == "/keys":
            reply = f"📋 关键词: {fmt_list(cfg['keywords'])}"
        elif cmd == "/clear_keys":
            cfg["keywords"] = []
            changed = True
            reply = "✅ 已清空关键词"
        elif cmd == "/add_ex":
            if not arg:
                reply = "用法: /add_ex 排除词"
            elif arg in cfg["excludes"]:
                reply = f"排除词已存在: {arg}"
            else:
                cfg["excludes"].append(arg)
                changed = True
                reply = f"✅ 已添加排除词: {arg}\n当前: {fmt_list(cfg['excludes'])}"
        elif cmd == "/del_ex":
            if arg in cfg["excludes"]:
                cfg["excludes"].remove(arg)
                changed = True
                reply = f"✅ 已删除排除词: {arg}"
            else:
                reply = f"未找到排除词: {arg}"
        elif cmd == "/ex_keys":
            reply = f"🚫 排除词: {fmt_list(cfg['excludes'])}"
        elif cmd == "/clear_ex":
            cfg["excludes"] = []
            changed = True
            reply = "✅ 已清空排除词"
        elif cmd == "/interval":
            try:
                n = int(arg)
                if n < 10:
                    reply = "⚠️ 间隔不能小于 10 秒"
                else:
                    cfg["interval"] = n
                    changed = True
                    reply = f"✅ 轮询间隔已设为 {n} 秒"
            except ValueError:
                reply = "用法: /interval 120"
        elif cmd == "/open":
            cfg["enabled"] = True
            # 顺便把当前 chat 设为推送目标 (方便第一次使用)
            if str(cfg.get("chat_id")) != str(chat_id):
                cfg["chat_id"] = str(chat_id)
            changed = True
            reply = f"✅ 提醒已开启, 推送到 chat_id: <code>{cfg['chat_id']}</code>"
        elif cmd == "/close":
            cfg["enabled"] = False
            changed = True
            reply = "🔕 提醒已关闭"
        elif cmd == "/status":
            reply = (
                f"<b>运行状态</b>\n"
                f"开关: {'🟢 开启' if cfg['enabled'] else '🔴 关闭'}\n"
                f"推送 chat_id: <code>{cfg.get('chat_id') or '(未设置)'}</code>\n"
                f"间隔: {cfg['interval']} 秒\n"
                f"板块: {CATEGORY}\n"
                f"关键词 ({len(cfg['keywords'])}): {fmt_list(cfg['keywords'])}\n"
                f"排除词 ({len(cfg['excludes'])}): {fmt_list(cfg['excludes'])}\n"
                f"已去重: {len(seen)} 条"
            )
        else:
            reply = "未知命令, 发送 /help 查看帮助"

        if changed:
            save_config(cfg)

    if reply:
        tg_send(chat_id, reply, disable_preview=True)

# ========== TG long polling ==========
def tg_updates_loop():
    offset = None
    # 启动时跳过堆积的旧消息
    first = tg_call("getUpdates", timeout=0, offset=-1)
    if first and first.get("ok") and first.get("result"):
        offset = first["result"][-1]["update_id"] + 1

    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            data = tg_call("getUpdates", **params)
            if not data or not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        handle_command(msg)
                    except Exception as e:
                        log.exception("处理命令出错: %s", e)
        except Exception as e:
            log.warning("getUpdates 异常: %s", e)
            time.sleep(5)

# ========== RSS 轮询循环 ==========
def poll_loop():
    # 启动时先跑一次, 但只记录 ID 不推送 (避免上线瞬间刷屏)
    if not seen:
        try:
            r = requests.get(RSS_URL, timeout=30, proxies=PROXIES,
                             headers={"User-Agent": "Mozilla/5.0 ns-monitor"})
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for e in feed.entries:
                pid = e.get("id") or e.get("link")
                if pid:
                    seen.append(pid)
            save_seen(seen)
            log.info("初始化完成, 记录 %d 条已有帖子, 下次开始推送新帖", len(seen))
        except Exception as e:
            log.warning("初始化拉取失败: %s", e)

    while True:
        try:
            poll_once()
        except Exception as e:
            log.exception("轮询出错: %s", e)
        with _lock:
            interval = config["interval"]
        time.sleep(max(10, interval))

# ========== main ==========
def main():
    if not TG_TOKEN:
        log.error("必须设置 TG_BOT_TOKEN 环境变量")
        return
    if not ALLOWED_IDS:
        log.warning("未设置 ALLOWED_USER_IDS, 任何人都能控制 bot (不安全)")

    log.info("启动 NodeSeek 监控, 板块=%s, 间隔=%ds, 关键词=%s, 排除词=%s",
             CATEGORY, config["interval"], config["keywords"], config["excludes"])

    t = threading.Thread(target=tg_updates_loop, daemon=True)
    t.start()
    poll_loop()

if __name__ == "__main__":
    main()
