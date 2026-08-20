# -*- coding: utf-8 -*-
"""Сборка состояния для фронта и значения по умолчанию."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from . import db, parts
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
    # разовая правка: «Chekni bekor qilish» открыто и сотувчи — это просили отдельно.
    # Делается один раз: если директор потом заберёт право, оно не вернётся.
    if "permFix_seller_del2" not in have:
        row = await s.get(db.Setting, "perm")
        cur = (row.val or {}).get("v") if row else None
        if row and isinstance(cur, dict) and cur.get("delete_chek") is not None:
            lst = list(cur.get("delete_chek") or [])
            if "seller" not in lst:
                row.val = {"v": {**cur, "delete_chek": lst + ["seller"]}}
        s.add(db.Setting(key="permFix_seller_del2", val={"v": True}))
    await s.commit()
    # старые расходы и оплаты переносим в кассовую книгу — один раз, при обновлении
    if "cashBackfill1" not in have:
        try:
            n = await backfill_cash(s)
        except Exception as e:                # касса не должна ронять запуск программы
            print("backfill_cash:", e)
            await s.rollback()
        else:
            s.add(db.Setting(key="cashBackfill1", val={"v": n}))
            await s.commit()


def _day_of(val, fallback=None):
    """День движения денег по местному времени цеха."""
    if isinstance(val, str) and val:
        try:
            d = datetime.fromisoformat(val.replace("Z", ""))
        except ValueError:
            d = None
        if d:
            if d.tzinfo is None:            # в истории платежей лежит UTC без зоны
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(TZ).date()
    if isinstance(val, datetime):
        d = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return d.astimezone(TZ).date()
    if fallback is not None:
        return _day_of(fallback)
    return datetime.now(TZ).date()


async def backfill_cash(s) -> int:
    """Старые движения денег (до кассовой книги) переносим в кассу задним числом.

    Повтор безопасен: строка, которая уже есть в кассе, второй раз не добавится.
    """
    from . import actions as _acts

    seen = set()
    for r in (await s.execute(select(db.CashFlow))).scalars().all():
        seen.add((r.ref, r.day, r.dir, int(r.amount or 0)))
    added = 0

    def put(*, day, dr, way, who, title, amount, ref, by):
        nonlocal added
        amount = int(amount or 0)
        if amount <= 0:
            return
        key = (ref, day, dr, amount)
        if key in seen:                     # это движение уже в кассе
            return
        seen.add(key)
        s.add(db.CashFlow(day=day, dir=dr, way=_acts.cash_way(way), who=(who or "")[:120],
                          title=(title or "")[:120], amount=amount, ref=ref, by=by))
        added += 1

    def from_pays(row, *, dr, who, title, ref, first_title=None, first_in_pays=False):
        """Платежи из истории документа + остаток, записанный сразу при заводе.

        У чека первая оплата лежит в истории, у поставки — только в поле «paid».
        """
        got = 0
        for i, p in enumerate(row.pays or []):
            amount = int((p or {}).get("amount") or 0)
            got += amount
            put(day=_day_of(p.get("at"), row.at), dr=dr, way=p.get("way"), who=who,
                title=(first_title or title) if (first_in_pays and i == 0) else title,
                amount=amount, ref=ref, by=p.get("by") or row.by)
        rest = int(row.paid or 0) - got      # оплата при заведении документа
        if rest > 0:
            put(day=_day_of(row.at), dr=dr, way="cash", who=who,
                title=first_title or title, amount=rest, ref=ref, by=row.by)

    names = {c.id: c.name for c in (await s.execute(select(db.Client))).scalars().all()}

    for row in (await s.execute(select(db.Expense))).scalars().all():
        put(day=row.day, dr="out", way="cash", who=row.name, title="xarajat",
            amount=row.amount, ref=f"xar:{row.day.isoformat()}", by=row.by)

    for row in (await s.execute(select(db.Sale))).scalars().all():
        if row.returned or row.status in ("del", "sent"):
            continue                         # отменённый чек денег не приносил
        from_pays(row, dr="in", who=names.get(row.client_id, "Mijozsiz"),
                  title=f"qarz №{row.id}", ref=f"chek:{row.id}",
                  first_title=f"chek №{row.id}", first_in_pays=True)

    for row in (await s.execute(select(db.Debt))).scalars().all():
        from_pays(row, dr="in", who=names.get(row.client_id, "Mijozsiz"),
                  title="qo'lda qarz", ref=f"debt:{row.id}")

    for row in (await s.execute(select(db.Supply))).scalars().all():
        from_pays(row, dr="out", who=row.who, title="ta'minotchiga to'lov",
                  ref=f"sup:{row.id}",
                  first_title="un uchun to'lov" if row.kind == "un" else "qop uchun to'lov")

    for row in (await s.execute(select(db.Buy))).scalars().all():
        from_pays(row, dr="out", who=row.who, title="tayyor mahsulot uchun",
                  ref=f"buy:{row.id}")

    for row in (await s.execute(select(db.Prepay))).scalars().all():
        from_pays(row, dr="out", who=row.who, title="buyurtmaga oldindan to'lov",
                  ref=f"pre:{row.id}", first_title="oldindan to'lov")

    for row in (await s.execute(select(db.Fault))).scalars().all():
        if row.cost:
            put(day=_day_of(row.fixed_at, row.at), dr="out", way="cash", who=row.fixed_by,
                title="ta'mir", amount=row.cost, ref=f"fix:{row.id}", by="director")

    if added:
        await s.commit()
    return added


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
    # поломки видят все, кто допущен в раздел, но стоимость ремонта — только с правом на суммы
    data["faults"] = [{**x, "cost": 0} for x in data["faults"]]
    data["expenses"] = []
    data["cash"] = []
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
    cash = (await s.execute(select(db.CashFlow).order_by(db.CashFlow.id.desc()).limit(800))).scalars().all()
    fxs = (await s.execute(select(db.Fault).order_by(db.Fault.id.desc()).limit(400))).scalars().all()
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
        # поломки в цехе: что сломалось, когда, починили ли
        "faults": [{"id": x.id, "at": _dt(x.at), "part": x.part, "text": x.text, "who": x.who,
                    "src": x.src, "status": x.status, "fixedAt": _dt(x.fixed_at),
                    "fixedBy": x.fixed_by, "cost": x.cost, "note": x.note} for x in fxs],
        "parts": [{"id": k, "name": n, "zone": z} for k, n, z in parts.PARTS],
        # касса: каждое движение денег — наличные и по счёту фирмы
        "cash": [{"id": x.id, "at": _dt(x.at), "day": x.day.isoformat(), "dir": x.dir,
                  "way": x.way, "who": x.who, "title": x.title, "amount": x.amount,
                  "ref": x.ref, "by": x.by} for x in reversed(cash)],
        "expNames": EXPENSE_NAMES,
        "perm": {**{k: sorted(v) for k, v in _acts.VIEWS.items()},
                 **{k: sorted(v) for k, v in _acts.RIGHTS.items()},
                 **{k: v for k, v in (st.get("perm") or {}).items()}},
        "settings": st,
        "today": datetime.now(TZ).date().isoformat(),
        "server_time": datetime.now(TZ).isoformat(),
    }
    return out if money else _strip(out, role)
