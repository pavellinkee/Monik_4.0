"""Открытие сделки: три проверки, каждая из которых отменяет её целиком.

Найденная возможность сама по себе не является разрешением тратить
деньги (``the_main_rules.md``, правило 11, сохраняющее суть §56).
"""

from __future__ import annotations

from decimal import Decimal

from eth_account import Account

from monik.config import parse_configuration
from monik.config.secrets import SecretValue
from monik.domain.enums.lifecycle import ScanStatus
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.operations import OperationType
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.opportunity import Candidate
from monik.domain.models.scan import Scan, ScanScope
from monik.domain.value_objects.identifiers import ScanId
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.level1.results import ScanResult
from monik.services.observability import FakeClock
from monik.services.registries import TokenRegistry
from monik.services.trading import TradeExecutor, TradingWallet
from tests import factories as f
from tests.component.level1.conftest import level1_document
from tests.unit.config.conftest import VALID_ENV
from tests.unit.trading.support import (
    MemoryPositions,
    MemorySequences,
    ScriptedNode,
    build_account,
    build_sender,
)

USDT_ADDRESS = str(f.USDT.address).lower()


def _tokens() -> TokenRegistry:
    """Реестр из двух токенов Polygon: базовый и промежуточный."""
    document = level1_document()
    configuration = parse_configuration(document, environ=dict(VALID_ENV)).config
    return TokenRegistry(configuration)


def _wallet() -> TradingWallet:
    return TradingWallet(SecretValue("K", str(Account.create().key.hex())))


def _candidate(raw_input: int, net_profit: str) -> Candidate:
    buy = f.quote(
        operation=OperationType.BUY,
        provider_id=ProviderId.UNISWAP,
        input_token=f.USDT,
        output_token=f.AAVE,
        input_raw=raw_input,
    )
    sell = f.quote(
        operation=OperationType.SELL,
        provider_id=ProviderId.UNISWAP,
        input_token=f.AAVE,
        output_token=f.USDT,
        input_raw=buy.output_amount.raw,
        output_raw=raw_input + 100_000,
    )
    result = f.profit_result(input_raw=raw_input, output_raw=raw_input + 100_000)
    return Candidate(
        scan_id=ScanId.generate(),
        buy_quote=buy,
        sell_quote=sell,
        preliminary_result=result.model_copy(update={"net_profit": Decimal(net_profit)}),
        detected_at=f.NOW,
    )


def _result(*candidates: Candidate) -> ScanResult:
    scope = ScanScope(
        mode=ScanMode.ANN,
        networks=(f.POLYGON,),
        providers=(ProviderId.UNISWAP,),
        tokens=(f.AAVE.key,),
        raw_amounts=(50_000_000,),
    )
    scan = Scan(
        scan_id=ScanId.generate(),
        status=ScanStatus.COMPLETE,
        scope=scope,
        started_at=f.NOW,
        finished_at=f.NOW,
    )
    return ScanResult(scan=scan, qualified=candidates)


def _executor(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    execution_enabled: bool = True,
) -> TradeExecutor:
    clock = FakeClock(f.NOW)
    wallet = _wallet()
    return TradeExecutor(
        adapters={ProviderId.UNISWAP.value: FakeAdapter(ProviderId.UNISWAP, clock)},
        positions=positions,
        sequences=MemorySequences(),
        account=build_account(node, clock, wallet.address),
        sender=build_sender(node, clock, wallet),
        tokens=_tokens(),
        clock=clock,
        execution_enabled=execution_enabled,
        slippage_bps=10,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )


class TestChoice:
    async def test_the_most_profitable_affordable_amount_wins(self) -> None:
        """Выбор идёт по заработку, но только среди посильных сумм."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()
        executor = _executor(node, positions)

        await executor.consider(
            (_result(_candidate(100_000_000, "0.20"), _candidate(50_000_000, "0.10")),)
        )

        assert len(positions.items) == 1
        opened = next(iter(positions.items.values()))
        assert opened.raw_input == 50_000_000, "сто не помещается в остаток, берём пятьдесят"

    async def test_nothing_happens_when_no_amount_fits(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 1_000_000})
        positions = MemoryPositions()

        executor = _executor(node, positions)

        assert await executor.consider((_result(_candidate(50_000_000, "0.1")),)) is None
        assert not positions.items
        assert not node.sent

    async def test_money_promised_to_open_trades_is_not_counted_twice(self) -> None:
        """Остаток на счёте включает деньги уже открытых сделок."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()
        executor = _executor(node, positions)

        await executor.consider((_result(_candidate(50_000_000, "0.1")),))
        await executor.consider((_result(_candidate(50_000_000, "0.1")),))

        assert len(positions.items) == 1, "вторая сделка не открывается: деньги заняты"


class TestGuards:
    async def test_disabled_execution_sends_nothing(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        result = await _executor(node, positions, execution_enabled=False).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert result is None
        assert not positions.items
        assert not node.sent, "в сеть не ушло ничего"

    async def test_a_swap_that_would_revert_is_not_sent(self) -> None:
        """Симуляция — защита от завышенной котировки."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, simulation_ok=False)
        positions = MemoryPositions()

        result = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert result is None
        assert not positions.items
        assert not node.sent


class TestRecording:
    async def test_position_is_written_before_the_transaction_goes_out(self) -> None:
        """Иначе перезапуск между отправкой и записью потерял бы токены."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        await _executor(node, positions).consider((_result(_candidate(50_000_000, "0.1")),))

        assert positions.created_before_send, "запись сделана"
        assert node.sent, "и только потом отправлено"

    async def test_successful_buy_moves_the_trade_to_holding(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000})
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.HOLDING
        assert position.buy_tx_hash is not None

    async def test_reverted_buy_closes_the_trade_as_failed(self) -> None:
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, receipt_ok=False)
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.FAILED
        assert not position.status.is_open

    async def test_missing_receipt_leaves_the_trade_open(self) -> None:
        """Транзакция может попасть в блок позже; забывать её нельзя."""
        node = ScriptedNode(balances={USDT_ADDRESS: 60_000_000}, receipt_ok=None)
        positions = MemoryPositions()

        position = await _executor(node, positions).consider(
            (_result(_candidate(50_000_000, "0.1")),)
        )

        assert position is not None
        assert position.status is PositionStatus.BUYING
