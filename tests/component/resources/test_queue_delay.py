"""Пауза между запросами внутри одной очереди агрегатора.

Требование: между двумя запросами **одной** очереди выдерживается
минимальная пауза. Ограничение поочерёдное, а не общее: очереди
агрегаторов независимы (``05_RESOURCE_MANAGER.md`` §48), поэтому запрос
одного провайдера не задерживает другого.

Время моделируется: ждать настоящие паузы тест не может, а уменьшать
нагрузку нельзя — проверяется именно она.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import timedelta

import pytest

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import RequestPriority
from monik.domain.errors import ProviderError
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.value_objects.identifiers import RequestId
from monik.services.resources import ResourceLimits, ResourceManager
from tests import factories as f
from tests.unit.resources.conftest import resource_config

from .conftest import VirtualTime

#: Пауза, требуемая внутри одной очереди.
DELAY = 0.1

#: Частота, при которой пауза является определяющим ограничением.
#: При настроенных 4.9-5.9 запроса в секунду шаг корзины и так больше
#: паузы, поэтому для проверки самой паузы нужна частота повыше.
FAST_RATE = 50.0

#: Ресурс вне очередей агрегаторов: Telegram лимитов не регистрирует.
TELEGRAM_RESOURCE = "telegram"


def _manager(time_machine: VirtualTime, *, rate: float = FAST_RATE) -> ResourceManager:
    """Resource Manager с тремя очередями агрегаторов и паузой."""
    manager = ResourceManager(
        resource_config(global_max_concurrent_requests=16, queue_capacity=4096),
        time_machine,
        sleeper=time_machine.sleep,
    )
    for provider in (ProviderId.ZERO_X, ProviderId.VELORA, ProviderId.UNISWAP):
        manager.register_limits(
            ResourceKey(provider_id=provider),
            ResourceLimits(
                max_concurrent=4,
                requests_per_second=rate,
                burst=1,
                min_interval_seconds=DELAY,
            ),
        )
    return manager


def _request(
    provider: str,
    *,
    operation: CapabilityOperation = CapabilityOperation.QUOTE_BUY,
    priority: RequestPriority = RequestPriority.UR_LEVEL1_BUY,
    sequence: int = 0,
    priority_at: object = None,
) -> ResourceRequest:
    return ResourceRequest(
        request_id=RequestId.generate(),
        key=ResourceKey(provider_id=provider, network_id=f.POLYGON, operation=operation),
        priority=priority,
        timeout=timedelta(seconds=30),
        created_at=f.NOW,
        sequence=sequence,
        priority_at=priority_at,  # type: ignore[arg-type]
    )


class Recorder:
    """Моменты выполнения и порядок операций."""

    def __init__(self, clock: VirtualTime) -> None:
        self._clock = clock
        self.moments: dict[str, list[float]] = {}
        self.order: list[str] = []

    def operation(self, resource: str, label: str | None = None):  # type: ignore[no-untyped-def]
        async def run() -> str:
            self.moments.setdefault(resource, []).append(self._clock.monotonic())
            self.order.append(label if label is not None else resource)
            return resource

        return run

    def gaps(self, resource: str) -> list[float]:
        """Паузы между соседними запросами одной очереди."""
        moments = self.moments[resource]
        return [second - first for first, second in itertools.pairwise(moments)]


@pytest.fixture
def time_machine() -> VirtualTime:
    return VirtualTime(f.NOW)


class TestPerQueueDelay:
    async def test_requests_of_one_queue_are_spaced(self, time_machine: VirtualTime) -> None:
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        await time_machine.gather(
            *(
                manager.execute(
                    _request(ProviderId.ZERO_X, sequence=index),
                    recorder.operation("zero_x"),
                )
                for index in range(6)
            )
        )

        assert recorder.gaps("zero_x") == pytest.approx([DELAY] * 5)

    async def test_concurrent_requests_do_not_bypass_the_delay(
        self, time_machine: VirtualTime
    ) -> None:
        """Четыре одновременных слота не дают четыре одновременных запроса."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        await time_machine.gather(
            *(
                manager.execute(
                    _request(ProviderId.ZERO_X, sequence=index),
                    recorder.operation("zero_x"),
                )
                for index in range(4)
            )
        )

        assert min(recorder.gaps("zero_x")) >= DELAY - 1e-9

    async def test_retry_goes_through_the_same_delay(self, time_machine: VirtualTime) -> None:
        """Повтор — отдельный запрос очереди, а не обход механизма."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)
        attempts = 0

        async def failing_once() -> str:
            nonlocal attempts
            attempts += 1
            await recorder.operation("zero_x")()
            if attempts == 1:
                raise ProviderError("temporary", code="provider_error")
            return "ok"

        await time_machine.run(manager.execute(_request(ProviderId.ZERO_X), failing_once))

        assert attempts == 2
        # Повтор ждёт и backoff, и свою долю очереди: раньше паузы он не уходит.
        assert recorder.gaps("zero_x")[0] >= DELAY - 1e-9


class TestQueueIndependence:
    async def test_one_queue_does_not_delay_another(self, time_machine: VirtualTime) -> None:
        """Пауза поочерёдная, а не общая для всех запросов."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)
        providers = (ProviderId.ZERO_X, ProviderId.VELORA, ProviderId.UNISWAP)

        await time_machine.gather(
            *(
                manager.execute(
                    _request(provider, sequence=index),
                    recorder.operation(provider.value),
                )
                for index in range(6)
                for provider in providers
            )
        )

        for provider in providers:
            assert recorder.gaps(provider.value) == pytest.approx([DELAY] * 5)

    async def test_every_queue_starts_immediately(self, time_machine: VirtualTime) -> None:
        """Глобальной очерёдности нет: первые запросы уходят вместе."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)
        providers = (ProviderId.ZERO_X, ProviderId.VELORA, ProviderId.UNISWAP)

        await time_machine.gather(
            *(
                manager.execute(_request(provider), recorder.operation(provider.value))
                for provider in providers
            )
        )

        assert [recorder.moments[provider.value][0] for provider in providers] == [0.0] * 3

    async def test_a_busy_queue_does_not_hold_the_others(self, time_machine: VirtualTime) -> None:
        """Долгая очередь одного агрегатора не удлиняет чужие."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        await time_machine.gather(
            *(
                manager.execute(
                    _request(ProviderId.ZERO_X, sequence=index),
                    recorder.operation("zero_x"),
                )
                for index in range(20)
            ),
            *(
                manager.execute(
                    _request(ProviderId.VELORA, sequence=index),
                    recorder.operation("velora"),
                )
                for index in range(3)
            ),
        )

        assert recorder.moments["velora"][-1] == pytest.approx(2 * DELAY)
        assert recorder.moments["zero_x"][-1] == pytest.approx(19 * DELAY)


class TestUnregisteredResourcesAreUntouched:
    async def test_telegram_queue_has_no_delay(self, time_machine: VirtualTime) -> None:
        """Очередь уведомлений не относится к очередям агрегаторов."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        await time_machine.gather(
            *(
                manager.execute(
                    _request(TELEGRAM_RESOURCE, sequence=index),
                    recorder.operation(TELEGRAM_RESOURCE),
                )
                for index in range(5)
            )
        )

        assert recorder.moments[TELEGRAM_RESOURCE] == [0.0] * 5

    async def test_rpc_resource_has_no_delay(self, time_machine: VirtualTime) -> None:
        """RPC — самостоятельный ресурс, лимиты провайдеров ему не заданы."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        await time_machine.gather(
            *(
                manager.execute(
                    _request("rpc", operation=CapabilityOperation.GAS_ESTIMATE, sequence=index),
                    recorder.operation("rpc"),
                )
                for index in range(5)
            )
        )

        assert recorder.moments["rpc"] == [0.0] * 5


class TestOrderIsPreserved:
    async def test_fifo_survives_the_delay(self, time_machine: VirtualTime) -> None:
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)

        queued = [
            (
                f"r{index}",
                _request(ProviderId.ZERO_X, sequence=index),
            )
            for index in range(5)
        ]
        await time_machine.gather(
            *(manager.execute(item, recorder.operation("zero_x", label)) for label, item in queued)
        )

        assert recorder.order == ["r0", "r1", "r2", "r3", "r4"]

    async def test_level2_still_preempts_level1(self, time_machine: VirtualTime) -> None:
        """Пауза не позволяет Level 1 обойти Level 2."""
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)
        blocker = asyncio.Event()

        async def hold() -> str:
            await blocker.wait()
            return "hold"

        async def scenario() -> None:
            held = [
                asyncio.ensure_future(manager.execute(_request(ProviderId.ZERO_X), hold))
                for _ in range(4)
            ]
            for _ in range(8):
                await asyncio.sleep(0)

            waiting = [
                asyncio.ensure_future(
                    manager.execute(
                        _request(ProviderId.ZERO_X, sequence=index),
                        recorder.operation("zero_x", f"l1-{index}"),
                    )
                )
                for index in range(4)
            ]
            waiting.append(
                asyncio.ensure_future(
                    manager.execute(
                        _request(
                            ProviderId.ZERO_X,
                            priority=RequestPriority.UR_LEVEL2,
                            priority_at=f.NOW,
                            sequence=99,
                        ),
                        recorder.operation("zero_x", "l2"),
                    )
                )
            )
            for _ in range(8):
                await asyncio.sleep(0)

            blocker.set()
            await asyncio.gather(*held)
            await asyncio.gather(*waiting)

        await time_machine.run(scenario())

        assert recorder.order[0] == "l2", "Level 2 обслуживается первым"

    async def test_earlier_level2_scan_still_wins(self, time_machine: VirtualTime) -> None:
        manager = _manager(time_machine)
        recorder = Recorder(time_machine)
        blocker = asyncio.Event()

        async def hold() -> str:
            await blocker.wait()
            return "hold"

        async def scenario() -> None:
            held = [
                asyncio.ensure_future(manager.execute(_request(ProviderId.VELORA), hold))
                for _ in range(4)
            ]
            for _ in range(8):
                await asyncio.sleep(0)

            queued = [
                asyncio.ensure_future(
                    manager.execute(
                        _request(
                            ProviderId.VELORA,
                            priority=RequestPriority.UR_LEVEL2,
                            priority_at=f.NOW + timedelta(seconds=30),
                            sequence=1,
                        ),
                        recorder.operation("velora", "later"),
                    )
                ),
                asyncio.ensure_future(
                    manager.execute(
                        _request(
                            ProviderId.VELORA,
                            priority=RequestPriority.UR_LEVEL2,
                            priority_at=f.NOW,
                            sequence=2,
                        ),
                        recorder.operation("velora", "earlier"),
                    )
                ),
            ]
            for _ in range(8):
                await asyncio.sleep(0)

            blocker.set()
            await asyncio.gather(*held)
            await asyncio.gather(*queued)

        await time_machine.run(scenario())

        assert recorder.order == ["earlier", "later"]
