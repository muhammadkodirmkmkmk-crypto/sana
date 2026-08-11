# -*- coding: utf-8 -*-
"""Настройки берутся из переменных окружения Railway."""
import os


def _clean(url: str) -> str:
    """Railway отдаёт postgres://, SQLAlchemy ждёт postgresql+asyncpg://"""
    if not url:
        return "sqlite+aiosqlite:///./sana.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url.split("?")[0]


DB_URL = _clean(os.getenv("DATABASE_URL", ""))
SCHEMA = os.getenv("DB_SCHEMA", "sana")          # своя схема — база общая с другим проектом

TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHECKER = os.getenv("TG_CHECKER", "")          # chat_id Обида — напоминания о долгах
TG_DIRECTOR = os.getenv("TG_DIRECTOR", "")        # chat_id директора — вечерний отчёт
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "19"))  # час вечернего отчёта
TZ_OFFSET = int(os.getenv("TZ_OFFSET", "5"))       # Узбекистан UTC+5

# PIN-коды по умолчанию: меняются в разделе «Настройки» или переменными окружения
PINS = {
    "seller":   os.getenv("PIN_SELLER", "1111"),
    "checker":  os.getenv("PIN_CHECKER", "2222"),
    "store":    os.getenv("PIN_STORE", "3333"),
    "director": os.getenv("PIN_DIRECTOR", "9999"),
}
NAMES = {
    "seller":   os.getenv("NAME_SELLER", "Xudoyor"),
    "checker":  os.getenv("NAME_CHECKER", "Obid"),
    "store":    os.getenv("NAME_STORE", "Omborchi"),
    "director": os.getenv("NAME_DIRECTOR", "Direktor"),
}
