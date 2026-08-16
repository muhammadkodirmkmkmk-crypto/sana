# -*- coding: utf-8 -*-
"""Вся арифметика — на сервере. Клиент только присылает намерение."""
from datetime import date, datetime, timezone

from sqlalchemy import select

from . import db, parts, state
from .config import NAMES

PRICE = {1: 7500, 5: 37500, 12: 81600}
WAYS = {"cash": "naqd", "card": "plastik", "transfer": "o'tkazma", "click": "Click/Payme"}
# упаковка: мешки штуками, рулонная плёнка и пакеты — килограммами
QOPS = {"qop": "Qop", "qop1": "Rulon paket 1 kg", "qop2": "Rulon paket 2 kg",
        "qop5": "Rulon paket 5 kg", "qopsp": "Spagetti paket", "qopblok": "Blok paket"}
# у спагетти своя цена — 10 000 сум за 1 кг
PPRICE = {"sp_pautinka": {1: 10000}, "sp_lapsha": {1: 10000}, "chiqindi": {1: 3000}}
WASTE = {"chiqindi"}   # отход: муки на него не считаем и мешок не тратим


def pack_kind(pid: str, pack: int) -> str:
    """Что тратится на одну упаковку: у спагетти свой пакет, 12 кг — мешок."""
    if pid in WASTE:
        return ""
    if str(pid).startswith("sp_"):
        return "qopsp"
    return {1: "qop1", 2: "qop2", 5: "qop5", 12: "qop"}.get(int(pack), "")


async def qop_got(s) -> dict:
    """Сколько упаковки пришло от поставщиков — по каждому виду."""
    out = {}
    for r in (await s.execute(select(db.Supply))).scalars().all():
        if r.kind in QOPS:
            out[r.kind] = out.get(r.kind, 0) + int(r.qty or 0)
    return out


async def qop_left(s) -> dict:
    """Остаток упаковки по видам: пришло минус израсходовано."""
    st = await state.settings(s)
    use = st.get("qopUse") or {}
    got = await qop_got(s)
    keys = set(got) | set(use)
    return {k: max(0, int(got.get(k) or 0) - int(use.get(k) or 0)) for k in keys}


def base_price(pid: str, pack: int) -> int:
    own = PPRICE.get(pid) or {}
    if int(pack) not in PRICE and int(pack) not in own:
        raise Bad("pack")
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
    "del_flour":       {"director"},
    "add_stock":       {"store", "director"},
    "inventory":       {"store", "director"},
    "set_settings":    {"director"},
    "set_exp":         {"director"},
    "add_supply":      {"director", "checker", "store"},
    "set_supply_price": {"director", "checker"},
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
    "set_perm":        {"director"},
    "add_note":        {"seller", "checker", "director"},
    "del_note":        {"director"},
    "del_log":         {"director"},
    "add_prepay":      {"director", "checker"},
    "pay_prepay":      {"director", "checker"},
    "close_prepay":    {"director", "checker"},
    "del_prepay":      {"director"},
    "add_fault":       {"director", "checker", "store", "seller"},
    "fix_fault":       {"director", "checker", "store"},
    "del_fault":       {"director"},
}


# разделы: кто их видит (директор видит всё всегда)
VIEWS = {
    "v_sell":  {"seller", "director"},
    "v_chek":  {"checker", "director"},
    "v_stock": {"store", "director"},
    "v_rep":   {"director"},
    "v_in":    {"store", "director"},
    "m_inv":   {"store", "director"},
    "m_sup":   {"checker", "director"},
    "m_xar":   {"checker", "director"},
    "m_debt":  {"checker", "director"},
    "m_cli":   {"checker", "director"},
    "m_my":    {"seller", "director"},
    "m_log":   {"checker", "store", "director"},
    "m_fix":   {"director", "checker", "store"},   # поломки в цехе
    "m_tg":    {"director"},
    "m_exp":   {"checker", "director"},
    "m_set":   {"director"},
    "see_money": {"checker", "director"},     # кто вообще видит суммы
}
ROLES = ("seller", "checker", "store")


def default_perm() -> dict:
    out = {k: sorted(v) for k, v in VIEWS.items()}
    out.update({k: sorted(v) for k, v in RIGHTS.items()})
    return out


def allowed_for(perm: dict, key: str) -> set:
    """Что разрешено сейчас: настройка директора или значение по умолчанию."""
    base = RIGHTS.get(key) or VIEWS.get(key) or set()
    got = (perm or {}).get(key)
    return set(got) | {"director"} if got is not None else set(base) | {"director"}


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


def _lot_id(note: str) -> int:
    """«lot:12 коммент» → 12. Так поставка и партия муки остаются связанными."""
    for w in (note or "").split():
        if w.startswith("lot:") and w[4:].isdigit():
            return int(w[4:])
    return 0


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
    """Цену ставит тот, кому это разрешено. Она запоминается — дальше система считает сама."""
    st = await state.settings(s)
    last = dict(st.get("lastPrice") or {})
    right = "set_buy_price" if str(key).startswith("tovar:") else "set_supply_price"
    may = role in allowed_for(st.get("perm") or {}, right)
    if may and given > 0:
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


def _merge(items):
    """Один товар одной фасовки — одна строка: иначе остаток проверялся бы дважды."""
    out = []
    for it in items:
        key = (it["id"], int(it["pack"]))
        same = next((x for x in out if (x["id"], int(x["pack"])) == key), None)
        if same and int(same.get("price") or 0) == int(it.get("price") or 0):
            same["n"] = int(same["n"]) + int(it["n"])
        else:
            out.append(dict(it))
    return out


def _need(items, pid, pack):
    return sum(int(x["n"]) for x in items
               if x["id"] == pid and int(x["pack"]) == int(pack))


def _totals(items, cost):
    total = sum(int(i["price"]) * int(i["n"]) for i in items)
    kg = sum(int(i["pack"]) * int(i["n"]) for i in items)
    cst = sum(cost(int(i["pack"]), i["id"]) * int(i["n"]) for i in items)
    return total, kg, cst


async def _reserved(s, pid, pack, skip=None):
    rows = (await s.execute(select(db.Sale).where(
        db.Sale.status == "sent", db.Sale.returned.is_(False)))).scalars().all()
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
    st = await state.settings(s)
    if role not in allowed_for(st.get("perm") or {}, kind):
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
    for it in _merge(items):
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
    for it in _merge(items):
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
    sale.status = "del"
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
    """Приём муки: сразу с поставщиком — запись видна и в разделе «Ta'minotchilar»."""
    kg = int(d.get("kg") or 0)
    if kg <= 0:
        raise Bad("kg")
    who = (d.get("who") or "").strip()[:120]
    order = await _open_prepay(s, d.get("order"), "un", who)
    if order and not who:
        who = order.who
    price = await _price_for(s, "un", int(d.get("price") or 0), role)
    if order and order.price > 0 and int(d.get("price") or 0) <= 0:
        price = order.price                      # цена уже согласована в заказе
    if price <= 0:
        price = await _flour_avg(s)
    lot = db.FlourLot(kg=kg, price=price, by=role)
    s.add(lot)
    await s.flush()
    total = kg * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    paid = min(total, paid + await _use_prepay(order, kg, total - paid))   # аванс закрывает долг
    sup = db.Supply(kind="un", who=who, qty=kg, price=price, sum=total,
                    paid=paid, debt=total - paid,
                    due=date.fromisoformat(d["due"]) if (total - paid) and d.get("due") else None,
                    note=f"lot:{lot.id} ", by=role)
    s.add(sup)
    await s.flush()
    await _remember_supplier(s, who)
    st = await state.settings(s)
    await state.set_setting(s, "flourIn", int(st.get("flourIn") or 0) + kg)
    await _log(s, role, "a_flour", _line(
        f"{_m(kg)} kg", who or "ta'minotchi yozilmagan", f"1 kg × {_m(price)}",
        f"jami {_m(total)}",
        f"oldindan to'langan buyurtmadan · qoldi {_m(max(0, order.qty - order.got))} kg"
        if order else "", "omborga un kirimi", ref=f"sup:{sup.id}"))


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
    # на каждую упаковку уходит свой пакет: 1 кг — рулон 1 кг, 12 кг — мешок
    qk = pack_kind(p.id, pack)
    if qk:
        use = dict(cfg.get("qopUse") or {})
        use[qk] = int(use.get(qk) or 0) + n
        await state.set_setting(s, "qopUse", use)
        await state.set_setting(s, "qopUsed", int(use.get("qop") or 0))
    await _log(s, role, "a_in", _line(
        f"{_m(n)} dona", p.name, f"{pack} kg o'ram", f"{_m(n * pack)} kg omborga",
        "sotib olingandan fasovka" if src == "buy" else "o'z sexi",
        f"{QOPS.get(qk, 'qop')}: {_m(n)} ishlatildi" if qk else "o'ram ishlatilmadi",
        ref=f"in:{p.id}:{n * pack}:{pack}:{n}:{src}"))


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
    who0 = (d.get("who") or "").strip()[:120]
    order = await _open_prepay(s, d.get("order"), kind, who0)
    price = await _price_for(s, kind, price, role)   # цену задаёт директор, дальше сама
    if order and order.price > 0 and int(d.get("price") or 0) <= 0:
        price = order.price
    total = qty * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    paid = min(total, paid + await _use_prepay(order, qty, total - paid))
    row = db.Supply(kind=kind, who=(d.get("who") or "").strip()[:120], qty=qty, price=price,
                    sum=total, paid=paid, debt=total - paid,
                    due=date.fromisoformat(d["due"]) if (total - paid) and d.get("due") else None,
                    note=(d.get("note") or "").strip()[:200], by=role)
    s.add(row)
    await s.flush()
    await _remember_supplier(s, row.who)
    if kind == "un":                       # мука от поставщика = приход муки
        lot = db.FlourLot(kg=qty, price=price or await _flour_avg(s), by=role)
        s.add(lot)
        await s.flush()
        row.note = f"lot:{lot.id} " + (row.note or "")     # цена и удаление ходят вместе
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
    """Пересчёт упаковки вручную: сколько осталось на самом деле, по виду."""
    kind = d.get("kind") if d.get("kind") in QOPS else "qop"
    left = max(0, int(d.get("left") or 0))
    got = int((await qop_got(s)).get(kind) or 0)
    st = await state.settings(s)
    use = dict(st.get("qopUse") or {})
    use[kind] = max(0, got - left)
    await state.set_setting(s, "qopUse", use)
    await state.set_setting(s, "qopUsed", int(use.get("qop") or 0))
    await _log(s, role, "a_qop", _line(
        f"{_m(left)} {'dona' if kind == 'qop' else 'kg'}",
        QOPS.get(kind, kind), "qoldiq qo'lda tenglashtirildi"))


# ------------------------------------------------------------------ предоплата поставщику
async def _open_prepay(s, oid: int, kind: str, who: str):
    """Открытый заказ: тот же поставщик, тот же вид товара, ещё не закрыт."""
    row = await s.get(db.Prepay, int(oid or 0)) if oid else None
    if not row or row.done:
        return None
    if row.kind != kind:
        raise Bad("order")
    if who and row.who and row.who.strip().lower() != who.strip().lower():
        raise Bad("order")
    return row


async def _use_prepay(row, qty: int, total: int) -> int:
    """Списываем с аванса: сколько килограммов пришло и сколько денег этим закрыто."""
    if not row:
        return 0
    row.got = int(row.got or 0) + int(qty)
    money = max(0, min(int(row.paid or 0) - int(row.used or 0), int(total or 0)))
    row.used = int(row.used or 0) + money
    if row.got >= row.qty:
        row.done = True
    return money


async def do_add_prepay(s, role, d):
    """Обид платит вперёд: поставщик привезёт частями, система следит за остатком."""
    kind = d.get("kind") or "un"
    if kind != "un" and kind not in QOPS and not str(kind).startswith("tovar:"):
        raise Bad("item")
    qty = int(d.get("qty") or 0)
    if qty <= 0:
        raise Bad("qty")
    price = await _price_for(s, kind, int(d.get("price") or 0), role)
    if price <= 0 and kind == "un":
        price = await _flour_avg(s)
    total = qty * price
    paid = max(0, int(d.get("paid") or 0))
    who = (d.get("who") or "").strip()[:120]
    row = db.Prepay(kind=kind, who=who, qty=qty, price=price, sum=total, paid=paid,
                    note=(d.get("note") or "").strip()[:200], by=role,
                    pays=[{"at": datetime.utcnow().isoformat(), "by": role, "amount": paid}] if paid else [])
    s.add(row)
    await s.flush()
    await _remember_supplier(s, who)
    await _log(s, role, "a_pre", _line(
        _m(paid), who or "ta'minotchi", f"buyurtma {_m(qty)} {_unit(kind)}",
        f"1 birlik {_m(price)}", f"jami {_m(total)}",
        "oldindan to'lov", ref=f"pre:{row.id}"))


async def do_pay_prepay(s, role, d):
    row = await s.get(db.Prepay, int(d["id"]))
    if not row:
        raise Bad("order")
    amount = int(d.get("amount") or 0)
    if amount <= 0:
        raise Bad("amount")
    pays = list(row.pays or [])
    pays.append({"at": datetime.utcnow().isoformat(), "by": role, "amount": amount})
    row.pays = pays
    row.paid = int(row.paid or 0) + amount
    await _log(s, role, "a_pre_pay", _line(
        _m(amount), row.who or "ta'minotchi", "buyurtmaga to'lov",
        f"jami to'landi {_m(row.paid)}", ref=f"pre:{row.id}"))


async def do_close_prepay(s, role, d):
    """Заказ закрыт: остаток товара уже не ждём."""
    row = await s.get(db.Prepay, int(d["id"]))
    if not row:
        raise Bad("order")
    row.done = not row.done
    await _log(s, role, "a_pre_end", _line(
        f"{_m(max(0, row.qty - row.got))} {_unit(row.kind)}", row.who or "ta'minotchi",
        "buyurtma yopildi" if row.done else "buyurtma qayta ochildi", ref=f"pre:{row.id}"))


async def do_del_prepay(s, role, d):
    row = await s.get(db.Prepay, int(d["id"]))
    if not row:
        raise Bad("order")
    await _log(s, role, "a_pre_del", _line(
        _m(row.paid), row.who or "ta'minotchi", f"buyurtma {_m(row.qty)} {_unit(row.kind)}"))
    await s.delete(row)


def _unit(kind: str) -> str:
    if kind == "un" or str(kind).startswith("tovar:"):
        return "kg"
    return "dona" if kind == "qop" else "kg"


# ------------------------------------------------------------------ поломки в цехе
async def do_add_fault(s, role, d):
    """Поломка: узел из списка, текст как написали, кто сообщил."""
    text = (d.get("text") or "").strip()[:400]
    part = d.get("part") or parts.match(text) or ""
    if part and part not in parts.NAMES:
        part = ""
    if not part and not text:
        raise Bad("empty")
    who = (d.get("who") or "").strip()[:80] or NAMES.get(role, role)
    row = db.Fault(part=part, text=text, who=who,
                   src="telegram" if d.get("src") == "telegram" else "app")
    s.add(row)
    await s.flush()
    await _log(s, role, "a_fix", _line(
        parts.NAMES.get(part, "boshqa nosozlik"), who, text[:120],
        parts.ZONES.get(part, ""), ref=f"fix:{row.id}"))
    return row


async def do_fix_fault(s, role, d):
    """Починили: закрываем и, если сказали, записываем стоимость ремонта."""
    row = await s.get(db.Fault, int(d["id"]))
    if not row:
        raise Bad("fault")
    if row.status == "fixed" and not d.get("cost") and not d.get("note"):
        row.status = "open"                       # нажали второй раз — снова открыта
        row.fixed_at, row.fixed_by = None, ""
        await _log(s, role, "a_fix_open", _line(
            parts.NAMES.get(row.part, "nosozlik"), "qayta ochildi", ref=f"fix:{row.id}"))
        return
    row.status = "fixed"
    row.fixed_at = datetime.now(state.TZ)
    row.fixed_by = (d.get("who") or "").strip()[:80] or NAMES.get(role, role)
    if d.get("cost") is not None:
        row.cost = max(0, int(d.get("cost") or 0))
    if d.get("note"):
        row.note = str(d["note"]).strip()[:400]
    hours = ""
    if row.at:
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=timezone.utc)
        h = (row.fixed_at - at).total_seconds() / 3600
        hours = f"{h:.1f} soatda tuzatildi" if h >= 1 else "bir soatda tuzatildi"
    await _log(s, role, "a_fix_done", _line(
        parts.NAMES.get(row.part, "nosozlik"), row.fixed_by, hours,
        f"ta'mir {_m(row.cost)}" if row.cost else "", ref=f"fix:{row.id}"))


async def do_del_fault(s, role, d):
    row = await s.get(db.Fault, int(d["id"]))
    if not row:
        raise Bad("fault")
    await _log(s, role, "a_fix_del", _line(
        parts.NAMES.get(row.part, "nosozlik"), (row.text or "")[:80]))
    await s.delete(row)


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
    order = await _open_prepay(s, d.get("order"), f"tovar:{d.get('pid')}",
                               (d.get("who") or "").strip())
    if not p:
        raise Bad("item")
    if kg <= 0:
        raise Bad("kg")
    price = await _price_for(s, "tovar:" + p.id, price, role)
    if order and order.price > 0 and int(d.get("price") or 0) <= 0:
        price = order.price
    total = kg * price
    paid = max(0, min(total, int(d.get("paid") or 0)))
    paid = min(total, paid + await _use_prepay(order, kg, total - paid))
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
    was = sum(int(x.get("amount") or 0) for x in (row.pays or []))
    asked = int(d.get("paid")) if d.get("paid") is not None else row.paid
    row.paid = max(was, max(0, min(row.sum, asked)))    # уже принятые деньги не теряем
    row.debt = max(0, row.sum - row.paid)
    row.due = date.fromisoformat(d["due"]) if row.debt and d.get("due") else None
    if row.kind == "un":                     # цена муки уходит и в саму партию — тан-нарх сходится
        lot = await s.get(db.FlourLot, _lot_id(row.note)) if _lot_id(row.note) else None
        if lot:
            lot.price = price
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
    was = sum(int(x.get("amount") or 0) for x in (row.pays or []))
    asked = int(d.get("paid")) if d.get("paid") is not None else row.paid
    row.paid = max(was, max(0, min(row.sum, asked)))    # уже принятые деньги не теряем
    row.debt = max(0, row.sum - row.paid)
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


# ------------------------------------------------------------------ права доступа
async def do_set_perm(s, role, d):
    """Директор сам решает, кому какой раздел и какая кнопка доступны."""
    perm = d.get("perm") or {}
    clean = {}
    for k, v in perm.items():
        if k in VIEWS or k in RIGHTS:
            clean[k] = sorted({r for r in v if r in ROLES})
    await state.set_setting(s, "perm", clean)
    await _log(s, role, "a_perm", _line(f"{len(clean)} ta huquq", "ruxsatlar o'zgardi"))


# ------------------------------------------------------------------ комментарии к чекам
async def do_add_note(s, role, d):
    """Комментарий к чеку — его видят все, кто видит сам чек."""
    sale = await s.get(db.Sale, int(d.get("sale") or 0))
    text = (d.get("text") or "").strip()[:400]
    if not sale:
        raise Bad("sale")
    if not text:
        raise Bad("text")
    row = db.Note(sale_id=sale.id, who=role, text=text)
    s.add(row)
    await s.flush()
    await _log(s, role, "a_note", _line(
        f"№{sale.id}", await _cname(s, sale.client_id), text[:80], ref=f"chek:{sale.id}"))


async def do_del_note(s, role, d):
    row = await s.get(db.Note, int(d["id"]))
    if not row:
        raise Bad("note")
    await _log(s, role, "a_note_del", _line(f"№{row.sale_id}", row.text[:60]))
    await s.delete(row)


async def do_del_flour(s, role, d):
    """Удалить приход муки: партия уходит и из среднего, и из «пришло всего»."""
    row = await s.get(db.FlourLot, int(d["id"]))
    if not row:
        raise Bad("flour")
    st = await state.settings(s)
    await state.set_setting(s, "flourIn", max(0, int(st.get("flourIn") or 0) - row.kg))
    sup = (await s.execute(select(db.Supply).where(
        db.Supply.note == f"lot:{row.id}"))).scalars().first()
    if sup:
        await s.delete(sup)
    await _log(s, role, "a_flour_del", _line(
        f"{_m(row.kg)} kg", f"1 kg × {_m(row.price)}", "un kirimi o'chirildi"))
    await s.delete(row)


# ------------------------------------------------------------------ удаление записи журнала
SALE_KINDS = {"a_send", "a_check", "a_edit", "a_del", "a_pay", "a_debt", "a_ret"}


async def _drop_logs(s, ref: str):
    """Убрать все записи журнала, которые ссылаются на этот документ."""
    from sqlalchemy import delete as _del
    rows = (await s.execute(select(db.LogRow))).scalars().all()
    for r in rows:
        parts = (r.text or "").split("|")
        if len(parts) > 2 and parts[2] == ref:
            await s.delete(r)


async def do_del_log(s, role, d):
    """Директор убирает запись из журнала — вместе с самим документом."""
    row = await s.get(db.LogRow, int(d["id"]))
    if not row:
        raise Bad("log")
    parts = (row.text or "").split("|")
    ref = parts[2] if len(parts) > 2 else ""
    kind, _, rest = ref.partition(":")
    gone = ""

    if kind == "chek" and row.kind in SALE_KINDS:
        sale = await s.get(db.Sale, int(rest))
        if sale:
            if sale.status in ("ok", "paid") and not sale.returned:   # товар возвращаем на склад
                prods = {p.id: p for p in (await s.execute(select(db.Product))).scalars().all()}
                for it in sale.items:
                    p = prods.get(it["id"])
                    if not p:
                        continue
                    st = dict(p.stock)
                    st[str(it["pack"])] = int(st.get(str(it["pack"]), 0)) + int(it["n"])
                    p.stock = st
            for n in (await s.execute(select(db.Note).where(db.Note.sale_id == sale.id))).scalars().all():
                await s.delete(n)
            await _drop_logs(s, ref)
            await s.delete(sale)
            gone = f"chek №{sale.id}"

    elif kind == "sup":
        sup = await s.get(db.Supply, int(rest))
        if sup:
            if sup.kind == "un":                       # мука уходит и из прихода, и из партий
                st = await state.settings(s)
                await state.set_setting(s, "flourIn", max(0, int(st.get("flourIn") or 0) - sup.qty))
                lot = await s.get(db.FlourLot, _lot_id(sup.note)) if _lot_id(sup.note) else None
                if lot:
                    await s.delete(lot)
            await _drop_logs(s, ref)
            await s.delete(sup)
            gone = f"kirim {_m(sup.qty)}"

    elif kind == "buy":
        buy = await s.get(db.Buy, int(rest))
        if buy:
            await _drop_logs(s, ref)
            await s.delete(buy)
            gone = f"sotib olish {_m(buy.kg)} kg"

    elif kind == "debt":
        dbt = await s.get(db.Debt, int(rest))
        if dbt:
            await _drop_logs(s, ref)
            await s.delete(dbt)
            gone = f"qarz {_m(dbt.debt)}"

    elif kind == "in":                                  # сдача на склад: снимаем обратно
        bits = rest.split(":")
        if len(bits) >= 4:
            pid, _kg, pack, n = bits[0], int(bits[1]), int(bits[2]), int(bits[3])
            src = bits[4] if len(bits) > 4 else "sex"
            p = await s.get(db.Product, pid)
            if p:
                st = dict(p.stock)
                st[str(pack)] = max(0, int(st.get(str(pack), 0)) - n)
                p.stock = st
            cfg = await state.settings(s)
            if src == "buy":
                packed = dict(cfg.get("buyPacked") or {})
                packed[pid] = max(0, int(packed.get(pid) or 0) - n * pack)
                await state.set_setting(s, "buyPacked", packed)
            else:
                await state.set_setting(s, "produced", max(0, int(cfg.get("produced") or 0) - n * pack))
            qk = pack_kind(pid, pack)
            if qk:
                use = dict(cfg.get("qopUse") or {})
                use[qk] = max(0, int(use.get(qk) or 0) - n)
                await state.set_setting(s, "qopUse", use)
                await state.set_setting(s, "qopUsed", int(use.get("qop") or 0))
            gone = f"{_m(n)} dona omborga kirim"

    await _log(s, role, "a_log_del", _line(
        parts[0] if parts else "", t_kind(row.kind), gone or "yozuv o'chirildi"))
    await s.delete(row)


def t_kind(k: str) -> str:
    return {"a_send": "chek yuborildi", "a_check": "chek tasdiqlandi", "a_pay": "to'lov",
            "a_debt": "qarz to'lovi", "a_in": "omborga kirim", "a_flour": "un kirimi",
            "a_sup": "ta'minotchi kirimi", "a_buy": "tayyor mahsulot",
            "a_debt_add": "qarz", "a_xar_add": "xarajat",
            "a_log_del": "jurnal yozuvi", "a_sup_price": "narx belgilandi",
            "a_qop": "qop kirimi", "a_inv": "inventarizatsiya"}.get(k, k)
