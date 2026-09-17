"""Состояние торгового счёта читается у самого узла сети.

Источник истины о деньгах — цепь. Отказ узла не превращается в ноль:
неизвестный остаток остаётся неизвестным (``CLAUDE.md`` §12), иначе
подсистема решила бы, что средств нет, и пропустила бы сделку — или,
хуже, что их хватает.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import DataError
from monik.domain.models.execution import (
    AllowanceKind,
    AllowanceRequirement,
    SwapTransaction,
)
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.services.observability import FakeClock
from monik.services.trading import ChainAccount
from monik.services.trading.chain import UNLIMITED_ALLOWANCE
from tests import factories as f
from tests.unit.providers.support import resource_manager
from tests.unit.trading.support import RPC_URL, ScriptedNode

ADDRESS = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"
ROUTER = "0xA51afAFe0263b40EdaEf0Df8781eA9aa03E381a3"


def _account(clock: FakeClock, responses: list[HttpResponse]) -> ChainAccount:
    return ChainAccount(
        address=ADDRESS,
        http=FakeHttpClient(responses),
        resources=resource_manager(clock),
        clock=clock,
        rpc_urls={str(f.POLYGON): "https://polygon-rpc.example"},
    )


def _result(value: object) -> HttpResponse:
    return HttpResponse(status_code=200, text=json.dumps({"result": value}))


def _error(message: str = "execution reverted") -> HttpResponse:
    return HttpResponse(status_code=200, text=json.dumps({"error": {"message": message}}))


class TestBalances:
    async def test_token_balance_is_read_from_the_chain(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result(hex(53_796_000))])

        balance = await account.token_balance(f.USDT)

        assert balance.raw == 53_796_000
        assert balance.as_decimal == Decimal("53.796")

    async def test_balance_knows_whether_it_covers_a_trade(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result(hex(53_796_000))])
        balance = await account.token_balance(f.USDT)

        assert balance.covers(50_000_000), "50 USDT помещаются в остаток"
        assert not balance.covers(100_000_000), "100 USDT — уже нет"

    async def test_native_balance_is_read_for_gas(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result(hex(2_052_000_000_000_000))])

        assert await account.native_balance(f.POLYGON) == 2_052_000_000_000_000


class TestAllowance:
    async def test_missing_allowance_reads_as_zero(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result("0x0")])

        assert await account.allowance(f.USDT, ROUTER) == 0

    async def test_unlimited_allowance_is_recognised(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result(hex(UNLIMITED_ALLOWANCE))])

        value = await account.allowance(f.USDT, ROUTER)
        assert value == UNLIMITED_ALLOWANCE


class TestUnknownIsNotZero:
    async def test_node_error_is_raised_not_silently_zero(self) -> None:
        """Отказ узла — это «не знаем», а не «ноль» (``CLAUDE.md`` §12)."""
        clock = FakeClock(f.NOW)
        account = _account(clock, [_error()])

        with pytest.raises(DataError, match="rpc refused"):
            await account.token_balance(f.USDT)

    async def test_malformed_value_is_rejected(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result("не число")])

        with pytest.raises(DataError, match="malformed"):
            await account.token_balance(f.USDT)

    async def test_network_without_an_endpoint_is_reported(self) -> None:
        """Сеть без узла — ошибка конфигурации, а не пустой ответ."""
        clock = FakeClock(f.NOW)
        account = _account(clock, [])

        with pytest.raises(DataError, match="no rpc endpoint"):
            await account.native_balance(NetworkId("arbitrum"))


class TestSimulation:
    """Сделка проверяется вызовом узла, но никуда не отправляется."""

    def _transaction(self) -> SwapTransaction:
        quote = f.quote(output_raw=50_100_000)
        return SwapTransaction(
            provider_id=ProviderId.UNISWAP,
            network_id=f.POLYGON,
            chain_id=137,
            to="0x" + "11" * 20,
            data="0xdeadbeef",
            value=0,
            gas_limit=200_000,
            allowances=(
                AllowanceRequirement(
                    kind=AllowanceKind.ERC20,
                    contract=str(f.USDT.address),
                    spender="0x" + "22" * 20,
                ),
            ),
            quote=quote,
            min_output_raw=50_000_000,
        )

    async def test_successful_call_means_the_swap_would_pass(self) -> None:
        clock = FakeClock(f.NOW)
        account = _account(clock, [_result("0x" + "0" * 64)])

        outcome = await account.simulate(self._transaction())

        assert outcome.succeeded
        assert outcome.revert_reason is None
        assert outcome.describe() == "прошла бы"

    async def test_revert_is_an_answer_not_a_failure(self) -> None:
        """Отказ узла — это «сделка не прошла бы», а не сбой связи.

        Именно так завышенная котировка и должна себя вести: транзакция
        откатывается, теряется газ, деньги остаются.
        """
        clock = FakeClock(f.NOW)
        account = _account(clock, [_error("execution reverted: Too little received")])

        outcome = await account.simulate(self._transaction())

        assert not outcome.succeeded
        assert "Too little received" in (outcome.revert_reason or "")
        assert "откатилась бы" in outcome.describe()

    async def test_nothing_is_sent_to_the_chain(self) -> None:
        """Проверка использует eth_call, а не отправку транзакции."""
        clock = FakeClock(f.NOW)
        http = FakeHttpClient([_result("0x")])
        account = ChainAccount(
            address=ADDRESS,
            http=http,
            resources=resource_manager(clock),
            clock=clock,
            rpc_urls={str(f.POLYGON): "https://polygon-rpc.example"},
        )

        await account.simulate(self._transaction())

        methods = [call.request.json_body["method"] for call in http.calls]
        assert methods == ["eth_call"]
        assert "eth_sendRawTransaction" not in methods


class TestWaiting:
    """Ожидание квитанции обязано завершаться всегда."""

    async def test_wait_returns_none_after_a_bounded_number_of_attempts(self) -> None:
        """Цикл, выходящий по часам, зависит от того, что часы идут."""
        from datetime import timedelta

        from eth_account import Account

        from monik.config.secrets import SecretValue
        from monik.services.trading import TradingWallet, TransactionSender

        clock = FakeClock(f.NOW)  # часы стоят
        wallet = TradingWallet(SecretValue("K", str(Account.create().key.hex())))

        def node(request: object) -> HttpResponse:
            body = getattr(request, "json_body", {}) or {}
            method = body.get("method")
            if method == "eth_gasPrice":
                return _result(hex(30_000_000_000))
            if method == "eth_getTransactionCount":
                return _result("0x1")
            if method == "eth_sendRawTransaction":
                return _result("0x" + "ab" * 32)
            return _result(None)

        http = FakeHttpClient(handler=node)
        account = ChainAccount(
            address=wallet.address,
            http=http,
            resources=resource_manager(clock),
            clock=clock,
            rpc_urls={str(f.POLYGON): "https://rpc.example"},
        )
        sender = TransactionSender(
            wallet=wallet, account=account, clock=clock, chain_ids={str(f.POLYGON): 137}
        )
        sent = await sender.send(f.POLYGON, to="0x" + "11" * 20, data="0x", gas_limit=21_000)

        receipt = await sender.wait(sent, timeout=timedelta(seconds=6), poll=timedelta(seconds=3))

        assert receipt is None, "остановились, хотя часы не шли"


class TestPriority:
    """Приоритет задаёт вызывающая сторона, а не сам счёт.

    Один и тот же счёт обслуживает и покупку, и продажу, а у них
    приоритет разный (``the_main_rules.md``, правило 13). Решить, ради
    чего задан вопрос, может только тот, кто его задаёт.
    """

    class _Recorder:
        """Менеджер ресурсов, запоминающий поданные заявки."""

        def __init__(self, inner: object) -> None:
            self._inner = inner
            self.priorities: list[RequestPriority] = []

        async def execute(self, request: object, operation: object):  # noqa: ANN202
            self.priorities.append(request.priority)  # type: ignore[attr-defined]
            return await self._inner.execute(request, operation)  # type: ignore[attr-defined]

    def _account(self, node: ScriptedNode, clock: FakeClock, recorder: object) -> ChainAccount:
        return ChainAccount(
            address="0x" + "11" * 20,
            http=FakeHttpClient(handler=node),
            resources=recorder,  # type: ignore[arg-type]
            clock=clock,
            rpc_urls={str(f.POLYGON): RPC_URL},
        )

    async def test_the_caller_chooses_the_priority(self) -> None:
        clock = FakeClock(f.NOW)
        recorder = self._Recorder(resource_manager(clock))
        account = self._account(ScriptedNode(), clock, recorder)

        await account.gas_price(f.POLYGON, priority=RequestPriority.ANN_SELL)
        await account.gas_price(f.POLYGON, priority=RequestPriority.ANN_BUY)

        assert recorder.priorities == [RequestPriority.ANN_SELL, RequestPriority.ANN_BUY]

    async def test_unnamed_priority_falls_back_to_the_buy_one(self) -> None:
        """Покупка — не самый высокий приоритет: продажа обгоняет и её."""
        clock = FakeClock(f.NOW)
        recorder = self._Recorder(resource_manager(clock))
        account = self._account(ScriptedNode(), clock, recorder)

        await account.gas_price(f.POLYGON)

        assert recorder.priorities == [RequestPriority.ANN_BUY]
