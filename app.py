import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8181))
HERMES_CMD = shutil.which("hermes") or "hermes"


def find_hermes_file(filename: str) -> str:
    """Find Hermes state DB or config YAML across Windows, Linux, and macOS."""
    env_var = "HERMES_STATE_DB" if filename.endswith(".db") else "HERMES_CONFIG"
    if os.environ.get(env_var):
        return os.environ[env_var]

    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", filename),
        os.path.expanduser(f"~/AppData/Local/hermes/{filename}"),
        os.path.expanduser(f"~/.hermes/{filename}"),
        os.path.expanduser(f"~/.config/hermes/{filename}"),
        os.path.expanduser(f"~/.local/share/hermes/{filename}"),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p

    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes", filename)
    return os.path.expanduser(f"~/.hermes/{filename}")


STATE_DB = find_hermes_file("state.db")
CONFIG_PATH = find_hermes_file("config.yaml")


def get_db():
    if os.path.exists(STATE_DB):
        # ponytail: read-only URI mode to prevent lock conflicts with active Hermes writers
        return sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    return None


def is_9router_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:20128/", timeout=1.5) as r:
            return r.status in (200, 307)
    except Exception:
        return False


def ensure_9router():
    """Ensure local 9router LLM server is up before firing Hermes calls (if available)."""
    if not shutil.which("9router"):
        return
    if not is_9router_running():
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["cmd", "/c", "9router"],
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                subprocess.Popen(["9router"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.0)
        except Exception:
            pass


def get_session_context_stats(session_id: str = None) -> dict:
    """Calculate live token counts, context window limit, and usage percentage for a session."""
    conn = get_db()
    if not conn:
        return {
            "session_id": session_id,
            "tokens": 0,
            "max_tokens": 200000,
            "pct": 0.0,
            "messages": 0,
            "model": "claude-sonnet-3-6",
            "needs_compact": False,
        }
    try:
        cur = conn.cursor()
        if not session_id:
            cur.execute(
                """
                SELECT id FROM sessions
                WHERE source = 'oneshot' AND archived = 0
                ORDER BY coalesce(last_activity_at, started_at) DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return {
                    "session_id": None,
                    "tokens": 0,
                    "max_tokens": 200000,
                    "pct": 0.0,
                    "messages": 0,
                    "model": "claude-sonnet-3-6",
                    "needs_compact": False,
                }
            session_id = row[0]

        cur.execute("SELECT coalesce(model, 'claude-sonnet-3-6') FROM sessions WHERE id = ?", (session_id,))
        mrow = cur.fetchone()
        raw_model_name = mrow[0] if mrow else "claude-sonnet-3-6"
        model_name = raw_model_name.lower()

        max_tokens = 200000
        if "32k" in model_name:
            max_tokens = 32768
        elif "64k" in model_name:
            max_tokens = 65536
        elif "qwen" in model_name:
            max_tokens = 131072
        elif "gpt-4" in model_name or "128k" in model_name:
            max_tokens = 128000

        cur.execute(
            """
            SELECT count(*), coalesce(sum(coalesce(token_count, length(content) / 3)), 0)
            FROM messages
            WHERE session_id = ? AND active = 1
            """,
            (session_id,),
        )
        row = cur.fetchone()
        msg_count = row[0] if row else 0
        active_tokens = int(row[1]) if row else 0

        pct = round((active_tokens / max_tokens) * 100, 1) if max_tokens > 0 else 0.0
        return {
            "session_id": session_id,
            "tokens": active_tokens,
            "max_tokens": max_tokens,
            "pct": min(pct, 100.0),
            "messages": msg_count,
            "model": raw_model_name,
            "needs_compact": pct >= 70.0,
        }
    finally:
        conn.close()


def get_recent_sessions(limit=30):
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, coalesce(title, 'گفتگوی بدون عنوان') as title,
                   coalesce(last_activity_at, started_at) as act_time
            FROM sessions
            WHERE (message_count > 0 OR title IS NOT NULL) AND archived = 0 AND source IN ('oneshot', 'cli')
            ORDER BY act_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [{"id": r[0], "title": r[1], "time": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def get_session_messages(session_id=None):
    conn = get_db()
    if not conn:
        return {"session_id": None, "messages": [], "context": get_session_context_stats(None)}
    try:
        cur = conn.cursor()
        if not session_id:
            cur.execute(
                """
                SELECT id FROM sessions
                WHERE (message_count > 0 OR title IS NOT NULL) AND archived = 0 AND source IN ('oneshot', 'cli')
                ORDER BY coalesce(last_activity_at, started_at) DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return {"session_id": None, "messages": [], "context": get_session_context_stats(None)}
            session_id = row[0]

        cur.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
                  AND content IS NOT NULL AND trim(content) != ''
            ORDER BY id ASC
            """,
            (session_id,),
        )
        msgs = [{"role": r[0], "content": r[1]} for r in cur.fetchall()]
        ctx = get_session_context_stats(session_id)
        return {"session_id": session_id, "messages": msgs, "context": ctx}
    finally:
        conn.close()


def delete_session(session_id: str) -> bool:
    try:
        proc = subprocess.run(
            [HERMES_CMD, "sessions", "delete", session_id, "--yes"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


def resolve_session_id_after_turn(original_session_id: str = None) -> str | None:
    conn = get_db()
    if not conn:
        return original_session_id
    try:
        cur = conn.cursor()
        if original_session_id:
            cur.execute(
                """
                SELECT id FROM sessions
                WHERE parent_session_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (original_session_id,),
            )
            child = cur.fetchone()
            if child:
                return child[0]

            cur.execute("SELECT id FROM sessions WHERE id = ?", (original_session_id,))
            if cur.fetchone():
                return original_session_id

        cur.execute(
            """
            SELECT id FROM sessions
            WHERE source = 'oneshot'
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return row[0] if row else original_session_id
    finally:
        conn.close()


def execute_hermes(prompt: str, session_id: str = None) -> tuple[str, str | None]:
    """Execute Hermes in one-shot mode with UTF-8, extended 360s timeout, and session recovery."""
    ensure_9router()

    cmd = [HERMES_CMD, "-z", prompt]
    if session_id:
        cmd.extend(["--resume", session_id])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=360,
        )
        output = proc.stdout.strip()
        if not output and proc.stderr:
            clean_err = "\n".join(
                line
                for line in proc.stderr.splitlines()
                if "install out of sync" not in line and "warning" not in line.lower()
            ).strip()
            if clean_err:
                output = f"خطا: {clean_err}"

        new_sid = resolve_session_id_after_turn(session_id)
        return output or "پاسخی دریافت نشد.", new_sid

    except subprocess.TimeoutExpired:
        return (
            "⚠️ زمان پاسخ‌دهی به پایان رسید (Timeout - ۶ دقیقه).\n\n"
            "مدل یا ابزار در حال اجرای پردازش سنگین بود. نشست شما حفظ شده است. "
            "می‌توانید پیام کوتاه‌تری ارسال کنید یا برای سبک‌سازی حافظه، از دستور `/compact` استفاده فرمایید.",
            session_id,
        )
    except Exception as e:
        return f"خطا در اجرای دستور: {e}", session_id


def compact_session_in_db(session_id: str):
    """Mark previous messages as compacted=1, active=0 so token count genuinely drops."""
    if not session_id or not os.path.exists(STATE_DB):
        return
    conn = sqlite3.connect(STATE_DB)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM messages WHERE session_id = ? AND active = 1 ORDER BY id ASC", (session_id,))
        active_ids = [r[0] for r in cur.fetchall()]
        if len(active_ids) > 2:
            archive_ids = active_ids[:-2]
            placeholders = ",".join("?" * len(archive_ids))
            cur.execute(f"UPDATE messages SET active = 0, compacted = 1 WHERE id IN ({placeholders})", archive_ids)
            cur.execute("UPDATE messages SET _compressed_summary = 1 WHERE id = ?", (active_ids[-1],))
            cur.execute(
                "UPDATE sessions SET message_count = (SELECT count(*) FROM messages WHERE session_id = ? AND active = 1) WHERE id = ?",
                (session_id, session_id),
            )
            conn.commit()
    finally:
        conn.close()


def run_compact_command(session_id: str = None) -> tuple[str, str | None]:
    """Execute real context compaction, summarizing history and updating DB flags to genuinely free tokens."""
    stats = get_session_context_stats(session_id)
    if not session_id or stats["messages"] < 4:
        return "کانتکست این گفتگو در حال حاضر کوتاه است و نیازی به فشرده‌سازی ندارد.", session_id

    old_tokens = stats["tokens"]
    old_pct = stats["pct"]

    prompt = (
        "دستور سیستمی فشرده‌سازی کانتکست: لطفاً کل اطلاعات، فایل‌ها، کدها، متغیرها و تصمیمات مهم این گفتگو "
        "را به صورت خلاصه، ساختاریافته و جامع جمع‌بندی کن تا حافظه برای ادامه کار سبک شود."
    )
    res, new_sid = execute_hermes(prompt, session_id=session_id)

    target_sid = new_sid or session_id
    compact_session_in_db(target_sid)

    new_stats = get_session_context_stats(target_sid)
    new_tokens = new_stats["tokens"]
    new_pct = new_stats["pct"]

    return (
        f"🗜️ **فشرده‌سازی کانتکست با موفقیت انجام شد.**\n\n"
        f"حافظه فعال سبک شد: از ~{old_tokens:,} توکن ({old_pct}%) به ~{new_tokens:,} توکن (**{new_pct}%**).\n\n"
        f"**خلاصه وضعیت ثبت‌شده:**\n\n{res}",
        new_sid,
    )


def get_help_markdown() -> str:
    return """### 🛠️ راهنمای دستورات هرمس (Hermes Commands)

می‌توانید دستورات زیر را مستقیماً در کادر پیام بنویسید یا روی دکمه‌های سریع کلیک کنید:

* **`/status`** — نمایش وضعیت زنده مدل، نشست، مصرف توکن‌ها و سرور محلی
* **`/context`** یا **`/ctx`** — نمایش جزئیات حافظه کانتکست و درصد پر بودن آن
* **`/compact`** یا **`/compress`** — فشرده‌سازی کانتکست و سبک کردن حافظه گفتگو برای افزایش سرعت
* **`/usage`** — آمار دقیق مصرف توکن‌ها و تعداد فراخوانی‌های API در این گفتگو
* **`/new`** یا **`/reset`** — شروع یک گفتگوی جدید و تمیز
* **`/clear`** — پاکسازی پیام‌های صفحه چت و شروع مجدد
* **`/model`** — مشاهده مدل فعال یا راهنمای تغییر مدل
* **`/skills`** — مشاهده لیست مهارت‌های تخصصی فعال در هرمس
* **`/tools`** — لیست ابزارهای سیستم (ترمینال، فایل‌ها، مرورگر و...)
"""


def get_status_markdown(session_id: str = None) -> str:
    stats = get_session_context_stats(session_id)
    router_ok = is_9router_running()
    router_status = "متصل و فعال ✅" if router_ok else "قطع ❌ (در حال راه‌اندازی)"

    return f"""### 📊 وضعیت سیستم هرمس (Hermes Status)

* **مدل فعال:** `{stats['model']}`
* **سرویس‌دهنده:** `custom:local-(localhost:20128)`
* **وضعیت سرور ۹router:** {router_status}
* **شناسه نشست (Session ID):** `{stats.get('session_id') or 'نشست جدید'}`
* **تعداد پیام‌ها:** {stats['messages']} پیام
* **مصرف کانتکست:** ~{stats['tokens']:,} / {stats['max_tokens']:,} توکن (**{stats['pct']}%**)
* **وضعیت حافظه:** {'⚠️ نیازمند فشرده‌سازی با /compact' if stats.get('needs_compact') else 'سالم و سبک ✅'}
"""


def get_context_markdown(session_id: str = None) -> str:
    stats = get_session_context_stats(session_id)
    tokens = stats["tokens"]
    max_tokens = stats["max_tokens"]
    pct = stats["pct"]

    if pct < 40:
        status_msg = "بسیار سبک و روان ✅"
    elif pct < 70:
        status_msg = "وضعیت متوسط 🟡"
    else:
        status_msg = "⚠️ در مرز پر شدن — پیشنهاد فشرده‌سازی با /compact"

    return f"""### 🧠 وضعیت حافظه کانتکست (Context Usage)

* **توکن‌های فعال گفتگو:** ~{tokens:,} توکن
* **سقف کانتکست مدل:** {max_tokens:,} توکن
* **درصد پر بودن:** **{pct}%**
* **تعداد پیام‌های فعال:** {stats['messages']} پیام
* **وضعیت:** {status_msg}

> **نکته:** هرچه کانتکست سبک‌تر باشد، سرعت پاسخ‌دهی هرمس بالاتر رفته و احتمال وقفه (Timeout) کمتر می‌شود. برای فشرده‌سازی می‌توانید از دستور `/compact` استفاده کنید.
"""


def get_usage_markdown(session_id: str = None) -> str:
    if not session_id:
        return "هیچ گفتگوی فعالی برای نمایش آمار وجود ندارد."
    conn = get_db()
    if not conn:
        return "دیتابیس در دسترس نیست."
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT input_tokens, output_tokens, cache_read_tokens, api_call_count, tool_call_count, model
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            return "اطلاعات آماری این گفتگو یافت نشد."
        inp, out, cache, apis, tools, model = row
        return f"""### 📈 آمار مصرف توکن‌ها (Token Usage)

* **مدل:** `{model or 'نامشخص'}`
* **توکن‌های ورودی (Input):** ~{inp or 0:,}
* **توکن‌های خروجی (Output):** ~{out or 0:,}
* **توکن‌های کش‌شده (Cache Read):** ~{cache or 0:,}
* **تعداد فراخوانی‌های API:** {apis or 0}
* **تعداد اجرای ابزارها (Tools):** {tools or 0}
"""
    finally:
        conn.close()


def handle_chat_or_command(message: str, session_id: str = None) -> tuple[str, str | None]:
    """Intercept slash commands for zero-latency local handling, or route to Hermes agent."""
    msg = message.strip()
    if msg.startswith("/"):
        parts = msg.split(maxsplit=1)
        cmd = parts[0].lower().lstrip("/")
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("help", "?", "راهنما"):
            return get_help_markdown(), session_id
        elif cmd in ("status", "وضعیت"):
            return get_status_markdown(session_id), session_id
        elif cmd in ("context", "ctx", "کانتکست"):
            return get_context_markdown(session_id), session_id
        elif cmd in ("usage", "آمار", "مصرف"):
            return get_usage_markdown(session_id), session_id
        elif cmd in ("new", "reset", "تازه", "جدید"):
            return "✨ گفتگوی جدید آغاز شد. آماده دریافت پیام شما هستم.", None
        elif cmd in ("clear", "پاکسازی"):
            return "✨ صفحه چت پاکسازی شد و گفتگوی تازه شروع شد.", None
        elif cmd in ("compact", "compress", "فشرده"):
            return run_compact_command(session_id)
        elif cmd in ("model", "مدل"):
            stats = get_session_context_stats(session_id)
            if not arg:
                return f"مدل فعال فعلی: `{stats['model']}`\nبرای تغییر مدل می‌توانید از دستور `hermes model <نام>` استفاده کنید.", session_id
            return execute_hermes(f"/model {arg}", session_id=session_id)
        elif cmd in ("skills", "tools"):
            return execute_hermes(f"/{cmd}", session_id=session_id)

    # Auto-compaction pre-check: if context is >= 85%, compact first
    ctx = get_session_context_stats(session_id)
    if ctx["pct"] >= 85.0 and session_id and ctx["messages"] >= 6:
        run_compact_command(session_id)

    return execute_hermes(msg, session_id=session_id)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Hermes AI | گفتگوی هوشمند</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Vazirmatn:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --gemini-bg: #131314;
      --sidebar-bg: #1e1f20;
      --surface: #1e1f20;
      --surface-hover: #282a2c;
      --surface-active: #2a2b2e;
      --text-main: #e3e3e3;
      --text-muted: #c4c7c5;
      --text-dim: #8e918f;
      --border-subtle: rgba(255, 255, 255, 0.08);
      --input-bg: #1e1f20;
      --user-bubble: #282a2c;
      --accent-blue: #4285f4;
      --danger: #f28b82;
      --radius-pill: 9999px;
      --radius-card: 16px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }

    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.14);
      border-radius: var(--radius-pill);
      transition: background 0.2s;
    }
    ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.3); }
    * { scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, 0.14) transparent; }

    body {
      font-family: 'Vazirmatn', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: var(--gemini-bg);
      color: var(--text-main);
      display: flex;
      height: 100vh;
      overflow: hidden;
      line-height: 1.8;
      -webkit-font-smoothing: antialiased;
    }

    #sidebar {
      width: 270px;
      background: var(--sidebar-bg);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      border-left: 1px solid var(--border-subtle);
      transition: width 0.28s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.2s ease;
      overflow: hidden;
      white-space: nowrap;
    }
    #sidebar.collapsed {
      width: 0 !important;
      min-width: 0 !important;
      border-left: none !important;
      opacity: 0;
      pointer-events: none;
    }
    .sidebar-top {
      padding: 16px 14px 10px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      width: 270px;
    }
    .sidebar-toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 4px;
    }
    .icon-btn {
      background: transparent;
      border: none;
      color: var(--text-dim);
      font-size: 1.25rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      width: 38px;
      height: 38px;
      border-radius: 50%;
      transition: all 0.2s;
    }
    .icon-btn:hover {
      background: rgba(255, 255, 255, 0.08);
      color: var(--text-main);
    }
    .btn-new-chat {
      background: #1a1a1c;
      color: var(--text-muted);
      border: 1px solid var(--border-subtle);
      padding: 10px 16px;
      border-radius: var(--radius-pill);
      font-family: inherit;
      font-size: 0.88rem;
      font-weight: 500;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      justify-content: flex-start;
      transition: all 0.2s ease;
    }
    .btn-new-chat:hover {
      background: #282a2c;
      color: var(--text-main);
      border-color: rgba(255, 255, 255, 0.16);
    }
    .new-chat-icon {
      font-size: 1.2rem;
      color: var(--accent-blue);
      font-weight: 300;
      line-height: 1;
    }
    .sidebar-section-title {
      padding: 12px 18px 4px;
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--text-dim);
      width: 270px;
    }
    .session-list {
      flex: 1;
      overflow-y: auto;
      padding: 4px 10px 16px;
      display: flex;
      flex-direction: column;
      gap: 3px;
      width: 270px;
    }
    .session-item {
      padding: 9px 12px;
      border-radius: var(--radius-pill);
      cursor: pointer;
      font-size: 0.86rem;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      transition: all 0.15s ease;
      user-select: none;
    }
    .session-item:hover {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-main);
    }
    .session-item.active {
      background: #004a77;
      color: #c2e7ff;
      font-weight: 500;
    }
    .session-item.active .session-icon { color: #c2e7ff; }
    .session-icon {
      font-size: 0.95rem;
      color: var(--text-dim);
      display: flex;
      align-items: center;
    }
    .session-title {
      flex: 1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .btn-delete-session {
      background: transparent;
      border: none;
      color: inherit;
      cursor: pointer;
      padding: 3px 6px;
      border-radius: 50%;
      font-size: 0.8rem;
      opacity: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s;
    }
    .session-item:hover .btn-delete-session { opacity: 0.6; }
    .btn-delete-session:hover {
      color: var(--danger) !important;
      background: rgba(242, 139, 130, 0.15);
      opacity: 1 !important;
    }

    #main-area {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100vh;
      min-width: 0;
      position: relative;
    }
    header {
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
      z-index: 10;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }
    .header-left {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .gemini-title {
      font-size: 1.25rem;
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
      letter-spacing: -0.2px;
    }
    .gemini-model-badge {
      font-size: 0.8rem;
      color: var(--text-dim);
      font-weight: 400;
      background: rgba(255, 255, 255, 0.05);
      padding: 2px 8px;
      border-radius: 8px;
    }

    .header-center {
      display: flex;
      align-items: center;
    }
    .context-meter {
      display: flex;
      align-items: center;
      gap: 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-pill);
      padding: 5px 12px;
      font-size: 0.78rem;
      transition: all 0.2s;
    }
    .context-meter:hover {
      border-color: rgba(255, 255, 255, 0.16);
      background: rgba(255, 255, 255, 0.06);
    }
    .context-label {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--text-muted);
      font-weight: 500;
    }
    .context-progress-bar {
      width: 80px;
      height: 6px;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 999px;
      overflow: hidden;
    }
    .context-progress-fill {
      height: 100%;
      width: 0%;
      border-radius: 999px;
      background: #34a853;
      transition: width 0.3s ease, background 0.3s ease;
    }
    .context-progress-fill.warning { background: #fbbc04; }
    .context-progress-fill.danger { background: #ea4335; }

    .btn-compact-quick {
      background: rgba(66, 133, 244, 0.15);
      color: #8ab4f8;
      border: 1px solid rgba(66, 133, 244, 0.3);
      border-radius: var(--radius-pill);
      padding: 2px 9px;
      font-size: 0.72rem;
      font-family: inherit;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
      transition: all 0.2s ease;
    }
    .btn-compact-quick:hover {
      background: rgba(66, 133, 244, 0.3);
      color: #fff;
      border-color: rgba(66, 133, 244, 0.5);
    }
    .btn-compact-quick:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .status-indicator {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.8rem;
      color: var(--text-dim);
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #81c995;
    }
    .status-dot.busy {
      background: #8ab4f8;
      box-shadow: 0 0 8px #8ab4f8;
      animation: pulse 1s infinite;
    }

    .context-alert-banner {
      max-width: 780px;
      margin: 0 auto 10px;
      background: rgba(234, 67, 53, 0.12);
      border: 1px solid rgba(234, 67, 53, 0.3);
      border-radius: 14px;
      padding: 9px 16px;
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-size: 0.82rem;
      color: #f28b82;
      animation: fadeIn 0.3s ease;
    }
    .context-alert-banner.show { display: flex; }
    .context-alert-text { display: flex; align-items: center; gap: 8px; }
    .btn-alert-compact {
      background: #ea4335;
      color: #fff;
      border: none;
      border-radius: var(--radius-pill);
      padding: 3px 12px;
      font-size: 0.76rem;
      font-family: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      white-space: nowrap;
    }
    .btn-alert-compact:hover { background: #d93025; }

    #chat-container {
      flex: 1;
      overflow-y: auto;
      padding: 20px 0;
      display: flex;
      flex-direction: column;
      gap: 28px;
      scroll-behavior: smooth;
    }
    .chat-inner-width {
      max-width: 780px;
      width: 100%;
      margin: 0 auto;
      padding: 0 24px;
    }

    .user-turn {
      display: flex;
      justify-content: flex-start;
      margin-bottom: 24px;
    }
    .user-bubble {
      background: var(--user-bubble);
      color: var(--text-main);
      padding: 12px 20px;
      border-radius: 22px;
      font-size: 0.98rem;
      line-height: 1.8;
      max-width: 85%;
      word-break: break-word;
    }

    .bot-turn {
      display: flex;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 28px;
    }
    .gemini-sparkle-icon {
      width: 30px;
      height: 30px;
      flex-shrink: 0;
      margin-top: 3px;
    }
    .bot-content {
      flex: 1;
      color: var(--text-main);
      font-size: 0.98rem;
      line-height: 1.85;
      word-break: break-word;
    }

    .bot-content p { margin-bottom: 12px; }
    .bot-content p:last-child { margin-bottom: 0; }
    .bot-content pre {
      direction: ltr;
      text-align: left;
      background: #000000;
      padding: 16px;
      border-radius: 12px;
      overflow-x: auto;
      margin: 14px 0;
      border: 1px solid var(--border-subtle);
      font-family: 'Consolas', 'Fira Code', monospace;
      font-size: 0.88rem;
      line-height: 1.55;
      position: relative;
    }
    .bot-content code {
      direction: ltr;
      background: rgba(255, 255, 255, 0.08);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: 'Consolas', monospace;
      font-size: 0.9em;
    }
    .bot-content pre code {
      background: transparent;
      padding: 0;
      border: none;
      display: block;
    }
    .copy-btn {
      position: absolute;
      top: 8px;
      right: 8px;
      background: rgba(255, 255, 255, 0.1);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: var(--text-muted);
      border-radius: 6px;
      padding: 3px 8px;
      font-size: 0.72rem;
      cursor: pointer;
      font-family: inherit;
      transition: all 0.2s;
    }
    .copy-btn:hover {
      background: rgba(255, 255, 255, 0.2);
      color: #fff;
    }

    .gemini-loading {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 0;
    }
    .gemini-loading-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #8ab4f8;
      animation: geminiPulse 1.4s infinite ease-in-out both;
    }
    .gemini-loading-dot:nth-child(1) { animation-delay: -0.32s; }
    .gemini-loading-dot:nth-child(2) { animation-delay: -0.16s; }
    @keyframes geminiPulse {
      0%, 80%, 100% { transform: scale(0.6); opacity: 0.3; }
      40% { transform: scale(1.1); opacity: 1; }
    }

    .gemini-hero {
      max-width: 780px;
      width: 100%;
      margin: auto;
      padding: 40px 24px;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .gemini-hero-sparkle {
      width: 44px;
      height: 44px;
      margin-bottom: 20px;
    }
    .gemini-hero-title {
      font-size: 2.8rem;
      font-weight: 700;
      background: linear-gradient(90deg, #4285f4 0%, #9b72cb 50%, #d96570 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 4px;
      letter-spacing: -0.5px;
    }
    .gemini-hero-sub {
      font-size: 1.8rem;
      font-weight: 500;
      color: #444746;
      margin-bottom: 32px;
    }

    .cmd-chips-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 24px;
    }
    .cmd-chip {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.09);
      color: var(--text-muted);
      border-radius: var(--radius-pill);
      padding: 6px 14px;
      font-size: 0.82rem;
      font-family: inherit;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }
    .cmd-chip:hover {
      background: rgba(66, 133, 244, 0.15);
      border-color: rgba(66, 133, 244, 0.4);
      color: #c2e7ff;
      transform: translateY(-1px);
    }

    .gemini-chips-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
    }
    .gemini-chip-card {
      background: var(--surface);
      border-radius: var(--radius-card);
      padding: 18px;
      min-height: 110px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.2s ease;
    }
    .gemini-chip-card:hover {
      background: var(--surface-hover);
      border-color: rgba(255, 255, 255, 0.08);
      transform: translateY(-2px);
    }
    .chip-card-text {
      font-size: 0.88rem;
      color: var(--text-main);
      font-weight: 400;
      line-height: 1.6;
    }
    .chip-card-icon {
      align-self: flex-end;
      width: 32px;
      height: 32px;
      background: #131314;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 0.95rem;
    }

    /* Input Section and Slash Autocomplete Menu */
    #input-section {
      padding: 8px 24px 20px;
      flex-shrink: 0;
    }
    .input-max-width {
      max-width: 780px;
      margin: 0 auto;
      width: 100%;
      position: relative;
    }
    .gemini-capsule {
      background: var(--input-bg);
      border-radius: 28px;
      border: 1px solid rgba(255, 255, 255, 0.08);
      padding: 12px 18px 10px 14px;
      display: flex;
      flex-direction: column;
      transition: all 0.2s ease;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
    }
    .gemini-capsule:focus-within {
      background: #282a2c;
      border-color: rgba(255, 255, 255, 0.18);
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
    }
    textarea {
      background: transparent;
      border: none;
      color: var(--text-main);
      font-family: inherit;
      font-size: 0.98rem;
      resize: none;
      min-height: 44px;
      max-height: 180px;
      outline: none;
      line-height: 1.6;
      direction: rtl;
      padding: 4px 2px;
    }
    textarea::placeholder { color: var(--text-dim); }
    .capsule-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 4px;
    }
    .capsule-hint {
      font-size: 0.76rem;
      color: var(--text-dim);
    }
    .btn-gemini-send {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      border: none;
      background: #131314;
      color: var(--text-dim);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .btn-gemini-send.active {
      background: #ffffff;
      color: #131314;
      box-shadow: 0 2px 8px rgba(255, 255, 255, 0.2);
    }
    .btn-gemini-send:disabled {
      opacity: 0.3;
      cursor: not-allowed;
    }
    .footer-disclaimer {
      text-align: center;
      font-size: 0.72rem;
      color: var(--text-dim);
      margin-top: 8px;
    }

    /* Floating Slash Commands Menu */
    .slash-menu {
      position: absolute;
      bottom: calc(100% + 8px);
      left: 10px;
      right: 10px;
      max-width: 440px;
      max-height: 280px;
      overflow-y: auto;
      background: #1e1f20;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 16px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
      display: none;
      flex-direction: column;
      padding: 6px;
      z-index: 100;
      animation: fadeIn 0.15s ease;
    }
    .slash-menu.show { display: flex; }
    .slash-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 12px;
      border-radius: 10px;
      cursor: pointer;
      transition: all 0.15s;
      font-size: 0.85rem;
    }
    .slash-item:hover, .slash-item.selected {
      background: rgba(66, 133, 244, 0.2);
      color: #c2e7ff;
    }
    .slash-cmd {
      font-family: 'Consolas', monospace;
      font-weight: 600;
      color: #8ab4f8;
      direction: ltr;
    }
    .slash-desc {
      font-size: 0.78rem;
      color: var(--text-dim);
    }
    .slash-item:hover .slash-desc, .slash-item.selected .slash-desc {
      color: #c4c7c5;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }
  </style>
</head>
<body>
  <aside id="sidebar">
    <div class="sidebar-top">
      <div class="sidebar-toggle-row">
        <button class="icon-btn" onclick="toggleSidebar()" title="بستن سایدبار">☰</button>
      </div>
      <button class="btn-new-chat" onclick="startNewChat()">
        <span class="new-chat-icon">+</span>
        <span>گفتگوی جدید</span>
      </button>
    </div>
    <div class="sidebar-section-title">گفتگوهای اخیر</div>
    <div class="session-list" id="session-list"></div>
  </aside>

  <main id="main-area">
    <header>
      <div class="header-left">
        <button class="icon-btn" onclick="toggleSidebar()" title="منوی سایدبار">☰</button>
        <div class="gemini-title">
          <span>Hermes</span>
          <span class="gemini-model-badge" id="model-badge">Agent</span>
        </div>
      </div>

      <div class="header-center">
        <div class="context-meter" id="context-meter" title="میزان مصرف کانتکست (حافظه گفتگو)">
          <div class="context-label">
            <span>🧠</span>
            <span id="context-text">کانتکست: ۰٪</span>
          </div>
          <div class="context-progress-bar">
            <div class="context-progress-fill" id="context-progress-fill" style="width: 0%;"></div>
          </div>
          <button class="btn-compact-quick" id="btn-compact-quick" onclick="triggerCompact()" title="فشرده‌سازی کانتکست">🗜️ فشرده‌سازی</button>
        </div>
      </div>

      <div class="header-right">
        <div class="status-indicator">
          <span class="status-dot" id="status-dot"></span>
          <span id="session-status">آماده</span>
        </div>
      </div>
    </header>

    <div id="chat-container"></div>

    <div id="input-section">
      <div class="input-max-width">
        <!-- Floating Slash Commands Popup Menu -->
        <div class="slash-menu" id="slash-menu">
          <div class="slash-item" data-cmd="/help" onclick="selectSlashCmd('/help')">
            <span class="slash-cmd">/help</span>
            <span class="slash-desc">راهنمای کامل دستورات</span>
          </div>
          <div class="slash-item" data-cmd="/status" onclick="selectSlashCmd('/status')">
            <span class="slash-cmd">/status</span>
            <span class="slash-desc">وضعیت زنده مدل، نشست و توکن‌ها</span>
          </div>
          <div class="slash-item" data-cmd="/context" onclick="selectSlashCmd('/context')">
            <span class="slash-cmd">/context</span>
            <span class="slash-desc">گزارش دقیق حافظه کانتکست</span>
          </div>
          <div class="slash-item" data-cmd="/compact" onclick="selectSlashCmd('/compact')">
            <span class="slash-cmd">/compact</span>
            <span class="slash-desc">فشرده‌سازی و سبک کردن حافظه</span>
          </div>
          <div class="slash-item" data-cmd="/usage" onclick="selectSlashCmd('/usage')">
            <span class="slash-cmd">/usage</span>
            <span class="slash-desc">آمار مصرف توکن‌ها و هزینه‌ها</span>
          </div>
          <div class="slash-item" data-cmd="/new" onclick="selectSlashCmd('/new')">
            <span class="slash-cmd">/new</span>
            <span class="slash-desc">شروع گفتگوی تازه و تمیز</span>
          </div>
          <div class="slash-item" data-cmd="/clear" onclick="selectSlashCmd('/clear')">
            <span class="slash-cmd">/clear</span>
            <span class="slash-desc">پاکسازی صفحه چت</span>
          </div>
          <div class="slash-item" data-cmd="/model" onclick="selectSlashCmd('/model')">
            <span class="slash-cmd">/model</span>
            <span class="slash-desc">مشاهده یا تغییر مدل فعال</span>
          </div>
          <div class="slash-item" data-cmd="/skills" onclick="selectSlashCmd('/skills')">
            <span class="slash-cmd">/skills</span>
            <span class="slash-desc">لیست مهارت‌های فعال</span>
          </div>
          <div class="slash-item" data-cmd="/tools" onclick="selectSlashCmd('/tools')">
            <span class="slash-cmd">/tools</span>
            <span class="slash-desc">لیست ابزارهای سیستم</span>
          </div>
        </div>

        <!-- Context Warning Alert Banner -->
        <div class="context-alert-banner" id="context-alert-banner">
          <div class="context-alert-text">
            <span>⚠️</span>
            <span id="alert-text">مصرف کانتکست به بیش از ۷۰٪ رسیده است. برای جلوگیری از کندی یا Timeout، پیشنهاد می‌شود کانتکست را فشرده کنید.</span>
          </div>
          <button class="btn-alert-compact" onclick="triggerCompact()">فشرده‌سازی اکنون</button>
        </div>

        <div class="gemini-capsule">
          <textarea id="prompt" placeholder="پیام خود را بنویسید..." rows="1"></textarea>
          <div class="capsule-footer">
            <span class="capsule-hint">Enter برای ارسال • Shift+Enter خط جدید</span>
            <button class="btn-gemini-send" id="send-btn" onclick="sendMessage()" disabled>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
              </svg>
            </button>
          </div>
        </div>
        <div class="footer-disclaimer">
          هوش مصنوعی ممکن است اطلاعات نادرست نمایش دهد. پاسخ‌ها را بررسی کنید.
        </div>
      </div>
    </div>
  </main>

  <script>
    const sidebarEl = document.getElementById('sidebar');
    const container = document.getElementById('chat-container');
    const promptInput = document.getElementById('prompt');
    const sendBtn = document.getElementById('send-btn');
    const sessionListEl = document.getElementById('session-list');
    const sessionStatusEl = document.getElementById('session-status');
    const statusDotEl = document.getElementById('status-dot');
    const contextTextEl = document.getElementById('context-text');
    const contextFillEl = document.getElementById('context-progress-fill');
    const alertBannerEl = document.getElementById('context-alert-banner');
    const modelBadgeEl = document.getElementById('model-badge');
    const slashMenuEl = document.getElementById('slash-menu');

    let selectedSlashIndex = -1;

    const GEMINI_SPARKLE_SVG = `
      <svg class="gemini-sparkle-icon" viewBox="0 0 24 24" fill="none">
        <path d="M12 2C12 7.52285 7.52285 12 2 12C7.52285 12 12 16.4771 12 22C12 16.4771 16.4771 12 22 12C16.4771 12 12 7.52285 12 2Z" fill="url(#gemini-grad)"/>
        <defs>
          <linearGradient id="gemini-grad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
            <stop stop-color="#4285F4"/>
            <stop offset="0.5" stop-color="#9B72CB"/>
            <stop offset="1" stop-color="#D96570"/>
          </linearGradient>
        </defs>
      </svg>
    `;

    let currentSessionId = localStorage.getItem('hermes_active_session_id') || null;

    function toggleSidebar() {
      sidebarEl.classList.toggle('collapsed');
      localStorage.setItem('hermes_sidebar_collapsed', sidebarEl.classList.contains('collapsed') ? '1' : '0');
    }

    promptInput.addEventListener('input', () => {
      promptInput.style.height = 'auto';
      promptInput.style.height = Math.min(promptInput.scrollHeight, 180) + 'px';
      const val = promptInput.value;

      if (val.trim().length > 0) {
        sendBtn.disabled = false;
        sendBtn.classList.add('active');
      } else {
        sendBtn.disabled = true;
        sendBtn.classList.remove('active');
      }

      // Show and filter slash menu when starting with /
      if (val.startsWith('/')) {
        const filter = val.toLowerCase().trim();
        const items = slashMenuEl.querySelectorAll('.slash-item');
        let hasMatch = false;
        items.forEach((item) => {
          const cmd = item.getAttribute('data-cmd').toLowerCase();
          if (cmd.startsWith(filter) || filter === '/') {
            item.style.display = 'flex';
            hasMatch = true;
          } else {
            item.style.display = 'none';
          }
        });
        if (hasMatch) {
          slashMenuEl.classList.add('show');
          selectedSlashIndex = -1;
          updateSlashSelection();
        } else {
          slashMenuEl.classList.remove('show');
        }
      } else {
        slashMenuEl.classList.remove('show');
      }
    });

    promptInput.addEventListener('keydown', (e) => {
      if (slashMenuEl.classList.contains('show')) {
        const visibleItems = Array.from(slashMenuEl.querySelectorAll('.slash-item')).filter(i => i.style.display !== 'none');
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          selectedSlashIndex = (selectedSlashIndex + 1) % visibleItems.length;
          updateSlashSelection(visibleItems);
          return;
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          selectedSlashIndex = (selectedSlashIndex - 1 + visibleItems.length) % visibleItems.length;
          updateSlashSelection(visibleItems);
          return;
        } else if (e.key === 'Tab' || (e.key === 'Enter' && selectedSlashIndex >= 0)) {
          e.preventDefault();
          if (selectedSlashIndex >= 0 && visibleItems[selectedSlashIndex]) {
            selectSlashCmd(visibleItems[selectedSlashIndex].getAttribute('data-cmd'));
          }
          return;
        } else if (e.key === 'Escape') {
          slashMenuEl.classList.remove('show');
          return;
        }
      }

      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    function updateSlashSelection(visibleItems) {
      const items = visibleItems || Array.from(slashMenuEl.querySelectorAll('.slash-item')).filter(i => i.style.display !== 'none');
      items.forEach((item, idx) => {
        if (idx === selectedSlashIndex) {
          item.classList.add('selected');
          item.scrollIntoView({ block: 'nearest' });
        } else {
          item.classList.remove('selected');
        }
      });
    }

    function selectSlashCmd(cmd) {
      promptInput.value = cmd;
      slashMenuEl.classList.remove('show');
      promptInput.focus();
      promptInput.dispatchEvent(new Event('input'));
      // Direct execute if it is a complete slash command
      sendMessage();
    }

    document.addEventListener('click', (e) => {
      if (!slashMenuEl.contains(e.target) && e.target !== promptInput) {
        slashMenuEl.classList.remove('show');
      }
    });

    function escapeHtml(str) {
      return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function formatMarkdown(text) {
      if (!text) return '';
      let f = text.replace(/```([a-zA-Z0-9_-]*)\\n([\\s\\S]*?)```/g, (m, lang, code) => {
        const id = 'code-' + Math.random().toString(36).substring(2, 9);
        return `<pre><button class="copy-btn" onclick="copyCode(this, '${id}')">کپی</button><code id="${id}">${escapeHtml(code.trim())}</code></pre>`;
      });
      f = f.replace(/`([^`]+)`/g, (m, c) => `<code>${escapeHtml(c)}</code>`);
      f = f.replace(/\\*\\*([^\\*]+)\\*\\*/g, '<strong>$1</strong>');
      f = f.replace(/\\n/g, '<br>');
      return f;
    }

    function copyCode(btn, id) {
      const el = document.getElementById(id);
      if (!el) return;
      navigator.clipboard.writeText(el.innerText).then(() => {
        btn.innerText = 'کپی شد!';
        setTimeout(() => { btn.innerText = 'کپی'; }, 2000);
      });
    }

    function setStatus(busy) {
      if (busy) {
        sessionStatusEl.innerText = 'هرمس در حال پردازش...';
        statusDotEl.className = 'status-dot busy';
      } else {
        sessionStatusEl.innerText = 'آماده';
        statusDotEl.className = 'status-dot';
      }
    }

    function updateContextMeter(ctx) {
      if (!ctx) return;
      const pct = ctx.pct || 0;
      const tokensK = Math.round((ctx.tokens || 0) / 1000);
      const maxK = Math.round((ctx.max_tokens || 200000) / 1000);

      contextTextEl.innerText = `${pct}% (${tokensK}K / ${maxK}K)`;
      contextFillEl.style.width = `${Math.min(pct, 100)}%`;

      contextFillEl.className = 'context-progress-fill';
      if (pct >= 75) {
        contextFillEl.classList.add('danger');
        alertBannerEl.classList.add('show');
      } else if (pct >= 50) {
        contextFillEl.classList.add('warning');
        alertBannerEl.classList.remove('show');
      } else {
        alertBannerEl.classList.remove('show');
      }

      if (ctx.model) {
        modelBadgeEl.innerText = ctx.model.replace('claude-', '').replace('qwen-', 'qwen ');
      }
    }

    function renderMessage(role, content) {
      const wrapper = document.createElement('div');
      wrapper.className = 'chat-inner-width';

      if (role === 'user') {
        wrapper.innerHTML = `
          <div class="user-turn">
            <div class="user-bubble">${escapeHtml(content)}</div>
          </div>
        `;
      } else {
        wrapper.innerHTML = `
          <div class="bot-turn">
            ${GEMINI_SPARKLE_SVG}
            <div class="bot-content">${formatMarkdown(content)}</div>
          </div>
        `;
      }
      container.appendChild(wrapper);
    }

    function renderHero() {
      container.innerHTML = `
        <div class="gemini-hero">
          ${GEMINI_SPARKLE_SVG.replace('class="gemini-sparkle-icon"', 'class="gemini-hero-sparkle"')}
          <div class="gemini-hero-title">سلام، اشکان</div>
          <div class="gemini-hero-sub">امروز چطور می‌توانم کمکتان کنم؟</div>

          <div class="cmd-chips-row">
            <button class="cmd-chip" onclick="fillAndSend('/help')">❓ راهنما (/help)</button>
            <button class="cmd-chip" onclick="fillAndSend('/status')">📊 وضعیت سیستم (/status)</button>
            <button class="cmd-chip" onclick="fillAndSend('/context')">🧠 وضعیت کانتکست (/context)</button>
            <button class="cmd-chip" onclick="fillAndSend('/compact')">🗜️ فشرده‌سازی (/compact)</button>
            <button class="cmd-chip" onclick="fillAndSend('/usage')">📈 آمار توکن‌ها (/usage)</button>
          </div>

          <div class="gemini-chips-grid">
            <div class="gemini-chip-card" onclick="fillPrompt('یک روت اکسپرس برای ثبت محصول با اعتبارسنجی بنویس')">
              <div class="chip-card-text">ساخت روت Express.js برای ثبت سفارش و محصول</div>
              <div class="chip-card-icon">⚡</div>
            </div>
            <div class="gemini-chip-card" onclick="fillPrompt('نحوه اتصال به درگاه پرداخت زرین‌پال یا سداد در نود جی‌اس')">
              <div class="chip-card-text">راهنمای اتصال به درگاه پرداخت اینترنتی در Node</div>
              <div class="chip-card-icon">💳</div>
            </div>
            <div class="gemini-chip-card" onclick="fillPrompt('طراحی کامپوننت فیلترهای پیشرفته دیجی‌کالا در ری‌اکت')">
              <div class="chip-card-text">طراحی بخش فیلترهای قیمت و دسته‌بندی فروشگاه</div>
              <div class="chip-card-icon">🛍</div>
            </div>
          </div>
        </div>
      `;
    }

    function fillPrompt(txt) {
      promptInput.value = txt;
      promptInput.dispatchEvent(new Event('input'));
      promptInput.focus();
    }

    function fillAndSend(txt) {
      promptInput.value = txt;
      promptInput.dispatchEvent(new Event('input'));
      sendMessage();
    }

    function triggerCompact() {
      fillAndSend('/compact');
    }

    async function loadSessions() {
      try {
        const res = await fetch('/api/sessions');
        const sessions = await res.json();
        sessionListEl.innerHTML = '';
        sessions.forEach(s => {
          const item = document.createElement('div');
          item.className = 'session-item' + (s.id === currentSessionId ? ' active' : '');
          item.onclick = () => selectSession(s.id);

          const icon = document.createElement('span');
          icon.className = 'session-icon';
          icon.innerHTML = '💬';
          item.appendChild(icon);

          const titleSpan = document.createElement('span');
          titleSpan.className = 'session-title';
          titleSpan.innerText = s.title;
          titleSpan.title = s.title;
          item.appendChild(titleSpan);

          const delBtn = document.createElement('button');
          delBtn.className = 'btn-delete-session';
          delBtn.innerHTML = '✕';
          delBtn.title = 'حذف';
          delBtn.onclick = (e) => deleteSession(e, s.id);
          item.appendChild(delBtn);

          sessionListEl.appendChild(item);
        });
      } catch (err) {
        console.error('Failed to load sessions:', err);
      }
    }

    async function deleteSession(e, sid) {
      e.stopPropagation();
      if (!confirm('آیا از حذف این گفتگو مطمئن هستید؟')) return;

      try {
        const res = await fetch('/api/sessions/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sid })
        });
        const data = await res.json();
        if (data.status === 'ok') {
          if (currentSessionId === sid) {
            startNewChat();
          } else {
            loadSessions();
          }
        }
      } catch (err) {
        alert('خطا در حذف گفتگو: ' + err.message);
      }
    }

    async function selectSession(sid) {
      currentSessionId = sid;
      localStorage.setItem('hermes_active_session_id', sid);
      await loadSessionMessages(sid);
      loadSessions();
    }

    async function loadSessionMessages(sid) {
      container.innerHTML = '<div style="text-align: center; color: var(--text-dim); margin-top: 60px;">در حال بارگذاری گفتگو...</div>';
      try {
        const url = sid ? ('/api/messages?session_id=' + encodeURIComponent(sid)) : '/api/messages';
        const res = await fetch(url);
        const data = await res.json();
        container.innerHTML = '';

        if (data.session_id) {
          currentSessionId = data.session_id;
          localStorage.setItem('hermes_active_session_id', currentSessionId);
        }

        if (data.context) {
          updateContextMeter(data.context);
        }

        if (data.messages && data.messages.length > 0) {
          data.messages.forEach(m => renderMessage(m.role, m.content));
        } else {
          renderHero();
        }
        container.scrollTop = container.scrollHeight;
      } catch (err) {
        container.innerHTML = '<div style="text-align: center; color: var(--danger); margin-top: 60px;">خطا: ' + err.message + '</div>';
      }
    }

    function startNewChat() {
      currentSessionId = null;
      localStorage.removeItem('hermes_active_session_id');
      renderHero();
      updateContextMeter({ pct: 0, tokens: 0, max_tokens: 200000 });
      promptInput.focus();
      loadSessions();
    }

    async function sendMessage() {
      const text = promptInput.value.trim();
      if (!text || sendBtn.disabled) return;

      slashMenuEl.classList.remove('show');

      if (container.querySelector('.gemini-hero')) {
        container.innerHTML = '';
      }

      renderMessage('user', text);

      const loadingWrap = document.createElement('div');
      loadingWrap.className = 'chat-inner-width';
      loadingWrap.innerHTML = `
        <div class="bot-turn">
          ${GEMINI_SPARKLE_SVG}
          <div class="bot-content gemini-loading">
            <div class="gemini-loading-dot"></div>
            <div class="gemini-loading-dot"></div>
            <div class="gemini-loading-dot"></div>
          </div>
        </div>
      `;
      container.appendChild(loadingWrap);
      container.scrollTop = container.scrollHeight;

      promptInput.value = '';
      promptInput.style.height = '44px';
      promptInput.disabled = true;
      sendBtn.disabled = true;
      sendBtn.classList.remove('active');
      setStatus(true);

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text, session_id: currentSessionId })
        });
        const data = await res.json();
        if (loadingWrap.parentNode) {
          container.removeChild(loadingWrap);
        }

        if (data.session_id) {
          currentSessionId = data.session_id;
          localStorage.setItem('hermes_active_session_id', currentSessionId);
        } else if (text.startsWith('/new') || text.startsWith('/reset') || text.startsWith('/clear')) {
          currentSessionId = null;
          localStorage.removeItem('hermes_active_session_id');
        }

        if (data.context) {
          updateContextMeter(data.context);
        }

        renderMessage('assistant', data.reply || data.error || 'پاسخی دریافت نشد.');
        loadSessions();
      } catch (err) {
        if (loadingWrap.parentNode) {
          container.removeChild(loadingWrap);
        }
        renderMessage('assistant', 'خطای شبکه یا ارتباط با سرور: ' + err.message);
      } finally {
        promptInput.disabled = false;
        setStatus(false);
        promptInput.focus();
        container.scrollTop = container.scrollHeight;
      }
    }

    (async function init() {
      if (localStorage.getItem('hermes_sidebar_collapsed') === '1') {
        sidebarEl.classList.add('collapsed');
      }
      await loadSessionMessages(currentSessionId);
      await loadSessions();
      promptInput.focus();
    })();
  </script>
</body>
</html>
"""


class ChatHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif url.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "9router": is_9router_running()}).encode("utf-8"))
        elif url.path == "/api/sessions":
            sessions = get_recent_sessions()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(sessions).encode("utf-8"))
        elif url.path == "/api/messages":
            params = parse_qs(url.query)
            sid = params.get("session_id", [None])[0]
            data = get_session_messages(sid)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        elif url.path == "/api/context":
            params = parse_qs(url.query)
            sid = params.get("session_id", [None])[0]
            ctx = get_session_context_stats(sid)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(ctx).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/chat":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(body)
                message = data.get("message", "").strip()
                session_id = data.get("session_id")
                if not message:
                    res = json.dumps({"reply": "لطفاً پیامی بنویسید.", "session_id": session_id})
                else:
                    reply, new_sid = handle_chat_or_command(message, session_id=session_id)
                    ctx = get_session_context_stats(new_sid)
                    res = json.dumps({"reply": reply, "session_id": new_sid, "context": ctx})
            except Exception as e:
                res = json.dumps({"error": str(e), "session_id": None})

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(res.encode("utf-8"))
        elif url.path == "/api/sessions/delete":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len).decode("utf-8")
            try:
                data = json.loads(body)
                session_id = data.get("session_id")
                if not session_id:
                    res = json.dumps({"status": "error", "error": "شناسه سشن ارسال نشده است."})
                else:
                    ok = delete_session(session_id)
                    res = json.dumps({"status": "ok" if ok else "error"})
            except Exception as e:
                res = json.dumps({"status": "error", "error": str(e)})

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(res.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def self_test():
    """Verify server boots and serves sessions, health, context, and slash commands."""
    server = ThreadedHTTPServer(("127.0.0.1", 0), ChatHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health") as res:
        data = json.loads(res.read().decode())
        assert data.get("status") == "ok"

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions") as res:
        sessions = json.loads(res.read().decode())
        assert isinstance(sessions, list)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/context") as res:
        ctx = json.loads(res.read().decode())
        assert "tokens" in ctx and "pct" in ctx

    help_reply, _ = handle_chat_or_command("/help")
    assert "راهنمای دستورات" in help_reply

    server.shutdown()
    print("Self-test passed: health, sessions, context stats, and slash commands verified.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Agent Persian Web UI")
    parser.add_argument("--port", "-p", type=int, default=PORT, help="Port to run the web server on (default: 8181)")
    parser.add_argument("--host", "-H", type=str, default="127.0.0.1", help="Host address to bind (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically launch web browser")
    parser.add_argument("--test", action="store_true", help="Run integration self-test and exit")
    args = parser.parse_args()

    if args.test:
        self_test()
        sys.exit(0)

    server_port = args.port
    server_host = args.host
    server = ThreadedHTTPServer((server_host, server_port), ChatHandler)
    url = f"http://localhost:{server_port}" if server_host == "127.0.0.1" else f"http://{server_host}:{server_port}"
    print(f"سرور وب چت فارسی هرمس راه‌اندازی شد: {url}")
    if not args.no_browser:
        print("در حال باز کردن مرورگر...")
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nخروج از سرور.")
