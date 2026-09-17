"""Готовая к отправке транзакция обмена.

Как котировка превращается в транзакцию — особенность конкретного
агрегатора: Uniswap отдаёт готовый вызов роутера отдельным методом,
KyberSwap собирает calldata из выбранного маршрута. Эти различия
остаются в адаптерах (``CLAUDE.md`` §7), а наружу выходит одно понятие:
что вызвать, с какими данными и какой минимум мы согласны получить.

Минимум — не формальность. Транзакция несёт его в себе, и если пул не
даёт обещанного, обмен **откатывается сетью**. Поэтому завышенная
котировка приводит к потере газа, а не к исполнению по плохому курсу.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from monik.domain.enums.providers import ProviderId
from monik.domain.models.base import DomainModel
from monik.domain.models.quote import Quote
from monik.domain.value_objects.identity import NetworkId

__all__ = ["SwapTransaction"]


class SwapTransaction(DomainModel):
    """Вызов роутера агрегатора, собранный из котировки."""

    provider_id: ProviderId
    network_id: NetworkId
    chain_id: int = Field(gt=0)
    #: Контракт, которому адресована транзакция.
    to: str = Field(min_length=42, max_length=42)
    #: Закодированный вызов.
    data: str = Field(min_length=2)
    #: Сколько native token отправляется вместе с вызовом. Для обмена
    #: между ERC-20 токенами — ноль.
    value: int = Field(ge=0)
    #: Предел газа, названный агрегатором.
    gas_limit: int = Field(gt=0)
    #: Контракт, которому нужно разрешение списывать входной токен.
    spender: str = Field(min_length=42, max_length=42)
    #: Котировка, из которой собрана транзакция. Хранится целиком: решение
    #: об отправке принимается по ней, а не по той, на которой возможность
    #: была найдена минутой раньше.
    quote: Quote
    #: Минимум, ниже которого сделка откатится.
    min_output_raw: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Минимум обязан быть не выше ожидаемого выхода.

        Обратное означало бы транзакцию, которая откатится всегда: мы
        требуем больше, чем сам агрегатор обещает.
        """
        if self.min_output_raw > self.quote.output_amount.raw:
            raise ValueError(
                "minimum output exceeds the quoted output: such a swap can only revert"
            )
        if not self.data.startswith("0x"):
            raise ValueError("swap calldata must be hex-encoded with a 0x prefix")
        return self

    @property
    def slippage_room_raw(self) -> int:
        """На сколько выход может просесть, прежде чем сделка откатится."""
        return self.quote.output_amount.raw - self.min_output_raw
