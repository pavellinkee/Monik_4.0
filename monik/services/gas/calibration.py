"""Поправка к оценке расхода газа.

Агрегатор присылает оценку вместе с котировкой, и она стоит ноль
запросов — но считает голый обмен по одному лучшему пути. Платим мы за
реальный вызов роутера: с разрешениями, обёртками и маршрутом, который
на исполнении может разойтись на несколько пулов. Измеренная разница
оказалась кратной, а не процентной, поэтому поправка — множитель.

Множитель берётся из двух источников, и порядок между ними важен:

1. **измеренный** — отношение фактического расхода к обещанному,
   накопленное по квитанциям. Квитанция запрашивается в любом случае,
   поэтому знание достаётся бесплатно;
2. **заданный конфигурацией** — пока замеров мало. Одна сделка не
   статистика, и верить ей раньше времени опаснее, чем не верить вовсе.

Поправка привязана к паре «сеть — агрегатор»: у разных агрегаторов
расхождение своё, и общий множитель одному занижал бы, другому завышал.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from monik.config.sections.fees import GasCalibrationConfig
from monik.domain.enums.providers import ProviderId
from monik.domain.models.gas import GasCalibrationSample
from monik.domain.value_objects.identity import NetworkId
from monik.services.observability.clock import Clock
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["GasCalibration", "GasCalibrationStore"]

_LOGGER = get_logger("services.gas.calibration")


class GasCalibrationStore(Protocol):
    """Хранилище накопленных замеров."""

    async def all_samples(self) -> tuple[GasCalibrationSample, ...]:
        """Все накопленные итоги."""
        ...

    async def save(self, sample: GasCalibrationSample) -> None:
        """Сохранить итог пары."""
        ...


class GasCalibration:
    """Множитель поправки и его уточнение по факту."""

    def __init__(
        self,
        *,
        config: GasCalibrationConfig,
        clock: Clock,
        default_multiplier: Decimal,
        per_provider: dict[str, Decimal],
        store: GasCalibrationStore | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._default = default_multiplier
        self._per_provider = dict(per_provider)
        self._store = store
        self._samples: dict[tuple[str, str], GasCalibrationSample] = {}

    async def load(self) -> None:
        """Поднять накопленное из хранилища.

        Вызывается один раз при запуске: поправка не меняется от цикла к
        циклу, и спрашивать её у базы в каждом проходе незачем.
        """
        if self._store is None:
            return
        for sample in await self._store.all_samples():
            self._samples[_key(sample.network_id, sample.provider_id)] = sample

    def factor(self, network_id: NetworkId, provider_id: ProviderId) -> Decimal:
        """Во сколько раз умножить оценку котировки."""
        configured = self._per_provider.get(provider_id.value, self._default)
        if not self._config.enabled:
            return configured
        sample = self._samples.get(_key(network_id, provider_id))
        if sample is None or sample.samples < self._config.min_samples:
            return configured
        measured = sample.factor
        return configured if measured is None else measured

    async def record(
        self,
        network_id: NetworkId,
        provider_id: ProviderId,
        *,
        quoted_units: int,
        actual_units: int,
    ) -> None:
        """Учесть один исполненный вызов.

        Замер с неизвестной или нулевой обещанной оценкой отбрасывается:
        отношение к нулю смысла не имеет, а подставлять вместо него
        что-либо — значит выдумывать данные.
        """
        if quoted_units <= 0 or actual_units <= 0:
            return
        key = _key(network_id, provider_id)
        previous = self._samples.get(key)
        samples = 1 if previous is None else previous.samples + 1
        quoted = quoted_units if previous is None else previous.quoted_units + quoted_units
        actual = actual_units if previous is None else previous.actual_units + actual_units
        if samples > self._config.max_samples:
            # Накопленное уполовинивается: поправка обязана следовать за
            # изменением маршрутов, а не усреднять их за всё время работы.
            samples, quoted, actual = samples // 2, quoted // 2, actual // 2
        updated = GasCalibrationSample(
            network_id=network_id,
            provider_id=provider_id,
            samples=samples,
            quoted_units=quoted,
            actual_units=actual,
            updated_at=self._clock.now(),
        )
        self._samples[key] = updated
        if self._store is not None:
            await self._store.save(updated)
        _LOGGER.info(
            "gas calibration updated",
            extra=log_fields(
                network=str(network_id),
                provider=provider_id.value,
                samples=samples,
                quoted_units=quoted_units,
                actual_units=actual_units,
                factor=str(updated.factor),
            ),
        )


def _key(network_id: NetworkId, provider_id: ProviderId) -> tuple[str, str]:
    return (str(network_id), provider_id.value)
