"""Стоимость исполнения сделки в базовом токене.

Газ платится native token сети, а решение о сделке принимается в
базовом токене. Перевод одного в другое живёт здесь, отдельно от
наблюдателя: тому нужно знать, во сколько обойдётся круг, а не как
устроен курс.

Курс берётся у той же :class:`ConversionService`, что и в расчёте
прибыльности (решение D-4, ``09_PROFIT_CALCULATOR.md`` §74). Служба
кэширует курс и следит за его свежестью, поэтому проверка выхода каждые
десять секунд не превращается в десяток запросов курса в минуту.

Неизвестный курс делает стоимость **неизвестной**, а не нулевой: сделка,
расход которой не посчитан, не продаётся (``CLAUDE.md`` §12).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.logging import get_logger, log_fields
from monik.services.prices.conversion import ConversionService
from monik.services.registries.networks import NetworkRegistry
from monik.services.registries.tokens import TokenRegistry

__all__ = ["GasCostConverter"]

_LOGGER = get_logger("services.trading.costs")


class GasCostConverter:
    """Переводит стоимость газа в базовый токен сделки."""

    def __init__(
        self,
        *,
        tokens: TokenRegistry,
        networks: NetworkRegistry,
        rates: ConversionService,
    ) -> None:
        self._tokens = tokens
        self._networks = networks
        self._rates = rates

    async def to_base_raw(
        self, network_id: NetworkId, base_token: Token, *, wei: int
    ) -> int | None:
        """Стоимость ``wei`` газа в base units базового токена.

        ``None`` означает «посчитать не удалось». Округление — **вверх**:
        занизить собственный расход опаснее, чем завысить, потому что
        заниженный расход превращает убыточный круг в видимо прибыльный.
        """
        if wei <= 0:
            return 0
        native = self._tokens.get(self._networks.wrapped_native_token(network_id))
        if native is None:
            _LOGGER.warning(
                "gas cost unknown: the network has no wrapped native token",
                extra=log_fields(network=str(network_id)),
            )
            return None
        if native.key == base_token.key:
            return int(Decimal(wei).scaleb(base_token.decimals - native.decimals))
        rate = await self._rates.rate(native, base_token)
        if rate is None:
            _LOGGER.warning(
                "gas cost unknown: no fresh rate for the native token",
                extra=log_fields(
                    network=str(network_id),
                    native=str(native.symbol),
                    base=str(base_token.symbol),
                ),
            )
            return None
        in_base = Decimal(wei).scaleb(-native.decimals) * rate.rate
        return int(in_base.scaleb(base_token.decimals).to_integral_value(rounding=ROUND_CEILING))
