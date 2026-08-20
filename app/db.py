# -*- coding: utf-8 -*-
"""Таблицы. Одна база — своя схема, чтобы не мешать другому проекту."""
from datetime import datetime, date

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Integer,
                        JSON, String, Text, func)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DB_URL, SCHEMA

PK = BigInteger().with_variant(Integer, "sqlite")   # BIGSERIAL в Postgres, INTEGER в SQLite

IS_PG = DB_URL.startswith("postgresql")
engine = create_async_engine(DB_URL, pool_pre_ping=True, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    if IS_PG:
        __table_args__ = {"schema": SCHEMA}


def _fk(name: str) -> str:
    return f"{SCHEMA}.{name}" if IS_PG else name


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    packs: Mapped[list] = mapped_column(JSON, default=lambda: [1, 5, 12])
    stock: Mapped[dict] = mapped_column(JSON, default=dict)     # {"1": 120, "5": 40}
    pos: Mapped[int] = mapped_column(Integer, default=0)


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(40), default="")
    price: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # {"1":7200,...} или NULL
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    by: Mapped[str] = mapped_column(String(20))                 # роль-автор чека
    shift_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="sent")   # sent | ok | paid
    pay: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sum: Mapped[int] = mapped_column(BigInteger, default=0)
    cost: Mapped[int] = mapped_column(BigInteger, default=0)
    kg: Mapped[int] = mapped_column(Integer, default=0)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    debt: Mapped[int] = mapped_column(BigInteger, default=0)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    client_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    returned: Mapped[bool] = mapped_column(Boolean, default=False)
    items: Mapped[list] = mapped_column(JSON, default=list)     # [{id,pack,n,price}]
    pays: Mapped[list] = mapped_column(JSON, default=list)      # [{at,by,amount,way}]
    notified: Mapped[bool] = mapped_column(Boolean, default=False)   # напоминание о долге ушло


class FlourLot(Base):
    __tablename__ = "flour_lots"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kg: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    by: Mapped[str] = mapped_column(String(20), default="store")


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    opened: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    by: Mapped[str] = mapped_column(String(20))
    expected: Mapped[int] = mapped_column(BigInteger, default=0)
    actual: Mapped[int] = mapped_column(BigInteger, default=0)


class LogRow(Base):
    __tablename__ = "log"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    who: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(Text, default="")


class Debt(Base):
    """Долг, заведённый вручную — не привязан к чеку (старый долг, отдельная договорённость)."""
    __tablename__ = "debts"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    client_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)      # сколько было изначально
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    debt: Mapped[int] = mapped_column(BigInteger, default=0)        # сколько осталось
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(20), default="checker")
    pays: Mapped[list] = mapped_column(JSON, default=list)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class Supply(Base):
    """Поставка от поставщика: мука или мешки. Виден только директору и Обиду."""
    __tablename__ = "supplies"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(10), default="un")      # un | qop
    who: Mapped[str] = mapped_column(String(120), default="")        # поставщик
    qty: Mapped[int] = mapped_column(Integer, default=0)             # кг муки или штук мешков
    price: Mapped[int] = mapped_column(Integer, default=0)           # цена за единицу
    sum: Mapped[int] = mapped_column(BigInteger, default=0)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    debt: Mapped[int] = mapped_column(BigInteger, default=0)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(20), default="director")
    pays: Mapped[list] = mapped_column(JSON, default=list)


class Buy(Base):
    """Покупка готовой продукции: спагетти берут у поставщика и фасуют в свои пакеты."""
    __tablename__ = "buys"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    who: Mapped[str] = mapped_column(String(120), default="")      # у кого купили
    pid: Mapped[str] = mapped_column(String(40))                   # какой товар
    kg: Mapped[int] = mapped_column(Integer, default=0)            # сколько кг куплено
    price: Mapped[int] = mapped_column(Integer, default=0)         # цена за 1 кг
    sum: Mapped[int] = mapped_column(BigInteger, default=0)
    paid: Mapped[int] = mapped_column(BigInteger, default=0)
    debt: Mapped[int] = mapped_column(BigInteger, default=0)
    due: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(20), default="director")
    pays: Mapped[list] = mapped_column(JSON, default=list)


class Note(Base):
    """Комментарий к чеку: кто и что написал."""
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sale_id: Mapped[int] = mapped_column(BigInteger)
    who: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")


class Prepay(Base):
    """Предоплата поставщику: деньги ушли вперёд, товар приходит частями."""
    __tablename__ = "prepays"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(40), default="un")   # un | qop… | tovar:<pid>
    who: Mapped[str] = mapped_column(String(120), default="")     # кому заплатили
    qty: Mapped[int] = mapped_column(Integer, default=0)          # сколько заказано
    got: Mapped[int] = mapped_column(Integer, default=0)          # сколько уже привезли
    price: Mapped[int] = mapped_column(Integer, default=0)        # цена за единицу
    sum: Mapped[int] = mapped_column(BigInteger, default=0)       # стоимость всего заказа
    paid: Mapped[int] = mapped_column(BigInteger, default=0)      # сколько денег внесено
    used: Mapped[int] = mapped_column(BigInteger, default=0)      # сколько денег уже закрыто товаром
    note: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(20), default="checker")
    pays: Mapped[list] = mapped_column(JSON, default=list)
    done: Mapped[bool] = mapped_column(Boolean, default=False)    # заказ закрыт вручную


class Expense(Base):
    """Расход за конкретный день: название из шаблона + сумма."""
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    day: Mapped[date] = mapped_column(Date)                          # день, за который записан расход
    name: Mapped[str] = mapped_column(String(80))
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    by: Mapped[str] = mapped_column(String(20), default="director")


class Fault(Base):
    """Поломка в цехе: пришла из группы Telegram или записана в приложении."""
    __tablename__ = "faults"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    part: Mapped[str] = mapped_column(String(40), default="")     # ключ узла из parts.PARTS
    text: Mapped[str] = mapped_column(Text, default="")           # что написали
    who: Mapped[str] = mapped_column(String(80), default="")      # кто сообщил
    src: Mapped[str] = mapped_column(String(20), default="app")   # app | telegram
    status: Mapped[str] = mapped_column(String(10), default="open")   # open | fixed
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fixed_by: Mapped[str] = mapped_column(String(80), default="")
    cost: Mapped[int] = mapped_column(BigInteger, default=0)       # во сколько обошёлся ремонт
    note: Mapped[str] = mapped_column(Text, default="")


class CashFlow(Base):
    """Касса за день: приход и расход, наличные и по счёту фирмы."""
    __tablename__ = "cashflow"
    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    day: Mapped[date] = mapped_column(Date, index=True)
    dir: Mapped[str] = mapped_column(String(4), default="in")      # in | out
    way: Mapped[str] = mapped_column(String(8), default="naqd")    # naqd | bank
    who: Mapped[str] = mapped_column(String(120), default="")      # от кого / кому
    title: Mapped[str] = mapped_column(String(120), default="")    # за что
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    ref: Mapped[str] = mapped_column(String(40), default="")       # chek:12, sup:3 …
    by: Mapped[str] = mapped_column(String(20), default="director")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    val: Mapped[dict] = mapped_column(JSON, default=dict)


async def init_db():
    async with engine.begin() as conn:
        if IS_PG:
            from sqlalchemy import text
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {SCHEMA}'))
        await conn.run_sync(Base.metadata.create_all)
