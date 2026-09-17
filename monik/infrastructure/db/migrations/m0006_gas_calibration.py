"""Калибровка оценки газа и разбивка расхода по сделке.

Оценка расхода, приходящая в котировке, оказалась втрое ниже фактической:
агрегатор считает голый обмен по одному лучшему пути, а платим мы за
реальный вызов роутера, маршрут которого на исполнении расходится на
несколько пулов. Ошибка систематическая, поэтому её можно измерить и
учесть — фактический расход приходит в квитанции, которую подсистема
исполнения и так запрашивает.

Таблица накапливает по каждой паре «сеть — агрегатор» два итога:
обещанный котировками и фактический. Их отношение и есть поправка.
Хранятся именно итоги, а не отдельные замеры: поправка — одно число, и
история отдельных транзакций для неё не нужна (``CLAUDE.md`` §28 —
не хранить бесконечно всё подряд).

Столбцы позиции хранят обе величины по каждой ноге. Без них отношение
не из чего считать, а разбираться, в расходе ошибка или в цене, пришлось
бы снова по цепи.
"""

from __future__ import annotations

from monik.infrastructure.db.migrations.base import Migration

__all__ = ["MIGRATION"]

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS gas_calibration (
        network_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        samples INTEGER NOT NULL,
        quoted_units TEXT NOT NULL,
        actual_units TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (network_id, provider_id)
    )
    """,
    "ALTER TABLE positions ADD COLUMN buy_quoted_gas_units TEXT",
    "ALTER TABLE positions ADD COLUMN sell_quoted_gas_units TEXT",
    "ALTER TABLE positions ADD COLUMN buy_gas_units TEXT",
    "ALTER TABLE positions ADD COLUMN sell_gas_units TEXT",
)

MIGRATION = Migration(version=6, name="gas_calibration", statements=_STATEMENTS)
