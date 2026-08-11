# -*- coding: utf-8 -*-
"""Telegram: напоминания Обиду о долгах и вечерний отчёт директору."""
import asyncio
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from . import db, state
from .config import REPORT_HOUR, TG_CHECKER, TG_DIRECTOR, TG_TOKEN
from .state import TZ

API = "https://api.telegram.org/bot{}/{}"


def fmt(n) -> str:
    return f"{int(n):,}".replace(",", " ")


async def send(chat_id: str, text: str):
    if not (TG_TOKEN and chat_id):
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(API.format(TG_TOKEN, "sendMessage"),
                             json={"chat_id": chat_id, "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": True})
            return r.status_code == 200
    except Exception:
        return False


async def debt_watch():
    """Раз в час: долги, у которых сегодня срок или срок прошёл."""
    while True:
        try:
            async with db.Session() as s:
                today = datetime.now(TZ).date()
                rows = (await s.execute(select(db.Sale).where(
                    db.Sale.debt > 0, db.Sale.returned.is_(False),
                    db.Sale.notified.is_(False)))).scalars().all()
                man = (await s.execute(select(db.Debt).where(
                    db.Debt.debt > 0, db.Debt.notified.is_(False)))).scalars().all()
                due = [x for x in list(rows) + list(man) if x.due and x.due <= today]
                if due:
                    names = {c.id: c.name for c in
                             (await s.execute(select(db.Client))).scalars().all()}
                    lines = []
                    for x in due:
                        late = (today - x.due).days
                        when = "bugun muddati" if late == 0 else f"{late} kun kechikdi"
                        lines.append(f"• {names.get(x.client_id, '—')} — <b>{fmt(x.debt)}</b> so'm ({when})")
                        x.notified = True
                    total = sum(x.debt for x in due)
                    await send(TG_CHECKER,
                               "⏰ <b>Qarz muddati keldi</b>\n\n" + "\n".join(lines) +
                               f"\n\nJami: <b>{fmt(total)}</b> so'm")
                    await s.commit()
        except Exception:
            pass
        await asyncio.sleep(3600)


async def daily_report():
    """Каждый вечер: сводка директору."""
    sent_on = None
    while True:
        try:
            now = datetime.now(TZ)
            if now.hour >= REPORT_HOUR and sent_on != now.date():
                async with db.Session() as s:
                    txt = await report_text(s, now)
                if await send(TG_DIRECTOR, txt):
                    sent_on = now.date()
        except Exception:
            pass
        await asyncio.sleep(600)


async def report_text(s, now=None) -> str:
    now = now or datetime.now(TZ)
    st = await state.settings(s)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sales = (await s.execute(select(db.Sale).where(db.Sale.at >= day0))).scalars().all()
    live = [x for x in sales if x.status != "sent" and not x.returned]
    total = sum(x.sum for x in live)
    kg = sum(x.kg for x in live)
    gross = total - sum(x.cost for x in live)
    cash = sum(x.paid for x in live if x.status == "paid" and x.pay == "cash")
    xar = sum(int(v or 0) for v in (st.get("exp") or {}).values())
    today_exp = sum(x.amount for x in (await s.execute(select(db.Expense).where(
        db.Expense.day == now.date()))).scalars().all())
    net = gross - xar / 30 - today_exp

    prods = (await s.execute(select(db.Product))).scalars().all()
    stock = sum(int(v or 0) * int(k) for p in prods for k, v in (p.stock or {}).items())

    flour_in = int(st.get("flourIn") or 0)
    produced = int(st.get("produced") or 0)
    exp = round(flour_in * float(st.get("norm") or 0.92))
    diff = produced - exp

    waiting = len([x for x in sales if x.status == "sent" and not x.returned])
    debts = list((await s.execute(select(db.Sale).where(
        db.Sale.debt > 0, db.Sale.returned.is_(False)))).scalars().all())
    debts += list((await s.execute(select(db.Debt).where(db.Debt.debt > 0))).scalars().all())
    late = len([x for x in debts if x.due and x.due <= now.date()])
    sup_debt = sum(x.debt for x in (await s.execute(select(db.Supply).where(
        db.Supply.debt > 0))).scalars().all())

    return (
        f"📊 <b>Sana Bogatir</b> — {now:%d.%m.%Y}\n\n"
        f"Savdo: <b>{fmt(total)}</b> so'm ({len(live)} chek)\n"
        f"Sotilgan: {fmt(kg)} kg\n"
        f"Yalpi foyda: {fmt(gross)} so'm\n"
        f"Bugungi xarajat: {fmt(today_exp)} so'm\n"
        f"Sof foyda: <b>{fmt(net)}</b> so'm\n"
        f"Naqd: {fmt(cash)} so'm\n\n"
        f"Ombordagi mahsulot: {fmt(stock)} kg\n"
        f"Un: {fmt(flour_in)} kg → kutilgan {fmt(exp)} kg, chiqdi {fmt(produced)} kg\n"
        f"Farq: <b>{'+' if diff > 0 else ''}{fmt(diff)} kg</b> {'✅' if diff >= 0 else '⚠️'}\n\n"
        f"Tekshiruv kutayotgan cheklar: {waiting}\n"
        f"Qarzlar: {fmt(sum(x.debt for x in debts))} so'm"
        + (f" ({late} ta muddati keldi ⚠️)" if late else "")
        + (f"\nTa'minotchiga qarz: {fmt(sup_debt)} so'm" if sup_debt else "")
    )


async def poll():
    """Ловим /start, чтобы узнать chat_id — его вписывают в переменные Railway."""
    if not TG_TOKEN:
        return
    offset = None
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                r = await c.get(API.format(TG_TOKEN, "getUpdates"),
                                params={"timeout": 30, "offset": offset})
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    chat = msg.get("chat") or {}
                    text = (msg.get("text") or "").strip()
                    if not chat:
                        continue
                    if text.startswith("/start") or text.startswith("/id"):
                        await send(str(chat["id"]),
                                   f"Sana Bogatir 🍝\nSizning chat_id: <code>{chat['id']}</code>\n\n"
                                   "Bu raqamni Railway → Variables ga yozing:\n"
                                   "TG_CHECKER — qarz eslatmalari uchun\n"
                                   "TG_DIRECTOR — kunlik hisobot uchun")
                    elif text.startswith("/hisobot") or text.startswith("/report"):
                        async with db.Session() as s:
                            await send(str(chat["id"]), await report_text(s))
        except Exception:
            await asyncio.sleep(5)
