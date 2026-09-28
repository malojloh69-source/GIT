import os
import json
import sqlite3
import secrets
import contextlib
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ========== CONFIG ==========
# Основные настройки меняются в одном месте. Переменные окружения хостинга
# имеют приоритет, поэтому их также можно переопределить в панели Bothost.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:3000").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_urlsafe(16))
DB_PATH = os.environ.get("DB_PATH", "deals.db")

# Username бота используется в ссылках на сделки (без символа @).
BOT_USERNAME = os.environ.get("BOT_USERNAME", "MinappPlayerok_BOT").lstrip("@")
# Username поддержки используется во всех кнопках и правилах безопасности.
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Relaier_FunPay").lstrip("@")

WELCOME_TEXT = (
    "✨ <b>Добро пожаловать в FunPay Deals Bot</b> — ваш надёжный сервис "
    "безопасных и удобных сделок! ✨\n\n"
    "🌟 Автоматизированные сделки\n\n"
    "🌟 Реферальная система\n\n"
    "🌟 Вывод средств в любой валюте\n\n"
    "🌟 Поддержка 24/7"
)

# Необязательно: Telegram file_id изображений, которые будут отправлены
# перед приветствием в указанном порядке. Их можно заменить одним списком.
WELCOME_MEDIA_IDS = [
    "5325547803936572038",
    "5325547803936572038",
    "5312326644764018054",
    "5361841922560266597",
    "5310191758255099001",
    "5312103894875143512",
]

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()


# ========== DB ==========
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with contextlib.closing(get_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                short_id TEXT PRIMARY KEY,
                deal_id TEXT,
                type TEXT,
                amount TEXT,
                icon TEXT,
                desc TEXT,
                creator TEXT,
                creator_tg_id TEXT,
                joiner_name TEXT,
                joiner_tg_id TEXT,
                status TEXT DEFAULT 'inprogress',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


init_db()


# ========== TELEGRAM HELPERS ==========
async def tg_call(method: str, payload: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{TG_API}/{method}", json=payload)
        return r.json()


async def send_message(chat_id, text, web_app_url: Optional[str] = None, button_text: str = "Открыть сделку"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if web_app_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "web_app": {"url": web_app_url}}]]
        }
    await tg_call("sendMessage", payload)


async def send_welcome(chat_id, web_app_url: str):
    """Отправляет приветствие и, если указаны, медиа в заданном порядке."""
    if WELCOME_MEDIA_IDS:
        media = [
            {"type": "photo", "media": file_id}
            for file_id in WELCOME_MEDIA_IDS
            if str(file_id).strip()
        ]
        if media:
            try:
                await tg_call("sendMediaGroup", {"chat_id": chat_id, "media": media})
            except Exception as exc:
                # Неверные или удалённые file_id не должны ломать запуск приложения.
                print("sendMediaGroup failed:", exc)
    await send_message(
        chat_id,
        WELCOME_TEXT,
        web_app_url=web_app_url,
        button_text="Открыть сделку" if "startapp=" in web_app_url else "Открыть приложение",
    )


# ========== STATIC / MINI APP ==========
app.mount("/assets", StaticFiles(directory="static"), name="assets")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/config")
async def public_config():
    """Публичные настройки, которые нужны Mini App для ссылок и поддержки."""
    return {
        "botUsername": BOT_USERNAME,
        "supportUsername": SUPPORT_USERNAME,
    }


# ========== DEALS API ==========
@app.post("/api/deals/store")
async def store_deal(request: Request):
    body = await request.json()
    short_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
    with contextlib.closing(get_db()) as conn:
        conn.execute(
            """INSERT INTO deals (short_id, deal_id, type, amount, icon, desc, creator, creator_tg_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                short_id,
                body.get("id"),
                body.get("type"),
                body.get("amount"),
                body.get("icon", ""),
                body.get("desc", "—"),
                body.get("creator"),
                str(body.get("creatorTgId")) if body.get("creatorTgId") else None,
            ),
        )
        conn.commit()
    return {"shortId": short_id}


def row_to_deal(row) -> dict:
    return {
        "id": row["deal_id"],
        "shortId": row["short_id"],
        "type": row["type"],
        "amount": row["amount"],
        "icon": row["icon"],
        "desc": row["desc"],
        "creator": row["creator"],
        "creatorTgId": row["creator_tg_id"],
        "joinerName": row["joiner_name"],
        "joinerTgId": row["joiner_tg_id"],
        "status": row["status"],
    }


@app.get("/api/deals/{short_id}")
async def get_deal(short_id: str):
    with contextlib.closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM deals WHERE short_id = ?", (short_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row_to_deal(row)


@app.post("/api/deals/{short_id}/join")
async def join_deal(short_id: str, request: Request):
    body = await request.json()
    with contextlib.closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM deals WHERE short_id = ?", (short_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        conn.execute(
            "UPDATE deals SET joiner_name = ?, joiner_tg_id = ? WHERE short_id = ?",
            (body.get("joinerName"), str(body.get("joinerTgId")) if body.get("joinerTgId") else None, short_id),
        )
        conn.commit()

    if row["creator_tg_id"]:
        await send_message(
            row["creator_tg_id"],
            f"🤝 <b>{body.get('joinerName', 'Пользователь')}</b> присоединился к сделке {row['deal_id']}",
            web_app_url=f"{PUBLIC_URL}/?startapp={short_id}",
        )
    return {"ok": True}


@app.post("/api/deals/{short_id}/pay")
async def pay_deal(short_id: str, request: Request):
    body = await request.json()
    with contextlib.closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM deals WHERE short_id = ?", (short_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        conn.execute("UPDATE deals SET status = 'done' WHERE short_id = ?", (short_id,))
        conn.commit()

    if row["creator_tg_id"]:
        await send_message(
            row["creator_tg_id"],
            f"✅ Покупатель <b>{body.get('buyerName', '')}</b> оплатил сделку {row['deal_id']}. Проверьте эскроу.",
            web_app_url=f"{PUBLIC_URL}/?startapp={short_id}",
        )
    return {"ok": True}


@app.get("/api/balances")
async def balances():
    # заглушка под кошельки в профиле — подключите реальный источник балансов, когда будет готов
    return JSONResponse({})


# ========== TELEGRAM WEBHOOK ==========
@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    text = message.get("text", "")
    chat_id = message["chat"]["id"]

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else None
        url = f"{PUBLIC_URL}/?startapp={payload}" if payload else PUBLIC_URL
        await send_welcome(chat_id, url)
    return {"ok": True}


@app.on_event("startup")
async def set_webhook():
    url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
    try:
        result = await tg_call("setWebhook", {"url": url, "allowed_updates": ["message"]})
        print("setWebhook:", json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print("setWebhook failed (no network access?):", e)


# Bothost / standalone launcher
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3000"))
    )
