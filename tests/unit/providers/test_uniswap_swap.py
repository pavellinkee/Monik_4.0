"""Сборка транзакции обмена у Uniswap.

Trading API устроен в два шага: котировка и превращение её в вызов
роутера. Особенность остаётся в адаптере, наружу выходит одно понятие —
:class:`SwapTransaction` (``CLAUDE.md`` §7).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, UnsupportedError
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers.contract import QuoteRequest
from monik.infrastructure.providers.uniswap import endpoints
from monik.services.observability import FakeClock
from tests import factories as f
from tests.unit.providers.support import provider_config, resource_manager, secret

SWAPPER = "0x4745ab81B8FD8C9ea6Af85980eEC6F23865576aB"
ROUTER = "0xA51afAFe0263b40EdaEf0Df8781eA9aa03E381a3"


def _quote_body(minimum: str = "49900000") -> dict[str, Any]:
    return {
        "routing": "CLASSIC",
        "quote": {
            "chainId": 137,
            "swapper": SWAPPER,
            "tradeType": "EXACT_INPUT",
            "route": [[]],
            "input": {"amount": "50000000", "token": "0x1"},
            "output": {"amount": "50000000", "token": "0x2", "minimumAmount": minimum},
            "slippage": 0.1,
            "gasUseEstimate": "100430",
            "routeString": "[v4] 100.00%",
        },
    }


def _swap_body(**overrides: Any) -> dict[str, Any]:
    swap = {
        "to": ROUTER,
        "from": SWAPPER,
        "data": "0x3593564c",
        "value": "0x00",
        "gasLimit": "100429",
        "chainId": 137,
    }
    swap.update(overrides)
    return {"swap": swap}


def _adapter(clock: FakeClock, responses: list[HttpResponse]) -> Any:
    from monik.infrastructure.providers.uniswap.adapter import UniswapAdapter

    return UniswapAdapter(
        provider_config(ProviderId.UNISWAP, options={"swapper": SWAPPER}),
        http=FakeHttpClient(responses),
        resources=resource_manager(clock),
        clock=clock,
        api_key=secret(),
    )


def _request(network_id: NetworkId = f.POLYGON) -> QuoteRequest:
    return QuoteRequest(
        network_id=network_id,
        operation=OperationType.BUY,
        input_token=f.USDT,
        output_token=f.AAVE,
        input_amount=f.USDT.amount_from_base_units(50_000_000),
        request_id=f.RequestId.generate(),
    )


def _ok(body: dict[str, Any]) -> HttpResponse:
    return HttpResponse(status_code=200, text=json.dumps(body))


class TestBuildSwap:
    async def test_transaction_is_assembled_from_a_fresh_quote(self) -> None:
        clock = FakeClock(f.NOW)
        adapter = _adapter(clock, [_ok(_quote_body()), _ok(_swap_body())])

        transaction = await adapter.build_swap(_request())

        assert transaction.to == ROUTER
        assert transaction.data == "0x3593564c"
        assert transaction.value == 0
        assert transaction.gas_limit == 100_429
        assert transaction.chain_id == 137
        assert transaction.min_output_raw == 49_900_000

    async def test_allowance_goes_to_permit2_not_to_the_router(self) -> None:
        """Роутер списывает токены через Permit2 — разрешение ему."""
        clock = FakeClock(f.NOW)
        adapter = _adapter(clock, [_ok(_quote_body()), _ok(_swap_body())])

        transaction = await adapter.build_swap(_request())

        assert transaction.spender == endpoints.PERMIT2_ADDRESS
        assert transaction.spender != transaction.to

    async def test_minimum_comes_from_the_api_not_from_our_arithmetic(self) -> None:
        """Проверять будет роутер, и проверит он именно это число."""
        clock = FakeClock(f.NOW)
        adapter = _adapter(clock, [_ok(_quote_body(minimum="12345")), _ok(_swap_body())])

        transaction = await adapter.build_swap(_request())

        assert transaction.min_output_raw == 12_345

    async def test_malformed_response_is_rejected(self) -> None:
        clock = FakeClock(f.NOW)
        adapter = _adapter(clock, [_ok(_quote_body()), _ok({"swap": "не объект"})])

        with pytest.raises(DataError):
            await adapter.build_swap(_request())

    async def test_unsupported_network_is_refused_before_any_request(self) -> None:
        """Сеть, которой адаптер не заявлял, отсекается до обращения к API."""
        clock = FakeClock(f.NOW)
        adapter = _adapter(clock, [])
        other = NetworkId("optimism")
        request = QuoteRequest(
            network_id=other,
            operation=OperationType.BUY,
            input_token=f.USDT.model_copy(update={"network_id": other}),
            output_token=f.AAVE.model_copy(update={"network_id": other}),
            input_amount=f.USDT.amount_from_base_units(50_000_000),
            request_id=f.RequestId.generate(),
        )

        with pytest.raises(UnsupportedError):
            await adapter.build_swap(request)


class TestCapability:
    def test_uniswap_declares_execution(self) -> None:
        clock = FakeClock(f.NOW)
        assert _adapter(clock, []).capabilities.supports_execution is True
