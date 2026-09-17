"""Порты подсистемы исполнения.

Подсистема зависит от протоколов, а не от конкретных реализаций
(``25_PROJECT_STRUCTURE.md`` §8): хранилище сделок и источник номеров
подменяются в тестах без базы.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from monik.domain.enums.providers import ProviderId
from monik.domain.models.position import Position
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId

__all__ = ["ExecutionCosts", "GasCalibrationRecorder", "PositionStore", "SequenceSource"]


@runtime_checkable
class PositionStore(Protocol):
    """Хранилище сделок."""

    async def create(self, position: Position) -> None:
        """Записать начатую сделку."""
        ...

    async def update(self, position: Position) -> None:
        """Сохранить новое состояние сделки."""
        ...

    async def open_positions(self) -> tuple[Position, ...]:
        """Все незакрытые сделки."""
        ...

    async def reserved_raw_input(self, network_id: NetworkId) -> int:
        """Сколько базового токена занято открытыми сделками."""
        ...


@runtime_checkable
class SequenceSource(Protocol):
    """Монотонные номера сделок."""

    async def next_value(self, name: str) -> int:
        """Следующее значение последовательности."""
        ...


@runtime_checkable
class ExecutionCosts(Protocol):
    """Стоимость исполнения в базовом токене сделки.

    Наблюдателю нужно знать, во сколько обойдётся круг, а не как устроен
    курс native token. Отдельный порт держит эту границу: подменить
    источник курса в тестах можно, не собирая реестры и котировщиков.
    """

    async def to_base_raw(
        self, network_id: NetworkId, base_token: Token, *, wei: int
    ) -> int | None:
        """Стоимость ``wei`` газа в base units. ``None`` — неизвестна."""
        ...


@runtime_checkable
class GasCalibrationRecorder(Protocol):
    """Приёмник замеров расхода газа.

    Подсистема исполнения — единственное место, где известен **факт**:
    сколько газа вызов потребовал на самом деле. Поиск этого не узнаёт
    никогда, поэтому замер передаётся отсюда.
    """

    async def record(
        self,
        network_id: NetworkId,
        provider_id: ProviderId,
        *,
        quoted_units: int,
        actual_units: int,
    ) -> None:
        """Учесть один исполненный вызов."""
        ...
