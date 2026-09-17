"""Расход газа на круг покупка-продажа.

Ноги оцениваются по отдельности и каждая со своей поправкой: они могут
принадлежать разным агрегаторам, а расхождение между обещанным и
фактическим расходом у каждого своё.
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.services.gas.round_trip import round_trip_gas_units
from tests import factories as f


class _Correction:
    """Поправка, заданная по агрегаторам. **Test implementation**."""

    def __init__(self, **factors: str) -> None:
        self._factors = {name: Decimal(value) for name, value in factors.items()}

    def factor(self, network_id: object, provider_id: ProviderId) -> Decimal:
        return self._factors.get(provider_id.value, Decimal(1))


def _quote(provider: ProviderId, operation: OperationType, units: int | None):  # noqa: ANN202
    return f.quote(provider_id=provider, operation=operation).model_copy(
        update={"estimated_gas_units": units}
    )


class TestRoundTrip:
    def test_sums_both_legs_without_correction(self) -> None:
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 100_000)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 150_000)

        assert round_trip_gas_units(buy, sell) == 250_000

    def test_each_leg_gets_the_correction_of_its_own_provider(self) -> None:
        """Общая поправка одному агрегатору занижала бы, другому завышала."""
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 100_000)
        sell = _quote(ProviderId.KYBERSWAP, OperationType.SELL, 100_000)

        total = round_trip_gas_units(
            buy, sell, correction=_Correction(uniswap="3.0", kyberswap="1.5")
        )

        assert total == 300_000 + 150_000

    def test_correction_rounds_up(self) -> None:
        """Занизить собственный расход опаснее, чем завысить."""
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 1)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 1)

        total = round_trip_gas_units(buy, sell, correction=_Correction(uniswap="1.1"))

        assert total == 4, "1.1 округляется вверх до 2 на каждой ноге"

    def test_unknown_leg_makes_the_total_unknown(self) -> None:
        """Достраивать недостающую оценку нельзя (``CLAUDE.md`` §12)."""
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, None)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 150_000)

        assert round_trip_gas_units(buy, sell, correction=_Correction(uniswap="3.0")) is None
