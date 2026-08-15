# -*- coding: utf-8 -*-
"""Вся арифметика — на сервере. Клиент только присылает намерение."""
from datetime import date, datetime

from sqlalchemy import select

from . import db, state
from .config import NAMES

PRICE = {1: 7500, 5: 37500, 12: 81600}
WAYS = {"cash": "naqd", "card": "plastik", "transfer": "o'tkazma", "click": "Click/Payme"}
# упаковка: мешки штуками, рулонная плёнка и пакеты — килограммами
QOPS = {"qop": "Qop", "qop1": "Rulon paket 1 kg", "qop2": "Rulon paket 2 kg",
        "qop5": "Rulon paket 5 kg", "qopsp": "Spagetti paket", "qopblok": "Blok paket"}
# у спагетти своя цена — 10 000 сум за 1 кг
PPRICE = {"sp_pautinka": {1: 10000}, "sp_lapsha": {1: 10000}, "chiqindi": {1: 3000}}
WASTE = {"chiqindi"}   # отход: муки на него не считаем и мешок не тратим


def base_price(pid: str, pack: int) -> int:
    own = PPRICE.get(pid) or {}
    return int(own.get(int(pack)) or PRICE[int(pack)])


# кто что имеет право делать
RIGHTS = {
    "send_chek":       {"seller", "director"},
    "add_client":      {"seller", "checker", "director"},
    "set_client_price": {"checker", "director"},
    "del_client":      {"director"},
    "confirm_chek":    {"checker", "director"},
    "delete_chek":     {"checker", "director"},
    "pay_chek":        {"checker", "director"},
    "pay_debt":        {"checker", "director"},
    "add_debt":        {"checker", "director"},
    "pay_debt_manual": {"checker", "director"},
    "del_debt":        {"director"},
    "return_sale":     {"director"},
    "add_flour":       {"store", "director"},
    "add_stock":       {"store", "director"},
    "inventory":       {"store", "director"},
    "set_settings":    {"director"},
    "set_exp":         {"director"},
    "add_supply":      {"director", "checker", "store"},
    "set_supply_price": {"director"},
    "pay_supply":      {"director", "checker"},
    "del_supply":      {"director"},
    "set_qop":         {"director", "checker"},
    "add_expense":     {"director", "checker"},
    "del_expense":     {"director"},
    "add_buy":         {"director", "checker"},
    "pay_buy":         {"director", "checker"},
    "del_buy":         {"director"},
    "set_buy_price":   {"director"},
    "reset_data":      {"director"},
    "del_supplier":    {"director"},
}


class Denied(Exception):
    pass


class Bad(Exception):
    pass


async def _flour_avg(s) -> int:
    lots = (await s.execute(select(db.FlourLot))).scalars().all()
    kg = sum(l.kg for l in lots if l.kg > 0 and l.price > 0)
    if kg:
        return round(sum(l.kg * l.price for l in lots if l.kg > 0 and l.price > 0) / kg)
    st = await state.settings(s)
    return int(st.get("flourPrice") or 0)


async def _remember_supplier(s, who: str):
    """Новое имя поставщика само попадает в список — дальше его выбирают из списка."""
    who = (who or "").strip()
    if not who:
        return
    st = await state.settings(s)
    lst = list(st.get("suppliers") or [])
    if not any(x.strip().lower() == who.lower() for x in lst):
        lst.append(who)
        await state.set_setting(s, "suppliers", lst)


async def _price_for(s, key, given: int, role: str) -> int:
    """Цену ставит только директор. Она запоминается — дальше система считает сама."""
    st = await state.settings(s)
    last = dict(st.get("lastPrice") or {})
    if role == "director" and given > 0:
        last[key] = given
        await state.set_setting(s, "lastPrice", last)
        return given
    return int(last.get(key) or 0)


async def _buy_avg(s) -> dict:
    """Средняя цена 1 кг по каждому купленному товару."""
    rows = (await s.execute(select(db.Buy))).scalars().all()
    agg = {}
    for r in rows:
        if r.kg > 0 and r.price > 0:
            a = agg.setdefault(r.pid, [0, 0])
            a[0] += r.kg
            a[1] += r.kg * r.price
    return {pid: round(v[1] / v[0]) for pid, v in agg.items() if v[0]}


async def _cost_fn(s):
    st = await state.settings(s)
    avg = await _flour_avg(s)
    norm = float(st.get("norm") or 0.92)
    pack = {int(k): int(v or 0) for k, v in (st.get("packCost") or {}).items()}
    buy = await _buy_avg(s)

    def cost(p: int, pid: str = "") -> int:
        if pid in WASTE:                         # отход: тратим только упаковку
            return pack.get(p, 0)
        if pid and buy.get(pid):                 # товар куплен готовым — мука не при чём
            return round(p * buy[pid]) + pack.get(p, 0)
        return round(p * avg / norm) + pack.get(p, 0)
    return cost


async def _log(s, who, kind, text=""):
    s.add(db.LogRow(who=who, kind=kind, text=text))


def _m(n) -> str:
    """1000000 → «1 000 000»"""
    return f"{int(n or 0):,}".replace(",", " ")


def _line(main, *det, ref: str = "") -> str:
    """Строка журнала: «главное|детали|ссылка». Ссылка открывает сам документ."""
    d = " · ".join(str(x) for x in det if x)
    if ref:
        return f"{main}|{d}|{ref}"
    return f"{main}|{d}" if d else str(main)


async def _cname(s, cid) -> str:
    if not cid:
        return "Mijozsiz"
    c = await s.get(db.Client, int(cid))
    return c.name if c else "Mijozsiz"


async def _items_txt(s, items) -> str:
    prods = {p.id: p.name for p in (await s.execute(select(db.Product))).scalars().all()}
    return ", ".join(f"{prods.get(i['id'], i['id'])} {i['pack']}×{i['n']}" for i in items)


def _price_of(client, pack, pid=None) -> int:
    if pid and (PPRICE.get(pid) or {}).get(int(pack)):
        return base_price(pid, pack)        # своя цена товара сильнее договорной
    if client and client.price and client.price.get(str(pack)):
        return int(client.price[str(pack)])
    return PRICE[pack]


def _totals(items, cost):
    total = sum(int(i["price"]) * int(i["n"]) for i in items)
    kg = sum(int(i["pack"]) * int(i["n"]) for i in items)
    cst = sum(cost(int(i["pack"]), i["id"]) * int(i["n"]) for i in items)
    return total, kg, cst


async def _reserved(s, pid, pack, skip=None):
    rows = (await s.execute(select(db.Sale).where(db.Sale.status == "sent"))).scalars().all()
    n = 0
    for r in rows:
        if skip and r.id == skip:
            continue
        for it in r.items:
            if it["id"] == pid and int(it["pack"]) == int(pack):
                n += int(it["n"])
    return n


async def run(s, role: str, kind: str, data: dict):
    if kind not in RIGHTS:
        raise Bad(f"unknown action {kind}")
    if role not in RIGHTS[kind]:
        raise Denied(kind)
    fn = globals()["do_" + kind]
    await fn(s, role, data or {})
    await s.commit()


# ------------------------------------------------------------------ клиенты
async def do_add_client(s, role, d):
    name = (d.get("name") or "").strip()
    if not name:
        raise Bad("name")
    price = d.get("price") or None
    c = db.Client(name=name, phone=(d.get("phone") or "").strip(), price=price)
    s.add(c)
    await s.flush()
    await _log(s, role, "a_cli", _line(name, c.phone, "narx: " + " / ".join(
        _m(v) for v in price.values()) if price else "standart narx", ref=f"cli:{c.id}"))
    return c.id


async def do_set_client_price(s, role, d):
    c = await s.get(db.Client, int(d["id"]))
    if not c:
        raise Bad("client")
    c.price = d.get("price") or None
    await _log(s, role, "a_price", _line(c.name, " / ".join(
        _m(v) for v in c.price.values()) if c.price else "standart narxga qaytdi",
        ref=f"cli:{c.id}"))


# ------------------------------------------------------------------ чеки
async def do_del_client(s, role, d):
    """Клиент прячется из списков, но в старых чеках его имя остаётся."""
    c = await s.get(db.Client, int(d["id"]))
    if not c:
        raise Bad("client")
    debt = (await s.execute(select(db.Sale).where(
        db.Sale.client_id == c.id, db.Sale.debt > 0, db.Sale.returned.is_(False)))).scalars().first()
    man = (await s.execute(select(db.Debt).where(
        db.Debt.client_id == c.id, db.Debt.debt > 0))).scalars().first()
    if debt or man:
        raise Bad("has_debt")
    c.archived = True
    await _log(s, role, "a_cli_del", _line(c.name, c.phone, ref=f"cli:{c.id}"))


async def do_send_chek(s, role, d):
    items = d.get("items") or []
    if not items:
        raise Bad("empty")
    client = await s.get(db.Client, int(d["client"])) if d.get("client") else None
    prods = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
    clean = []
    for it in items:
        p = prods.get(it["id"])
        pack, n = int(it["pack"]), int(it["n"])
        if not p or pack not in p.packs or n <= 0:
            raise Bad("item")
        free = int(p.stock.get(str(pack), 0)) - await _reserved(s, p.id, pack)
        if n > max(0, free):
            raise Bad(f"stock:{p.name}")
        # продавец цен не видит и не присылает — считает сервер
        price = int(it.get("price") or 0) if role != "seller" else 0
        clean.append({"id": p.id, "pack": pack, "n": n,
                      "price": price or _price_of(client, pack, p.id)})
    cost = await _cost_fn(s)
    total, kg, cst = _totals(clean, cost)
    sale = db.Sale(by=role, shift_id=None, status="sent",
                   sum=total, cost=cst, kg=kg, client_id=client.id if client else None,
                   items=clean)
    s.add(sale)
    await s.flush()
    await _log(s, role, "a_send", _line(
        _m(total), f"№{sale.id}", client.name if client else "Mijozsiz",
        f"{_m(kg)} kg", await _items_txt(s, clean), ref=f"chek:{sale.id}"))


async def do_confirm_chek(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.status != "sent" or sale.returned:
        raise Bad("sale")
    prods = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
    items = d.get("items") or sale.items
    clean = []
    for it in items:
        p = prods.get(it["id"])
        pack, n = int(it["pack"]), int(it["n"])
        if not p or pack not in p.packs or n <= 0:
            raise Bad("item")
        free = int(p.stock.get(str(pack), 0)) - await _reserved(s, p.id, pack, skip=sale.id)
        if n > max(0, free):
            raise Bad(f"stock:{p.name}")
        clean.append({"id": p.id, "pack": pack, "n": n, "price": int(it["price"])})
    changed = clean != sale.items or d.get("client", sale.client_id) != sale.client_id
    if "client" in d:
        sale.client_id = int(d["client"]) if d["client"] else None
    cost = await _cost_fn(s)
    sale.items = clean
    sale.sum, sale.kg, sale.cost = _totals(clean, cost)
    for it in clean:                                   # только теперь списываем
        p = prods[it["id"]]
        st = dict(p.stock)
        st[str(it["pack"])] = max(0, int(st.get(str(it["pack"]), 0)) - it["n"])
        p.stock = st
    sale.status = "ok"
    who = await _cname(s, sale.client_id)
    if changed:
        await _log(s, role, "a_edit", _line(_m(sale.sum), f"№{sale.id}", who,
                                            ref=f"chek:{sale.id}"))
    await _log(s, role, "a_check", _line(
        _m(sale.sum), f"№{sale.id}", who, f"{_m(sale.kg)} kg",
        "ombordan yechildi: " + await _items_txt(s, clean), ref=f"chek:{sale.id}"))


async def do_delete_chek(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.status != "sent":
        raise Bad("sale")
    sale.returned = True
    await _log(s, role, "a_del", _line(
        _m(sale.sum), f"№{sale.id}", await _cname(s, sale.client_id),
        "ombor tegilmadi", ref=f"chek:{sale.id}"))


async def do_pay_chek(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.status != "ok" or sale.returned:
        raise Bad("sale")
    paid = max(0, min(sale.sum, int(d.get("paid") or 0)))
    sale.pay = d.get("pay") or "cash"
    sale.paid = paid
    sale.debt = sale.sum - paid
    sale.due = date.fromisoformat(d["due"]) if sale.debt and d.get("due") else None
    sale.notified = False
    sale.status = "paid"
    await _log(s, role, "a_pay", _line(
        _m(paid), f"№{sale.id}", await _cname(s, sale.client_id), WAYS.get(sale.pay, sale.pay),
        f"chek {_m(sale.sum)}",
        (f"qarz {_m(sale.debt)}" + (f" · {sale.due:%d.%m}" if sale.due else "")) if sale.debt
        else "to'liq to'landi", ref=f"chek:{sale.id}"))


async def do_pay_debt(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.debt <= 0:
        raise Bad("sale")
    amount = max(0, min(sale.debt, int(d.get("amount") or 0)))
    if not amount:
        raise Bad("amount")
    pays = list(sale.pays or [])
    pays.append({"at": datetime.utcnow().isoformat(), "by": role,
                 "amount": amount, "way": d.get("way") or "cash"})
    sale.pays = pays
    sale.paid += amount
    sale.debt -= amount
    if sale.debt <= 0:
        sale.debt, sale.due, sale.notified = 0, None, False
    await _log(s, role, "a_debt", _line(
        _m(amount), f"№{sale.id}", await _cname(s, sale.client_id),
        WAYS.get(d.get("way") or "cash", ""),
        f"qoldi {_m(sale.debt)}" if sale.debt else "qarz yopildi", ref=f"chek:{sale.id}"))


# ------------------------------------------------------------------ долги вручную
async def do_add_debt(s, role, d):
    amount = int(d.get("amount") or 0)
    if amount <= 0:
        raise Bad("amount")
    client = await s.get(db.Client, int(d["client"])) if d.get("client") else None
    due = date.fromisoformat(d["due"]) if d.get("due") else None
    row = db.Debt(client_id=client.id if client else None, amount=amount, paid=0,
                  debt=amount, due=due, note=(d.get("note") or "").strip()[:200], by=role)
    s.add(row)
    await s.flush()
    await _log(s, role, "a_debt_add", _line(
        _m(amount), client.name if client else "Mijozsiz", "qo'lda",
        f"muddat {due:%d.%m}" if due else "muddatsiz", row.note, ref=f"debt:{row.id}"))


async def do_pay_debt_manual(s, role, d):
    row = await s.get(db.Debt, int(d["id"]))
    if not row or row.debt <= 0:
        raise Bad("debt")
    amount = max(0, min(row.debt, int(d.get("amount") or 0)))
    if not amount:
        raise Bad("amount")
    pays = list(row.pays or [])
    pays.append({"at": datetime.utcnow().isoformat(), "by": role,
                 "amount": amount, "way": d.get("way") or "cash"})
    row.pays = pays
    row.paid += amount
    row.debt -= amount
    if row.debt <= 0:
        row.debt, row.due, row.notified = 0, None, False
    await _log(s, role, "a_debt", _line(
        _m(amount), await _cname(s, row.client_id), "qo'lda qarz",
        WAYS.get(d.get("way") or "cash", ""),
        f"qoldi {_m(row.debt)}" if row.debt else "qarz yopildi", ref=f"debt:{row.id}"))


async def do_del_debt(s, role, d):
    row = await s.get(db.Debt, int(d["id"]))
    if not row:
        raise Bad("debt")
    await _log(s, role, "a_debt_del", _line(
        _m(row.debt), await _cname(s, row.client_id), row.note))
    row.debt = 0
    row.due = None


async def do_return_sale(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.returned or sale.status == "sent":
        raise Bad("sale")
    prods = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
    for it in sale.items:
        p = prods[it["id"]]
        st = dict(p.stock)
        st[str(it["pack"])] = int(st.get(str(it["pack"]), 0)) + int(it["n"])
        p.stock = st
    sale.returned = True
    sale.debt = 0
    sale.due = None
    await _log(s, role, "a_ret", _line(
        _m(sale.sum), f"№{sale.id}", await _cname(s, sale.client_id),
        "omborga qaytdi: " + await _items_txt(s, sale.items), ref=f"chek:{sale.id}"))


# ------------------------------------------------------------------ склад
async def do_add_flour(s, role, d):
    kg, price = int(d.get("kg") or 0), int(d.get("price") or 0)
    if kg <= 0:
        raise Bad("kg")
    if price <= 0:
        price = await _flour_avg(s)
    s.add(db.FlourLot(kg=kg, price=price, by=role))
    st = await state.settings(s)
    await state.set_setting(s, "flourIn", int(st.get("flourIn") or 0) + kg)
    await _log(s, role, "a_flour", _line(
        f"{_m(kg)} kg", f"1 kg × {_m(price)}", f"jami {_m(kg * price)}", "omborga un kirimi"))


async def do_add_stock(s, role, d):
    """Сдача на склад. src=sex — наш цех из муки, src=buy — фасовка купленного товара."""
    p = await s.get(db.Product, d.get("id"))
    pack, n = int(d.get("pack") or 0), int(d.get("n") or 0)
    if not p or pack not in p.packs or n <= 0:
        raise Bad("item")
    src = "buy" if d.get("src") == "buy" else "sex"
    cfg = await state.settings(s)
    if src == "buy":
        left = (await buy_left(s)).get(p.id, 0)
        if n * pack > left:
            raise Bad(f"buy:{p.name}")
        packed = dict(cfg.get("buyPacked") or {})
        packed[p.id] = int(packed.get(p.id) or 0) + n * pack
        await state.set_setting(s, "buyPacked", packed)
    else:
        await state.set_setting(s, "produced", int(cfg.get("produced") or 0) + n * pack)
    st = dict(p.stock)
    st[str(pack)] = int(st.get(str(pack), 0)) + n
    p.stock = st
    # на каждую упаковку уходит один мешок/пакет от поставщика
    if p.id not in WASTE:
        await state.set_setting(s, "qopUsed", int(cfg.get("qopUsed") or 0) + n)
    await _log(s, role, "a_in", _line(
        f"{_m(n)} dona", p.name, f"{pack} kg o'ram", f"{_m(n * pack)} kg omborga",
        "sotib olingandan fasovka" if src == "buy" else "o'z sexi",
        f"{_m(n)} qop ishlatildi"))


async def buy_left(s) -> dict:
    """Сколько купленного товара ещё не расфасовано, по кг."""
    st = await state.settings(s)
    packed = st.get("buyPacked") or {}
    got = {}
    for r in (await s.execute(select(db.Buy))).scalars().all():
        got[r.pid] = got.get(r.pid, 0) + r.kg
    return {pid: max(0, kg - int(packed.get(pid) or 0)) for pid, kg in got.items()}


async def do_inventory(s, role, d):
    counts = d.get("counts") or {}
    prods = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
    diff = 0
    for pid, packs in counts.items():
        p = prods.get(pid)
        if not p:
            continue
        st = dict(p.stock)
        for pack, val in packs.items():
            if val in ("", None):
                continue
            diff += (int(val) - int(st.get(str(pack), 0))) * base_price(pid, pack)
            st[str(pack)] = int(val)
        p.stock = st
    await _log(s, role, "a_inv", _line(
        _m(diff), f"{len(counts)} mahsulot sanaldi",
        "kamomad" if diff < 0 else "ortiqcha" if diff > 0 else "hammasi joyida"))


# ------------------------------------------------------------------ настройки
async def do_set_settings(s, role, d):
    if "flourPrice" in d:
        await state.set_setting(s, "flourPrice", int(d["flourPrice"] or 0))
    if "packCost" in d:
        await state.set_setting(s, "packCost", {str(k): int(v or 0) for k, v in d["packCost"].items()})
    if "norm" in d:
        n = float(d["norm"])
        if 0.3 < n <= 1:
            await state.set_setting(s, "norm", n)
    await _log(s, role, "a_set", _line(
        f"un {_m(d.get('flourPrice') or 0)}",
        f"chiqish {round(float(d.get('norm') or 0.92) * 100)}%",
        "o'ram: " + " / ".join(_m(v) for v in (d.get("packCost") or {}).values())))


async def do_set_exp(s, role, d):
    await state.set_setting(s, "exp", {k: int(v or 0) for k, v in (d.get("exp") or {}).items()})
    await _log(s, role, "a_xar", _line(
        _m(sum(int(v or 0) for v in (d.get("exp") or {}).values())),
        "oylik doimiy xarajat yangilandi"))


# ------------------------------------------------------------------ поставщики
async def do_add_supply(s, role, d):
    """Поставщик привёз муку или мешки. Мука сразу ложится партией на склад."""
    kind = d.get("kind") or "un"
    if kind not in QOPS and kind != "un":
        kind = "un"
    qty = int(d.get("qty") or 0)
    price = int(d.get("price") or 0)
    if role == "store" and kind == "un":   # омборчи заводит только упаковку
        kind = "qop"
    if qty <= 0:
        raise Bad("qty")
    price = await _price_for(s, kind, price, role)   # цену задаёт директор, дальше сама
    total = qty * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    row = db.Supply(kind=kind, who=(d.get("who") or "").strip()[:120], qty=qty, price=price,
                    sum=total, paid=paid, debt=total - paid,
                    due=date.fromisoformat(d["due"]) if (total - paid) and d.get("due") else None,
                    note=(d.get("note") or "").strip()[:200], by=role)
    s.add(row)
    await s.flush()
    await _remember_supplier(s, row.who)
    if kind == "un":                       # мука от поставщика = приход муки
        s.add(db.FlourLot(kg=qty, price=price or await _flour_avg(s), by=role))
        st = await state.settings(s)
        await state.set_setting(s, "flourIn", int(st.get("flourIn") or 0) + qty)
    unit = "kg un" if kind == "un" else ("dona" if kind == "qop" else "kg")
    await _log(s, role, "a_sup", _line(
        f"{_m(qty)} {unit}" + ("" if kind == "un" else " · " + QOPS.get(kind, "")),
        row.who or "ta'minotchi yozilmagan", f"1 birlik {_m(price)}", f"jami {_m(total)}",
        f"to'landi {_m(paid)}", f"qarz {_m(total - paid)}" if total - paid else "to'liq to'langan",
        "omborga un kirimi" if kind == "un" else "", ref=f"sup:{row.id}"))


async def do_pay_supply(s, role, d):
    row = await s.get(db.Supply, int(d["id"]))
    if not row or row.debt <= 0:
        raise Bad("supply")
    amount = max(0, min(row.debt, int(d.get("amount") or 0)))
    if not amount:
        raise Bad("amount")
    pays = list(row.pays or [])
    pays.append({"at": datetime.utcnow().isoformat(), "by": role, "amount": amount})
    row.pays = pays
    row.paid += amount
    row.debt -= amount
    if row.debt <= 0:
        row.debt, row.due = 0, None
    await _log(s, role, "a_sup_pay", _line(
        _m(amount), row.who or "ta'minotchi", "ta'minotchiga to'lov",
        f"qoldi {_m(row.debt)}" if row.debt else "qarz yopildi", ref=f"sup:{row.id}"))


async def do_del_supply(s, role, d):
    row = await s.get(db.Supply, int(d["id"]))
    if not row:
        raise Bad("supply")
    await _log(s, role, "a_sup_del", _line(
        _m(row.sum), row.who or "ta'minotchi",
        f"{'un' if row.kind == 'un' else 'qop'} {_m(row.qty)}"))
    await s.delete(row)


async def do_set_qop(s, role, d):
    """Пересчёт мешков вручную: сколько осталось на самом деле."""
    left = int(d.get("left") or 0)
    got = sum(x.qty for x in (await s.execute(
        select(db.Supply).where(db.Supply.kind == "qop"))).scalars().all())
    await state.set_setting(s, "qopUsed", max(0, got - max(0, left)))
    await _log(s, role, "a_qop", _line(
        f"{_m(left)} dona", "qop qoldig'i qo'lda tenglashtirildi"))


# ------------------------------------------------------------------ расходы по дням
async def do_add_expense(s, role, d):
    """Строка шаблона + сумма. Расход ложится на тот день, когда его записали."""
    rows = d.get("rows") or []
    if not rows and d.get("name"):
        rows = [{"name": d["name"], "amount": d.get("amount")}]
    day = date.fromisoformat(d["day"]) if d.get("day") else datetime.now(state.TZ).date()
    n, total, names = 0, 0, []
    for r in rows:
        name = (r.get("name") or "").strip()[:80]
        amount = int(r.get("amount") or 0)
        if not name or amount <= 0:
            continue
        s.add(db.Expense(day=day, name=name, amount=amount, by=role))
        n += 1
        total += amount
        names.append(f"{name} {_m(amount)}")
    if not n:
        raise Bad("amount")
    await _log(s, role, "a_xar_add", _line(
        _m(total), f"{day:%d.%m}", ", ".join(names), ref=f"xar:{day.isoformat()}"))


async def do_del_expense(s, role, d):
    row = await s.get(db.Expense, int(d["id"]))
    if not row:
        raise Bad("expense")
    await _log(s, role, "a_xar_del", _line(_m(row.amount), row.name, f"{row.day:%d.%m}",
                                           ref=f"xar:{row.day.isoformat()}"))
    await s.delete(row)


# ------------------------------------------------------------------ покупка готового товара
async def do_add_buy(s, role, d):
    """Спагетти купили готовыми — фасуем в свои пакеты и продаём."""
    p = await s.get(db.Product, d.get("pid"))
    kg, price = int(d.get("kg") or 0), int(d.get("price") or 0)
    if not p:
        raise Bad("item")
    if kg <= 0:
        raise Bad("kg")
    price = await _price_for(s, "tovar:" + p.id, price, role)
    total = kg * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    row = db.Buy(who=(d.get("who") or "").strip()[:120], pid=p.id, kg=kg, price=price,
                 sum=total, paid=paid, debt=total - paid,
                 due=date.fromisoformat(d["due"]) if (total - paid) and d.get("due") else None,
                 note=(d.get("note") or "").strip()[:200], by=role)
    s.add(row)
    await s.flush()
    await _remember_supplier(s, row.who)
    await _log(s, role, "a_buy", _line(
        f"{_m(kg)} kg", p.name, row.who or "sotuvchi yozilmagan", f"1 kg {_m(price)}",
        f"jami {_m(total)}", f"to'landi {_m(paid)}",
        f"qarz {_m(total - paid)}" if total - paid else "to'liq to'langan",
        ref=f"buy:{row.id}"))


async def do_pay_buy(s, role, d):
    row = await s.get(db.Buy, int(d["id"]))
    if not row or row.debt <= 0:
        raise Bad("buy")
    amount = max(0, min(row.debt, int(d.get("amount") or 0)))
    if not amount:
        raise Bad("amount")
    pays = list(row.pays or [])
    pays.append({"at": datetime.utcnow().isoformat(), "by": role, "amount": amount})
    row.pays = pays
    row.paid += amount
    row.debt -= amount
    if row.debt <= 0:
        row.debt, row.due = 0, None
    await _log(s, role, "a_buy_pay", _line(
        _m(amount), row.who or "sotuvchi", "tayyor mahsulot uchun to'lov",
        f"qoldi {_m(row.debt)}" if row.debt else "qarz yopildi", ref=f"buy:{row.id}"))


async def do_del_buy(s, role, d):
    row = await s.get(db.Buy, int(d["id"]))
    if not row:
        raise Bad("buy")
    await _log(s, role, "a_buy_del", _line(_m(row.sum), row.who or "sotuvchi", f"{_m(row.kg)} kg"))
    await s.delete(row)


# ------------------------------------------------------------------ очистка данных
async def do_reset_data(s, role, d):
    """Директор чистит базу. what: savdo | ombor | hammasi."""
    what = d.get("what") or "savdo"
    if str(d.get("word") or "").strip().upper() != "TOZALASH":
        raise Bad("word")
    from sqlalchemy import delete
    await s.execute(delete(db.Sale))
    await s.execute(delete(db.Debt))
    await s.execute(delete(db.Expense))
    await s.execute(delete(db.Supply))
    await s.execute(delete(db.Buy))
    await s.execute(delete(db.FlourLot))
    await state.set_setting(s, "flourIn", 0)
    await state.set_setting(s, "produced", 0)
    await state.set_setting(s, "qopUsed", 0)
    await state.set_setting(s, "buyPacked", {})
    if what in ("ombor", "hammasi"):
        for p in (await s.execute(select(db.Product))).scalars().all():
            p.stock = {str(k): 0 for k in p.packs}
    if what == "hammasi":
        await s.execute(delete(db.Client))
        await state.set_setting(s, "exp", dict(state.DEFAULTS["exp"]))
    await s.execute(delete(db.LogRow))
    await _log(s, role, "a_reset", _line(
        {"savdo": "savdo va qarzlar", "ombor": "savdo + ombor qoldig'i",
         "hammasi": "hammasi"}.get(what, what), "ma'lumotlar tozalandi"))


async def do_set_supply_price(s, role, d):
    """Омборчи принял мешки без цены — Обид или директор ставит цену."""
    row = await s.get(db.Supply, int(d["id"]))
    if not row:
        raise Bad("supply")
    price = int(d.get("price") or 0)
    if price <= 0:
        raise Bad("price")
    row.price = await _price_for(s, row.kind, price, role)
    price = row.price
    row.sum = row.qty * price
    row.paid = max(0, min(row.sum, int(d.get("paid") if d.get("paid") is not None else row.paid)))
    row.debt = row.sum - row.paid
    row.due = date.fromisoformat(d["due"]) if row.debt and d.get("due") else None
    await _log(s, role, "a_sup_price", _line(
        _m(row.sum), row.who or "ta'minotchi", f"1 birlik {_m(price)}",
        f"qarz {_m(row.debt)}" if row.debt else "to'liq to'langan", ref=f"sup:{row.id}"))


async def do_set_buy_price(s, role, d):
    """Цена купленного готового товара — её ставит директор."""
    row = await s.get(db.Buy, int(d["id"]))
    if not row:
        raise Bad("buy")
    price = int(d.get("price") or 0)
    if price <= 0:
        raise Bad("price")
    row.price = await _price_for(s, "tovar:" + row.pid, price, role)
    row.sum = row.kg * row.price
    row.paid = max(0, min(row.sum, int(d.get("paid") if d.get("paid") is not None else row.paid)))
    row.debt = row.sum - row.paid
    row.due = date.fromisoformat(d["due"]) if row.debt and d.get("due") else None
    await _log(s, role, "a_sup_price", _line(
        _m(row.sum), row.who or "sotuvchi", f"1 kg {_m(row.price)}",
        f"qarz {_m(row.debt)}" if row.debt else "to'liq to'langan", ref=f"buy:{row.id}"))


async def do_del_supplier(s, role, d):
    """Убрать имя из списка поставщиков. Старые приходы остаются как были."""
    who = (d.get("who") or "").strip()
    st = await state.settings(s)
    lst = [x for x in (st.get("suppliers") or []) if x.strip().lower() != who.lower()]
    await state.set_setting(s, "suppliers", lst)
    await _log(s, role, "a_sup_del", _line(who, "ro'yxatdan olib tashlandi"))
