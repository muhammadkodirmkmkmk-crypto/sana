# -*- coding: utf-8 -*-
"""Sana Bogatir — сервер: API, статика и Telegram."""
import asyncio
import hashlib
import hmac
import os
import time

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import actions, bot, db, state
from app.config import NAMES, PINS

SECRET = os.getenv("SECRET", "sana-" + (os.getenv("RAILWAY_PROJECT_ID") or "local")).encode()
TTL = 60 * 60 * 24 * 30          # месяц

app = FastAPI(title="Sana Bogatir")


# ------------------------------------------------------------------ токены
def make_token(role: str) -> str:
    exp = str(int(time.time()) + TTL)
    sig = hmac.new(SECRET, f"{role}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{role}.{exp}.{sig}"


def read_token(tok: str | None) -> str:
    if not tok:
        raise HTTPException(401, "no token")
    try:
        role, exp, sig = tok.split(".")
    except ValueError:
        raise HTTPException(401, "bad token")
    good = hmac.new(SECRET, f"{role}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or int(exp) < time.time():
        raise HTTPException(401, "expired")
    return role


# ------------------------------------------------------------------ старт
@app.on_event("startup")
async def _start():
    await db.init_db()
    async with db.Session() as s:
        await state.seed(s)
    asyncio.create_task(bot.debt_watch())
    asyncio.create_task(bot.daily_report())
    asyncio.create_task(bot.poll())


@app.get("/health")
async def health():
    """Что настроено: сами значения не показываем, только «заполнено или нет»."""
    return {
        "ok": True,
        "db": db.DB_URL.split("@")[-1][:40],
        "tg": bool(os.getenv("TG_TOKEN")),
        "bot": await bot.who_am_i(),
        "tg_checker": bool(os.getenv("TG_CHECKER")),
        "tg_director": bool(os.getenv("TG_DIRECTOR")),
        "tg_group": bool(os.getenv("TG_GROUP")),
        "pins_default": [r for r, v in PINS.items()
                         if v in ("1111", "2222", "3333", "9999")],
        "secret_set": bool(os.getenv("SECRET")),
    }


# ------------------------------------------------------------------ API
@app.post("/api/login")
async def login(body: dict = Body(...)):
    role, pin = body.get("role", ""), str(body.get("pin", ""))
    if role not in PINS or pin != PINS[role]:
        raise HTTPException(403, "bad pin")
    return {"token": make_token(role), "role": role, "name": NAMES[role]}


@app.get("/api/state")
async def get_state(authorization: str | None = Header(None)):
    role = read_token((authorization or "").replace("Bearer ", ""))
    async with db.Session() as s:
        data = await state.build(s, role=role)
    data["role"] = role
    data["names"] = NAMES
    return data


@app.post("/api/do")
async def do(body: dict = Body(...), authorization: str | None = Header(None)):
    role = read_token((authorization or "").replace("Bearer ", ""))
    async with db.Session() as s:
        try:
            await actions.run(s, role, body.get("action", ""), body.get("data") or {})
        except actions.Denied:
            raise HTTPException(403, "not allowed")
        except actions.Bad as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        data = await state.build(s, role=role)
    data["role"] = role
    data["names"] = NAMES
    return data


@app.get("/api/report")
async def api_report(authorization: str | None = Header(None)):
    role = read_token((authorization or "").replace("Bearer ", ""))
    if role != "director":
        raise HTTPException(403, "director only")
    async with db.Session() as s:
        return {"text": await bot.report_text(s)}


# ------------------------------------------------------------------ статика
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
