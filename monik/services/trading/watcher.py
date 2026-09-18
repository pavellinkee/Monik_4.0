"""Ведение открытых сделок: ожидание и продажа.

Наблюдатель — вторая половина подсистемы исполнения. Он не ищет
возможностей и не открывает сделок; он доводит до конца уже открытые.
Разделение не формальное: открытие можно запретить, оставив ведение
работать, — иначе выключение торговли бросало бы купленные токены.

Правило выхода задано оператором: продавать, когда круг даёт **чистую
прибыль в базовом токене** не меньше заданной. Не процент — деньги.

Чистая — значит за вычетом газа. Газ платится в обе стороны и от суммы
сделки не зависит, поэтому на малых суммах он и решает исход: круг,
выигравший на цене меньше, чем стоила пара транзакций, приносит убыток.
Сравнивать с порогом одну лишь разницу токенов означало бы закрывать
сделки в минус, считая их прибыльными.

Порогов два. Сразу после покупки действует основной: шанс выйти в плюс
наивысший именно тогда, и соглашаться на меньшее незачем. Если момент
упущен и сделка ушла в ожидание, действует пониженный порог — деньги
заперты, и выйти из них выгоднее, чем ждать прежней прибыли
(решение оператора).

Система никогда не закрывает позицию в убыток сама. Если ожидание
затянулось, она **сообщает оператору** и продолжает ждать: решение
принимает человек (``the_main_rules.md``, правило 11).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.resources import RequestPriority
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.execution import SwapTransaction
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import RequestId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.chain import ChainAccount
from monik.services.trading.executor import TradeExecutor
from monik.services.trading.ports import ExecutionCosts, GasCalibrationRecorder, PositionStore
from monik.services.trading.sender import SentTransaction, TransactionSender

__all__ = ["LongWaitNotice", "PositionWatcher"]

_LOGGER = get_logger("services.trading.watcher")

#: Приоритет всех обращений продажи — наивысший в системе. За продажей
#: стоят уже потраченные деньги (``the_main_rules.md``, правило 13).
_PRIORITY = RequestPriority.ANN_SELL


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
        costs: ExecutionCosts,
        calibration: GasCalibrationRecorder | None,
        clock: Clock,
        min_exit_profit_raw: int,
        min_exit_profit_waiting_raw: int,
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
        self._costs = costs
        self._calibration = calibration
        self._clock = clock
        self._min_exit_profit_raw = min_exit_profit_raw
        self._min_exit_profit_waiting_raw = min_exit_profit_waiting_raw
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
            await self._consider_exit(position, immediate=True)
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
        return await self._consider_exit(position, immediate=False)

    async def _settle_pending_buy(self, position: Position) -> None:
        """Подобрать покупку, квитанция которой ещё не пришла."""
        if position.buy_tx_hash is None:
            return
        # Подбор покупки — работа покупки, а не продажи: приоритет тот же,
        # с которым эта транзакция отправлялась.
        receipt = await self._account.receipt(
            position.network_id, position.buy_tx_hash, priority=RequestPriority.ANN_BUY
        )
        if receipt is None:
            return
        await self._executor.settle_buy(position, succeeded=receipt.succeeded, receipt=receipt)

    async def _settle_pending_sell(self, position: Position) -> None:
        """Подобрать продажу, квитанция которой ещё не пришла."""
        if position.sell_tx_hash is None:
            return
        receipt = await self._account.receipt(
            position.network_id, position.sell_tx_hash, priority=_PRIORITY
        )
        if receipt is None:
            return
        now = self._clock.now()
        # Откатившаяся попытка тоже стоила газа, и этот расход относится
        # к сделке: считать его нулём значило бы объявить неудачу
        # бесплатной.
        spent_on_selling = _total_wei(position.sell_gas_wei, receipt.gas_cost_wei)
        sold_units = _total_wei(position.sell_gas_units, receipt.gas_used)
        await self._record_calibration(position, receipt.gas_used)
        if not receipt.succeeded:
            # Продажа откатилась — токен остался у нас. Возвращаемся к
            # ожиданию: это не потеря, а неудачная попытка выхода.
            await self._positions.update(
                position.model_copy(
                    update={
                        "status": PositionStatus.HOLDING,
                        "sell_tx_hash": None,
                        "sell_gas_wei": spent_on_selling,
                        "sell_gas_units": sold_units,
                        "updated_at": now,
                    }
                )
            )
            return
        if position.raw_base_before_sell is None:
            # Так может выглядеть только сделка, записанная версией без
            # этого поля: с тех пор его ставит сама отправка продажи.
            # Выручку в этом случае считать не из чего, и придумывать её
            # нельзя — итог объявляется неизвестным, а сделка всё равно
            # закрывается: токены проданы, держать запись открытой не за
            # что.
            _LOGGER.error(
                "trade closed with an unknown result: the balance before the sale was not recorded",
                extra=log_fields(t_id=str(position.t_id), tx=position.sell_tx_hash or ""),
            )
            await self._positions.update(
                position.model_copy(
                    update={
                        "status": PositionStatus.CLOSED,
                        "raw_returned": position.raw_input,
                        "raw_gas_cost": None,
                        "sell_gas_wei": spent_on_selling,
                        "sell_gas_units": sold_units,
                        "updated_at": now,
                        "closed_at": now,
                    }
                )
            )
            return
        after = await self._account.token_balance(position.base_token, priority=_PRIORITY)
        # Выручка — это прирост остатка, а не сам остаток: на счёте лежат
        # и деньги, к этой сделке отношения не имеющие.
        returned = after.raw - position.raw_base_before_sell
        gas_cost = await self._round_trip_cost_raw(
            position, buy_wei=position.buy_gas_wei, sell_wei=spent_on_selling
        )
        closed = position.model_copy(
            update={
                "status": PositionStatus.CLOSED,
                "raw_returned": max(returned, 0),
                "sell_gas_wei": spent_on_selling,
                "sell_gas_units": sold_units,
                "raw_gas_cost": gas_cost,
                "updated_at": now,
                "closed_at": now,
            }
        )
        await self._positions.update(closed)
        _LOGGER.info(
            "trade closed",
            extra=log_fields(
                t_id=str(position.t_id),
                tx=position.sell_tx_hash or "",
                returned=str(
                    Decimal(closed.raw_returned or 0).scaleb(-position.base_token.decimals)
                ),
                gross=_money(closed.gross_result),
                gas=_money(
                    None
                    if gas_cost is None
                    else Decimal(gas_cost).scaleb(-position.base_token.decimals)
                ),
                net=_money(closed.net_result),
            ),
        )

    async def _consider_exit(self, position: Position, *, immediate: bool) -> LongWaitNotice | None:
        """Проверить, выгодно ли продавать сейчас.

        Решение принимается по **реальному предложению** агрегатора, а
        не по котировке: по сумме, ниже которой он сам откатит обмен.
        Агрегатор — обменник, а не биржа, и обязательство у него своё,
        не равное рекламе.

        Порядок шагов выбран так, чтобы не тратить запросов впустую.
        Сначала берётся котировка — она служит дешёвым отсевом, потому
        что обязательство её не превышает: круг, не окупающийся даже по
        обещанию, не окупится и по минимуму. Транзакция собирается
        только у круга, у которого есть шанс.
        """
        if position.raw_acquired is None:
            return None
        adapter = self._adapters.get(position.sell_provider_id.value)
        if adapter is None:
            return None
        needed = self._min_exit_profit_raw if immediate else self._min_exit_profit_waiting_raw
        decimals = position.base_token.decimals
        request = QuoteRequest(
            network_id=position.network_id,
            operation=OperationType.SELL,
            input_token=position.target_token,
            output_token=position.base_token,
            input_amount=position.target_token.amount_from_base_units(position.raw_acquired),
            request_id=RequestId.generate(),
            slippage_bps=self._slippage_bps,
            # Открытая сделка ждать не может: пока котировка выхода стоит
            # в очереди, купленный токен остаётся на руках
            # (``the_main_rules.md``, правило 13).
            priority=_PRIORITY,
        )
        quote = await adapter.get_quote(request)
        # Котировка служит дешёвым отсевом: обязательство агрегатора её не
        # превышает, поэтому круг, не окупающийся даже по обещанию, не
        # окупится и по минимуму — собирать ради него транзакцию незачем.
        gross = position.profit_if_sold_for(quote.output_amount.raw)
        if gross < needed:
            return self._wait(position, profit=gross, needed=needed, costs=None)

        transaction = await adapter.build_swap(request)
        costs = await self._exit_costs_raw(position, transaction)
        if costs is None:
            # Стоимость круга неизвестна. Продавать вслепую нельзя:
            # неизвестный расход нулём не считается (``CLAUDE.md`` §12).
            return self._wait(position, profit=gross, needed=needed, costs=None)
        # Решение принимается по тому, что агрегатор **обязуется** отдать,
        # а не по тому, что обещает. Котировка — реклама, минимум —
        # обязательство: ниже него обмен откатится он сам. Значит
        # исполнение может выйти лучше расчёта, но не хуже.
        profit = position.profit_if_sold_for(transaction.min_output_raw, raw_costs=costs)
        if profit < needed:
            return self._wait(position, profit=profit, needed=needed, costs=costs)
        _LOGGER.info(
            "exit offer verified",
            extra=log_fields(
                t_id=str(position.t_id),
                promised=str(Decimal(quote.output_amount.raw).scaleb(-decimals)),
                guaranteed=str(Decimal(transaction.min_output_raw).scaleb(-decimals)),
                profit=str(Decimal(profit).scaleb(-decimals)),
                needed=str(Decimal(needed).scaleb(-decimals)),
                gas=str(Decimal(costs).scaleb(-decimals)),
            ),
        )
        await self._sell(
            position.model_copy(update={"sell_quoted_gas_units": quote.estimated_gas_units}),
            transaction,
        )
        return None

    async def _exit_costs_raw(self, position: Position, transaction: SwapTransaction) -> int | None:
        """Во что обойдётся круг целиком, в базовом токене.

        Считается всё, что сделка стоит и будет стоить: покупка, уже
        потраченное на неудачные попытки выхода и предстоящая продажа.
        Предел газа берётся у агрегатора — он же поедет в транзакцию,
        поэтому отдельная оценка узлом не нужна.
        """
        price = await self._account.gas_price(position.network_id, priority=_PRIORITY)
        total_wei = _total_wei(
            position.buy_gas_wei, position.sell_gas_wei, transaction.gas_limit * price
        )
        if total_wei is None:
            _LOGGER.warning(
                "exit postponed: the gas already spent is unknown",
                extra=log_fields(t_id=str(position.t_id)),
            )
            return None
        return await self._costs.to_base_raw(
            position.network_id, position.base_token, wei=total_wei
        )

    async def _record_calibration(self, position: Position, actual_units: int) -> None:
        """Передать замер расхода газа продажи.

        Нога продажи расходится с оценкой сильнее покупки: её маршрут чаще
        разбивается на несколько пулов. Именно поэтому замер по ней важнее
        всего, и он же достаётся бесплатно — из уже полученной квитанции.
        """
        if self._calibration is None or position.sell_quoted_gas_units is None:
            return
        await self._calibration.record(
            position.network_id,
            position.sell_provider_id,
            quoted_units=position.sell_quoted_gas_units,
            actual_units=actual_units,
        )

    async def _round_trip_cost_raw(
        self, position: Position, *, buy_wei: int | None, sell_wei: int | None
    ) -> int | None:
        """Фактическая стоимость круга в базовом токене."""
        total_wei = _total_wei(buy_wei, sell_wei)
        if total_wei is None:
            return None
        return await self._costs.to_base_raw(
            position.network_id, position.base_token, wei=total_wei
        )

    def _wait(
        self, position: Position, *, profit: int, needed: int, costs: int | None
    ) -> LongWaitNotice | None:
        """Отложить выход и при необходимости сообщить оператору."""
        decimals = position.base_token.decimals
        _LOGGER.info(
            "trade waiting",
            extra=log_fields(
                t_id=str(position.t_id),
                profit=str(Decimal(profit).scaleb(-decimals)),
                needed=str(Decimal(needed).scaleb(-decimals)),
                gas=_money(None if costs is None else Decimal(costs).scaleb(-decimals)),
            ),
        )
        return self._long_wait_notice(position)

    async def _sell(self, position: Position, transaction: SwapTransaction) -> None:
        """Проверить и отправить продажу.

        Транзакция приходит уже собранной: её собрали, чтобы узнать
        стоимость выхода, и собирать второй раз значило бы потратить
        лишний запрос на то же самое.
        """
        simulation = await self._account.simulate(transaction, priority=_PRIORITY)
        if not simulation.succeeded:
            _LOGGER.warning(
                "exit postponed: the swap would revert",
                extra=log_fields(t_id=str(position.t_id), reason=simulation.revert_reason or ""),
            )
            return
        # Остаток до продажи запоминается перед отправкой: выручка —
        # это прирост, и прирост должен быть досчитан даже если квитанцию
        # подберёт уже другой процесс.
        before = await self._account.token_balance(position.base_token, priority=_PRIORITY)
        sent: SentTransaction = await self._sender.send(
            position.network_id,
            to=transaction.to,
            data=transaction.data,
            value=transaction.value,
            gas_limit=transaction.gas_limit,
            priority=_PRIORITY,
        )
        selling = position.model_copy(
            update={
                "status": PositionStatus.SELLING,
                "sell_tx_hash": sent.tx_hash,
                "raw_base_before_sell": before.raw,
                "updated_at": self._clock.now(),
            }
        )
        await self._positions.update(selling)
        receipt = await self._sender.wait(
            sent, timeout=self._receipt_timeout, poll=self._receipt_poll, priority=_PRIORITY
        )
        if receipt is not None:
            await self._settle_pending_sell(selling)

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


def _total_wei(*parts: int | None) -> int | None:
    """Сумма расходов в wei.

    Неизвестное слагаемое делает неизвестной всю сумму: подставить вместо
    него ноль означало бы объявить часть расхода бесплатной
    (``CLAUDE.md`` §12).
    """
    total = 0
    for part in parts:
        if part is None:
            return None
        total += part
    return total


def _money(value: Decimal | None) -> str:
    """Денежная величина для журнала. Неизвестное так и называется."""
    return "неизвестно" if value is None else str(value)
