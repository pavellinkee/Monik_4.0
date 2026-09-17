"""Ведение открытых сделок: ожидание и продажа.

Наблюдатель — вторая половина подсистемы исполнения. Он не ищет
возможностей и не открывает сделок; он доводит до конца уже открытые.
Разделение не формальное: открытие можно запретить, оставив ведение
работать, — иначе выключение торговли бросало бы купленные токены.

Правило выхода задано оператором: продавать, когда круг даёт **чистую
прибыль в базовом токене** не меньше заданной. Не процент — деньги.

Система никогда не закрывает позицию в убыток сама. Если ожидание
затянулось, она **сообщает оператору** и продолжает ждать: решение
принимает человек (``the_main_rules.md``, правило 11).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import RequestId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.chain import ChainAccount
from monik.services.trading.executor import TradeExecutor
from monik.services.trading.ports import PositionStore
from monik.services.trading.sender import SentTransaction, TransactionSender

__all__ = ["LongWaitNotice", "PositionWatcher"]

_LOGGER = get_logger("services.trading.watcher")


class LongWaitNotice:
    """Что сообщить оператору о затянувшейся сделке."""

    __slots__ = ("position", "waited")

    def __init__(self, position: Position, waited: timedelta) -> None:
        self.position = position
        self.waited = waited

    def describe(self) -> str:
        """Текст для оператора."""
        hours = self.waited.total_seconds() / 3600
        return (
            f"{self.position.t_id}: {self.position.input_amount} "
            f"{self.position.base_token.symbol} → {self.position.target_token.symbol} "
            f"ждёт выхода {hours:.1f} ч"
        )


class PositionWatcher:
    """Доводит открытые сделки до конца."""

    def __init__(
        self,
        *,
        adapters: dict[str, AggregatorAdapter],
        positions: PositionStore,
        account: ChainAccount,
        sender: TransactionSender,
        executor: TradeExecutor,
        clock: Clock,
        min_exit_profit_raw: int,
        slippage_bps: int,
        long_wait_enabled: bool,
        long_wait_seconds: int,
        receipt_timeout_seconds: int = 120,
        receipt_poll_seconds: int = 2,
    ) -> None:
        self._adapters = dict(adapters)
        self._positions = positions
        self._account = account
        self._sender = sender
        self._executor = executor
        self._clock = clock
        self._min_exit_profit_raw = min_exit_profit_raw
        self._slippage_bps = slippage_bps
        self._long_wait_enabled = long_wait_enabled
        self._long_wait = timedelta(seconds=long_wait_seconds)
        self._receipt_timeout = timedelta(seconds=receipt_timeout_seconds)
        self._receipt_poll = timedelta(seconds=receipt_poll_seconds)

    async def tick(self) -> tuple[LongWaitNotice, ...]:
        """Один проход по открытым сделкам.

        Возвращает то, о чём стоит сообщить оператору. Сам наблюдатель
        ничего не отправляет: доставка — дело системы уведомлений.
        """
        notices: list[LongWaitNotice] = []
        for position in await self._positions.open_positions():
            try:
                notice = await self._advance(position)
            except Exception as error:  # noqa: BLE001 - сделка не должна ронять остальные
                _LOGGER.error(
                    "position could not be advanced",
                    extra=log_fields(
                        t_id=str(position.t_id), error=type(error).__name__, detail=str(error)
                    ),
                )
                continue
            if notice is not None:
                notices.append(notice)
        return tuple(notices)

    async def consider_now(self, position: Position) -> None:
        """Проверить выход у одной сделки немедленно.

        Вызывается исполнителем сразу после удачной покупки. Отдельный
        вход, а не полный такт: остальные сделки в этот момент трогать
        незачем, и обход всего списка только задержал бы проверку той,
        ради которой он затеян.
        """
        try:
            await self._consider_exit(position)
        except Exception as error:  # noqa: BLE001 - сделка уже открыта, её ведёт наблюдатель
            _LOGGER.error(
                "immediate exit check failed",
                extra=log_fields(
                    t_id=str(position.t_id), error=type(error).__name__, detail=str(error)
                ),
            )

    async def _advance(self, position: Position) -> LongWaitNotice | None:
        if position.status is PositionStatus.BUYING:
            await self._settle_pending_buy(position)
            return None
        if position.status is PositionStatus.SELLING:
            await self._settle_pending_sell(position)
            return None
        return await self._consider_exit(position)

    async def _settle_pending_buy(self, position: Position) -> None:
        """Подобрать покупку, квитанция которой ещё не пришла."""
        if position.buy_tx_hash is None:
            return
        receipt = await self._account.receipt(position.network_id, position.buy_tx_hash)
        if receipt is None:
            return
        await self._executor.settle_buy(position, succeeded=receipt.succeeded, before_raw=0)

    async def _settle_pending_sell(self, position: Position) -> None:
        """Подобрать продажу, квитанция которой ещё не пришла."""
        if position.sell_tx_hash is None:
            return
        receipt = await self._account.receipt(position.network_id, position.sell_tx_hash)
        if receipt is None:
            return
        now = self._clock.now()
        if not receipt.succeeded:
            # Продажа откатилась — токен остался у нас. Возвращаемся к
            # ожиданию: это не потеря, а неудачная попытка выхода.
            await self._positions.update(
                position.model_copy(
                    update={
                        "status": PositionStatus.HOLDING,
                        "sell_tx_hash": None,
                        "updated_at": now,
                    }
                )
            )
            return
        returned = await self._account.token_balance(position.base_token)
        await self._positions.update(
            position.model_copy(
                update={
                    "status": PositionStatus.CLOSED,
                    "raw_returned": returned.raw,
                    "updated_at": now,
                    "closed_at": now,
                }
            )
        )
        _LOGGER.info(
            "trade closed",
            extra=log_fields(t_id=str(position.t_id), tx=position.sell_tx_hash or ""),
        )

    async def _consider_exit(self, position: Position) -> LongWaitNotice | None:
        """Проверить, выгодно ли продавать сейчас."""
        if position.raw_acquired is None:
            return None
        adapter = self._adapters.get(position.sell_provider_id.value)
        if adapter is None:
            return None
        request = QuoteRequest(
            network_id=position.network_id,
            operation=OperationType.SELL,
            input_token=position.target_token,
            output_token=position.base_token,
            input_amount=position.target_token.amount_from_base_units(position.raw_acquired),
            request_id=RequestId.generate(),
            slippage_bps=self._slippage_bps,
        )
        quote = await adapter.get_quote(request)
        profit = position.profit_if_sold_for(quote.output_amount.raw)
        if profit < self._min_exit_profit_raw:
            _LOGGER.info(
                "trade waiting",
                extra=log_fields(
                    t_id=str(position.t_id),
                    profit=str(Decimal(profit).scaleb(-position.base_token.decimals)),
                    needed=str(
                        Decimal(self._min_exit_profit_raw).scaleb(-position.base_token.decimals)
                    ),
                ),
            )
            return self._long_wait_notice(position)
        await self._sell(position, adapter, request)
        return None

    async def _sell(
        self, position: Position, adapter: AggregatorAdapter, request: QuoteRequest
    ) -> None:
        """Собрать, проверить и отправить продажу."""
        transaction = await adapter.build_swap(request)
        simulation = await self._account.simulate(transaction)
        if not simulation.succeeded:
            _LOGGER.warning(
                "exit postponed: the swap would revert",
                extra=log_fields(t_id=str(position.t_id), reason=simulation.revert_reason or ""),
            )
            return
        sent: SentTransaction = await self._sender.send(
            position.network_id,
            to=transaction.to,
            data=transaction.data,
            value=transaction.value,
            gas_limit=transaction.gas_limit,
        )
        await self._positions.update(
            position.model_copy(
                update={
                    "status": PositionStatus.SELLING,
                    "sell_tx_hash": sent.tx_hash,
                    "updated_at": self._clock.now(),
                }
            )
        )
        receipt = await self._sender.wait(
            sent, timeout=self._receipt_timeout, poll=self._receipt_poll
        )
        if receipt is not None:
            await self._settle_pending_sell(
                position.model_copy(
                    update={"status": PositionStatus.SELLING, "sell_tx_hash": sent.tx_hash}
                )
            )

    def _long_wait_notice(self, position: Position) -> LongWaitNotice | None:
        """Сообщить о затянувшемся ожидании — один раз, а не каждый такт."""
        if not self._long_wait_enabled or position.long_wait_notified_at is not None:
            return None
        waited = self._clock.now() - position.opened_at
        if waited < self._long_wait:
            return None
        return LongWaitNotice(position=position, waited=waited)

    async def mark_notified(self, position: Position) -> None:
        """Запомнить, что об этой сделке уже сообщили."""
        await self._positions.update(
            position.model_copy(update={"long_wait_notified_at": self._clock.now()})
        )
