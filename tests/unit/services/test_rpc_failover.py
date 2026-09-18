"""Переход на запасной узел сети.

Узел — внешняя служба, и публичные узлы отказывают. Пока у сети один
адрес, такой отказ останавливает всё, что зависит от цепи, — включая
подбор квитанции уже отправленной покупки: деньги потрачены, а узнать их
судьбу нечем.
"""

from __future__ import annotations

import pytest

from monik.domain.errors import AuthenticationError, DataError, RateLimitError
from monik.domain.value_objects.identity import NetworkId
from monik.services.rpc import RpcEndpoints, call_with_failover

NETWORK = NetworkId("polygon")
FIRST = "https://first.example"
SECOND = "https://second.example"
THIRD = "https://third.example"


class _Node:
    """Узел, отвечающий по сценарию. **Test implementation**."""

    def __init__(self, answers: dict[str, object]) -> None:
        self._answers = answers
        self.asked: list[str] = []

    async def __call__(self, url: str) -> object:
        self.asked.append(url)
        answer = self._answers.get(url, "не настроен")
        if isinstance(answer, Exception):
            raise answer
        return answer


class TestFailover:
    async def test_first_working_endpoint_answers(self) -> None:
        node = _Node({FIRST: "0x1"})

        result = await call_with_failover(
            (FIRST, SECOND), node, network_id=NETWORK, method="eth_call"
        )

        assert result == "0x1"
        assert node.asked == [FIRST], "запасной не трогали"

    async def test_refusing_endpoint_is_replaced_by_the_next(self) -> None:
        """Отказ 403 — ровно то, из-за чего сделка осталась без присмотра."""
        node = _Node(
            {
                FIRST: AuthenticationError(
                    "provider rejected the credentials", code="http_forbidden"
                ),
                SECOND: "0x2",
            }
        )

        result = await call_with_failover(
            (FIRST, SECOND), node, network_id=NETWORK, method="eth_getTransactionReceipt"
        )

        assert result == "0x2"
        assert node.asked == [FIRST, SECOND]

    async def test_every_endpoint_is_tried_before_giving_up(self) -> None:
        node = _Node(
            {
                FIRST: RateLimitError("rate limited", code="http_rate_limited"),
                SECOND: AuthenticationError("rejected", code="http_forbidden"),
                THIRD: "0x3",
            }
        )

        result = await call_with_failover(
            (FIRST, SECOND, THIRD), node, network_id=NETWORK, method="eth_call"
        )

        assert result == "0x3"
        assert node.asked == [FIRST, SECOND, THIRD]

    async def test_last_failure_is_raised_when_no_endpoint_answers(self) -> None:
        """Наружу уходит текущее состояние, а не то, с чего перебор начался."""
        node = _Node(
            {
                FIRST: RateLimitError("rate limited", code="http_rate_limited"),
                SECOND: AuthenticationError("rejected", code="http_forbidden"),
            }
        )

        with pytest.raises(AuthenticationError):
            await call_with_failover((FIRST, SECOND), node, network_id=NETWORK, method="eth_call")


class TestAnswersAreNotRetried:
    """Отказ узла и ответ сети — разные вещи.

    Если цепь ответила «такой транзакции нет» или «вызов откатился», то
    это ответ. Спрашивать то же самое у соседнего узла незачем: он
    ответит так же, а мы потратим ещё один запрос.
    """

    async def test_data_error_does_not_move_to_the_next_endpoint(self) -> None:
        node = _Node(
            {
                FIRST: DataError("rpc refused eth_call", code="rpc_call_failed"),
                SECOND: "0x2",
            }
        )

        with pytest.raises(DataError):
            await call_with_failover((FIRST, SECOND), node, network_id=NETWORK, method="eth_call")

        assert node.asked == [FIRST], "второй узел не спрашивали"


class TestEndpointSet:
    def test_primary_is_the_first_declared(self) -> None:
        endpoints = RpcEndpoints({"polygon": (FIRST, SECOND)})

        assert endpoints.primary(NETWORK) == FIRST
        assert endpoints.for_network(NETWORK) == (FIRST, SECOND)

    def test_network_without_endpoints_is_not_supported(self) -> None:
        endpoints = RpcEndpoints({"polygon": ()})

        assert not endpoints.supports(NETWORK)
        assert endpoints.primary(NETWORK) is None

    def test_single_address_as_a_string_is_refused(self) -> None:
        """Строка — тоже последовательность, и разобралась бы на символы.

        Ошибка была бы тихой: список окажется непустым, проверка наличия
        узла пройдёт, а обращения пойдут по адресам «h», «t», «t», «p».
        """
        with pytest.raises(TypeError, match="ordered sequence"):
            RpcEndpoints({"polygon": FIRST})

    async def test_empty_endpoint_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no rpc endpoints"):
            await call_with_failover((), _Node({}), network_id=NETWORK, method="eth_call")
