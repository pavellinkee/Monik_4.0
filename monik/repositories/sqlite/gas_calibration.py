"""Хранение накопленных замеров расхода газа.

Хранятся итоги по паре «сеть — агрегатор», а не отдельные транзакции:
поправка — одно число, и история каждой сделки для неё не нужна
(``CLAUDE.md`` §28). Итоги переживают перезапуск: иначе поправка
обнулялась бы каждый раз и Monik снова считал бы по завышенной оценке.
"""

from __future__ import annotations

from monik.domain.enums.providers import ProviderId
from monik.domain.models.gas import GasCalibrationSample
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.db.connection import Database
from monik.infrastructure.db.types import (
    from_raw_amount,
    from_timestamp,
    to_raw_amount,
    to_timestamp,
)
from monik.repositories.sqlite.mapping import column

__all__ = ["SqliteGasCalibrationRepository"]

_COLUMNS = "network_id, provider_id, samples, quoted_units, actual_units, updated_at"


class SqliteGasCalibrationRepository:
    """Замеры расхода газа в SQLite."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def all_samples(self) -> tuple[GasCalibrationSample, ...]:
        """Все накопленные итоги."""
        rows = await self._database.fetch_all(f"SELECT {_COLUMNS} FROM gas_calibration")
        return tuple(_to_domain(row) for row in rows)

    async def save(self, sample: GasCalibrationSample) -> None:
        """Записать итог пары, заменив прежний."""
        await self._database.execute(
            f"INSERT INTO gas_calibration ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (network_id, provider_id) DO UPDATE SET "
            "samples = excluded.samples, quoted_units = excluded.quoted_units, "
            "actual_units = excluded.actual_units, updated_at = excluded.updated_at",
            (
                str(sample.network_id),
                sample.provider_id.value,
                sample.samples,
                to_raw_amount(sample.quoted_units),
                to_raw_amount(sample.actual_units),
                to_timestamp(sample.updated_at),
            ),
        )


def _to_domain(row: object) -> GasCalibrationSample:
    return GasCalibrationSample(
        network_id=NetworkId(str(column(row, "network_id"))),  # type: ignore[arg-type]
        provider_id=ProviderId(str(column(row, "provider_id"))),  # type: ignore[arg-type]
        samples=int(column(row, "samples")),  # type: ignore[arg-type]
        quoted_units=from_raw_amount(column(row, "quoted_units")),  # type: ignore[arg-type]
        actual_units=from_raw_amount(column(row, "actual_units")),  # type: ignore[arg-type]
        updated_at=from_timestamp(str(column(row, "updated_at"))),  # type: ignore[arg-type]
    )
