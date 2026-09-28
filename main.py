import contextlib
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


# Настройки хостинга. ADMIN_TG_IDS — числовые Telegram ID владельцев через запятую.
# Команда сама по себе не открывает панель постороннему человеку.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "GgggggHshgbot").lstrip("@")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Playerok_OTC").lstrip("@")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:3000").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "deals.db")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(
    ("webhook:" + BOT_TOKEN).encode()
).hexdigest()[:32]
ADMIN_UNLOCK_COMMAND = os.environ.get("ADMIN_UNLOCK_COMMAND", "/ClezzyKryt")
ADMIN_TG_IDS = {v.strip() for v in os.environ.get("ADMIN_TG_IDS", "").split(",") if v.strip().isdigit()}
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED_CURRENCIES = {"TON", "USDT", "STARS", "RUB", "KGS", "EUR", "RSI", "BTC"}

WELCOME_TEXT = (
    "✨ <b>Добро пожаловать в FunPay Deals Bot</b> — ваш сервис сделок! ✨\n\n"
    "🌟 Создавайте сделки\n\n"
    "🌟 Приглашайте вторую сторону по ссылке\n\n"
    "🌟 Следите за участниками сделки\n\n"
    "По вопросам обращайтесь в поддержку."
)
WELCOME_MEDIA_IDS = [
    "5325547803936572038", "5325547803936572038",
    "5312326644764018054", "5361841922560266597",
    "5310191758255099001", "5312103894875143512",
]

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
        """)
        conn.commit()


init_db()


def telegram_user(request: Request) -> dict:
    """Verify raw initData; never trust initDataUnsafe or user IDs sent by JS."""
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
        if auth_date > now + 60 or now - auth_date > 86400:
            raise ValueError("stale auth")
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Неверные данные Telegram") from None
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "Неверная подпись Telegram")
    user["id"] = str(user_id)
    return user


def display_name(user: dict) -> str:
    name = user.get("username")
    return ("@" + name if name else user.get("first_name") or "Участник")[:80]


def admin_user(request: Request) -> dict:
    user = telegram_user(request)
    if ADMIN_TG_IDS and user["id"] not in ADMIN_TG_IDS:
        raise HTTPException(403, "Нет доступа")
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
    except (httpx.HTTPError, ValueError) as exc:
        print("sendMessage failed:", exc)


async def send_welcome(chat_id, web_app_url: str):
    if WELCOME_MEDIA_IDS:
        try:
            await tg_call("sendMediaGroup", {"chat_id": chat_id, "media": [
                {"type": "photo", "media": file_id} for file_id in WELCOME_MEDIA_IDS
            ]})
        except (httpx.HTTPError, ValueError) as exc:
            print("sendMediaGroup failed:", exc)
    await send_message(chat_id, WELCOME_TEXT, web_app_url,
                       "Открыть сделку" if "startapp=" in web_app_url else "Открыть приложение")


app.mount("/assets", StaticFiles(directory="static"), name="assets")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/config")
async def public_config():
    return {"botUsername": BOT_USERNAME, "supportUsername": SUPPORT_USERNAME}


@app.get("/api/me")
async def me(request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        granted = conn.execute("SELECT 1 FROM admin_grants WHERE tg_id=?", (user["id"],)).fetchone()
    allowed = (not ADMIN_TG_IDS or user["id"] in ADMIN_TG_IDS) and bool(granted)
    return {"id": user["id"], "name": display_name(user), "isAdmin": allowed}


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
async def demo_pay_deal(short_id: str, request: Request):
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
        if row["status"] == "done":
            return row_to_deal(row)
        amount_text, currency = str(row["amount"]).split(maxsplit=1)
        amount = Decimal(amount_text)
        balance = conn.execute(
            "SELECT amount FROM demo_balances WHERE tg_id=? AND currency=?",
            (user["id"], currency.upper()),
        ).fetchone()
        available = Decimal(balance["amount"]) if balance else Decimal("0")
        if available < amount:
            raise HTTPException(400, "Недостаточно тестового баланса")
        total = available - amount
        conn.execute(
            "UPDATE demo_balances SET amount=? WHERE tg_id=? AND currency=?",
            (str(total), user["id"], currency.upper()),
        )
        conn.execute("UPDATE deals SET status='done' WHERE short_id=?", (short_id,))
        conn.commit()
        updated = conn.execute("SELECT * FROM deals WHERE short_id=?", (short_id,)).fetchone()
    if updated["creator_tg_id"] and updated["creator_tg_id"] != user["id"]:
        await send_message(updated["creator_tg_id"], f"✅ Тестовая оплата сделки {html.escape(updated['deal_id'])} подтверждена.")
    return row_to_deal(updated)


@app.get("/api/balances")
async def balances(request: Request):
    user = telegram_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute("SELECT currency, amount FROM demo_balances WHERE tg_id=?", (user["id"],)).fetchall()
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


@app.get("/api/admin/me")
async def admin_status(request: Request):
    user = admin_user(request)
    with contextlib.closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT currency, amount FROM demo_balances WHERE tg_id=? ORDER BY currency", (user["id"],)
        ).fetchall()
    return {"id": user["id"], "demoBalances": {r["currency"]: r["amount"] for r in rows}}


@app.post("/api/admin/demo-credit")
async def admin_credit(request: Request):
    user = admin_user(request)
    body = await request.json()
    amount = parse_amount(body.get("amount"))
    currency = str(body.get("currency", "")).upper()
    return {"currency": currency, "demoBalance": credit_demo_balance(user["id"], currency, amount)}


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
        url = f"{PUBLIC_URL}/?startapp={payload}" if payload else PUBLIC_URL
        await send_welcome(chat_id, url)
    elif command == ADMIN_UNLOCK_COMMAND:
        if chat.get("type") != "private" or (ADMIN_TG_IDS and sender_id not in ADMIN_TG_IDS):
            await send_message(chat_id, "Команда недоступна для этого аккаунта.")
        else:
            with contextlib.closing(get_db()) as conn:
                conn.execute("INSERT OR IGNORE INTO admin_grants(tg_id) VALUES (?)", (sender_id,))
                conn.commit()
            await send_message(
                chat_id, "🔐 Админ-панель открыта для вашего Telegram ID.",
                f"{PUBLIC_URL}/?admin=1", "Открыть админ-панель",
            )
    elif command == "/work":
        if chat.get("type") != "private" or (ADMIN_TG_IDS and sender_id not in ADMIN_TG_IDS):
            await send_message(chat_id, "Команда недоступна для этого аккаунта.")
        else:
            with contextlib.closing(get_db()) as conn:
                grant = conn.execute("SELECT 1 FROM admin_grants WHERE tg_id=?", (sender_id,)).fetchone()
            if not grant:
                await send_message(chat_id, f"Сначала введите {html.escape(ADMIN_UNLOCK_COMMAND)}.")
            else:
                args = text.split()
                if len(args) == 3:
                    try:
                        amount = parse_amount(args[1])
                        total = credit_demo_balance(sender_id, args[2].upper(), amount)
                        await send_message(chat_id, f"🧪 Тестовый баланс: {html.escape(total)} {html.escape(args[2].upper())}. Это не реальные деньги.")
                    except HTTPException:
                        await send_message(chat_id, "Формат: /work 100 USDT. Только тестовый баланс.")
                else:
                    await send_message(
                        chat_id, "🧪 В панели можно выдать себе тестовый баланс. Это не реальные деньги.",
                        f"{PUBLIC_URL}/?admin=1", "Открыть админ-панель",
                    )
    return {"ok": True}


@app.on_event("startup")
async def set_webhook():
    if not BOT_TOKEN or not PUBLIC_URL.startswith("https://"):
        print("Webhook skipped: BOT_TOKEN and HTTPS PUBLIC_URL are required")
        return
    try:
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
