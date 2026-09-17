"""Поправка к оценке расхода газа.

Агрегатор оценивает голый обмен по одному лучшему пути, а платим мы за
реальный вызов роутера. Измеренное расхождение оказалось кратным, и
ошибка систематическая — значит её можно измерить и учесть.

Источник знания бесплатен: обещанное приходит с котировкой, фактическое —
с квитанцией, которую подсистема исполнения запрашивает в любом случае.
"""

from __future__ import annotations

from decimal import Decimal

from monik.config.sections.fees import GasCalibrationConfig
from monik.domain.enums.providers import ProviderId
from monik.services.gas.calibration import GasCalibration
from monik.services.observability import FakeClock
from tests import factories as f


class _Store:
    """Хранилище замеров в памяти. **Test implementation**."""

    def __init__(self, *samples: object) -> None:
        self.samples = list(samples)
        self.saved: list[object] = []

    async def all_samples(self):  # noqa: ANN201
        return tuple(self.samples)

    async def save(self, sample: object) -> None:
        self.saved.append(sample)


def _calibration(
    *,
    min_samples: int = 5,
    max_samples: int = 100,
    enabled: bool = True,
    default: str = "1.0",
    per_provider: dict[str, str] | None = None,
    store: _Store | None = None,
) -> GasCalibration:
    return GasCalibration(
        config=GasCalibrationConfig(
            enabled=enabled, min_samples=min_samples, max_samples=max_samples
        ),
        clock=FakeClock(f.NOW),
        default_multiplier=Decimal(default),
        per_provider={k: Decimal(v) for k, v in (per_provider or {}).items()},
        store=store,  # type: ignore[arg-type]
    )


class TestFactor:
    def test_configured_multiplier_is_used_without_measurements(self) -> None:
        """Одна сделка не статистика, и до замеров верим настройке."""
        calibration = _calibration(default="3.0")

        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("3.0")

    def test_provider_multiplier_overrides_the_common_one(self) -> None:
        """У разных агрегаторов расхождение своё."""
        calibration = _calibration(default="3.0", per_provider={"kyberswap": "1.5"})

        assert calibration.factor(f.POLYGON, ProviderId.KYBERSWAP) == Decimal("1.5")
        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("3.0")

    async def test_measurements_replace_the_configured_value(self) -> None:
        calibration = _calibration(min_samples=2, default="3.0")

        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=250)
        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("3.0"), (
            "одного замера мало"
        )

        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=250)
        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("2.5")

    async def test_measurement_of_one_provider_does_not_touch_another(self) -> None:
        calibration = _calibration(min_samples=1, default="3.0")

        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=250)

        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("2.5")
        assert calibration.factor(f.POLYGON, ProviderId.KYBERSWAP) == Decimal("3.0")

    async def test_disabled_calibration_keeps_the_configured_value(self) -> None:
        calibration = _calibration(min_samples=1, enabled=False, default="3.0")

        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=250)

        assert calibration.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("3.0")


class TestRecording:
    async def test_meaningless_measurement_is_discarded(self) -> None:
        """Отношение к нулю смысла не имеет."""
        store = _Store()
        calibration = _calibration(min_samples=1, store=store)

        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=0, actual_units=250)
        await calibration.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=0)

        assert store.saved == []

    async def test_history_is_halved_at_the_limit(self) -> None:
        """Поправка следует за маршрутами, а не усредняет их за всё время.

        Без предела давние замеры навсегда перевешивали бы свежие, и
        изменение маршрутов агрегатора Monik заметил бы через месяцы.
        """
        store = _Store()
        calibration = _calibration(min_samples=1, max_samples=4, store=store)

        for _ in range(5):
            await calibration.record(
                f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=300
            )

        last = store.saved[-1]
        assert last.samples == 2, "накопленное уполовинено"  # type: ignore[attr-defined]
        assert last.factor == Decimal("3")  # type: ignore[attr-defined]

    async def test_saved_measurements_survive_a_restart(self) -> None:
        """Иначе каждый перезапуск возвращал бы завышенную оценку."""
        store = _Store()
        first = _calibration(min_samples=1, store=store, default="1.0")
        await first.record(f.POLYGON, ProviderId.UNISWAP, quoted_units=100, actual_units=300)

        restarted = _calibration(min_samples=1, store=_Store(*store.saved), default="1.0")
        await restarted.load()

        assert restarted.factor(f.POLYGON, ProviderId.UNISWAP) == Decimal("3")
