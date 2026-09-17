"""Порты подсистемы исполнения.

Подсистема зависит от протоколов, а не от конкретных реализаций
(``25_PROJECT_STRUCTURE.md`` §8): хранилище сделок и источник номеров
подменяются в тестах без базы.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from monik.domain.models.position import Position
from monik.domain.value_objects.identity import NetworkId

__all__ = ["PositionStore", "SequenceSource"]


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
