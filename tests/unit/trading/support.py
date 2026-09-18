"""Окружение для тестов подсистемы исполнения.

Все внешние зависимости — **test implementations** (``CLAUDE.md`` §10):
узел сети отвечает по сценарию, хранилище держит сделки в памяти,
транзакции никуда не уходят.
"""

from __future__ import annotations

from typing import Any

from monik.domain.enums.providers import ProviderId
from monik.domain.models.position import Position
from monik.domain.models.token import Token
from monik.domain.value_objects.identity import NetworkId
from monik.infrastructure.http import FakeHttpClient, HttpRequest, HttpResponse
from monik.services.observability import FakeClock
from monik.services.trading import ChainAccount, TradingWallet, TransactionSender
from tests import factories as f
from tests.unit.providers.support import resource_manager

RPC_URL = "https://rpc.example"


class MemoryPositions:
    """Хранилище сделок в памяти."""

    def __init__(self) -> None:
        self.items: dict[str, Position] = {}
        self.order: list[str] = []
        self.created_before_send: list[str] = []

    async def create(self, position: Position) -> None:
        key = str(position.t_id)
        self.items[key] = position
        self.order.append(key)
        self.created_before_send.append(key)

    async def update(self, position: Position) -> None:
        self.items[str(position.t_id)] = position

    async def open_positions(self) -> tuple[Position, ...]:
        return tuple(self.items[key] for key in self.order if self.items[key].status.is_open)

    async def reserved_raw_input(self, network_id: NetworkId) -> int:
        return sum(
            item.raw_input
            for item in self.items.values()
            if item.network_id == network_id and item.status.is_open
        )


class FixedCosts:
    """Стоимость исполнения, заданная заранее.

    **Test implementation** (``CLAUDE.md`` §10). Настоящий пересчёт газа
    в базовый токен требует курса native token и проверяется отдельно;
    здесь важно только само влияние расхода на решение о выходе, поэтому
    величина задаётся числом.

    ``raw=None`` означает «посчитать не удалось» — случай, в котором
    продавать нельзя.
    """

    def __init__(self, raw: int | None = 0) -> None:
        self.raw = raw
        self.calls: list[int] = []

    async def to_base_raw(
        self, network_id: NetworkId, base_token: Token, *, wei: int
    ) -> int | None:
        self.calls.append(wei)
        return self.raw


class MemoryCalibration:
    """Приёмник замеров расхода газа, копящий их в памяти.

    **Test implementation** (``CLAUDE.md`` §10).
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, int, int]] = []

    async def record(
        self,
        network_id: NetworkId,
        provider_id: ProviderId,
        *,
        quoted_units: int,
        actual_units: int,
    ) -> None:
        self.records.append((str(network_id), provider_id.value, quoted_units, actual_units))


class MemorySequences:
    """Монотонные номера в памяти."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    async def next_value(self, name: str) -> int:
        self._values[name] = self._values.get(name, 0) + 1
        return self._values[name]


class ScriptedNode:
    """Узел сети, отвечающий по методу вызова.

    Балансы задаются заранее; ``eth_call`` различается по селектору:
    ``0x70a08231`` — остаток, всё остальное — симуляция сделки.
    """

    def __init__(
        self,
        *,
        balances: dict[str, int] | None = None,
        after_send: dict[str, int] | None = None,
        simulation_ok: bool = True,
        receipt_ok: bool | None = True,
        effective_gas_price: int | None = 30_000_000_000,
    ) -> None:
        self.balances = dict(balances or {})
        #: Остатки, которые узел начинает показывать **после** первой
        #: отправки. Так стенд повторяет главное свойство цепи: покупка
        #: меняет баланс, и полученное количество узнаётся из него, а не
        #: из котировки.
        self.after_send = dict(after_send or {})
        self.simulation_ok = simulation_ok
        self.receipt_ok = receipt_ok
        self.effective_gas_price = effective_gas_price
        self.sent: list[str] = []
        #: Надбавка последней отправленной транзакции, в wei.
        self.last_tip: int | None = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        body: dict[str, Any] = request.json_body or {}
        method = body.get("method")
        params = body.get("params") or []
        if method == "eth_call":
            data = str(params[0].get("data", ""))
            if data.startswith("0x70a08231"):
                token = str(params[0].get("to", "")).lower()
                if self.sent and token in self.after_send:
                    return _ok(hex(self.after_send[token]))
                return _ok(hex(self.balances.get(token, 0)))
            if self.simulation_ok:
                return _ok("0x")
            return _err("execution reverted: тестовый откат")
        if method == "eth_getBalance":
            return _ok(hex(10**18))
        if method == "eth_getTransactionCount":
            return _ok("0x1")
        if method == "eth_gasPrice":
            return _ok(hex(30_000_000_000))
        if method == "eth_estimateGas":
            return _ok(hex(200_000))
        if method == "eth_sendRawTransaction":
            raw = str(params[0])
            self.sent.append(raw[:20])
            # Надбавку узел видит только из самой подписанной транзакции,
            # поэтому стенд её оттуда и достаёт: иначе проверить, какое
            # значение ушло в сеть, было бы нечем.
            self.last_tip = _decoded_tip(raw)
            return _ok("0x" + "ab" * 32)
        if method == "eth_getTransactionReceipt":
            if self.receipt_ok is None:
                return _ok(None)
            receipt = {
                "status": "0x1" if self.receipt_ok else "0x0",
                "gasUsed": hex(180_000),
                "blockNumber": "0x64",
            }
            # Цена приходит той же квитанцией, поэтому фактическая
            # стоимость транзакции известна без единого лишнего запроса.
            # Узел, который поля не присылает, тоже нужен: стоимость
            # тогда неизвестна, а не равна нулю.
            if self.effective_gas_price is not None:
                receipt["effectiveGasPrice"] = hex(self.effective_gas_price)
            return _ok(receipt)
        raise AssertionError(f"неожиданный вызов узла: {method}")


def _decoded_tip(raw_transaction: str) -> int:
    """``maxPriorityFeePerGas`` из подписанной транзакции типа 2."""
    import rlp  # type: ignore[import-untyped]

    payload = bytes.fromhex(raw_transaction.removeprefix("0x"))
    fields = rlp.decode(payload[1:])
    return int.from_bytes(fields[2], "big") if fields[2] else 0


def _ok(result: Any) -> HttpResponse:
    import json

    return HttpResponse(status_code=200, text=json.dumps({"result": result}))


def _err(message: str) -> HttpResponse:
    import json

    return HttpResponse(status_code=200, text=json.dumps({"error": {"message": message}}))


def build_account(node: ScriptedNode, clock: FakeClock, address: str) -> ChainAccount:
    return ChainAccount(
        address=address,
        http=FakeHttpClient(handler=node),
        resources=resource_manager(clock),
        clock=clock,
        rpc_urls={str(f.POLYGON): (RPC_URL,)},
    )


def build_sender(node: ScriptedNode, clock: FakeClock, wallet: TradingWallet) -> TransactionSender:
    return TransactionSender(
        wallet=wallet,
        account=build_account(node, clock, wallet.address),
        clock=clock,
        chain_ids={str(f.POLYGON): 137},
        # Надбавка — свойство сети; в стенде она заметная, чтобы её
        # попадание в подписанную транзакцию было проверяемо.
        priority_fees_wei={str(f.POLYGON): 30_000_000_000},
    )
