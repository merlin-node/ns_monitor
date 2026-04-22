#!/usr/bin/env python3
"""NodeSeek 关键词监控 -> Telegram Bot (NsAlert)
纯面板版:
  - 输入框左下角持久化菜单按钮 -> 弹出控制面板
  - 所有操作通过内联按钮完成
  - 文字命令已废弃 (仅保留 /menu /start /cancel)
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

# ========== 启动配置 ==========
RSS_URL       = os.getenv("RSS_URL", "https://rss.nodeseek.com/")
TG_TOKEN      = os.getenv("TG_BOT_TOKEN", "").strip()
PROXY         = os.getenv("HTTPS_PROXY", "").strip()
DATA_DIR      = Path(os.getenv("DATA_DIR", "/data"))

ALLOWED_IDS   = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

INIT_CHAT_ID  = os.getenv("TG_CHAT_ID", "").strip()
INIT_KEYS     = [k.strip() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
INIT_EXCLUDES = [k.strip() for k in os.getenv("EXCLUDES", "").split(",") if k.strip()]
INIT_INTERVAL = int(os.getenv("INTERVAL", "120"))
INIT_BOARDS   = [b.strip() for b in os.getenv("BOARDS", "trade").split(",") if b.strip()]

DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
SEEN_FILE   = DATA_DIR / "seen.json"
MAX_SEEN    = 500

TG_API  = f"https://api.telegram.org/bot{TG_TOKEN}"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

BOARDS = {
    "trade":       "交易",
    "daily":       "日常",
    "tech":        "技术",
    "info":        "情报",
    "review":      "测评",
    "dev":         "Dev",
    "carpool":     "拼车",
    "promotion":   "推广",
    "life":        "生活",
    "photo":       "贴图",
    "expose":      "曝光",
    "meaningless": "无意义",
    "sandbox":     "沙盒",
}

INTERVAL_PRESETS = [10, 30, 60, 120, 300]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nsmon")

# ========== 配置管理 ==========
_lock = threading.Lock()
_pending = {}  # user_id -> {"action": ..., "chat_id": ..., "menu_msg_id": ...}

def _default_cfg():
    return {
        "chat_id":  INIT_CHAT_ID,
        "keywords": INIT_KEYS,
        "excludes": INIT_EXCLUDES,
        "interval": INIT_INTERVAL,
        "enabled":  True,
        "boards":   list(INIT_BOARDS),
    }

def load_config():
    cfg = _default_cfg()
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("config.json loads failed: %s", e)
    if "boards" not in cfg:
        cfg["boards"] = list(INIT_BOARDS)
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

# ========== Telegram API ==========
def tg_call(method, **params):
    if not TG_TOKEN:
        log.error("no TG_BOT_TOKEN")
        return None
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=70, proxies=PROXIES)
        data = r.json()
        if not data.get("ok"):
            log.warning("TG %s failed: %s", method, data)
        return data
    except Exception as e:
        log.warning("TG %s error: %s", method, e)
        return None

def tg_send(chat_id, text, reply_markup=None, disable_preview=True):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return tg_call("sendMessage", **params)

def tg_edit(chat_id, message_id, text, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return tg_call("editMessageText", **params)

def tg_answer_cb(cb_id, text=None):
    params = {"callback_query_id": cb_id}
    if text:
        params["text"] = text
    return tg_call("answerCallbackQuery", **params)

def setup_tg_ui():
    """注册菜单按钮 + 命令列表 (启动时调用一次)"""
    # 菜单按钮 (输入框左边): 点击后展示 commands 列表
    tg_call("setChatMenuButton", menu_button={"type": "commands"})
    # 只注册一个 /menu, 避免命令列表里出现一堆已废弃的命令
    tg_call("setMyCommands", commands=[
        {"command": "menu", "description": "🎯 打开 NsAlert 控制面板"},
    ])
    log.info("TG UI registered")

# ========== 匹配逻辑 ==========
TAG_RE = re.compile(r"<[^>]+>")

def clean_text(s):
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def match(title, content, keywords, excludes):
    hay = (title + "\n" + content).lower()
    if excludes and any(ex.lower() in hay for ex in excludes):
        return False
    if not keywords:
        return False
    return any(kw.lower() in hay for kw in keywords)

def entry_board(entry):
    tags = entry.get("tags", []) or []
    for t in tags:
        term = (t.get("term") or "").strip()
        if term:
            return term
    cat = entry.get("category")
    if isinstance(cat, str):
        return cat.strip()
    return ""

# ========== RSS 轮询 ==========
def poll_once():
    with _lock:
        cfg = dict(config)
    if not cfg["enabled"]:
        return
    if not cfg["chat_id"]:
        return

    try:
        r = requests.get(RSS_URL, timeout=30, proxies=PROXIES,
                         headers={"User-Agent": "Mozilla/5.0 ns-monitor"})
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception as e:
        log.warning("fetch RSS failed: %s", e)
        return

    subscribed = cfg.get("boards") or []
    new_hits = 0

    for entry in reversed(feed.entries):
        pid = entry.get("id") or entry.get("link")
        if not pid or pid in seen:
            continue
        seen.append(pid)

        if subscribed:
            board = entry_board(entry)
            if board not in subscribed:
                continue

        title   = clean_text(entry.get("title", ""))
        content = clean_text(entry.get("summary", "") or entry.get("description", ""))

        if not match(title, content, cfg["keywords"], cfg["excludes"]):
            continue

        link     = entry.get("link", "")
        author   = entry.get("author", "unknown")
        board    = entry_board(entry)
        board_zh = BOARDS.get(board, board)
        snippet  = content[:200] + ("..." if len(content) > 200 else "")

        msg = (
            f"🔔 <b>NodeSeek · {html.escape(board_zh)}</b>\n\n"
            f"<b>{html.escape(title)}</b>\n"
            f"👤 {html.escape(author)}\n\n"
            f"{html.escape(snippet)}\n\n"
            f"🔗 <a href=\"{html.escape(link)}\">查看原帖</a>"
        )
        tg_send(cfg["chat_id"], msg, disable_preview=False)
        new_hits += 1
        time.sleep(0.5)

    save_seen(seen)
    if new_hits:
        log.info("pushed %d new posts", new_hits)

# ========== 工具 ==========
def fmt_list(items):
    return "、".join(items) if items else "(空)"

def fmt_boards(subscribed):
    if not subscribed:
        return "全站"
    parts = []
    for code in subscribed:
        zh = BOARDS.get(code, code)
        parts.append(f"{code}({zh})")
    return "、".join(parts)

def is_allowed(user_id):
    return (not ALLOWED_IDS) or (user_id in ALLOWED_IDS)

# ========== 面板视图 ==========
def kb(rows):
    return {"inline_keyboard": rows}

def btn(text, data):
    return {"text": text, "callback_data": data}

def view_main():
    with _lock:
        cfg = dict(config)
    status_emoji = "🟢" if cfg["enabled"] else "🔴"
    status_text  = "开启" if cfg["enabled"] else "关闭"
    board_text   = fmt_boards(cfg.get("boards") or [])
    text = (
        "🎯 <b>NsAlert 控制面板</b>\n\n"
        f"状态: {status_emoji} {status_text}　 间隔: {cfg['interval']}秒\n"
        f"关键词: {len(cfg['keywords'])} 个　 排除词: {len(cfg['excludes'])} 个\n"
        f"板块: {board_text}\n"
        f"已去重: {len(seen)} 条"
    )
    toggle_btn = btn("🔕 关闭提醒", "toggle_enabled") if cfg["enabled"] else btn("🔔 开启提醒", "toggle_enabled")
    markup = kb([
        [toggle_btn, btn("📊 刷新状态", "main")],
        [btn("📋 关键词管理", "menu_keys"), btn("🚫 排除词管理", "menu_ex")],
        [btn("📑 板块订阅", "menu_boards"), btn("⚙️ 间隔设置", "menu_interval")],
        [btn("📖 说明书", "menu_guide")],
    ])
    return text, markup

def view_keys():
    with _lock:
        cfg = dict(config)
    ks = cfg["keywords"]
    text = "📋 <b>关键词管理</b>\n\n"
    if ks:
        text += f"当前 ({len(ks)} 个):\n"
        text += "\n".join(f"• <code>{html.escape(k)}</code>" for k in ks)
    else:
        text += "(空) 请先添加关键词, 否则不会有推送"
    markup = kb([
        [btn("➕ 添加", "add_key"), btn("➖ 删除", "del_key_list")],
        [btn("🗑️ 清空全部", "clear_keys_confirm")],
        [btn("⬅️ 返回主菜单", "main")],
    ])
    return text, markup

def view_del_key_list():
    with _lock:
        cfg = dict(config)
    ks = cfg["keywords"]
    text = "➖ <b>删除关键词</b>\n\n点击要删除的关键词:"
    rows = []
    for i in range(0, len(ks), 2):
        row = [btn(f"❌ {ks[i]}", f"del_key|{ks[i]}")]
        if i + 1 < len(ks):
            row.append(btn(f"❌ {ks[i+1]}", f"del_key|{ks[i+1]}"))
        rows.append(row)
    if not ks:
        text += "\n\n(空)"
    rows.append([btn("⬅️ 返回", "menu_keys")])
    return text, kb(rows)

def view_ex():
    with _lock:
        cfg = dict(config)
    exs = cfg["excludes"]
    text = "🚫 <b>排除词管理</b>\n\n"
    text += "命中任一排除词的帖子会被丢弃, 优先级高于关键词\n\n"
    if exs:
        text += f"当前 ({len(exs)} 个):\n"
        text += "\n".join(f"• <code>{html.escape(k)}</code>" for k in exs)
    else:
        text += "(空)"
    markup = kb([
        [btn("➕ 添加", "add_ex"), btn("➖ 删除", "del_ex_list")],
        [btn("🗑️ 清空全部", "clear_ex_confirm")],
        [btn("⬅️ 返回主菜单", "main")],
    ])
    return text, markup

def view_del_ex_list():
    with _lock:
        cfg = dict(config)
    exs = cfg["excludes"]
    text = "➖ <b>删除排除词</b>\n\n点击要删除的排除词:"
    rows = []
    for i in range(0, len(exs), 2):
        row = [btn(f"❌ {exs[i]}", f"del_ex|{exs[i]}")]
        if i + 1 < len(exs):
            row.append(btn(f"❌ {exs[i+1]}", f"del_ex|{exs[i+1]}"))
        rows.append(row)
    if not exs:
        text += "\n\n(空)"
    rows.append([btn("⬅️ 返回", "menu_ex")])
    return text, kb(rows)

def view_boards():
    with _lock:
        cfg = dict(config)
    subs = cfg.get("boards") or []
    text = "📑 <b>板块订阅</b>\n\n"
    if not subs:
        text += "当前模式: <b>🌐 全站抓取</b>\n所有板块的新帖都会被扫描"
    else:
        text += f"已订阅 {len(subs)} 个板块: {fmt_boards(subs)}\n只扫描这些板块的新帖"
    text += "\n\n点击切换订阅 (✅=已订阅):"

    rows = []
    codes = list(BOARDS.keys())
    for i in range(0, len(codes), 2):
        row = []
        for j in range(2):
            if i + j < len(codes):
                code = codes[i + j]
                zh   = BOARDS[code]
                mark = "✅" if (not subs or code in subs) else "⬜"
                row.append(btn(f"{mark} {code} {zh}", f"toggle_board|{code}"))
        rows.append(row)
    rows.append([btn("🌐 切换全站模式", "all_boards")])
    rows.append([btn("⬅️ 返回主菜单", "main")])
    return text, kb(rows)

def view_interval():
    with _lock:
        cfg = dict(config)
    cur = cfg["interval"]
    text = (
        "⚙️ <b>轮询间隔设置</b>\n\n"
        f"当前: <b>{cur} 秒</b>\n\n"
        "常用预设 (秒):"
    )
    row1 = [btn(f"{'✅ ' if cur==n else ''}{n}", f"set_interval|{n}") for n in INTERVAL_PRESETS]
    markup = kb([
        row1,
        [btn("✏️ 自定义", "custom_interval")],
        [btn("⬅️ 返回主菜单", "main")],
    ])
    return text, markup

def view_guide():
    text = (
        "📖 <b>NsAlert 使用说明</b>\n\n"
        "<b>━━━ 这是什么 ━━━</b>\n"
        "自动监控 NodeSeek 新帖, 命中关键词就推送到你的 TG, 适合抢鸡、找货、盯交易。\n\n"
        "<b>━━━ 快速上手 ━━━</b>\n"
        "<b>1.</b> 点 <b>[📋 关键词管理]</b> 添加你关心的词\n"
        "  例: <code>cloudcone</code>、<code>甲骨文</code>、<code>香港</code>\n"
        "<b>2.</b> 点 <b>[🚫 排除词]</b> 过滤噪音\n"
        "  例: <code>求购</code>、<code>测评</code>、<code>中盘</code>\n"
        "<b>3.</b> 点 <b>[📑 板块订阅]</b> 选板块\n"
        "  默认 <code>trade</code> (交易), 也可选全站\n"
        "<b>4.</b> 点 <b>[🔔 开启提醒]</b> 就开始工作了\n\n"
        "<b>━━━ 匹配规则 ━━━</b>\n"
        "• 标题 + 内容同时搜索\n"
        "• 不区分大小写\n"
        "• 多个关键词是<b>或</b>关系 (命中任一即推送)\n"
        "• 排除词优先级高于关键词\n"
        "• 间隔 10-300 秒, 我选10秒"
    )
    markup = kb([[btn("⬅️ 返回主菜单", "main")]])
    return text, markup

def view_confirm(action, title):
    text = f"⚠️ <b>{title}</b>\n\n此操作不可恢复, 确定继续吗?"
    markup = kb([
        [btn("✅ 确定", f"confirm|{action}"), btn("❌ 取消", "main")],
    ])
    return text, markup

# ========== 回调处理 ==========
def handle_callback(cb):
    cb_id   = cb["id"]
    data    = cb.get("data", "")
    user_id = cb["from"]["id"]
    msg     = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    msg_id  = msg.get("message_id")

    if not is_allowed(user_id):
        tg_answer_cb(cb_id, "⛔ 无权限")
        return

    parts = data.split("|", 1)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    new_view = None
    toast = None

    if action == "main":
        new_view = view_main()
    elif action == "menu_keys":
        new_view = view_keys()
    elif action == "menu_ex":
        new_view = view_ex()
    elif action == "menu_boards":
        new_view = view_boards()
    elif action == "menu_interval":
        new_view = view_interval()
    elif action == "menu_guide":
        new_view = view_guide()
    elif action == "del_key_list":
        new_view = view_del_key_list()
    elif action == "del_ex_list":
        new_view = view_del_ex_list()

    elif action == "toggle_enabled":
        with _lock:
            config["enabled"] = not config["enabled"]
            if config["enabled"] and not config.get("chat_id"):
                config["chat_id"] = str(chat_id)
            save_config(config)
        toast = "已开启" if config["enabled"] else "已关闭"
        new_view = view_main()

    elif action == "toggle_board":
        if arg in BOARDS:
            with _lock:
                subs = config.setdefault("boards", [])
                if not subs:
                    config["boards"] = [arg]
                    toast = f"✅ 已关闭全站, 只订阅 {arg}"
                elif arg in subs:
                    subs.remove(arg)
                    toast = f"⬜ 已取消 {arg}"
                else:
                    subs.append(arg)
                    toast = f"✅ 已加入 {arg}"
                save_config(config)
        new_view = view_boards()

    elif action == "all_boards":
        with _lock:
            config["boards"] = []
            save_config(config)
        toast = "🌐 已切换全站模式"
        new_view = view_boards()

    elif action == "set_interval":
        try:
            n = int(arg)
            if n < 10:
                toast = "⚠️ 不能小于 10 秒"
            else:
                with _lock:
                    config["interval"] = n
                    save_config(config)
                toast = f"✅ 已设为 {n} 秒"
        except ValueError:
            toast = "参数错误"
        new_view = view_interval()

    elif action == "custom_interval":
        _pending[user_id] = {"action": "set_interval", "chat_id": chat_id, "menu_msg_id": msg_id}
        tg_answer_cb(cb_id)
        tg_send(chat_id,
                "✏️ 请发送数字 (秒, 最小 10), 例: <code>60</code>\n发 /cancel 取消")
        return

    elif action == "add_key":
        _pending[user_id] = {"action": "add_key", "chat_id": chat_id, "menu_msg_id": msg_id}
        tg_answer_cb(cb_id)
        tg_send(chat_id,
                "➕ 请发送要添加的<b>关键词</b>\n"
                "标题或内容包含它即会被推送\n"
                "发 /cancel 取消")
        return

    elif action == "add_ex":
        _pending[user_id] = {"action": "add_ex", "chat_id": chat_id, "menu_msg_id": msg_id}
        tg_answer_cb(cb_id)
        tg_send(chat_id,
                "➕ 请发送要添加的<b>排除词</b>\n"
                "命中它的帖子会被丢弃\n"
                "发 /cancel 取消")
        return

    elif action == "del_key":
        with _lock:
            if arg in config["keywords"]:
                config["keywords"].remove(arg)
                save_config(config)
                toast = f"已删除: {arg}"
            else:
                toast = "已不存在"
        new_view = view_del_key_list()

    elif action == "del_ex":
        with _lock:
            if arg in config["excludes"]:
                config["excludes"].remove(arg)
                save_config(config)
                toast = f"已删除: {arg}"
            else:
                toast = "已不存在"
        new_view = view_del_ex_list()

    elif action == "clear_keys_confirm":
        new_view = view_confirm("clear_keys", "清空所有关键词")
    elif action == "clear_ex_confirm":
        new_view = view_confirm("clear_ex", "清空所有排除词")

    elif action == "confirm":
        if arg == "clear_keys":
            with _lock:
                config["keywords"] = []
                save_config(config)
            toast = "✅ 已清空关键词"
            new_view = view_keys()
        elif arg == "clear_ex":
            with _lock:
                config["excludes"] = []
                save_config(config)
            toast = "✅ 已清空排除词"
            new_view = view_ex()

    else:
        toast = "未知操作"

    tg_answer_cb(cb_id, toast)
    if new_view:
        text, markup = new_view
        tg_edit(chat_id, msg_id, text, markup)

# ========== 消息处理 (仅 /menu /start /cancel + pending 输入) ==========
def handle_message(msg):
    text = (msg.get("text") or "").strip()
    chat    = msg.get("chat", {})
    chat_id = chat.get("id")
    user_id = msg.get("from", {}).get("id")

    if not is_allowed(user_id):
        log.warning("deny user %s: %s", user_id, text)
        tg_send(chat_id, "⛔ 你没有使用此机器人的权限")
        return

    # 1. pending 输入 (用户在添加关键词/自定义间隔等场景下发送的文字)
    if user_id in _pending and not text.startswith("/"):
        p = _pending.pop(user_id)
        action = p["action"]
        menu_msg_id = p.get("menu_msg_id")
        value = text.strip()

        toast_text = None
        next_view = None

        if action == "add_key":
            with _lock:
                if value in config["keywords"]:
                    toast_text = f"已存在: {value}"
                elif not value:
                    toast_text = "内容为空"
                else:
                    config["keywords"].append(value)
                    save_config(config)
                    toast_text = f"✅ 已添加关键词: {value}"
            next_view = view_keys()

        elif action == "add_ex":
            with _lock:
                if value in config["excludes"]:
                    toast_text = f"已存在: {value}"
                elif not value:
                    toast_text = "内容为空"
                else:
                    config["excludes"].append(value)
                    save_config(config)
                    toast_text = f"✅ 已添加排除词: {value}"
            next_view = view_ex()

        elif action == "set_interval":
            try:
                n = int(value)
                if n < 10:
                    toast_text = "⚠️ 不能小于 10 秒"
                else:
                    with _lock:
                        config["interval"] = n
                        save_config(config)
                    toast_text = f"✅ 已设为 {n} 秒"
            except ValueError:
                toast_text = "请发送数字"
            next_view = view_interval()

        tg_send(chat_id, toast_text or "已完成")
        if menu_msg_id and next_view:
            tg_edit(chat_id, menu_msg_id, next_view[0], next_view[1])
        return

    # 2. 非命令消息直接忽略 (或提示)
    if not text.startswith("/"):
        # 用户随便打字发过来, 提示用面板
        tg_send(chat_id, "👉 请点输入框左边的菜单按钮, 或发送 /menu 打开控制面板")
        return

    # 3. 命令
    cmd = text.split()[0].split("@")[0].lower()

    if cmd == "/cancel":
        if user_id in _pending:
            _pending.pop(user_id)
            tg_send(chat_id, "已取消")
        else:
            tg_send(chat_id, "当前没有待输入操作")
        return

    if cmd in ("/menu", "/start"):
        t, markup = view_main()
        tg_send(chat_id, t, reply_markup=markup)
        return

    # 其他命令一律提示用面板
    tg_send(chat_id, "该命令已废弃, 请点输入框左边的菜单按钮, 或发送 /menu 打开控制面板")

# ========== TG long polling ==========
def tg_updates_loop():
    offset = None
    first = tg_call("getUpdates", timeout=0, offset=-1)
    if first and first.get("ok") and first.get("result"):
        offset = first["result"][-1]["update_id"] + 1

    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            data = tg_call("getUpdates", **params)
            if not data or not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"])
                    except Exception as e:
                        log.exception("handle_callback error: %s", e)
                    continue
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        handle_message(msg)
                    except Exception as e:
                        log.exception("handle_message error: %s", e)
        except Exception as e:
            log.warning("getUpdates error: %s", e)
            time.sleep(5)

# ========== RSS 轮询循环 ==========
def poll_loop():
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
            log.info("init done, %d posts recorded", len(seen))
        except Exception as e:
            log.warning("init fetch failed: %s", e)

    while True:
        try:
            poll_once()
        except Exception as e:
            log.exception("poll error: %s", e)
        with _lock:
            interval = config["interval"]
        time.sleep(max(10, interval))

# ========== main ==========
def main():
    if not TG_TOKEN:
        log.error("TG_BOT_TOKEN required")
        return
    if not ALLOWED_IDS:
        log.warning("no ALLOWED_USER_IDS, anyone can control the bot")

    boards = config.get("boards") or []
    board_info = "all" if not boards else ",".join(boards)
    log.info("NsAlert start, boards=%s, interval=%ds, keys=%s, excludes=%s",
             board_info, config["interval"], config["keywords"], config["excludes"])

    # 注册 TG 菜单按钮 + 命令列表
    try:
        setup_tg_ui()
    except Exception as e:
        log.warning("setup_tg_ui failed: %s", e)

    t = threading.Thread(target=tg_updates_loop, daemon=True)
    t.start()
    poll_loop()

if __name__ == "__main__":
    main()
