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

from monik.domain.errors import DataError
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpResponse
from monik.services.observability import FakeClock
from monik.services.trading import ChainAccount
from monik.services.trading.chain import UNLIMITED_ALLOWANCE
from tests import factories as f
from tests.unit.providers.support import resource_manager

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
