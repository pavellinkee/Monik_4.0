"""Курс native token, выведенный из уже полученной котировки.

Стоимость газа считается в native token сети, а прибыль — в валюте
расчёта, поэтому нужен курс между ними. Обычно его берёт
:class:`ConversionService`, и для этого он запрашивает у агрегатора
отдельную пробную котировку — ещё одно обращение в каждом цикле.

Между тем часть агрегаторов присылает стоимость исполнения **в долларах**
вместе с самой котировкой. Если валюта расчёта привязана к доллару, этого
достаточно: курс выводится делением долларовой стоимости на стоимость в
native token, и запрашивать ничего не нужно.

Ограничения намеренно узкие, потому что доллар у провайдеров — их
собственная оценка и между агрегаторами слегка расходится:

* признак привязки к доллару объявляет оператор в конфигурации токена,
  код не угадывает его по символу;
* вывод применяется **только к стоимости газа**; суммы сделки
  сравниваются в токенах, как и прежде;
* при нехватке любого из слагаемых возвращается ``None``, и работает
  обычный источник курса — подставлять приблизительное значение вместо
  точного запрещено (``CLAUDE.md`` §12).
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.models.conversion import ConversionRate
from monik.domain.models.gas import Gas
from monik.domain.models.quote import Quote
from monik.domain.models.token import Token
from monik.domain.value_objects.timestamps import UtcDatetime

__all__ = ["gas_rate_from_quotes"]

#: Источник курса в диагностике.
SOURCE = "quote:gas_cost_usd"


def gas_rate_from_quotes(
    gas: Gas,
    quotes: tuple[Quote, ...],
    *,
    target: Token,
    now: UtcDatetime,
    units_correction: Decimal = Decimal(1),
) -> ConversionRate | None:
    """Курс native token в валюту расчёта по стоимости газа.

    Возвращает ``None``, если валюта расчёта не привязана к доллару, если
    стоимость газа в native token неизвестна, либо если ни одна котировка
    не сообщила долларовую стоимость.

    ``units_correction`` — во сколько раз оценка расхода была увеличена
    поправкой. Курс выводится по **неисправленной** стоимости, то есть по
    той самой, к которой относится названная агрегатором долларовая
    цифра. Делить на исправленную нельзя: курс уменьшился бы ровно во
    столько же раз, во сколько выросли единицы, поправка сократилась бы, и
    стоимость газа осталась бы равной оценке агрегатора — той, которую
    поправка и призвана исправить.
    """
    if not target.usd_stable:
        return None
    if gas.native_token is None or gas.cost_native is None or gas.cost_native <= Decimal(0):
        return None
    cost_usd = _first_cost_usd(quotes)
    if cost_usd is None or cost_usd <= Decimal(0):
        return None
    if units_correction <= Decimal(0):
        return None
    quoted_native = gas.cost_native / units_correction
    return ConversionRate(
        from_token=gas.native_token,
        to_token=target.key,
        rate=cost_usd / quoted_native,
        source=SOURCE,
        observed_at=now,
    )


def _first_cost_usd(quotes: tuple[Quote, ...]) -> Decimal | None:
    """Первая сообщённая долларовая стоимость исполнения.

    Стоимость относится к сети, а не к провайдеру, поэтому достаточно,
    чтобы её сообщил хотя бы один из них.
    """
    for quote in quotes:
        if quote.estimated_gas_cost_usd is not None:
            return Decimal(quote.estimated_gas_cost_usd)
    return None
