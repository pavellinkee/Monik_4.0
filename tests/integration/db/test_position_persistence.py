"""Сохранение сделки целиком.

Полей у позиции много, и половина из них — деньги. Порядок столбцов и
порядок значений обязаны совпадать; расхождение не заметит ни один
типовой тест, потому что все величины целые и любая встанет в любое
поле. Здесь сделка записывается со **всеми** заполненными полями и
читается обратно.
"""

from __future__ import annotations

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import TId
from monik.infrastructure.db import Database, MigrationRunner
from monik.repositories.sqlite.positions import SqlitePositionRepository
from tests import factories as f


def _full_position() -> Position:
    """Закрытая сделка, у которой заполнено всё, что может быть заполнено.

    Значения намеренно **различны**: одинаковые числа скрыли бы перепутанные
    местами столбцы.
    """
    return Position(
        t_id=TId.from_sequence(7),
        network_id=f.POLYGON,
        status=PositionStatus.CLOSED,
        base_token=f.USDT,
        target_token=f.AAVE,
        buy_provider_id=ProviderId.UNISWAP,
        sell_provider_id=ProviderId.KYBERSWAP,
        raw_input=50_000_000,
        raw_acquired=49_968_534,
        raw_returned=50_014_747,
        raw_target_before_buy=11,
        raw_base_before_sell=22,
        buy_gas_wei=33,
        sell_gas_wei=44,
        raw_gas_cost=55,
        buy_quoted_gas_units=66,
        sell_quoted_gas_units=77,
        buy_gas_units=88,
        sell_gas_units=99,
        buy_tx_hash="0x" + "ab" * 32,
        sell_tx_hash="0x" + "cd" * 32,
        opened_at=f.NOW,
        updated_at=f.NOW,
        closed_at=f.NOW,
    )


class TestRoundTrip:
    async def test_every_field_survives_a_write_and_a_read(self, database: Database) -> None:
        await MigrationRunner(database).upgrade()
        repository = SqlitePositionRepository(database)
        position = _full_position()

        await repository.create(position)
        restored = await repository.get(position.t_id)

        assert restored == position

    async def test_update_rewrites_every_changeable_field(self, database: Database) -> None:
        """Обновление идёт другим запросом, и его порядок тоже может разойтись."""
        await MigrationRunner(database).upgrade()
        repository = SqlitePositionRepository(database)
        opened = Position(
            t_id=TId.from_sequence(8),
            network_id=f.POLYGON,
            status=PositionStatus.BUYING,
            base_token=f.USDT,
            target_token=f.AAVE,
            buy_provider_id=ProviderId.UNISWAP,
            sell_provider_id=ProviderId.KYBERSWAP,
            raw_input=50_000_000,
            opened_at=f.NOW,
        )
        await repository.create(opened)

        closed = _full_position().model_copy(update={"t_id": opened.t_id})
        await repository.update(closed)

        assert await repository.get(opened.t_id) == closed
