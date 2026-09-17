"""Окружение для тестов подсистемы исполнения.

Все внешние зависимости — **test implementations** (``CLAUDE.md`` §10):
узел сети отвечает по сценарию, хранилище держит сделки в памяти,
транзакции никуда не уходят.
"""

from __future__ import annotations

from typing import Any

from monik.domain.models.position import Position
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
        simulation_ok: bool = True,
        receipt_ok: bool | None = True,
    ) -> None:
        self.balances = dict(balances or {})
        self.simulation_ok = simulation_ok
        self.receipt_ok = receipt_ok
        self.sent: list[str] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        body: dict[str, Any] = request.json_body or {}
        method = body.get("method")
        params = body.get("params") or []
        if method == "eth_call":
            data = str(params[0].get("data", ""))
            if data.startswith("0x70a08231"):
                token = str(params[0].get("to", "")).lower()
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
            self.sent.append(str(params[0])[:20])
            return _ok("0x" + "ab" * 32)
        if method == "eth_getTransactionReceipt":
            if self.receipt_ok is None:
                return _ok(None)
            return _ok(
                {
                    "status": "0x1" if self.receipt_ok else "0x0",
                    "gasUsed": hex(180_000),
                    "blockNumber": "0x64",
                }
            )
        raise AssertionError(f"неожиданный вызов узла: {method}")


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
        rpc_urls={str(f.POLYGON): RPC_URL},
    )


def build_sender(node: ScriptedNode, clock: FakeClock, wallet: TradingWallet) -> TransactionSender:
    return TransactionSender(
        wallet=wallet,
        account=build_account(node, clock, wallet.address),
        clock=clock,
        chain_ids={str(f.POLYGON): 137},
    )
