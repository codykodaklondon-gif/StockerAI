"""Owner alerts: a daily summary, and optional instant stock-out alerts, by Telegram or email.

Runs inside the app as a background thread while it is open, or on its own from a
scheduler (cron, Windows Task Scheduler) when the app is closed:
    python -m stocker.notifier          # send whatever is due now, then exit
    python -m stocker.notifier --loop   # keep checking every minute
Settings live in each workspace's database; bot tokens and SMTP passwords can instead come
from .streamlit/secrets.toml or the environment (TELEGRAM_BOT_TOKEN, SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD), which take effect when the matching field is left blank.
"""
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

import pandas as pd

from . import advisor, db, events

DEFAULTS = {
    "enabled": False, "send_time": "07:30", "min_severity": "high", "include_buying": True,
    "only_if_needed": False, "instant": False,
    "telegram": {"on": False, "token": "", "chat_id": ""},
    "email": {"on": False, "host": "", "port": 587, "security": "STARTTLS", "user": "", "password": "",
              "sender": "", "to": ""},
}
ENV = {("telegram", "token"): "TELEGRAM_BOT_TOKEN", ("email", "host"): "SMTP_HOST", ("email", "port"): "SMTP_PORT",
       ("email", "user"): "SMTP_USER", ("email", "password"): "SMTP_PASSWORD"}
SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
INSTANT_KINDS = ("Out of stock", "Stock-out risk")
INSTANT_EVERY = 600          # seconds between instant-alert checks per workspace
_last_instant: dict[str, float] = {}


def settings() -> dict:
    """Saved settings merged over the defaults (what the settings form shows)."""
    saved = db.get_setting("alerts", {}) or {}
    out = {**DEFAULTS, **saved}
    for ch in ("telegram", "email"):
        out[ch] = {**DEFAULTS[ch], **saved.get(ch, {})}
    return out


def save_settings(cfg: dict):
    db.set_setting("alerts", cfg)


def config() -> dict:
    """Settings actually used for sending: blank secrets are filled from the environment."""
    cfg = settings()
    for (ch, k), env in ENV.items():
        if not cfg[ch][k] and os.environ.get(env):
            cfg[ch][k] = os.environ[env]
    return cfg


def state() -> dict:
    return db.get_setting("alerts_state", {}) or {}


# ------------------------------------------------------------------ messages
# Written as Telegram-flavoured HTML (<b>, <i> and newlines); email turns the newlines into <br>.

def daily_report(cfg: dict, today: pd.Timestamp | None = None) -> tuple[str, str] | None:
    """(subject, message), or None when only_if_needed is on and nothing needs attention."""
    today = today or pd.Timestamp.today()
    rank = advisor.SEVERITY_ORDER
    risks = advisor.risks_frame()
    top = risks[risks["severity"].map(rank) <= rank[cfg["min_severity"]]] if not risks.empty else risks
    rec = advisor.recommendations() if cfg["include_buying"] else pd.DataFrame()
    buy = rec[rec["action"] == "Stock more"] if not rec.empty else rec
    coming = events.upcoming(7)
    if cfg["only_if_needed"] and top.empty and buy.empty:
        return None
    lines = [f"<b>🌿 Stocker AI · {today:%a %d %b %Y}</b>", ""]
    if top.empty:
        lines.append("✅ Nothing urgent today.")
    else:
        lines.append(f"<b>{len(top)} alert{'s' if len(top) > 1 else ''} to check</b>")
        for r in top.head(10).itertuples():
            lines.append(f"{SEV_ICON.get(r.severity, '•')} <b>{escape(str(r.product))}</b> · {escape(r.kind)}: "
                         f"{escape(r.message)}\n    → {escape(r.action)}")
        if len(top) > 10:
            lines.append(f"<i>…and {len(top) - 10} more in the app.</i>")
    if not buy.empty:
        cost = buy["est_cost"].sum()
        lines += ["", f"<b>🛒 Buy today · {len(buy)} item{'s' if len(buy) > 1 else ''}"
                      + (f" · about S${cost:,.0f}" if cost > 0 else "") + "</b>"]
        lines += [f"• {escape(str(r.product))}: {r.order_low:,}–{r.order_high:,} units" for r in buy.head(15).itertuples()]
    if not coming.empty:
        lines += ["", "<b>📅 Coming up this week</b>"]
        lines += [f"• {r.date:%a %d %b}: {escape(r.name)}" + (f" ({escape(r.scope)})" if r.scope != events.ALL else "")
                  for r in coming.itertuples()]
    return f"Stocker AI · {len(top)} alerts · {today:%d %b}", "\n".join(lines)


def instant_risks() -> pd.DataFrame:
    risks = advisor.risks_frame()
    return risks[risks["kind"].isin(INSTANT_KINDS)] if not risks.empty else risks


def instant_report(risks: pd.DataFrame) -> tuple[str, str]:
    lines = ["<b>🚨 Stocker AI · running out</b>", ""]
    lines += [f"{SEV_ICON.get(r.severity, '•')} <b>{escape(str(r.product))}</b>: {escape(r.message)}\n    → {escape(r.action)}"
              for r in risks.itertuples()]
    return f"Stocker AI · {len(risks)} product(s) running out", "\n".join(lines)


# ------------------------------------------------------------------ channels

def _telegram(token: str, method: str, params: dict | None = None) -> dict:
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=15) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:     # Telegram explains the problem in the body
        try:
            res = json.load(e)
        except Exception:
            raise RuntimeError(f"Telegram: HTTP {e.code}") from None
    if not res.get("ok"):
        raise RuntimeError(f"Telegram: {res.get('description', 'request failed')}")
    return res


def send_telegram(tg: dict, text: str):
    if len(text) > 4000:     # Telegram's limit is 4096 characters
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"
    _telegram(tg["token"], "sendMessage", {"chat_id": tg["chat_id"], "text": text, "parse_mode": "HTML",
                                          "disable_web_page_preview": "true"})


def telegram_chats(token: str) -> list[tuple[str, str]]:
    """Chats that recently messaged the bot, as (chat id, name): how the owner finds their chat id."""
    found = {}
    for u in _telegram(token, "getUpdates").get("result", []):
        chat = (u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            found[str(chat["id"])] = name or chat.get("username") or "?"
    return list(found.items())


def send_email(em: dict, subject: str, text: str):
    to = [a.strip() for a in re.split(r"[,;]", em["to"]) if a.strip()]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, em["sender"] or em["user"], ", ".join(to)
    msg.set_content(re.sub(r"<[^>]+>", "", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
    msg.add_alternative(f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.55;color:#493a31">'
                        f'{text.replace(chr(10), "<br>")}</div>', subtype="html")
    port = int(em["port"] or 587)
    if em["security"] == "SSL":
        server = smtplib.SMTP_SSL(em["host"], port, context=ssl.create_default_context(), timeout=20)
    else:
        server = smtplib.SMTP(em["host"], port, timeout=20)
    with server:
        if em["security"] == "STARTTLS":
            server.starttls(context=ssl.create_default_context())
        if em["user"]:
            server.login(em["user"], em["password"])
        server.send_message(msg)


def channels(cfg: dict) -> list[str]:
    out = []
    if cfg["telegram"]["on"] and cfg["telegram"]["token"] and cfg["telegram"]["chat_id"]:
        out.append("Telegram")
    if cfg["email"]["on"] and cfg["email"]["host"] and cfg["email"]["to"]:
        out.append("Email")
    return out


def deliver(cfg: dict, subject: str, text: str) -> dict[str, str | None]:
    """Send on every configured channel; returns {channel: error or None}. Never raises."""
    results = {}
    for ch in channels(cfg):
        try:
            send_telegram(cfg["telegram"], text) if ch == "Telegram" else send_email(cfg["email"], subject, text)
            results[ch] = None
        except Exception as e:
            results[ch] = f"{type(e).__name__}: {e}"
    return results


def _record(st: dict, what: str, results: dict):
    st["last"] = {"what": what, "at": datetime.now().strftime("%Y-%m-%d %H:%M"), "results": results}


def send_now() -> dict[str, str | None]:
    """Send today's summary immediately (the 'Send a test now' button)."""
    cfg = config()
    subject, text = daily_report({**cfg, "only_if_needed": False})
    results = deliver(cfg, subject, text)
    st = state()
    _record(st, "Test / manual summary", results)
    db.set_setting("alerts_state", st)
    return results


# ------------------------------------------------------------------ scheduling

def run_due(now: datetime | None = None) -> int:
    """Send whatever is due in every workspace. Returns the number of messages sent."""
    now = now or datetime.now()
    sent = 0
    for path in sorted(db.DATA_DIR.glob("*.db")):
        db.use_workspace(path.stem)
        try:
            cfg = config()
        except (sqlite3.OperationalError, pd.errors.DatabaseError):   # older workspace, no settings table yet
            continue
        if not cfg["enabled"] or not channels(cfg):
            continue
        try:
            db.init()       # upgrade older workspaces before checking the new dated-batch alerts
            st, changed = state(), False
            today = now.date().isoformat()
            if now.strftime("%H:%M") >= cfg["send_time"] and st.get("last_daily") != today:
                st["last_daily"], changed = today, True    # marked first, so a failing channel is not retried every minute
                if report := daily_report(cfg, pd.Timestamp(now)):
                    _record(st, "Daily summary", deliver(cfg, *report))
                    sent += 1
            if cfg["instant"] and time.time() - _last_instant.get(path.stem, 0) >= INSTANT_EVERY:
                _last_instant[path.stem] = time.time()
                risks = instant_risks()
                keys = sorted(f"{r.sku}|{r.kind}" for r in risks.itertuples())
                new = set(keys) - set(st.get("alerted", []))
                if new:
                    _record(st, "Instant alert",
                            deliver(cfg, *instant_report(risks[[f"{r.sku}|{r.kind}" in new for r in risks.itertuples()]])))
                    sent += 1
                if keys != st.get("alerted", []):    # resolved items drop out, so they can alert again later
                    st["alerted"], changed = keys, True
            if changed:    # write only on change: the app's caches key on the database file's timestamp
                db.set_setting("alerts_state", st)
        except Exception as e:      # one broken workspace must not stop the others
            print(f"[stocker alerts] {path.stem}: {type(e).__name__}: {e}", file=sys.stderr)
    return sent


_thread: threading.Thread | None = None


def start_background(interval: int = 60) -> threading.Thread:
    """Check every `interval` seconds while the app runs. Safe to call more than once."""
    global _thread
    if _thread and _thread.is_alive():
        return _thread

    def loop():
        while True:
            try:
                run_due()
            except Exception as e:
                print(f"[stocker alerts] {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(interval)
    _thread = threading.Thread(target=loop, name="stocker-alerts", daemon=True)
    _thread.start()
    return _thread


def load_secrets():
    """Outside Streamlit, read .streamlit/secrets.toml into the environment like the app does."""
    path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    try:
        import tomllib
        values = tomllib.loads(path.read_text()) if path.exists() else {}
    except Exception:      # Python 3.10 has no tomllib; the environment still works
        values = {}
    for k, v in values.items():
        os.environ.setdefault(k, str(v))


if __name__ == "__main__":
    load_secrets()
    if "--loop" in sys.argv:
        start_background().join()
    else:
        print(f"Sent {run_due()} message(s).")
