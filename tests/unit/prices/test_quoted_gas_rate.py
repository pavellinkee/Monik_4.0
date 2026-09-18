"""Курс native token, выведенный из стоимости газа в долларах."""

from __future__ import annotations

from decimal import Decimal

from monik.domain.enums.fees import FeeStatus
from monik.domain.models.gas import Gas, GasPrice
from monik.services.prices.quoted import SOURCE, gas_rate_from_quotes
from tests import factories as f


def _gas(cost_native: str | None = "0.01") -> Gas:
    if cost_native is None:
        return Gas(
            network_id=f.POLYGON,
            status=FeeStatus.UNKNOWN,
            observed_at=f.NOW,
            source="test",
        )
    return Gas(
        network_id=f.POLYGON,
        status=FeeStatus.KNOWN,
        gas_units=200_000,
        gas_price=GasPrice(
            network_id=f.POLYGON,
            wei_per_gas=50_000_000_000,
            source="test",
            observed_at=f.NOW,
        ),
        native_token=f.WMATIC.key,
        cost_native=Decimal(cost_native),
        observed_at=f.NOW,
        source="test",
    )


def _quote(cost_usd: str | None) -> object:
    quote = f.quote()
    return quote.model_copy(
        update={"estimated_gas_cost_usd": Decimal(cost_usd) if cost_usd else None}
    )


class TestDerivedRate:
    def test_rate_is_dollars_per_native_unit(self) -> None:
        """0.005 доллара за 0.01 POL — значит POL стоит 0.5 доллара."""
        rate = gas_rate_from_quotes(
            _gas("0.01"), (_quote("0.005"),), target=f.USDT_STABLE, now=f.NOW
        )

        assert rate is not None
        assert rate.rate == Decimal("0.5")
        assert rate.source == SOURCE
        assert rate.to_token == f.USDT_STABLE.key

    def test_cost_of_every_leg_is_added_up(self) -> None:
        """Стоимость в native token относится к кругу целиком.

        Делить её на цену одной ноги — значит получить не курс, а его
        долю, с ошибкой ровно во столько раз, сколько ног в круге.
        Раньше бралась первая нога, сообщившая цену, и курс выходил
        вдвое ниже настоящего.
        """
        rate = gas_rate_from_quotes(
            _gas("0.01"),
            (_quote("0.003"), _quote("0.002")),
            target=f.USDT_STABLE,
            now=f.NOW,
        )

        assert rate is not None
        assert rate.rate == Decimal("0.5"), "0.005 доллара за 0.01 native token"

    def test_leg_without_a_price_makes_the_sum_unknown(self) -> None:
        """Достраивать цену второй ноги по первой нельзя.

        Ноги обходятся по-разному: у одной маршрут простой, у другой
        может разойтись на несколько пулов.
        """
        assert (
            gas_rate_from_quotes(
                _gas("0.01"), (_quote(None), _quote("0.005")), target=f.USDT_STABLE, now=f.NOW
            )
            is None
        )


class TestRefusals:
    """Приблизительное значение вместо точного не подставляется."""

    def test_target_must_be_declared_usd_stable(self) -> None:
        """Признак объявляет оператор; по символу код не угадывает."""
        assert gas_rate_from_quotes(_gas(), (_quote("0.005"),), target=f.AAVE, now=f.NOW) is None

    def test_unknown_native_cost_gives_no_rate(self) -> None:
        assert (
            gas_rate_from_quotes(_gas(None), (_quote("0.005"),), target=f.USDT_STABLE, now=f.NOW)
            is None
        )

    def test_no_reported_dollars_gives_no_rate(self) -> None:
        assert (
            gas_rate_from_quotes(_gas("0.01"), (_quote(None),), target=f.USDT_STABLE, now=f.NOW)
            is None
        )

    def test_zero_values_give_no_rate(self) -> None:
        assert (
            gas_rate_from_quotes(_gas("0"), (_quote("0.005"),), target=f.USDT_STABLE, now=f.NOW)
            is None
        )
        assert (
            gas_rate_from_quotes(_gas("0.01"), (_quote("0"),), target=f.USDT_STABLE, now=f.NOW)
            is None
        )


class TestCorrectedUnits:
    """Поправка к расходу не должна сокращаться сама с собой.

    Курс выводится делением долларовой цифры агрегатора на стоимость в
    native token. Долларовая цифра относится к **необоснованно низкой**
    оценке расхода — к той самой, которую поправка и исправляет. Поэтому
    делить надо на неисправленную стоимость: иначе курс уменьшится ровно
    во столько же раз, во сколько выросли единицы, и стоимость газа
    останется равной оценке агрегатора.
    """

    def test_rate_is_derived_from_the_uncorrected_cost(self) -> None:
        """Расход утроен, значит курс считается по трети стоимости."""
        rate = gas_rate_from_quotes(
            _gas("0.03"),  # 0.01 POL по котировке, утроенные поправкой
            (_quote("0.005"),),
            target=f.USDT_STABLE,
            now=f.NOW,
            units_correction=Decimal(3),
        )

        assert rate is not None
        # 0.005 доллара приходились на 0.01 POL: курс прежний, настоящий.
        assert rate.rate == Decimal("0.5")

    def test_corrected_gas_ends_up_costing_more(self) -> None:
        """Проверка того, ради чего поправка и вводилась.

        Стоимость газа — это ``cost_native × rate``. При утроенном расходе
        она обязана вырасти втрое, а не остаться равной оценке агрегатора.
        """
        # Числа подобраны так, чтобы делиться нацело: проверяется
        # тождество, а не поведение округления.
        gas = _gas("0.03")
        quoted_usd = Decimal("0.006")

        without = gas_rate_from_quotes(
            gas, (_quote(str(quoted_usd)),), target=f.USDT_STABLE, now=f.NOW
        )
        with_correction = gas_rate_from_quotes(
            gas,
            (_quote(str(quoted_usd)),),
            target=f.USDT_STABLE,
            now=f.NOW,
            units_correction=Decimal(3),
        )

        assert without is not None and with_correction is not None
        assert gas.cost_native is not None
        assert gas.cost_native * without.rate == quoted_usd, "поправка сократилась"
        assert gas.cost_native * with_correction.rate == quoted_usd * 3

    def test_meaningless_correction_yields_no_rate(self) -> None:
        """Ноль или отрицательная поправка — не курс, а ошибка."""
        assert (
            gas_rate_from_quotes(
                _gas("0.03"),
                (_quote("0.005"),),
                target=f.USDT_STABLE,
                now=f.NOW,
                units_correction=Decimal(0),
            )
            is None
        )
