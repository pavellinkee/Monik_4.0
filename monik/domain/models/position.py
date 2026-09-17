"""Сделка режима ``ann``: покупка, ожидание, продажа.

Позиция — единственное место, где Monik держит деньги. Поэтому она
хранится в базе с самого начала, ещё до того как покупка попадёт в блок:
иначе перезапуск между отправкой и записью оставил бы токены, о которых
система не знает.

Суммы хранятся в base units и в знаках своего токена. Выводить знаки из
символа запрещено (``09_PROFIT_CALCULATOR.md`` §5), поэтому у каждой
суммы свой токен.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.base import DomainModel
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import TId
from monik.domain.value_objects.identity import NetworkId
from monik.domain.value_objects.timestamps import UtcDatetime

__all__ = ["Position"]


class Position(DomainModel):
    """Одна сделка: круг ``base → target → base``."""

    t_id: TId
    network_id: NetworkId
    status: PositionStatus
    #: Токен, которым входим и выходим.
    base_token: Token
    #: Токен, который покупаем и держим.
    target_token: Token
    buy_provider_id: ProviderId
    sell_provider_id: ProviderId

    #: Сколько базового токена вложено.
    raw_input: int = Field(gt=0)
    #: Сколько промежуточного токена фактически получено. ``None``, пока
    #: покупка не подтверждена: расчётное количество не подставляется —
    #: неизвестное не равно ожидаемому.
    raw_acquired: int | None = None
    #: Сколько базового токена получено обратно.
    raw_returned: int | None = None

    buy_tx_hash: str | None = None
    sell_tx_hash: str | None = None

    opened_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    #: Когда оператору сообщили о слишком долгом ожидании. Пустое поле
    #: означает «ещё не сообщали»: уведомление шлётся один раз, а не
    #: каждые десять секунд.
    long_wait_notified_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.base_token.network_id != self.network_id:
            raise ValueError("position base token belongs to another network")
        if self.target_token.network_id != self.network_id:
            raise ValueError("position target token belongs to another network")
        if self.base_token.key == self.target_token.key:
            raise ValueError("position must exchange two different tokens")
        if self.status.holds_tokens and self.raw_acquired is None:
            raise ValueError("a position holding tokens must know how many were acquired")
        if self.status is PositionStatus.CLOSED and self.raw_returned is None:
            raise ValueError("a closed position must know how much came back")
        return self

    @property
    def input_amount(self) -> Decimal:
        """Вложенная сумма в человеческих единицах."""
        return Decimal(self.raw_input).scaleb(-self.base_token.decimals)

    @property
    def net_result_raw(self) -> int | None:
        """Итог круга в base units базового токена.

        ``None``, пока круг не завершён: незавершённая сделка не имеет
        результата, и подставлять вместо него ноль нельзя.
        """
        if self.raw_returned is None:
            return None
        return self.raw_returned - self.raw_input

    @property
    def net_result(self) -> Decimal | None:
        """Итог круга в человеческих единицах."""
        raw = self.net_result_raw
        if raw is None:
            return None
        return Decimal(raw).scaleb(-self.base_token.decimals)

    def profit_if_sold_for(self, raw_output: int) -> int:
        """Каким был бы итог, продай мы сейчас за ``raw_output``."""
        return raw_output - self.raw_input
