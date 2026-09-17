"""Расход газа на круг покупка-продажа.

Обе ноги оцениваются **по отдельности** и каждая со своей поправкой:
ноги могут принадлежать разным агрегаторам, а расхождение между обещанным
и фактическим расходом у каждого своё. Сложить сначала, а поправить потом
означало бы применить к одному агрегатору поправку другого.

Округление вверх выбрано сознательно: занизить собственный расход опаснее,
чем завысить. Заниженный расход превращает убыточный круг в видимо
прибыльный, а завышенный лишь отсеет пограничную возможность.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from monik.domain.enums.providers import ProviderId
from monik.domain.models.quote import Quote
from monik.domain.value_objects.identity import NetworkId

__all__ = ["GasUnitsCorrection", "round_trip_gas_units"]


class GasUnitsCorrection(Protocol):
    """Источник поправки к оценке расхода."""

    def factor(self, network_id: NetworkId, provider_id: ProviderId) -> Decimal:
        """Во сколько раз умножить оценку котировки."""
        ...


def round_trip_gas_units(
    buy_quote: Quote, sell_quote: Quote, *, correction: GasUnitsCorrection | None = None
) -> int | None:
    """Суммарная оценка газа круга с поправкой.

    Если хотя бы одна нога не сообщила оценку, суммарное значение
    неизвестно — достраивать его нельзя (``CLAUDE.md`` §12).
    """
    legs = (buy_quote.estimated_gas_units, sell_quote.estimated_gas_units)
    if any(leg is None for leg in legs):
        return None
    total = 0
    for quote, quoted in zip((buy_quote, sell_quote), legs, strict=True):
        units = Decimal(quoted or 0)
        if correction is not None:
            units *= correction.factor(quote.network_id, quote.provider_id)
        total += int(units.to_integral_value(rounding=ROUND_CEILING))
    return total
