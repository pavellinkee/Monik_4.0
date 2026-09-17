"""Сделки режима ``ann``.

Позиция записывается до того, как покупка попадёт в блок: иначе
перезапуск между отправкой транзакции и записью результата оставил бы
токены на счёте, о которых система не знает (``the_main_rules.md``,
правило 11).

Хеш покупки уникален: одна отправленная транзакция не может относиться к
двум сделкам, и повторная запись означала бы двойной учёт денег.
"""

from __future__ import annotations

from monik.infrastructure.db.migrations.base import Migration

__all__ = ["MIGRATION"]

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS positions (
        position_id TEXT PRIMARY KEY,
        t_id TEXT NOT NULL UNIQUE,
        network_id TEXT NOT NULL,
        status TEXT NOT NULL,
        base_token TEXT NOT NULL,
        base_decimals INTEGER NOT NULL,
        base_symbol TEXT NOT NULL,
        target_token TEXT NOT NULL,
        target_decimals INTEGER NOT NULL,
        target_symbol TEXT NOT NULL,
        buy_provider_id TEXT NOT NULL,
        sell_provider_id TEXT NOT NULL,
        raw_input TEXT NOT NULL,
        raw_acquired TEXT,
        raw_returned TEXT,
        buy_tx_hash TEXT UNIQUE,
        sell_tx_hash TEXT,
        opened_at TEXT NOT NULL,
        updated_at TEXT,
        closed_at TEXT,
        long_wait_notified_at TEXT
    )
    """,
    # Наблюдатель каждые десять секунд спрашивает только незакрытые
    # сделки: без индекса это был бы полный перебор таблицы, которая
    # растёт всё время работы.
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status, opened_at)",
    # Отчёт за период берёт закрытые сделки по времени закрытия.
    "CREATE INDEX IF NOT EXISTS idx_positions_closed_at ON positions (closed_at)",
)

MIGRATION = Migration(version=4, name="positions", statements=_STATEMENTS)
