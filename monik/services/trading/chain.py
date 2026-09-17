"""Чтение состояния счёта у самого узла сети.

Источник истины о деньгах — цепь, а не агрегатор и не наша база. Перед
каждой сделкой подсистема спрашивает у сети три вещи: сколько базового
токена на счёте, сколько разрешено списывать роутеру и сколько native
token остаётся на газ.

Запросы идут через Resource Manager (``CLAUDE.md`` §14) тем же путём,
что и проверка адресов токенов: для ядра это ещё один внешний ресурс с
именем ``rpc``, а не особый случай.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import DataError
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import RequestId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import HttpClient, HttpRequest, classify_response
from monik.services.gas.providers import RPC_RESOURCE_OWNER
from monik.services.observability.clock import Clock
from monik.services.resources import ResourceManager

__all__ = ["ChainAccount", "TokenBalance"]

#: Селектор ``balanceOf(address)``.
_BALANCE_OF = "0x70a08231"
#: Селектор ``allowance(address,address)``.
_ALLOWANCE = "0xdd62ed3e"

#: Значение «разрешено неограниченно» у ERC-20: 2**256 - 1.
UNLIMITED_ALLOWANCE = (1 << 256) - 1


@dataclass(frozen=True, slots=True)
class TokenBalance:
    """Остаток токена на счёте."""

    token: Token
    raw: int

    @property
    def as_decimal(self) -> Decimal:
        """Остаток в человеческих единицах."""
        return Decimal(self.raw).scaleb(-self.token.decimals)

    def covers(self, raw_amount: int) -> bool:
        """Хватает ли остатка на сумму сделки."""
        return self.raw >= raw_amount


def _pad_address(address: str) -> str:
    """Адрес в виде 32-байтного слова аргумента вызова."""
    return address.lower().removeprefix("0x").rjust(64, "0")


class ChainAccount:
    """Состояние торгового счёта в сети."""

    def __init__(
        self,
        *,
        address: str,
        http: HttpClient,
        resources: ResourceManager,
        clock: Clock,
        rpc_urls: dict[str, str],
        timeout_seconds: float = 5.0,
    ) -> None:
        self._address = address
        self._http = http
        self._resources = resources
        self._clock = clock
        self._rpc_urls = dict(rpc_urls)
        self._timeout = timedelta(seconds=timeout_seconds)

    @property
    def address(self) -> str:
        """Адрес счёта."""
        return self._address

    def supports(self, network_id: NetworkId) -> bool:
        """Есть ли у сети узел, которому можно задать вопрос."""
        return str(network_id) in self._rpc_urls

    async def token_balance(self, token: Token) -> TokenBalance:
        """Остаток токена на счёте."""
        raw = await self._call(
            token.network_id,
            to=str(token.address),
            data=_BALANCE_OF + _pad_address(self._address),
            operation=CapabilityOperation.TOKEN_METADATA,
            dedup=f"balance:{token.key}",
        )
        return TokenBalance(token=token, raw=_parse_uint(raw, field="balanceOf"))

    async def allowance(self, token: Token, spender: str) -> int:
        """Сколько роутеру разрешено списывать с нашего счёта."""
        raw = await self._call(
            token.network_id,
            to=str(token.address),
            data=_ALLOWANCE + _pad_address(self._address) + _pad_address(spender),
            operation=CapabilityOperation.TOKEN_METADATA,
            dedup=f"allowance:{token.key}:{spender.lower()}",
        )
        return _parse_uint(raw, field="allowance")

    async def native_balance(self, network_id: NetworkId) -> int:
        """Остаток native token — тот, которым платится газ."""
        raw = await self._request(
            network_id,
            method="eth_getBalance",
            params=[self._address, "latest"],
            dedup=f"native:{network_id}",
        )
        return _parse_uint(raw, field="eth_getBalance")

    # --- внутреннее -------------------------------------------------------

    async def _call(
        self,
        network_id: NetworkId,
        *,
        to: str,
        data: str,
        operation: CapabilityOperation,
        dedup: str,
    ) -> Any:
        return await self._request(
            network_id,
            method="eth_call",
            params=[{"to": to, "data": data}, "latest"],
            dedup=dedup,
            operation=operation,
        )

    async def _request(
        self,
        network_id: NetworkId,
        *,
        method: str,
        params: list[Any],
        dedup: str,
        operation: CapabilityOperation = CapabilityOperation.TOKEN_METADATA,
    ) -> Any:
        url = self._rpc_urls.get(str(network_id))
        if url is None:
            raise DataError(
                f"network {network_id} has no rpc endpoint configured",
                code="rpc_endpoint_missing",
            )
        request_id = RequestId.generate()
        resource_request = ResourceRequest(
            request_id=request_id,
            key=ResourceKey(
                provider_id=RPC_RESOURCE_OWNER,
                network_id=network_id,
                operation=operation,
            ),
            # Деньги проверяются перед сделкой, поэтому запрос не может
            # ждать в общей очереди обслуживания.
            priority=RequestPriority.LEVEL2,
            timeout=self._timeout,
            created_at=self._clock.now(),
            sequence=0,
            deduplication_key=f"rpc:{network_id}:{dedup}",
        )

        async def call() -> Any:
            response = await self._http.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    request_id=request_id,
                    timeout_seconds=self._timeout.total_seconds(),
                )
            )
            classify_response(response, provider="rpc")
            body = response.json()
            if not isinstance(body, dict):
                raise DataError("rpc response is not a JSON object", code="rpc_response_malformed")
            error = body.get("error")
            if error is not None:
                # Остаток и разрешение — это не «свойство адреса», как у
                # metadata: отказ здесь означает, что мы не знаем сумму.
                # Неизвестное не равно нулю (``CLAUDE.md`` §12).
                raise DataError(
                    f"rpc refused {method}",
                    code="rpc_call_failed",
                )
            return body.get("result")

        return await self._resources.execute(resource_request, call)


def _parse_uint(raw: Any, *, field: str) -> int:
    """Разобрать беззнаковое число из ответа узла."""
    if raw is None:
        raise DataError(f"rpc returned no value for {field!r}", code="rpc_value_missing")
    try:
        return int(str(raw), 16)
    except ValueError as error:
        raise DataError(
            f"rpc returned a malformed value for {field!r}",
            code="rpc_value_invalid",
        ) from error
