# -*- coding: utf-8 -*-
"""Вся арифметика — на сервере. Клиент только присылает намерение."""
from datetime import date, datetime

from sqlalchemy import select

from . import db, state
from .config import NAMES

PRICE = {1: 7500, 5: 37500, 12: 81600}
WAYS = {"cash": "naqd", "card": "plastik", "transfer": "o'tkazma", "click": "Click/Payme"}
# у спагетти своя цена — 10 000 сум за 1 кг
PPRICE = {"sp_pautinka": {1: 10000}, "sp_lapsha": {1: 10000}}


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
    "add_supply":      {"director", "checker"},
    "pay_supply":      {"director", "checker"},
    "del_supply":      {"director"},
    "set_qop":         {"director", "checker"},
    "add_expense":     {"director", "checker"},
    "del_expense":     {"director"},
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


async def _cost_fn(s):
    st = await state.settings(s)
    avg = await _flour_avg(s)
    norm = float(st.get("norm") or 0.92)
    pack = {int(k): int(v or 0) for k, v in (st.get("packCost") or {}).items()}

    def cost(p: int) -> int:
        return round(p * avg / norm) + pack.get(p, 0)
    return cost


async def _log(s, who, kind, text=""):
    s.add(db.LogRow(who=who, kind=kind, text=text))


def _m(n) -> str:
    """1000000 → «1 000 000»"""
    return f"{int(n or 0):,}".replace(",", " ")


def _line(main, *det) -> str:
    """Строка журнала: слева суть, справа подробности — «главное|детали»."""
    d = " · ".join(str(x) for x in det if x)
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
    cst = sum(cost(int(i["pack"])) * int(i["n"]) for i in items)
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
        _m(v) for v in price.values()) if price else "standart narx"))
    return c.id


async def do_set_client_price(s, role, d):
    c = await s.get(db.Client, int(d["id"]))
    if not c:
        raise Bad("client")
    c.price = d.get("price") or None
    await _log(s, role, "a_price", _line(c.name, " / ".join(
        _m(v) for v in c.price.values()) if c.price else "standart narxga qaytdi"))


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
    await _log(s, role, "a_cli_del", _line(c.name, c.phone))


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
        f"{_m(kg)} kg", await _items_txt(s, clean)))


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
        await _log(s, role, "a_edit", _line(_m(sale.sum), f"№{sale.id}", who))
    await _log(s, role, "a_check", _line(
        _m(sale.sum), f"№{sale.id}", who, f"{_m(sale.kg)} kg",
        "ombordan yechildi: " + await _items_txt(s, clean)))


async def do_delete_chek(s, role, d):
    sale = await s.get(db.Sale, int(d["id"]))
    if not sale or sale.status != "sent":
        raise Bad("sale")
    sale.returned = True
    await _log(s, role, "a_del", _line(
        _m(sale.sum), f"№{sale.id}", await _cname(s, sale.client_id),
        "ombor tegilmadi"))


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
        else "to'liq to'landi"))


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
        f"qoldi {_m(sale.debt)}" if sale.debt else "qarz yopildi"))


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
    await _log(s, role, "a_debt_add", _line(
        _m(amount), client.name if client else "Mijozsiz", "qo'lda",
        f"muddat {due:%d.%m}" if due else "muddatsiz", row.note))


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
        f"qoldi {_m(row.debt)}" if row.debt else "qarz yopildi"))


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
        "omborga qaytdi: " + await _items_txt(s, sale.items)))


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
    p = await s.get(db.Product, d.get("id"))
    pack, n = int(d.get("pack") or 0), int(d.get("n") or 0)
    if not p or pack not in p.packs or n <= 0:
        raise Bad("item")
    st = dict(p.stock)
    st[str(pack)] = int(st.get(str(pack), 0)) + n
    p.stock = st
    cfg = await state.settings(s)
    await state.set_setting(s, "produced", int(cfg.get("produced") or 0) + n * pack)
    # на каждую упаковку уходит один мешок/пакет от поставщика
    await state.set_setting(s, "qopUsed", int(cfg.get("qopUsed") or 0) + n)
    await _log(s, role, "a_in", _line(
        f"{_m(n)} dona", p.name, f"{pack} kg o'ram", f"{_m(n * pack)} kg omborga",
        f"{_m(n)} qop ishlatildi"))


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
    kind = "qop" if d.get("kind") == "qop" else "un"
    qty = int(d.get("qty") or 0)
    price = int(d.get("price") or 0)
    if qty <= 0:
        raise Bad("qty")
    total = qty * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    row = db.Supply(kind=kind, who=(d.get("who") or "").strip()[:120], qty=qty, price=price,
                    sum=total, paid=paid, debt=total - paid,
                    due=date.fromisoformat(d["due"]) if (total - paid) and d.get("due") else None,
                    note=(d.get("note") or "").strip()[:200], by=role)
    s.add(row)
    if kind == "un":                       # мука от поставщика = приход муки
        s.add(db.FlourLot(kg=qty, price=price or await _flour_avg(s), by=role))
        st = await state.settings(s)
        await state.set_setting(s, "flourIn", int(st.get("flourIn") or 0) + qty)
    await _log(s, role, "a_sup", _line(
        f"{_m(qty)} {'kg un' if kind == 'un' else 'dona qop'}",
        row.who or "ta'minotchi yozilmagan", f"1 birlik {_m(price)}", f"jami {_m(total)}",
        f"to'landi {_m(paid)}", f"qarz {_m(total - paid)}" if total - paid else "to'liq to'langan",
        "omborga un kirimi" if kind == "un" else ""))


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
        f"qoldi {_m(row.debt)}" if row.debt else "qarz yopildi"))


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
        _m(total), f"{day:%d.%m}", ", ".join(names)))


async def do_del_expense(s, role, d):
    row = await s.get(db.Expense, int(d["id"]))
    if not row:
        raise Bad("expense")
    await _log(s, role, "a_xar_del", _line(_m(row.amount), row.name, f"{row.day:%d.%m}"))
    await s.delete(row)
