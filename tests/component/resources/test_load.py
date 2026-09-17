"""Нагрузка производственного масштаба на очереди агрегаторов.

Воспроизводится настоящий объём цикла: три агрегатора, 29 промежуточных
токенов, четыре суммы, BUY и SELL, поверх которых идёт Level 2.

Проверяется то, из-за чего очереди и появились: настроенные
``requests_per_second`` действительно удерживаются, агрегаторы
изолированы, порядок обслуживания соответствует приоритетам, а массовых
``resource_circuit_open`` не возникает.

Запросы к настоящим API не выполняются (``CLAUDE.md`` §10).
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import timedelta

import pytest

from monik.domain.enums.capability import CapabilityOperation
from monik.domain.enums.providers import ProviderId
from monik.domain.enums.resources import CircuitState, RequestPriority, ResourceResultStatus
from monik.domain.models.resource import ResourceKey, ResourceRequest
from monik.domain.value_objects.identifiers import RequestId
from monik.services.resources import ResourceLimits, ResourceManager
from tests import factories as f
from tests.unit.resources.conftest import resource_config

from .conftest import VirtualTime

#: Настроенные лимиты production.
RATES: dict[ProviderId, float] = {
    ProviderId.ZERO_X: 4.9,
    ProviderId.VELORA: 4.9,
    ProviderId.UNISWAP: 5.9,
}
CONCURRENCY: dict[ProviderId, int] = {
    ProviderId.ZERO_X: 4,
    ProviderId.VELORA: 4,
    ProviderId.UNISWAP: 2,
}

#: Пауза между запросами внутри одной очереди.
MIN_INTERVAL = 0.1

#: Документированные лимиты ключей: столько запросов в секунду
#: агрегатор принимает.
DOCUMENTED_LIMITS: dict[ProviderId, int] = {
    ProviderId.ZERO_X: 5,
    ProviderId.VELORA: 5,
    ProviderId.UNISWAP: 6,
}

#: Масштаб цикла: 29 промежуточных токенов и четыре суммы.
TOKENS = 29
AMOUNTS = 4


@pytest.fixture
def time_machine() -> VirtualTime:
    return VirtualTime(f.NOW)


@pytest.fixture
def manager(time_machine: VirtualTime) -> ResourceManager:
    instance = ResourceManager(
        resource_config(global_max_concurrent_requests=16, queue_capacity=4096),
        time_machine,
        sleeper=time_machine.sleep,
    )
    for provider, rate in RATES.items():
        instance.register_limits(
            ResourceKey(provider_id=provider),
            ResourceLimits(
                max_concurrent=CONCURRENCY[provider],
                requests_per_second=rate,
                # Те же значения, что регистрирует composition root.
                burst=1,
                min_interval_seconds=MIN_INTERVAL,
            ),
        )
    return instance


def _request(
    provider: ProviderId,
    *,
    operation: CapabilityOperation,
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


def _level1_requests() -> list[ResourceRequest]:
    """Запросы одного цикла Level 1: BUY по всем комбинациям, затем SELL."""
    requests: list[ResourceRequest] = []
    sequence = 0
    for operation, priority in (
        (CapabilityOperation.QUOTE_BUY, RequestPriority.UR_LEVEL1_BUY),
        (CapabilityOperation.QUOTE_SELL, RequestPriority.UR_LEVEL1_SELL),
    ):
        for provider in RATES:
            for _token in range(TOKENS):
                for _amount in range(AMOUNTS):
                    requests.append(
                        _request(
                            provider, operation=operation, priority=priority, sequence=sequence
                        )
                    )
                    sequence += 1
    return requests


class Recorder:
    """Моменты выполнения операций по агрегаторам."""

    def __init__(self, clock: VirtualTime) -> None:
        self._clock = clock
        self.moments: dict[ProviderId, list[float]] = {provider: [] for provider in RATES}
        self.order: list[str] = []

    def operation(self, provider: ProviderId, label: str = ""):  # type: ignore[no-untyped-def]
        async def run() -> str:
            self.moments[provider].append(self._clock.monotonic())
            if label:
                self.order.append(label)
            return label or provider.value

        return run

    def span(self, provider: ProviderId) -> float:
        """Сколько заняли все запросы к агрегатору."""
        moments = self.moments[provider]
        return moments[-1] - moments[0]

    def earliest_possible(self, provider: ProviderId) -> float:
        """Минимально возможная длительность при настроенной частоте.

        Стартовый запас корзины равен одному запросу, поэтому остальные
        идут ровно с настроенной частотой: накопленного всплеска нет.
        """
        rate = RATES[provider]
        return (len(self.moments[provider]) - 1) / rate

    def peak_rate(self, provider: ProviderId, window: float = 1.0) -> int:
        """Наибольшее число запросов в скользящем окне."""
        moments = self.moments[provider]
        peak = 0
        start = 0
        for index, moment in enumerate(moments):
            while moment - moments[start] > window:
                start += 1
            peak = max(peak, index - start + 1)
        return peak


class TestFullCycleLoad:
    """Полный объём Level 1 через очереди."""

    @pytest.fixture
    async def executed(self, manager: ResourceManager, time_machine: VirtualTime) -> Recorder:
        recorder = Recorder(time_machine)
        requests = _level1_requests()
        await time_machine.gather(
            *(
                manager.execute(item, recorder.operation(ProviderId(item.key.provider_id)))
                for item in requests
            )
        )
        return recorder

    async def test_every_request_completes(
        self, executed: Recorder, manager: ResourceManager
    ) -> None:
        total = sum(len(moments) for moments in executed.moments.values())
        assert total == len(RATES) * TOKENS * AMOUNTS * 2
        assert all(result.status is ResourceResultStatus.SUCCESS for result in manager.results())

    @pytest.mark.parametrize("provider", list(RATES))
    async def test_configured_rate_is_not_exceeded(
        self, executed: Recorder, provider: ProviderId
    ) -> None:
        """Регрессия: без применённых лимитов выходило ~17 запросов в секунду.

        Сверх стартового запаса корзина не выдаёт быстрее настроенного —
        отсюда минимальная длительность всей нагрузки.
        """
        assert executed.span(provider) >= executed.earliest_possible(provider) - 1e-6

    @pytest.mark.parametrize("provider", list(RATES))
    async def test_no_second_exceeds_the_documented_provider_limit(
        self, executed: Recorder, provider: ProviderId
    ) -> None:
        """Всплеск не выходит за лимит ключа агрегатора.

        Регрессия: накопленный стартовый запас выдавал за первую секунду
        около одиннадцати запросов при документированных шести, и
        Uniswap отвечал 429.
        """
        assert executed.peak_rate(provider) <= DOCUMENTED_LIMITS[provider]

    @pytest.mark.parametrize("provider", list(RATES))
    async def test_requests_of_one_queue_keep_the_minimum_gap(
        self, executed: Recorder, provider: ProviderId
    ) -> None:
        """Внутри очереди выдерживается минимальная пауза."""
        moments = executed.moments[provider]
        gaps = [second - first for first, second in itertools.pairwise(moments)]
        assert min(gaps) >= MIN_INTERVAL - 1e-9

    @pytest.mark.parametrize("provider", list(RATES))
    async def test_the_rate_does_not_depend_on_the_other_aggregators(
        self, executed: Recorder, provider: ProviderId
    ) -> None:
        """Каждая очередь тратит собственный бюджет (05 §48).

        Регрессия: общий потолок одновременности удерживался и во время
        паузы по частоте, поэтому все три агрегатора проседали примерно
        до трёх запросов в секунду вместо настроенных.
        """
        assert executed.span(provider) == pytest.approx(
            executed.earliest_possible(provider), rel=0.01
        )

    async def test_aggregators_do_not_wait_for_each_other(self, executed: Recorder) -> None:
        """Более быстрый агрегатор заканчивает раньше (05 §48)."""
        finished = {provider: moments[-1] for provider, moments in executed.moments.items()}
        assert finished[ProviderId.UNISWAP] < finished[ProviderId.ZERO_X]

    async def test_buy_and_sell_share_one_budget(self, executed: Recorder) -> None:
        """5.9 зап/с не превращаются в 11.8 (05 §51)."""
        provider = ProviderId.UNISWAP
        assert executed.span(provider) >= executed.earliest_possible(provider) - 1e-6

    async def test_no_circuit_breaker_opens(
        self, executed: Recorder, manager: ResourceManager
    ) -> None:
        """Штатная нагрузка не должна выглядеть как отказ провайдера."""
        for provider in RATES:
            assert manager.circuit_state(ResourceKey(provider_id=provider)) is CircuitState.CLOSED
        assert not [
            result for result in manager.results() if result.error_code == "resource_circuit_open"
        ]

    async def test_the_cycle_takes_the_time_the_limits_imply(self, executed: Recorder) -> None:
        """Длительность определяется самым медленным агрегатором."""
        requests_per_provider = TOKENS * AMOUNTS * 2
        slowest = max((requests_per_provider - 1) / rate for rate in RATES.values())
        longest = max(executed.span(provider) for provider in RATES)
        assert longest == pytest.approx(slowest, rel=0.01)


class TestLevel2Preemption:
    """Level 2 обгоняет Level 1 в общей очереди (``CLAUDE.md`` §15)."""

    async def test_level2_is_served_before_a_queued_level1(
        self, manager: ResourceManager, time_machine: VirtualTime
    ) -> None:
        recorder = Recorder(time_machine)
        provider = ProviderId.ZERO_X

        level1 = [
            manager.execute(
                _request(
                    provider,
                    operation=CapabilityOperation.QUOTE_BUY,
                    priority=RequestPriority.UR_LEVEL1_BUY,
                    sequence=index,
                ),
                recorder.operation(provider, f"l1-{index}"),
            )
            for index in range(40)
        ]

        async def level2_after_the_queue_fills() -> None:
            # Level 2 появляется, когда очередь Level 1 уже стоит.
            await time_machine.sleep(1.0)
            await manager.execute(
                _request(
                    provider,
                    operation=CapabilityOperation.QUOTE_BUY,
                    priority=RequestPriority.UR_LEVEL2,
                    priority_at=f.NOW,
                    sequence=999,
                ),
                recorder.operation(provider, "l2"),
            )

        await time_machine.gather(*level1, level2_after_the_queue_fills())

        position = recorder.order.index("l2")
        assert position < len(recorder.order) - 1, "Level 2 не должен уходить в хвост"
        # Обогнал большинство ожидавших запросов Level 1.
        assert position < 20

    async def test_an_earlier_level2_scan_goes_first(
        self, manager: ResourceManager, time_machine: VirtualTime
    ) -> None:
        """Приоритет проверки определяется временем её начала (05 §18).

        Обе проверки — Level 2, поэтому их разделяет только время старта.
        """
        recorder = Recorder(time_machine)
        provider = ProviderId.VELORA
        blocker = asyncio.Event()

        async def hold() -> str:
            await blocker.wait()
            return "hold"

        def level2(label: str, *, started_at: object, sequence: int):  # type: ignore[no-untyped-def]
            return manager.execute(
                _request(
                    provider,
                    operation=CapabilityOperation.QUOTE_BUY,
                    priority=RequestPriority.UR_LEVEL2,
                    priority_at=started_at,
                    sequence=sequence,
                ),
                recorder.operation(provider, label),
            )

        async def scenario() -> None:
            # Все места агрегатора заняты: очередь действительно есть.
            held = [
                asyncio.ensure_future(
                    manager.execute(
                        _request(provider, operation=CapabilityOperation.QUOTE_BUY), hold
                    )
                )
                for _ in range(CONCURRENCY[provider])
            ]
            for _ in range(8):
                await asyncio.sleep(0)

            # Позже начатая проверка встаёт в очередь первой.
            queued = [
                asyncio.ensure_future(
                    level2("later-scan", started_at=f.NOW + timedelta(seconds=30), sequence=1)
                ),
                asyncio.ensure_future(level2("earlier-scan", started_at=f.NOW, sequence=2)),
            ]
            for _ in range(8):
                await asyncio.sleep(0)

            blocker.set()
            await asyncio.gather(*held)
            await asyncio.gather(*queued)

        await time_machine.run(scenario())

        assert recorder.order == ["earlier-scan", "later-scan"]
