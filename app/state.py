# -*- coding: utf-8 -*-
"""Сборка состояния для фронта и значения по умолчанию."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from . import db
from .config import TZ_OFFSET

TZ = timezone(timedelta(hours=TZ_OFFSET))

DEFAULTS = {
    "norm": 0.92,
    "flourPrice": 0,
    "packCost": {"1": 0, "5": 0, "12": 0},
    "exp": {"gaz": 0, "el": 0, "ish": 0, "tr": 0, "ij": 0},
    "flourIn": 0,
    "produced": 0,
    "qopUsed": 0,          # мешки: израсходовано (по числу сданных упаковок)
    "qopUse": {},          # упаковка: израсходовано по каждому виду
    "buyPacked": {},       # сколько кг купленного товара уже расфасовано, по товарам
    "lastPrice": {},       # последняя цена по виду прихода: система сама считает сумму
    "perm": {},            # права доступа: что директор разрешил ролям
    # список поставщиков: пополняется сам, когда заводят новый приход
    "suppliers": ["JAMSHID SPAGETI", "ZILOLA QOP", "OQ QURGON UN"],
}

PRODUCTS = [
    ("quchqor", "Quchqor", [1, 5, 12]), ("pero", "Pero", [1, 5, 12]),
    ("speral", "Speral", [1, 5, 12]), ("burama", "Burama", [1, 5, 12]),
    ("trupka", "Trupka", [1, 5, 12]), ("zrak", "Zrak", [1, 5, 12]),
    ("rochki", "Rochki", [1, 5, 12]), ("rakushka", "Rakushka", [1, 5, 12]),
    ("kalta_pero", "Kalta Pero", [1, 5, 12]), ("gladkiy", "Gladkiy", [1, 5, 12]),
    ("manpar", "Manpar", [1, 5, 12]), ("vidkiy", "Vidkiy", [1, 5, 12]),
    ("gildirak", "Gildirak", [1, 5, 12]), ("lapsha", "Lapsha", [1, 5, 12]),
    ("pautinka", "Pautinka", [1, 5, 12]),
    ("sp_pautinka", "Spagetti Vermishel", [1]), ("sp_lapsha", "Spagetti Lapsha", [1]),
    ("chiqindi", "Chiqindi makaron", [1]),      # отход, продаётся на вес
]

# шаблон ежемесячных расходов: человек вписывает только сумму
EXPENSE_NAMES = [
    "Dastafka", "Arava", "Stayanka", "Benzin", "Moika", "Moy", "Moshina remontiga",
    "Guruchik", "Produkta", "Povir oyligi", "Bollaga oylik", "Bahrom oylik",
    "Obid oylik", "Bohodir shopir oylik", "Usta Ravshan oylik", "Usta Abduraxmon",
    "Agent Sohib oylik", "Yulkira Oq qurgon", "Yulkira bozor", "Abdumalik salapan",
    "Zilola qop", "Orif salapan", "Viloyatka rasxot", "Sexka rasxot",
    "Olimaka rasxotlar", "Oq qurgon UN", "Qolip moikaga", "Bugaltir oylik",
    "Xoji Murod oylik", "Sexka ustalaga", "Seles doktir", "Mayda chuda rasxot",
]


async def seed(s):
    """Первый запуск: товары и настройки. Дальше — досев новых товаров и имён."""
    rows = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
    for i, (pid, name, packs) in enumerate(PRODUCTS):
        p = rows.get(pid)
        if not p:
            s.add(db.Product(id=pid, name=name, packs=packs,
                             stock={str(k): 0 for k in packs}, pos=i))
            continue
        if p.name != name:          # переименование не трогает остатки и старые чеки
            p.name = name
        if list(p.packs or []) != packs:
            p.packs = packs
        if p.pos != i:
            p.pos = i
    have = set((await s.execute(select(db.Setting.key))).scalars().all())
    for k, v in DEFAULTS.items():
        if k not in have:
            s.add(db.Setting(key=k, val={"v": v}))
    await s.commit()


async def settings(s) -> dict:
    rows = (await s.execute(select(db.Setting))).scalars().all()
    out = dict(DEFAULTS)
    for r in rows:
        out[r.key] = r.val.get("v")
    return out


async def set_setting(s, key, value):
    row = await s.get(db.Setting, key)
    if row:
        row.val = {"v": value}
    else:
        s.add(db.Setting(key=key, val={"v": value}))


def _dt(d):
    return d.astimezone(TZ).isoformat() if d else None


def _strip(data: dict, role: str = "seller") -> dict:
    """Продавец и омборчи денег не видят вообще: только кг, товар и клиент."""
    data["sales"] = [{"id": x["id"], "at": x["at"], "by": x["by"], "status": x["status"],
                      "kg": x["kg"], "client": x["client"], "returned": x["returned"],
                      "sum": 0, "cost": 0, "paid": 0, "debt": 0, "pay": None,
                      "due": None, "pays": [],
                      "items": [{"id": i["id"], "pack": i["pack"], "n": i["n"], "price": 0}
                                for i in x["items"]]}
                     for x in data["sales"]]
    data["clients"] = [{**c, "price": None} for c in data["clients"]]
    data["debts"] = []
    data["buys"] = []
    if role == "seller":
        data["prepays"] = []
    data["expenses"] = []
    if role == "seller":
        data["flourLots"] = []
        data["supplies"] = []
        data["log"] = []
    else:
        # склад: партии и приходы видны, деньги — нет
        data["flourLots"] = [{**l, "price": 0} for l in data["flourLots"]]
        data["supplies"] = [{**x, "price": 0, "sum": 0, "paid": 0, "debt": 0,
                             "due": None, "pays": []} for x in data["supplies"]]
        # склад видит, сколько ещё должен привезти поставщик — но не деньги
        data["prepays"] = [{**x, "price": 0, "sum": 0, "paid": 0, "used": 0,
                            "money": 0, "pays": []} for x in data["prepays"]]
    st = dict(data["settings"])
    st.update({"flourPrice": 0, "packCost": {"1": 0, "5": 0, "12": 0},
               "exp": {k: 0 for k in DEFAULTS["exp"]}, "lastPrice": {}})
    data["settings"] = st
    return data


async def build(s, limit_sales: int = 300, role: str = "director") -> dict:
    from . import actions as _acts        # права описаны там, импорт по месту
    prods = (await s.execute(select(db.Product).order_by(db.Product.pos))).scalars().all()
    clients = (await s.execute(select(db.Client).order_by(db.Client.id))).scalars().all()
    sales = (await s.execute(
        select(db.Sale).order_by(db.Sale.id.desc()).limit(limit_sales))).scalars().all()
    lots = (await s.execute(select(db.FlourLot).order_by(db.FlourLot.id))).scalars().all()
    dbts = (await s.execute(
        select(db.Debt).where(db.Debt.debt > 0).order_by(db.Debt.id))).scalars().all()
    logs = (await s.execute(select(db.LogRow).order_by(db.LogRow.id.desc()).limit(200))).scalars().all()
    sup = (await s.execute(select(db.Supply).order_by(db.Supply.id.desc()).limit(120))).scalars().all()
    buys = (await s.execute(select(db.Buy).order_by(db.Buy.id.desc()).limit(120))).scalars().all()
    exps = (await s.execute(select(db.Expense).order_by(db.Expense.id.desc()).limit(400))).scalars().all()
    notes = (await s.execute(select(db.Note).order_by(db.Note.id.desc()).limit(500))).scalars().all()
    pres = (await s.execute(select(db.Prepay).order_by(db.Prepay.id.desc()).limit(120))).scalars().all()
    st = await settings(s)
    money = role in _acts.allowed_for(st.get("perm") or {}, "see_money")

    out = {
        "products": [{"id": p.id, "name": p.name, "packs": p.packs, "stock": p.stock} for p in prods],
        "clients": [{"id": c.id, "name": c.name, "phone": c.phone, "price": c.price,
                     "archived": c.archived} for c in clients],
        "sales": [{
            "id": x.id, "at": _dt(x.at), "by": x.by, "shift": x.shift_id, "status": x.status,
            "pay": x.pay, "sum": x.sum, "cost": x.cost, "kg": x.kg, "paid": x.paid,
            "debt": x.debt, "due": x.due.isoformat() if x.due else None,
            "client": x.client_id, "returned": x.returned, "items": x.items, "pays": x.pays,
        } for x in reversed(sales)],
        "flourLots": [{"id": l.id, "at": _dt(l.at), "kg": l.kg, "price": l.price, "by": l.by}
                      for l in lots],
        "debts": [{"id": d.id, "at": _dt(d.at), "client": d.client_id, "amount": d.amount,
                   "paid": d.paid, "debt": d.debt, "due": d.due.isoformat() if d.due else None,
                   "note": d.note, "by": d.by, "pays": d.pays} for d in dbts],
        # без права на суммы в журнал попадают только складские записи
        "log": [{"id": l.id, "at": _dt(l.at), "who": l.who, "kind": l.kind, "text": l.text}
                for l in reversed(logs)
                if money or l.kind in ("a_in", "a_qop")],
        "supplies": [{"id": x.id, "at": _dt(x.at), "kind": x.kind, "who": x.who, "qty": x.qty,
                      "price": x.price, "sum": x.sum, "paid": x.paid, "debt": x.debt,
                      "due": x.due.isoformat() if x.due else None, "note": x.note,
                      "by": x.by, "pays": x.pays} for x in sup],
        "expenses": [{"id": x.id, "at": _dt(x.at), "day": x.day.isoformat(),
                      "name": x.name, "amount": x.amount, "by": x.by} for x in exps],
        "buys": [{"id": x.id, "at": _dt(x.at), "who": x.who, "pid": x.pid, "kg": x.kg,
                  "price": x.price, "sum": x.sum, "paid": x.paid, "debt": x.debt,
                  "due": x.due.isoformat() if x.due else None, "note": x.note,
                  "by": x.by, "pays": x.pays} for x in buys],
        # сколько купленного товара ещё не расфасовано — это килограммы, их видят все
        "buyLeft": await _acts.buy_left(s),
        # упаковка: пришло и осталось по каждому виду — это штуки и кг, не деньги
        "qopGot": await _acts.qop_got(s),
        "qopLeft": await _acts.qop_left(s),
        "notes": [{"id": x.id, "at": _dt(x.at), "sale": x.sale_id, "who": x.who, "text": x.text}
                  for x in reversed(notes)],
        # предоплата: сколько заказано, сколько привезли, сколько денег осталось за поставщиком
        "prepays": [{"id": x.id, "at": _dt(x.at), "kind": x.kind, "who": x.who, "qty": x.qty,
                     "got": x.got, "left": max(0, x.qty - x.got), "price": x.price,
                     "sum": x.sum, "paid": x.paid, "used": x.used,
                     "money": max(0, x.paid - x.used), "note": x.note, "by": x.by,
                     "pays": x.pays, "done": x.done} for x in pres],
        "expNames": EXPENSE_NAMES,
        "perm": {**{k: sorted(v) for k, v in _acts.VIEWS.items()},
                 **{k: sorted(v) for k, v in _acts.RIGHTS.items()},
                 **{k: v for k, v in (st.get("perm") or {}).items()}},
        "settings": st,
        "today": datetime.now(TZ).date().isoformat(),
        "server_time": datetime.now(TZ).isoformat(),
    }
    return out if money else _strip(out, role)
