"""Ведение открытой сделки: ожидание и выход.

Правило выхода задано оператором: продавать, когда круг даёт **чистую
прибыль в базовом токене** не меньше заданной. Не процент — деньги.

Система никогда не закрывает позицию в убыток сама: при затянувшемся
ожидании она сообщает оператору и продолжает ждать.
"""

from __future__ import annotations

from datetime import timedelta

from eth_account import Account

from monik.config.secrets import SecretValue
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import TId
from monik.infrastructure.providers.fake import FakeAdapter
from monik.services.observability import FakeClock
from monik.services.trading import PositionWatcher, TradeExecutor, TradingWallet
from tests import factories as f
from tests.unit.trading.support import (
    MemoryPositions,
    MemorySequences,
    ScriptedNode,
    build_account,
    build_sender,
)
from tests.unit.trading.test_executor import _tokens

# Купили AAVE на 50 USDT. Курс адаптера задаётся правилом обмена.
ACQUIRED = 5_000_000_000_000_000_000


def _position(**overrides: object) -> Position:
    base: dict[str, object] = {
        "t_id": TId.from_sequence(1),
        "network_id": f.POLYGON,
        "status": PositionStatus.HOLDING,
        "base_token": f.USDT,
        "target_token": f.AAVE,
        "buy_provider_id": ProviderId.UNISWAP,
        "sell_provider_id": ProviderId.UNISWAP,
        "raw_input": 50_000_000,
        "raw_acquired": ACQUIRED,
        "opened_at": f.NOW,
    }
    base.update(overrides)
    return Position(**base)  # type: ignore[arg-type]


def _watcher(
    node: ScriptedNode,
    positions: MemoryPositions,
    *,
    clock: FakeClock | None = None,
    min_exit_profit_raw: int = 10_000,
    sell_rate: str = "10.00",
    long_wait_enabled: bool = True,
    long_wait_seconds: int = 7_200,
) -> PositionWatcher:
    active = clock or FakeClock(f.NOW)
    wallet = TradingWallet(SecretValue("K", str(Account.create().key.hex())))
    adapter = FakeAdapter(ProviderId.UNISWAP, active, rate=_rate(sell_rate))
    account = build_account(node, active, wallet.address)
    sender = build_sender(node, active, wallet)
    executor = TradeExecutor(
        adapters={ProviderId.UNISWAP.value: adapter},
        positions=positions,
        sequences=MemorySequences(),
        account=account,
        sender=sender,
        tokens=_tokens(),
        clock=active,
        execution_enabled=True,
        slippage_bps=10,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )
    return PositionWatcher(
        adapters={ProviderId.UNISWAP.value: adapter},
        positions=positions,
        account=account,
        sender=sender,
        executor=executor,
        clock=active,
        min_exit_profit_raw=min_exit_profit_raw,
        slippage_bps=10,
        long_wait_enabled=long_wait_enabled,
        long_wait_seconds=long_wait_seconds,
        receipt_timeout_seconds=1,
        receipt_poll_seconds=1,
    )


def _rate(value: str):  # noqa: ANN202
    from decimal import Decimal

    return Decimal(value)


class TestExit:
    async def test_trade_waits_while_profit_is_below_the_target(self) -> None:
        """Ниже цели — не продаём, даже если круг уже в плюсе."""
        node = ScriptedNode()
        positions = MemoryPositions()
        # 5 AAVE по курсу 10.000002 дают 50.00001 USDT: прибыль 0.00001,
        # цель 0.01 — значит ждём.
        watcher = _watcher(node, positions, sell_rate="10.000002")
        await positions.create(_position())

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert not node.sent, "в сеть не ушло ничего"

    async def test_trade_exits_when_the_target_is_reached(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        # 5 AAVE по курсу 10.1 дают 50.5 USDT: прибыль 0.5 — продаём.
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert node.sent, "продажа отправлена"
        assert positions.items["#T1"].status is PositionStatus.CLOSED

    async def test_target_is_money_not_percent(self) -> None:
        """Одна и та же доходность решается по-разному на разных суммах."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions, min_exit_profit_raw=1_000_000, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent, "0.5 USDT меньше цели в 1 USDT — ждём"


class TestFailedExit:
    async def test_reverted_sell_returns_the_trade_to_waiting(self) -> None:
        """Откат продажи — неудачная попытка выхода, а не потеря."""
        node = ScriptedNode(receipt_ok=False)
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert positions.items["#T1"].sell_tx_hash is None

    async def test_sell_that_would_revert_is_not_sent(self) -> None:
        node = ScriptedNode(simulation_ok=False)
        positions = MemoryPositions()
        watcher = _watcher(node, positions, sell_rate="10.1")
        await positions.create(_position())

        await watcher.tick()

        assert not node.sent
        assert positions.items["#T1"].status is PositionStatus.HOLDING


class TestLongWait:
    async def test_notice_appears_only_after_the_configured_time(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="10.000002")
        await positions.create(_position())

        assert await watcher.tick() == ()

        clock.advance(timedelta(seconds=7_201))
        notices = await watcher.tick()

        assert len(notices) == 1
        assert "#T1" in notices[0].describe()

    async def test_the_system_never_closes_at_a_loss_itself(self) -> None:
        """Долгое ожидание сообщается, но позиция остаётся открытой."""
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="9.0")
        await positions.create(_position())
        clock.advance(timedelta(seconds=7_201))

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING
        assert not node.sent

    async def test_notice_is_sent_once_not_every_tick(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(node, positions, clock=clock, sell_rate="10.000002")
        await positions.create(_position())
        clock.advance(timedelta(seconds=7_201))

        first = await watcher.tick()
        await watcher.mark_notified(positions.items["#T1"])
        second = await watcher.tick()

        assert len(first) == 1
        assert second == (), "повторно о той же сделке не сообщаем"

    async def test_disabled_notice_never_fires(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        clock = FakeClock(f.NOW)
        watcher = _watcher(
            node, positions, clock=clock, sell_rate="10.000002", long_wait_enabled=False
        )
        await positions.create(_position())
        clock.advance(timedelta(seconds=100_000))

        assert await watcher.tick() == ()


class TestRecovery:
    async def test_pending_buy_is_picked_up_after_a_restart(self) -> None:
        """Сделка, отправленная до перезапуска, доводится наблюдателем."""
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions)
        await positions.create(
            _position(status=PositionStatus.BUYING, raw_acquired=None, buy_tx_hash="0x" + "cd" * 32)
        )

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.HOLDING

    async def test_pending_sell_is_picked_up_after_a_restart(self) -> None:
        node = ScriptedNode()
        positions = MemoryPositions()
        watcher = _watcher(node, positions)
        await positions.create(
            _position(status=PositionStatus.SELLING, sell_tx_hash="0x" + "ef" * 32)
        )

        await watcher.tick()

        assert positions.items["#T1"].status is PositionStatus.CLOSED
