"""Перевод стоимости газа в базовый токен сделки.

Газ платится native token сети, а решение о сделке принимается в базовом
токене. Ошибка здесь не видна глазом: она не ломает работу, а тихо
смещает границу между прибыльной сделкой и убыточной.
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.models.conversion import ConversionRate
from monik.domain.models.token import Token
from monik.services.observability import FakeClock
from monik.services.prices.conversion import ConversionService
from monik.services.trading.costs import GasCostConverter
from tests import factories as f


class _Rates:
    """Источник курса, отвечающий заданным числом.

    **Test implementation** (``CLAUDE.md`` §10).
    """

    def __init__(self, rate: str | None) -> None:
        self._rate = rate
        self.asked = 0

    async def rate(self, from_token: Token, to_token: Token) -> ConversionRate | None:
        self.asked += 1
        if self._rate is None:
            return None
        return ConversionRate(
            from_token=from_token.key,
            to_token=to_token.key,
            rate=Decimal(self._rate),
            source="test",
            observed_at=f.NOW,
        )


class _Tokens:
    """Реестр из двух токенов сети."""

    def __init__(self, *tokens: Token) -> None:
        self._items = {str(token.key): token for token in tokens}

    def get(self, key: object) -> Token | None:
        return self._items.get(str(key))


class _Networks:
    """Реестр, знающий обёрнутый native token сети."""

    def __init__(self, native: Token) -> None:
        self._native = native

    def wrapped_native_token(self, network_id: object) -> object:
        return self._native.key


def _converter(rate: str | None) -> tuple[GasCostConverter, _Rates]:
    rates = _Rates(rate)
    converter = GasCostConverter(
        tokens=_Tokens(f.USDT, f.WMATIC),  # type: ignore[arg-type]
        networks=_Networks(f.WMATIC),  # type: ignore[arg-type]
        rates=ConversionService(FakeClock(f.NOW), providers=(rates,)),  # type: ignore[arg-type]
    )
    return converter, rates


class TestConversion:
    async def test_converts_wei_into_the_base_token(self) -> None:
        """0.1 WMATIC по курсу 0.5 USDT — это 0.05 USDT."""
        converter, _ = _converter("0.5")

        raw = await converter.to_base_raw(f.POLYGON, f.USDT, wei=10**17)

        assert raw == 50_000  # 0.05 USDT в base units

    async def test_rounds_the_cost_up(self) -> None:
        """Занизить собственный расход опаснее, чем завысить.

        Заниженный расход превращает убыточный круг в видимо прибыльный,
        а завышенный лишь отложит выход.
        """
        converter, _ = _converter("0.5")

        # 1 wei по курсу 0.5 — это 5e-19 USDT, то есть меньше одной
        # неделимой единицы. Округление вниз дало бы «бесплатно».
        assert await converter.to_base_raw(f.POLYGON, f.USDT, wei=1) == 1

    async def test_unknown_rate_makes_the_cost_unknown(self) -> None:
        """Без курса расход неизвестен, а не равен нулю."""
        converter, _ = _converter(None)

        assert await converter.to_base_raw(f.POLYGON, f.USDT, wei=10**17) is None

    async def test_zero_costs_nothing_and_asks_nobody(self) -> None:
        converter, rates = _converter("0.5")

        assert await converter.to_base_raw(f.POLYGON, f.USDT, wei=0) == 0
        assert rates.asked == 0, "курс ради нуля не запрашивается"

    async def test_rate_is_asked_once_for_repeated_checks(self) -> None:
        """Проверка выхода идёт каждые десять секунд.

        Запрашивать курс на каждой из них значило бы тратить запросы на
        значение, которое не меняется (минимум обращений к внешним API).
        """
        converter, rates = _converter("0.5")

        for _ in range(5):
            await converter.to_base_raw(f.POLYGON, f.USDT, wei=10**17)

        assert rates.asked == 1
