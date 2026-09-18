"""Отправка транзакций одного счёта.

Единственная точка, из которой состояние цепи меняется. Отправка
последовательна, и это не ограничение реализации, а свойство сети:
транзакции одного адреса нумеруются подряд, и две параллельные отправки
получили бы один номер — вторая вытеснила бы первую.

Поэтому здесь есть замок. Он же удерживает подсистему от соблазна вести
две сделки одновременно: пока одна транзакция не отправлена, вторая ждёт.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from monik.domain.enums.resources import RequestPriority
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields
from monik.services.trading.chain import ChainAccount, TransactionReceipt
from monik.services.trading.wallet import TradingWallet

__all__ = ["SentTransaction", "TransactionSender"]

_LOGGER = get_logger("services.trading.sender")

#: Запас к оценке газа. Оценка делается на текущем состоянии, а исполнение
#: происходит на следующем: между ними состояние пулов меняется, и вызов
#: может потребовать чуть больше.
_GAS_HEADROOM = 1.3


@dataclass(frozen=True, slots=True)
class SentTransaction:
    """Отправленная транзакция."""

    tx_hash: str
    network_id: NetworkId
    gas_limit: int


class TransactionSender:
    """Подписывает и отправляет транзакции, соблюдая порядок номеров."""

    def __init__(
        self,
        *,
        wallet: TradingWallet,
        account: ChainAccount,
        clock: Clock,
        chain_ids: dict[str, int],
        priority_fees_wei: dict[str, int],
    ) -> None:
        self._wallet = wallet
        self._account = account
        self._clock = clock
        self._chain_ids = dict(chain_ids)
        #: Надбавка к базовой цене газа по сетям. Значение принадлежит
        #: сети: в одной за место в блоке идёт торг, в другой его нет
        #: вовсе, и общее значение означало бы либо застрявшие
        #: транзакции, либо многократную переплату.
        self._priority_fees_wei = dict(priority_fees_wei)
        self._lock = asyncio.Lock()

    async def send(
        self,
        network_id: NetworkId,
        *,
        to: str,
        data: str,
        value: int = 0,
        gas_limit: int | None = None,
        priority: RequestPriority = RequestPriority.ANN_BUY,
    ) -> SentTransaction:
        """Отправить вызов и вернуть его хеш.

        ``gas_limit`` можно не задавать: тогда он оценивается узлом. Эта
        же оценка служит проверкой, что вызов вообще выполним, — узел
        отказывает на том, что откатилось бы.

        ``priority`` называет вызывающая сторона: отправка покупки и
        отправка продажи обслуживаются по-разному
        (``the_main_rules.md``, правило 13).
        """
        chain_id = self._chain_ids.get(str(network_id))
        if chain_id is None:
            raise ValueError(f"network {network_id} has no chain id configured")
        async with self._lock:
            limit = gas_limit or await self._account.estimate_gas(
                network_id, to=to, data=data, value=value, priority=priority
            )
            gas = int(limit * _GAS_HEADROOM)
            price = await self._account.gas_price(network_id, priority=priority)
            nonce = await self._account.nonce(network_id, priority=priority)
            tip = self._priority_fees_wei.get(str(network_id), 0)
            raw = self._wallet.sign_transaction(
                {
                    "to": to,
                    "value": value,
                    "data": data,
                    "gas": gas,
                    "maxFeePerGas": price * 2 + tip,
                    "maxPriorityFeePerGas": tip,
                    "nonce": nonce,
                    "chainId": chain_id,
                    "type": 2,
                }
            )
            tx_hash = await self._account.send_raw(network_id, raw, priority=priority)
        _LOGGER.info(
            "transaction sent",
            extra=log_fields(network=str(network_id), tx=tx_hash, gas=gas, nonce=nonce),
        )
        return SentTransaction(tx_hash=tx_hash, network_id=network_id, gas_limit=gas)

    async def wait(
        self,
        sent: SentTransaction,
        *,
        timeout: timedelta,
        poll: timedelta,
        priority: RequestPriority = RequestPriority.ANN_BUY,
    ) -> TransactionReceipt | None:
        """Дождаться квитанции.

        ``None`` означает «ещё не в блоке», а не «не прошла»: транзакция
        может попасть в блок позже, и считать её неудачной нельзя — иначе
        деньги окажутся потрачены, а сделка забыта.

        Число попыток считается заранее, а не сверяется с часами на каждом
        витке. Цикл, который выходит по времени, зависит от того, что
        часы идут, — и останавливается только там, где они идут. Здесь он
        обязан завершаться всегда.
        """
        attempts = max(int(timeout / poll), 1)
        for attempt in range(attempts):
            receipt = await self._account.receipt(sent.network_id, sent.tx_hash, priority=priority)
            if receipt is not None:
                return receipt
            if attempt + 1 < attempts:
                await asyncio.sleep(poll.total_seconds())
        return None
