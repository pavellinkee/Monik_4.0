"""Расход газа на круг покупка-продажа.

Ноги оцениваются по отдельности и каждая со своей поправкой: они могут
принадлежать разным агрегаторам, а расхождение между обещанным и
фактическим расходом у каждого своё.
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.services.gas.round_trip import round_trip_correction, round_trip_gas_units
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


class TestCorrectionFactor:
    """Во сколько раз поправка увеличила оценку круга.

    Величина нужна там, где курс native token выводится из самой
    котировки: делить долларовую цифру агрегатора на **исправленную**
    стоимость нельзя, иначе поправка сокращается сама с собой.
    """

    def test_factor_is_the_ratio_of_corrected_to_quoted(self) -> None:
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 100_000)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 100_000)

        factor = round_trip_correction(buy, sell, correction=_Correction(uniswap="3.0"))

        assert factor == Decimal(3)

    def test_mixed_providers_give_the_effective_factor(self) -> None:
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 100_000)
        sell = _quote(ProviderId.KYBERSWAP, OperationType.SELL, 100_000)

        factor = round_trip_correction(
            buy, sell, correction=_Correction(uniswap="3.0", kyberswap="1.0")
        )

        assert factor == Decimal(2), "(300000 + 100000) / 200000"

    def test_no_correction_is_one(self) -> None:
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, 100_000)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 100_000)

        assert round_trip_correction(buy, sell) == Decimal(1)

    def test_unknown_estimate_is_one(self) -> None:
        """Считать не из чего — поправки нет, а не ноль."""
        buy = _quote(ProviderId.UNISWAP, OperationType.BUY, None)
        sell = _quote(ProviderId.UNISWAP, OperationType.SELL, 100_000)

        assert round_trip_correction(buy, sell, correction=_Correction(uniswap="3.0")) == Decimal(1)
