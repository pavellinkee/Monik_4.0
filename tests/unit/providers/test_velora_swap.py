"""Сборка транзакции Velora.

Формы ответов сняты с живого API 2026-09-18. Особенность провайдера, ради
которой он и нужен в паре: **минимум задаём мы сами**. Uniswap называет
его отдельным полем, KyberSwap выводит из допуска, а Velora принимает
параметром ``destAmount`` — сколько назвали, ниже того роутер и не
отдаст. Это самая честная из трёх форм: обязательство не выводится и не
угадывается, а задаётся.
"""

from __future__ import annotations

import json

import pytest

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, UnsupportedError
from monik.domain.models.execution import AllowanceKind
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.velora import VeloraAdapter
from monik.services.observability import FakeClock
from tests import factories as f
from tests.unit.providers.support import provider_config, resource_manager, secret

SWAPPER = "0x" + "c6" * 20
ROUTER = "0x6a000F20005980200259B80c5102003040001068"
PROXY = "0x216B4B4Ba9F3e719726886d34a177484278Bfcae"

#: Ответ ``/prices``. Списывает не роутер, а отдельный посредник, и его
#: адрес приходит здесь же.
PRICE_PAYLOAD = {
    "priceRoute": {
        "srcAmount": "100000000",
        "destAmount": "5140000000000000000",
        "gasCost": "218300",
        "contractAddress": ROUTER,
        "tokenTransferProxy": PROXY,
        "hmac": "0cafe",
        "bestRoute": [{"swaps": [{"swapExchanges": [{"exchange": "QuickSwapV3"}]}]}],
    }
}

#: Ответ ``/transactions``. Предела газа здесь нет: проверка на стороне
#: провайдера отключена, и расход берётся из маршрута.
TX_PAYLOAD = {
    "from": SWAPPER,
    "to": ROUTER,
    "value": "0",
    "data": "0xe3ead59e" + "00" * 32,
    "gasPrice": "376789872320",
    "chainId": 137,
}


def _adapter(*, options: dict[str, str] | None = None) -> VeloraAdapter:
    clock = FakeClock(f.NOW)
    return VeloraAdapter(
        provider_config(
            ProviderId.VELORA,
            options=options if options is not None else {"swapper": SWAPPER},
        ),
        http=FakeHttpClient(
            [
                HttpResponse(status_code=200, text=json.dumps(PRICE_PAYLOAD)),
                HttpResponse(status_code=200, text=json.dumps(TX_PAYLOAD)),
            ]
        ),
        resources=resource_manager(clock),
        clock=clock,
        api_key=secret(),
    )


def _request(**overrides: object) -> QuoteRequest:
    base: dict[str, object] = {
        "network_id": f.POLYGON,
        "operation": OperationType.BUY,
        "input_token": f.USDT,
        "output_token": f.AAVE,
        "input_amount": f.USDT.amount_from_base_units(100_000_000),
        "request_id": f.RequestId.generate(),
        "slippage_bps": 10,
    }
    base.update(overrides)
    return QuoteRequest(**base)  # type: ignore[arg-type]


class TestBuiltTransaction:
    async def test_router_calldata_and_gas_come_from_the_provider(self) -> None:
        transaction = await _adapter().build_swap(_request())

        assert transaction.to == ROUTER
        assert transaction.data.startswith("0xe3ead59e")
        assert transaction.value == 0
        assert transaction.chain_id == 137

    async def test_gas_limit_comes_from_the_route(self) -> None:
        """Ответ сборки предела газа не несёт: расход называет маршрут."""
        transaction = await _adapter().build_swap(_request())

        assert transaction.gas_limit == 218_300

    async def test_allowance_goes_to_the_transfer_proxy(self) -> None:
        """Списывает посредник, а не роутер, и адрес приходит в ответе.

        Угадывать его нельзя: он меняется вместе с версией роутера.
        """
        transaction = await _adapter().build_swap(_request())

        assert len(transaction.allowances) == 1
        allowance = transaction.allowances[0]
        assert allowance.kind is AllowanceKind.ERC20
        assert allowance.contract == str(f.USDT.address)
        assert allowance.spender == PROXY
        assert allowance.spender != transaction.to


class TestGuaranteedMinimum:
    @pytest.mark.parametrize(
        ("bps", "expected"),
        [
            (1, 5140000000000000000 * 9_999 // 10_000),
            (10, 5140000000000000000 * 9_990 // 10_000),
            (100, 5140000000000000000 * 9_900 // 10_000),
        ],
    )
    async def test_minimum_follows_the_requested_tolerance(self, bps: int, expected: int) -> None:
        transaction = await _adapter().build_swap(_request(slippage_bps=bps))

        assert transaction.min_output_raw == expected

    async def test_minimum_never_exceeds_the_promise(self) -> None:
        transaction = await _adapter().build_swap(_request(slippage_bps=1))

        assert transaction.min_output_raw < transaction.quote.output_amount.raw
        assert transaction.slippage_room_raw > 0

    async def test_swap_without_a_tolerance_is_refused(self) -> None:
        """Минимум задаём мы, и задать его не из чего."""
        with pytest.raises(DataError, match="slippage tolerance"):
            await _adapter().build_swap(_request(slippage_bps=None))


class TestRefusals:
    async def test_swap_requires_the_trading_account(self) -> None:
        with pytest.raises(UnsupportedError, match="swapper"):
            await _adapter(options={}).build_swap(_request())

    async def test_unknown_network_is_refused(self) -> None:
        other = f.NetworkId("base")
        with pytest.raises(UnsupportedError, match="does not support network"):
            await _adapter().build_swap(
                _request(
                    network_id=other,
                    input_token=f.USDT.model_copy(update={"network_id": other}),
                    output_token=f.AAVE.model_copy(update={"network_id": other}),
                )
            )
