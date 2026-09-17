"""Хранение сделок режима ``ann``.

Позиция пишется в базу раньше, чем покупка попадёт в блок, и обновляется
на каждом шаге. Это не журнал для отчётности, а рабочее состояние: после
перезапуска подсистема исполнения поднимает отсюда всё незакрытое и
продолжает вести.
"""

from __future__ import annotations

import aiosqlite

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import TId
from monik.domain.value_objects.identity import NetworkId, TokenAddress, TokenSymbol
from monik.domain.value_objects.timestamps import UtcDatetime
from monik.infrastructure.db.connection import Database
from monik.infrastructure.db.types import (
    from_raw_amount,
    from_timestamp,
    to_raw_amount,
    to_timestamp,
)
from monik.repositories.sqlite.mapping import column, optional_column

__all__ = ["SqlitePositionRepository"]

_COLUMNS = (
    "position_id, t_id, network_id, status, base_token, base_decimals, base_symbol, "
    "target_token, target_decimals, target_symbol, buy_provider_id, sell_provider_id, "
    "raw_input, raw_acquired, raw_returned, buy_tx_hash, sell_tx_hash, "
    "opened_at, updated_at, closed_at, long_wait_notified_at"
)


class SqlitePositionRepository:
    """Сделки в SQLite."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, position: Position) -> None:
        """Записать начатую сделку.

        Вызывается **до** отправки покупки: если запись не удалась,
        отправлять деньги нельзя — иначе токен уйдёт, а следа не
        останется.
        """
        await self._database.execute(
            f"INSERT INTO positions ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(position.t_id),
                str(position.t_id),
                str(position.network_id),
                position.status.value,
                str(position.base_token.address),
                position.base_token.decimals,
                str(position.base_token.symbol),
                str(position.target_token.address),
                position.target_token.decimals,
                str(position.target_token.symbol),
                position.buy_provider_id.value,
                position.sell_provider_id.value,
                to_raw_amount(position.raw_input),
                None if position.raw_acquired is None else to_raw_amount(position.raw_acquired),
                None if position.raw_returned is None else to_raw_amount(position.raw_returned),
                position.buy_tx_hash,
                position.sell_tx_hash,
                to_timestamp(position.opened_at),
                to_timestamp(position.updated_at) if position.updated_at else None,
                to_timestamp(position.closed_at) if position.closed_at else None,
                to_timestamp(position.long_wait_notified_at)
                if position.long_wait_notified_at
                else None,
            ),
        )

    async def update(self, position: Position) -> None:
        """Сохранить новое состояние сделки."""
        await self._database.execute(
            "UPDATE positions SET status = ?, raw_acquired = ?, raw_returned = ?, "
            "buy_tx_hash = ?, sell_tx_hash = ?, updated_at = ?, closed_at = ?, "
            "long_wait_notified_at = ? WHERE t_id = ?",
            (
                position.status.value,
                None if position.raw_acquired is None else to_raw_amount(position.raw_acquired),
                None if position.raw_returned is None else to_raw_amount(position.raw_returned),
                position.buy_tx_hash,
                position.sell_tx_hash,
                to_timestamp(position.updated_at) if position.updated_at else None,
                to_timestamp(position.closed_at) if position.closed_at else None,
                to_timestamp(position.long_wait_notified_at)
                if position.long_wait_notified_at
                else None,
                str(position.t_id),
            ),
        )

    async def get(self, t_id: TId) -> Position | None:
        """Найти сделку по идентификатору."""
        row = await self._database.fetch_one(
            f"SELECT {_COLUMNS} FROM positions WHERE t_id = ?", (str(t_id),)
        )
        return None if row is None else _to_domain(row)

    async def open_positions(self) -> tuple[Position, ...]:
        """Все незакрытые сделки, старые первыми.

        Порядок важен: первой ведётся та, что ждёт дольше.
        """
        rows = await self._database.fetch_all(
            f"SELECT {_COLUMNS} FROM positions WHERE status IN (?, ?, ?) ORDER BY opened_at",
            (
                PositionStatus.BUYING.value,
                PositionStatus.HOLDING.value,
                PositionStatus.SELLING.value,
            ),
        )
        return tuple(_to_domain(row) for row in rows)

    async def closed_between(
        self, *, since: UtcDatetime, until: UtcDatetime
    ) -> tuple[Position, ...]:
        """Сделки, завершённые в отчётном промежутке."""
        rows = await self._database.fetch_all(
            f"SELECT {_COLUMNS} FROM positions WHERE closed_at IS NOT NULL "
            "AND closed_at >= ? AND closed_at < ? ORDER BY closed_at",
            (to_timestamp(since), to_timestamp(until)),
        )
        return tuple(_to_domain(row) for row in rows)

    async def reserved_raw_input(self, network_id: NetworkId) -> int:
        """Сколько базового токена занято незакрытыми сделками этой сети.

        Нужно перед новой сделкой: остаток на счёте включает деньги,
        которые уже обещаны открытым позициям.
        """
        rows = await self._database.fetch_all(
            "SELECT raw_input FROM positions WHERE network_id = ? AND status IN (?, ?)",
            (
                str(network_id),
                PositionStatus.BUYING.value,
                PositionStatus.HOLDING.value,
            ),
        )
        return sum(from_raw_amount(row["raw_input"]) for row in rows)


def _token(row: aiosqlite.Row, prefix: str, network_id: NetworkId) -> Token:
    return Token(
        network_id=network_id,
        address=TokenAddress(str(column(row, f"{prefix}_token"))),
        symbol=TokenSymbol(str(column(row, f"{prefix}_symbol"))),
        decimals=int(column(row, f"{prefix}_decimals")),
    )


def _optional_raw(row: aiosqlite.Row, name: str) -> int | None:
    value = optional_column(row, name)
    return None if value is None else from_raw_amount(value)


def _optional_time(row: aiosqlite.Row, name: str) -> UtcDatetime | None:
    value = optional_column(row, name)
    return None if value is None else from_timestamp(str(value))


def _to_domain(row: aiosqlite.Row) -> Position:
    network_id = NetworkId(str(column(row, "network_id")))
    return Position(
        t_id=TId(str(column(row, "t_id"))),
        network_id=network_id,
        status=PositionStatus(str(column(row, "status"))),
        base_token=_token(row, "base", network_id),
        target_token=_token(row, "target", network_id),
        buy_provider_id=ProviderId(str(column(row, "buy_provider_id"))),
        sell_provider_id=ProviderId(str(column(row, "sell_provider_id"))),
        raw_input=from_raw_amount(column(row, "raw_input")),
        raw_acquired=_optional_raw(row, "raw_acquired"),
        raw_returned=_optional_raw(row, "raw_returned"),
        buy_tx_hash=_optional_str(row, "buy_tx_hash"),
        sell_tx_hash=_optional_str(row, "sell_tx_hash"),
        opened_at=from_timestamp(str(column(row, "opened_at"))),
        updated_at=_optional_time(row, "updated_at"),
        closed_at=_optional_time(row, "closed_at"),
        long_wait_notified_at=_optional_time(row, "long_wait_notified_at"),
    )


def _optional_str(row: aiosqlite.Row, name: str) -> str | None:
    value = optional_column(row, name)
    return None if value is None else str(value)
