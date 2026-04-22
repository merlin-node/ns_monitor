#!/usr/bin/env python3
"""NodeSeek 关键词监控 -> Telegram Bot (NsAlert)
单气泡无痕版:
  - 聊天里只有一个面板气泡 + 新帖推送
  - 文字输入流程在面板内原地完成, 无脏消息
  - 反馈行嵌在面板顶部, 下次操作自动消失
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

# ========== 状态 ==========
_lock = threading.Lock()

# user_id -> {"action": "add_key"/"add_ex"/"set_interval"}
_pending = {}

# chat_id -> message_id  (当前活跃面板气泡)
_panel_msg = {}

# chat_id -> str  (面板顶部的临时反馈行, 下次刷新后清空)
_panel_flash = {}

# ========== 配置 ==========
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
            log.debug("TG %s not ok: %s", method, data)
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

def tg_delete(chat_id, message_id):
    """删消息, 失败静默忽略"""
    if not message_id:
        return
    try:
        tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def tg_answer_cb(cb_id, text=None, alert=False):
    params = {"callback_query_id": cb_id}
    if text:
        params["text"] = text
    if alert:
        params["show_alert"] = True
    return tg_call("answerCallbackQuery", **params)

def setup_tg_ui():
    """启动时注册 TG 菜单按钮 + 命令列表"""
    tg_call("setChatMenuButton", menu_button={"type": "commands"})
    tg_call("setMyCommands", commands=[
        {"command": "menu", "description": "🎯 打开 NsAlert 控制面板"},
    ])
    log.info("TG UI registered")

# ========== 匹配 ==========
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
            f"<b>{html.escape(title)}</b>\n\n"
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

def pop_flash(chat_id):
    """取出并清空反馈行"""
    return _panel_flash.pop(chat_id, None)

def set_flash(chat_id, text):
    """设置反馈行 (下次刷新后消失)"""
    _panel_flash[chat_id] = text

# ========== 面板视图 ==========
def kb(rows):
    return {"inline_keyboard": rows}

def btn(text, data):
    return {"text": text, "callback_data": data}

def _flash_prefix(chat_id):
    flash = pop_flash(chat_id)
    if flash:
        return f"{flash}\n━━━━━━━━━━━━━━━━\n\n"
    return ""

def view_main(chat_id):
    with _lock:
        cfg = dict(config)
    status_emoji = "🟢" if cfg["enabled"] else "🔴"
    status_text  = "开启" if cfg["enabled"] else "关闭"
    board_text   = fmt_boards(cfg.get("boards") or [])
    text = (
        f"{_flash_prefix(chat_id)}"
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

def view_keys(chat_id, waiting=False):
    with _lock:
        cfg = dict(config)
    ks = cfg["keywords"]
    body = "📋 <b>关键词管理</b>\n\n"
    if ks:
        body += f"当前 ({len(ks)} 个):\n"
        body += "\n".join(f"• <code>{html.escape(k)}</code>" for k in ks)
    else:
        body += "(空) 请先添加关键词, 否则不会有推送"

    if waiting:
        body += (
            "\n\n━━━━━━━━━━━━━━━━\n"
            "✏️ <b>请发送要添加的关键词</b>\n"
            "标题或内容包含它即会被推送\n"
            "━━━━━━━━━━━━━━━━"
        )
        markup = kb([[btn("❌ 取消", "cancel_input")]])
    else:
        markup = kb([
            [btn("➕ 添加", "add_key"), btn("➖ 删除", "del_key_list")],
            [btn("🗑️ 清空全部", "clear_keys_confirm")],
            [btn("⬅️ 返回主菜单", "main")],
        ])

    text = f"{_flash_prefix(chat_id)}{body}"
    return text, markup

def view_del_key_list(chat_id):
    with _lock:
        cfg = dict(config)
    ks = cfg["keywords"]
    body = "➖ <b>删除关键词</b>\n\n点击要删除的关键词:"
    rows = []
    for i in range(0, len(ks), 2):
        row = [btn(f"❌ {ks[i]}", f"del_key|{ks[i]}")]
        if i + 1 < len(ks):
            row.append(btn(f"❌ {ks[i+1]}", f"del_key|{ks[i+1]}"))
        rows.append(row)
    if not ks:
        body += "\n\n(空)"
    rows.append([btn("⬅️ 返回", "menu_keys")])
    text = f"{_flash_prefix(chat_id)}{body}"
    return text, kb(rows)

def view_ex(chat_id, waiting=False):
    with _lock:
        cfg = dict(config)
    exs = cfg["excludes"]
    body = "🚫 <b>排除词管理</b>\n\n"
    body += "命中任一排除词的帖子会被丢弃, 优先级高于关键词\n\n"
    if exs:
        body += f"当前 ({len(exs)} 个):\n"
        body += "\n".join(f"• <code>{html.escape(k)}</code>" for k in exs)
    else:
        body += "(空)"

    if waiting:
        body += (
            "\n\n━━━━━━━━━━━━━━━━\n"
            "✏️ <b>请发送要添加的排除词</b>\n"
            "命中它的帖子会被丢弃\n"
            "━━━━━━━━━━━━━━━━"
        )
        markup = kb([[btn("❌ 取消", "cancel_input")]])
    else:
        markup = kb([
            [btn("➕ 添加", "add_ex"), btn("➖ 删除", "del_ex_list")],
            [btn("🗑️ 清空全部", "clear_ex_confirm")],
            [btn("⬅️ 返回主菜单", "main")],
        ])

    text = f"{_flash_prefix(chat_id)}{body}"
    return text, markup

def view_del_ex_list(chat_id):
    with _lock:
        cfg = dict(config)
    exs = cfg["excludes"]
    body = "➖ <b>删除排除词</b>\n\n点击要删除的排除词:"
    rows = []
    for i in range(0, len(exs), 2):
        row = [btn(f"❌ {exs[i]}", f"del_ex|{exs[i]}")]
        if i + 1 < len(exs):
            row.append(btn(f"❌ {exs[i+1]}", f"del_ex|{exs[i+1]}"))
        rows.append(row)
    if not exs:
        body += "\n\n(空)"
    rows.append([btn("⬅️ 返回", "menu_ex")])
    text = f"{_flash_prefix(chat_id)}{body}"
    return text, kb(rows)

def view_boards(chat_id):
    with _lock:
        cfg = dict(config)
    subs = cfg.get("boards") or []
    body = "📑 <b>板块订阅</b>\n\n"
    if not subs:
        body += "当前模式: <b>🌐 全站抓取</b>\n所有板块的新帖都会被扫描"
    else:
        body += f"已订阅 {len(subs)} 个板块: {fmt_boards(subs)}\n只扫描这些板块的新帖"
    body += "\n\n点击切换订阅 (✅=已订阅):"

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

    text = f"{_flash_prefix(chat_id)}{body}"
    return text, kb(rows)

def view_interval(chat_id, waiting=False):
    with _lock:
        cfg = dict(config)
    cur = cfg["interval"]
    body = (
        "⚙️ <b>轮询间隔设置</b>\n\n"
        f"当前: <b>{cur} 秒</b>\n\n"
        "常用预设 (秒):"
    )

    if waiting:
        body += (
            "\n\n━━━━━━━━━━━━━━━━\n"
            "✏️ <b>请发送自定义秒数</b> (最小 10)\n"
            "例如: <code>60</code>\n"
            "━━━━━━━━━━━━━━━━"
        )
        markup = kb([[btn("❌ 取消", "cancel_input")]])
    else:
        row1 = [btn(f"{'✅ ' if cur==n else ''}{n}", f"set_interval|{n}") for n in INTERVAL_PRESETS]
        markup = kb([
            row1,
            [btn("✏️ 自定义", "custom_interval")],
            [btn("⬅️ 返回主菜单", "main")],
        ])

    text = f"{_flash_prefix(chat_id)}{body}"
    return text, markup

def view_guide(chat_id):
    body = (
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
        "• 间隔 10-300 秒, 建议 60 秒"
    )
    text = f"{_flash_prefix(chat_id)}{body}"
    return text, kb([[btn("⬅️ 返回主菜单", "main")]])

def view_confirm(chat_id, action, title):
    body = f"⚠️ <b>{title}</b>\n\n此操作不可恢复, 确定继续吗?"
    text = f"{_flash_prefix(chat_id)}{body}"
    return text, kb([
        [btn("✅ 确定", f"confirm|{action}"), btn("❌ 取消", "main")],
    ])

# ========== 面板状态路由 ==========
def render_current_view(chat_id, user_id):
    """根据用户当前 pending 状态决定显示哪个视图"""
    p = _pending.get(user_id)
    if p:
        action = p["action"]
        if action == "add_key":
            return view_keys(chat_id, waiting=True)
        if action == "add_ex":
            return view_ex(chat_id, waiting=True)
        if action == "set_interval":
            return view_interval(chat_id, waiting=True)
    # 默认回到主菜单
    return view_main(chat_id)

def send_new_panel(chat_id, user_id):
    """发新面板到底部, 并记录 msg_id (删除旧面板由调用者负责)"""
    text, markup = render_current_view(chat_id, user_id)
    resp = tg_send(chat_id, text, reply_markup=markup)
    if resp and resp.get("ok"):
        new_id = resp["result"]["message_id"]
        _panel_msg[chat_id] = new_id
        return new_id
    return None

def refresh_panel(chat_id, user_id, view_fn=None, *view_args):
    """就地刷新面板 (editMessageText). view_fn 可选, 不传就用当前 pending 状态"""
    msg_id = _panel_msg.get(chat_id)
    if not msg_id:
        # 没有面板, 新发一个
        send_new_panel(chat_id, user_id)
        return
    if view_fn:
        text, markup = view_fn(chat_id, *view_args)
    else:
        text, markup = render_current_view(chat_id, user_id)
    resp = tg_edit(chat_id, msg_id, text, markup)
    if resp and not resp.get("ok"):
        # 编辑失败 (可能面板被删了), 重发
        _panel_msg.pop(chat_id, None)
        send_new_panel(chat_id, user_id)

# ========== callback 处理 ==========
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

    # 如果点的是过期面板的按钮 (不是当前 _panel_msg), 提示后重发面板
    if _panel_msg.get(chat_id) and msg_id != _panel_msg.get(chat_id):
        tg_answer_cb(cb_id, "面板已过期, 请发 /menu 重新打开")
        return
    # 如果没有记录的面板 id, 把当前这个消息当成面板 (兼容 VPS 重启后的老面板)
    if chat_id not in _panel_msg:
        _panel_msg[chat_id] = msg_id

    parts = data.split("|", 1)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    toast = None
    next_view = None  # (view_fn, *args)

    # ---- 导航 ----
    if action == "main":
        # 清除 pending (如果有)
        _pending.pop(user_id, None)
        next_view = (view_main,)
    elif action == "menu_keys":
        _pending.pop(user_id, None)
        next_view = (view_keys, False)
    elif action == "menu_ex":
        _pending.pop(user_id, None)
        next_view = (view_ex, False)
    elif action == "menu_boards":
        next_view = (view_boards,)
    elif action == "menu_interval":
        _pending.pop(user_id, None)
        next_view = (view_interval, False)
    elif action == "menu_guide":
        next_view = (view_guide,)
    elif action == "del_key_list":
        next_view = (view_del_key_list,)
    elif action == "del_ex_list":
        next_view = (view_del_ex_list,)

    # ---- 进入等待输入状态 ----
    elif action == "add_key":
        _pending[user_id] = {"action": "add_key"}
        next_view = (view_keys, True)
    elif action == "add_ex":
        _pending[user_id] = {"action": "add_ex"}
        next_view = (view_ex, True)
    elif action == "custom_interval":
        _pending[user_id] = {"action": "set_interval"}
        next_view = (view_interval, True)

    # ---- 取消输入 ----
    elif action == "cancel_input":
        p = _pending.pop(user_id, None)
        set_flash(chat_id, "⬜ 已取消")
        if p:
            if p["action"] == "add_key":
                next_view = (view_keys, False)
            elif p["action"] == "add_ex":
                next_view = (view_ex, False)
            elif p["action"] == "set_interval":
                next_view = (view_interval, False)
            else:
                next_view = (view_main,)
        else:
            next_view = (view_main,)

    # ---- 开关 ----
    elif action == "toggle_enabled":
        with _lock:
            config["enabled"] = not config["enabled"]
            if config["enabled"] and not config.get("chat_id"):
                config["chat_id"] = str(chat_id)
            save_config(config)
        toast = "已开启" if config["enabled"] else "已关闭"
        next_view = (view_main,)

    # ---- 板块 ----
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
        next_view = (view_boards,)

    elif action == "all_boards":
        with _lock:
            config["boards"] = []
            save_config(config)
        toast = "🌐 已切换全站模式"
        next_view = (view_boards,)

    # ---- 预设间隔 ----
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
        next_view = (view_interval, False)

    # ---- 删关键词/排除词 ----
    elif action == "del_key":
        with _lock:
            if arg in config["keywords"]:
                config["keywords"].remove(arg)
                save_config(config)
                toast = f"已删除: {arg}"
            else:
                toast = "已不存在"
        next_view = (view_del_key_list,)

    elif action == "del_ex":
        with _lock:
            if arg in config["excludes"]:
                config["excludes"].remove(arg)
                save_config(config)
                toast = f"已删除: {arg}"
            else:
                toast = "已不存在"
        next_view = (view_del_ex_list,)

    # ---- 清空确认 ----
    elif action == "clear_keys_confirm":
        next_view = (view_confirm, "clear_keys", "清空所有关键词")
    elif action == "clear_ex_confirm":
        next_view = (view_confirm, "clear_ex", "清空所有排除词")
    elif action == "confirm":
        if arg == "clear_keys":
            with _lock:
                config["keywords"] = []
                save_config(config)
            toast = "✅ 已清空关键词"
            next_view = (view_keys, False)
        elif arg == "clear_ex":
            with _lock:
                config["excludes"] = []
                save_config(config)
            toast = "✅ 已清空排除词"
            next_view = (view_ex, False)

    else:
        toast = "未知操作"

    tg_answer_cb(cb_id, toast)
    if next_view:
        view_fn = next_view[0]
        args = next_view[1:]
        text, markup = view_fn(chat_id, *args)
        resp = tg_edit(chat_id, msg_id, text, markup)
        if resp and not resp.get("ok"):
            _panel_msg.pop(chat_id, None)
            send_new_panel(chat_id, user_id)

# ========== 消息处理 ==========
def handle_message(msg):
    text = (msg.get("text") or "").strip()
    chat    = msg.get("chat", {})
    chat_id = chat.get("id")
    user_id = msg.get("from", {}).get("id")
    user_msg_id = msg.get("message_id")

    if not is_allowed(user_id):
        log.warning("deny user %s: %s", user_id, text)
        tg_send(chat_id, "⛔ 你没有使用此机器人的权限")
        return

    # ---- pending 输入 ----
    if user_id in _pending and not text.startswith("/"):
        p = _pending[user_id]
        action = p["action"]
        value = text.strip()

        # 先删用户消息
        tg_delete(chat_id, user_msg_id)

        # 处理输入
        ok = False
        if action == "add_key":
            with _lock:
                if not value:
                    set_flash(chat_id, "⚠️ 内容为空")
                elif value in config["keywords"]:
                    set_flash(chat_id, f"⚠️ 已存在: {html.escape(value)}")
                else:
                    config["keywords"].append(value)
                    save_config(config)
                    set_flash(chat_id, f"✅ 已添加关键词: {html.escape(value)}")
                    ok = True
            _pending.pop(user_id, None)
            refresh_panel(chat_id, user_id, view_keys, False)
            return

        if action == "add_ex":
            with _lock:
                if not value:
                    set_flash(chat_id, "⚠️ 内容为空")
                elif value in config["excludes"]:
                    set_flash(chat_id, f"⚠️ 已存在: {html.escape(value)}")
                else:
                    config["excludes"].append(value)
                    save_config(config)
                    set_flash(chat_id, f"✅ 已添加排除词: {html.escape(value)}")
                    ok = True
            _pending.pop(user_id, None)
            refresh_panel(chat_id, user_id, view_ex, False)
            return

        if action == "set_interval":
            try:
                n = int(value)
                if n < 10:
                    set_flash(chat_id, "⚠️ 不能小于 10 秒")
                else:
                    with _lock:
                        config["interval"] = n
                        save_config(config)
                    set_flash(chat_id, f"✅ 间隔已设为 {n} 秒")
                    ok = True
            except ValueError:
                set_flash(chat_id, "⚠️ 请发送数字")
            _pending.pop(user_id, None)
            refresh_panel(chat_id, user_id, view_interval, False)
            return

    # ---- 命令 ----
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()

        if cmd == "/cancel":
            # 删用户消息
            tg_delete(chat_id, user_msg_id)
            if user_id in _pending:
                p = _pending.pop(user_id)
                set_flash(chat_id, "⬜ 已取消")
                action = p["action"]
                if action == "add_key":
                    refresh_panel(chat_id, user_id, view_keys, False)
                elif action == "add_ex":
                    refresh_panel(chat_id, user_id, view_ex, False)
                elif action == "set_interval":
                    refresh_panel(chat_id, user_id, view_interval, False)
                else:
                    refresh_panel(chat_id, user_id, view_main)
            # 不在 pending 状态, 不作任何回复
            return

        if cmd in ("/menu", "/start"):
            # 删用户消息
            tg_delete(chat_id, user_msg_id)
            # 删旧面板
            old = _panel_msg.pop(chat_id, None)
            if old:
                tg_delete(chat_id, old)
            # 清除 pending
            _pending.pop(user_id, None)
            # 发新面板到底部
            send_new_panel(chat_id, user_id)
            return

        # 废弃命令 / 未知命令: 不回复, 不删 (用户自己清理)
        return

    # ---- 非命令、非 pending 的乱打字: 不回复, 不删 ----
    return

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

# ========== RSS loop ==========
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

    try:
        setup_tg_ui()
    except Exception as e:
        log.warning("setup_tg_ui failed: %s", e)

    t = threading.Thread(target=tg_updates_loop, daemon=True)
    t.start()
    poll_loop()

if __name__ == "__main__":
    main()
