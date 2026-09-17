"""Открытие сделки режима ``ann``.

Подсистема отделена от сканера намеренно (``the_main_rules.md``,
правило 11, сохраняющее суть §56): найденная возможность сама по себе не
является разрешением тратить деньги. Прежде чем отправить покупку,
исполнитель проверяет три вещи, и любая из них отменяет сделку целиком:

1. разрешена ли торговля вообще;
2. хватает ли остатка с учётом уже занятого открытыми сделками;
3. прошла бы транзакция, если отправить её прямо сейчас.

Третья проверка — симуляция — и есть защита от завышенной котировки:
сделка, которая не прошла бы, не отправляется вовсе.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from monik.domain.enums.operations import OperationType
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.opportunity import Candidate
from monik.domain.models.position import Position
from monik.domain.models.token import Token
from monik.domain.value_objects.identifiers import RequestId, TId
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.providers.contract import AggregatorAdapter, QuoteRequest
from monik.services.level1.results import ScanResult
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.registries.tokens import TokenRegistry
from monik.services.trading.chain import ChainAccount, TransactionReceipt
from monik.services.trading.ports import PositionStore, SequenceSource
from monik.services.trading.sender import TransactionSender

__all__ = ["TradeExecutor"]

_LOGGER = get_logger("services.trading.executor")

#: Последовательность сделок.
POSITION_SEQUENCE = "position"


@dataclass(frozen=True, slots=True)
class _Choice:
    """Кандидат, который подсистема готова исполнить."""

    candidate: Candidate
    target: Token
    base: Token


class TradeExecutor:
    """Превращает находку режима ``ann`` в открытую сделку."""

    def __init__(
        self,
        *,
        adapters: dict[str, AggregatorAdapter],
        positions: PositionStore,
        sequences: SequenceSource,
        account: ChainAccount,
        sender: TransactionSender,
        tokens: TokenRegistry,
        clock: Clock,
        is_execution_open: Callable[[], bool],
        slippage_bps: int,
        receipt_timeout_seconds: int = 120,
        receipt_poll_seconds: int = 2,
    ) -> None:
        self._adapters = dict(adapters)
        self._positions = positions
        self._sequences = sequences
        self._account = account
        self._sender = sender
        self._tokens = tokens
        self._clock = clock
        self._is_execution_open = is_execution_open
        self._slippage_bps = slippage_bps
        self._receipt_timeout = timedelta(seconds=receipt_timeout_seconds)
        self._receipt_poll = timedelta(seconds=receipt_poll_seconds)
        #: Немедленная проверка выхода после удачной покупки. Ставится
        #: снаружи, потому что решение о продаже принимает наблюдатель, а
        #: он сам зависит от исполнителя.
        self._check_exit: Callable[[Position], Awaitable[None]] | None = None

    def set_exit_check(self, check: Callable[[Position], Awaitable[None]]) -> None:
        """Задать проверку выхода, выполняемую сразу после покупки.

        Правило оператора: «вначале производится покупка, затем **ещё
        раз** производится проверка, выгодно ли совершить продажу; если
        результат не положительный — сделка остаётся в ожидании».
        Немедленная проверка не заменяется периодической: именно сразу
        после покупки шанс выйти в плюс наивысший — расхождение, ради
        которого сделка открылась, ещё живо, а живёт оно минуты.
        """
        self._check_exit = check

    async def consider(self, results: tuple[ScanResult, ...]) -> Position | None:
        """Рассмотреть находки прохода и, если можно, открыть сделку.

        За один проход открывается **одна** сделка: транзакции счёта идут
        по очереди, и вторая покупка всё равно ждала бы первую, а цена за
        это время уже другая.
        """
        choice = await self._choose(results)
        if choice is None:
            return None
        if not self._is_execution_open():
            _LOGGER.info(
                "trade withheld: execution is disabled",
                extra=self._describe(choice),
            )
            return None
        return await self._open(choice)

    # --- выбор ------------------------------------------------------------

    async def _choose(self, results: tuple[ScanResult, ...]) -> _Choice | None:
        """Лучшая находка, которую позволяет остаток счёта.

        Кандидаты уже отсортированы по заработку. Из них берётся первый,
        чья сумма помещается в свободный остаток: правило «не хватает
        средств — сделка не производится» относится к набору целиком, а не
        к каждой сумме по отдельности.
        """
        available: dict[NetworkId, int] = {}
        for result in results:
            network_id = result.scan.scope.networks[0]
            if network_id not in available:
                available[network_id] = await self._available(network_id)
            for candidate in result.qualified:
                choice = self._as_choice(candidate, network_id)
                if choice is None:
                    continue
                if candidate.buy_quote.input_amount.raw <= available[network_id]:
                    return choice
                _LOGGER.info(
                    "trade skipped: balance does not cover the amount",
                    extra=log_fields(
                        network=str(network_id),
                        amount=str(candidate.buy_quote.input_amount.as_decimal),
                        available=str(Decimal(available[network_id])),
                    ),
                )
        return None

    def _as_choice(self, candidate: Candidate, network_id: NetworkId) -> _Choice | None:
        base = self._tokens.get(candidate.buy_quote.input_token)
        target = self._tokens.get(candidate.buy_quote.output_token)
        if base is None or target is None or base.key == target.key:
            return None
        if base.network_id != network_id:
            return None
        return _Choice(candidate=candidate, target=target, base=base)

    async def _available(self, network_id: NetworkId) -> int:
        """Свободный остаток базового токена сети.

        Из остатка вычитается то, что уже обещано открытым сделкам: иначе
        одни и те же деньги были бы вложены дважды.
        """
        base = self._tokens.base_token(network_id)
        balance = await self._account.token_balance(base)
        reserved = await self._positions.reserved_raw_input(network_id)
        return max(balance.raw - reserved, 0)

    # --- открытие ---------------------------------------------------------

    async def _open(self, choice: _Choice) -> Position | None:
        """Собрать, проверить и отправить покупку."""
        candidate = choice.candidate
        network_id = choice.base.network_id
        adapter = self._adapters.get(candidate.buy_quote.provider_id.value)
        if adapter is None:
            return None
        transaction = await adapter.build_swap(
            QuoteRequest(
                network_id=network_id,
                operation=OperationType.BUY,
                input_token=choice.base,
                output_token=choice.target,
                input_amount=candidate.buy_quote.input_amount,
                request_id=RequestId.generate(),
                slippage_bps=self._slippage_bps,
            )
        )
        simulation = await self._account.simulate(transaction)
        if not simulation.succeeded:
            _LOGGER.warning(
                "trade cancelled: the swap would revert",
                extra=self._describe(choice) | {"reason": simulation.revert_reason or ""},
            )
            return None

        before = await self._account.token_balance(choice.target)
        sequence = await self._sequences.next_value(POSITION_SEQUENCE)
        now = self._clock.now()
        position = Position(
            t_id=TId.from_sequence(sequence),
            network_id=network_id,
            status=PositionStatus.BUYING,
            base_token=choice.base,
            target_token=choice.target,
            buy_provider_id=candidate.buy_quote.provider_id,
            sell_provider_id=candidate.sell_quote.provider_id,
            raw_input=candidate.buy_quote.input_amount.raw,
            # Остаток до покупки запоминается сразу: полученное считается
            # разностью, а досчитать её может уже другой процесс, если
            # квитанция придёт после перезапуска.
            raw_target_before_buy=before.raw,
            # На продажу пока не потрачено ничего, и это знание, а не
            # пробел: пустое поле означало бы «неизвестно», и стоимость
            # круга не сошлась бы вовсе.
            sell_gas_wei=0,
            opened_at=now,
        )
        # Запись делается ДО отправки: иначе перезапуск между отправкой и
        # записью оставил бы токены, о которых система не знает.
        await self._positions.create(position)

        sent = await self._sender.send(
            network_id,
            to=transaction.to,
            data=transaction.data,
            value=transaction.value,
            gas_limit=transaction.gas_limit,
        )
        position = position.model_copy(
            update={"buy_tx_hash": sent.tx_hash, "updated_at": self._clock.now()}
        )
        await self._positions.update(position)
        _LOGGER.info(
            "trade opened",
            extra=self._describe(choice) | {"t_id": str(position.t_id), "tx": sent.tx_hash},
        )

        receipt = await self._sender.wait(
            sent, timeout=self._receipt_timeout, poll=self._receipt_poll
        )
        if receipt is None:
            # Квитанции ещё нет — сделка остаётся в BUYING, и наблюдатель
            # доведёт её. Считать её неудачной нельзя: транзакция может
            # попасть в блок позже.
            return position
        settled = await self.settle_buy(position, succeeded=receipt.succeeded, receipt=receipt)
        if settled.status is PositionStatus.HOLDING and self._check_exit is not None:
            # Не ждём следующего такта расписания: между покупкой и первой
            # проверкой прошло бы до десяти секунд, а отклонение столько
            # может и не прожить.
            await self._check_exit(settled)
        return settled

    async def settle_buy(
        self, position: Position, *, succeeded: bool, receipt: TransactionReceipt | None = None
    ) -> Position:
        """Зафиксировать итог покупки.

        Полученное количество берётся **с самого счёта**, а не из
        котировки: исполнение могло дать меньше обещанного, и дальнейшие
        расчёты должны опираться на факт. Считается при этом **прирост**
        остатка, а не сам остаток: на счёте может лежать тот же токен от
        прошлой сделки, и он к этой отношения не имеет.

        Стоимость газа берётся из той же квитанции — расход и цена
        приходят вместе с ней, поэтому знание о расходе не стоит ни
        одного дополнительного запроса.
        """
        now = self._clock.now()
        gas_wei = None if receipt is None else receipt.gas_cost_wei
        if not succeeded:
            updated = position.model_copy(
                update={
                    "status": PositionStatus.FAILED,
                    "updated_at": now,
                    "closed_at": now,
                    "raw_returned": position.raw_input,
                    "buy_gas_wei": gas_wei,
                }
            )
            await self._positions.update(updated)
            _LOGGER.warning(
                "trade failed: the buy transaction reverted",
                extra=log_fields(t_id=str(position.t_id), tx=position.buy_tx_hash or ""),
            )
            return updated
        after = await self._account.token_balance(position.target_token)
        acquired = after.raw - (position.raw_target_before_buy or 0)
        updated = position.model_copy(
            update={
                "status": PositionStatus.HOLDING,
                "raw_acquired": max(acquired, 1),
                "buy_gas_wei": gas_wei,
                "updated_at": now,
            }
        )
        await self._positions.update(updated)
        _LOGGER.info(
            "trade holding",
            extra=log_fields(
                t_id=str(position.t_id),
                token=str(position.target_token.symbol),
                acquired=str(Decimal(acquired).scaleb(-position.target_token.decimals)),
            ),
        )
        return updated

    def _describe(self, choice: _Choice) -> dict[str, object]:
        result = choice.candidate.preliminary_result
        return log_fields(
            network=str(choice.base.network_id),
            token=str(choice.target.symbol),
            amount=str(choice.candidate.buy_quote.input_amount.as_decimal),
            route=(
                f"{choice.candidate.buy_quote.provider_id.value}->"
                f"{choice.candidate.sell_quote.provider_id.value}"
            ),
            net_profit=str(result.net_profit) if result.net_profit is not None else "",
        )
