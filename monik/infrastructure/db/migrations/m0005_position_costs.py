"""Расходы сделки и остатки до её ног.

Итог круга считался как разность между **остатком счёта** после продажи
и вложенной суммой. Остаток включает деньги, к сделке отношения не
имеющие, поэтому результат завышался на всё, что лежало на счёте до неё.
Правильная величина — выручка самой продажи, то есть прирост остатка, а
для прироста нужно знать остаток **до** ноги. Он и сохраняется.

Вместе с ним сохраняется фактическая стоимость газа каждой ноги: газ —
расход сделки, и без него заработок не является заработком
(``the_main_rules.md``, правило 11).

Столбцы добавляются пустыми: у сделок, закрытых до этой миграции,
расходы неизвестны, а неизвестное не равно нулю (``CLAUDE.md`` §12).
"""

from __future__ import annotations

from monik.infrastructure.db.migrations.base import Migration

__all__ = ["MIGRATION"]

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE positions ADD COLUMN raw_target_before_buy TEXT",
    "ALTER TABLE positions ADD COLUMN raw_base_before_sell TEXT",
    "ALTER TABLE positions ADD COLUMN buy_gas_wei TEXT",
    "ALTER TABLE positions ADD COLUMN sell_gas_wei TEXT",
    "ALTER TABLE positions ADD COLUMN raw_gas_cost TEXT",
)

MIGRATION = Migration(version=5, name="position_costs", statements=_STATEMENTS)
