"""Обращение к узлу сети с переходом на запасной.

Узел — внешняя служба, и публичные узлы отказывают: по ограничению
частоты, по превышению квоты, просто отключившись. Пока у сети один
адрес, такой отказ останавливает всё, что зависит от цепи, — включая
подбор квитанции уже отправленной покупки. Тогда деньги потрачены, а
узнать их судьбу нечем.

Поэтому у сети не адрес, а **список**: первый — основной, остальные
запасные. Порядок значим и задаётся оператором.

Переход на следующий узел делается не на любой ошибке. Отказ узла и
отрицательный ответ сети — разные вещи: если цепь ответила «такой
транзакции нет» или «вызов откатился», то это ответ, и спрашивать то же
самое у соседнего узла незачем — он ответит так же. Переходим только
там, где отказал сам узел.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from monik.domain.errors import (
    AuthenticationError,
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["RpcEndpoints", "call_with_failover"]

_LOGGER = get_logger("services.rpc")

#: Ошибки, означающие «этот узел сейчас не работает». Остальные —
#: содержательные ответы сети, и повторять их у соседнего узла бессмысленно.
_ENDPOINT_FAILURES = (
    AuthenticationError,
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)


class RpcEndpoints:
    """Узлы сетей в порядке обращения."""

    __slots__ = ("_by_network",)

    def __init__(self, urls: Mapping[str, Sequence[str]]) -> None:
        for network, endpoints in urls.items():
            # Строка — тоже последовательность, и ``tuple`` разберёт её на
            # символы: сеть получит узлы «h», «t», «t», «p»… Ошибка тихая,
            # потому что список окажется непустым и проверка наличия узла
            # пройдёт. Поэтому она ловится здесь и сразу.
            if isinstance(endpoints, str):
                raise TypeError(
                    f"network {network} was given a single rpc address as a string; "
                    "endpoints are an ordered sequence"
                )
        self._by_network = {
            network: tuple(endpoints) for network, endpoints in urls.items() if endpoints
        }

    def supports(self, network_id: NetworkId) -> bool:
        """Есть ли у сети хоть один узел."""
        return str(network_id) in self._by_network

    def for_network(self, network_id: NetworkId) -> tuple[str, ...]:
        """Узлы сети: основной первым."""
        return self._by_network.get(str(network_id), ())

    def primary(self, network_id: NetworkId) -> str | None:
        """Основной узел сети."""
        endpoints = self.for_network(network_id)
        return endpoints[0] if endpoints else None


async def call_with_failover[T](
    endpoints: Sequence[str],
    attempt: Callable[[str], Awaitable[T]],
    *,
    network_id: NetworkId,
    method: str,
) -> T:
    """Выполнить обращение, перебирая узлы до первого ответившего.

    Если не ответил ни один, наружу уходит ошибка **последнего**: она
    описывает текущее состояние, а не то, с чего перебор начался.
    """
    if not endpoints:
        raise ValueError(f"network {network_id} has no rpc endpoints configured")
    last: Exception | None = None
    for position, url in enumerate(endpoints):
        try:
            return await attempt(url)
        except _ENDPOINT_FAILURES as error:
            last = error
            remaining = len(endpoints) - position - 1
            _LOGGER.warning(
                "rpc endpoint unavailable",
                extra=log_fields(
                    network=str(network_id),
                    method=method,
                    error=type(error).__name__,
                    # Адрес узла не секрет и нужен, чтобы понять, какой
                    # именно источник подвёл.
                    endpoint=url,
                    remaining=remaining,
                ),
            )
            if remaining == 0:
                raise
    raise last if last is not None else InternalStateError()


class InternalStateError(RuntimeError):
    """Недостижимое состояние перебора узлов."""
