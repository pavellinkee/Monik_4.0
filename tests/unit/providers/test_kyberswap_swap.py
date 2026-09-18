"""Сборка транзакции KyberSwap.

Формы ответов сняты с живого API 2026-09-18. Главная особенность
провайдера: **гарантированного минимума в ответе нет**. Uniswap называет
его отдельным полем, KyberSwap только зашивает в calldata, а наружу
отдаёт ожидаемый выход и заданный нами допуск. Минимум поэтому выводится
адаптером — и вывод проверен на живом API: полученное число нашлось в
самой calldata при допусках 1, 10 и 100 базисных пунктов.
"""

from __future__ import annotations

import pytest

from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.errors import DataError, UnsupportedError
from monik.domain.models.execution import AllowanceKind
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.infrastructure.providers import QuoteRequest
from monik.infrastructure.providers.kyberswap import KyberSwapAdapter
from monik.services.observability import FakeClock
from tests import factories as f
from tests.contract.test_kyberswap_contract import ROUTES_PAYLOAD
from tests.unit.providers.support import provider_config, resource_manager, secret

SWAPPER = "0x" + "c6" * 20
ROUTER = "0x6131B5fae19EA4f9D964eAc0408E4408b66337b5"

#: Ответ ``POST /route/build``. Минимума здесь нет — только ожидаемый
#: выход, расход газа и сама calldata.
BUILD_PAYLOAD = {
    "code": 0,
    "message": "successfully",
    "data": {
        "amountIn": "100000000",
        "amountOut": "8899446567405885440",
        "gas": "629502",
        "gasUsd": "0.0168811",
        "outputChange": {"amount": "0", "percent": 0, "level": 0},
        "data": "0xe21fd0e9" + "00" * 32,
        "routerAddress": ROUTER,
        "transactionValue": "0",
    },
}


def _adapter(*, options: dict[str, str] | None = None) -> KyberSwapAdapter:
    clock = FakeClock(f.NOW)
    return KyberSwapAdapter(
        provider_config(
            ProviderId.KYBERSWAP,
            options=options if options is not None else {"swapper": SWAPPER},
        ),
        # Сборка — это два обращения подряд: маршрут, затем его кодирование.
        http=FakeHttpClient(
            [
                HttpResponse(status_code=200, text=_json(ROUTES_PAYLOAD)),
                HttpResponse(status_code=200, text=_json(BUILD_PAYLOAD)),
            ]
        ),
        resources=resource_manager(clock),
        clock=clock,
        api_key=secret(),
    )


def _json(payload: object) -> str:
    import json

    return json.dumps(payload)


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
        assert transaction.data.startswith("0xe21fd0e9")
        assert transaction.gas_limit == 629_502
        assert transaction.value == 0
        assert transaction.chain_id == 137

    async def test_single_allowance_goes_straight_to_the_router(self) -> None:
        """Permit2 здесь нет: роутер списывает токен сам.

        У Uniswap разрешений два — токен разрешает Permit2, Permit2
        разрешает роутеру. Общая логика об этой разнице не знает: каждый
        адаптер называет свои требования сам.
        """
        transaction = await _adapter().build_swap(_request())

        assert len(transaction.allowances) == 1
        allowance = transaction.allowances[0]
        assert allowance.kind is AllowanceKind.ERC20
        assert allowance.contract == str(f.USDT.address)
        assert allowance.spender == ROUTER


class TestGuaranteedMinimum:
    """Минимум выводится адаптером: провайдер его не присылает."""

    @pytest.mark.parametrize(
        ("bps", "expected"),
        [
            (1, 8899446567405885440 * 9_999 // 10_000),
            (10, 8899446567405885440 * 9_990 // 10_000),
            (100, 8899446567405885440 * 9_900 // 10_000),
        ],
    )
    async def test_minimum_follows_the_requested_tolerance(self, bps: int, expected: int) -> None:
        transaction = await _adapter().build_swap(_request(slippage_bps=bps))

        assert transaction.min_output_raw == expected

    async def test_minimum_never_exceeds_the_promise(self) -> None:
        """Иначе транзакция откатывалась бы всегда."""
        transaction = await _adapter().build_swap(_request(slippage_bps=1))

        assert transaction.min_output_raw < transaction.quote.output_amount.raw
        assert transaction.slippage_room_raw > 0

    async def test_swap_without_a_tolerance_is_refused(self) -> None:
        """Без допуска минимум вывести не из чего.

        Собрать транзакцию всё равно можно было бы, но решение о сделке
        принимается именно по минимуму: транзакция без него — это
        транзакция без обязательства.
        """
        with pytest.raises(DataError, match="slippage tolerance"):
            await _adapter().build_swap(_request(slippage_bps=None))


class TestRefusals:
    async def test_swap_requires_the_trading_account(self) -> None:
        """Calldata собирается под конкретный счёт и другим не годится."""
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
