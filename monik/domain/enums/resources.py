"""Resource Manager: приоритеты, состояния ресурсов, результаты запросов."""

from __future__ import annotations

from monik.domain.enums.base import DomainEnum


class RequestPriority(DomainEnum):
    """Приоритет запроса к внешнему ресурсу.

    Порядок обслуживания (``CLAUDE.md`` §15, ``05_RESOURCE_MANAGER.md`` §16-20
    в редакции ``the_main_rules.md``, правило 13):

    ``EXECUTION`` > ``LEVEL2`` > ``LEVEL1_SELL`` > ``LEVEL1_BUY`` >
    ``MAINTENANCE`` > ``BACKGROUND``.

    ``EXECUTION`` стоит выше всего остального, потому что это единственные
    запросы, от которых зависят уже потраченные деньги. Поиск, уступивший
    очередь, теряет один цикл; сделка, уступившая очередь, держит купленный
    токен дольше, чем живёт отклонение, ради которого он куплен.

    Прибыльность возможности **не** влияет на приоритет
    (``04_SCHEDULER.md`` §26): выше ставится род работы, а не её ожидаемый
    доход.
    """

    #: Запросы подсистемы исполнения: сборка сделки, проверка узлом,
    #: котировка выхода у открытой позиции.
    EXECUTION = "execution"
    LEVEL2 = "level2"
    LEVEL1_SELL = "level1_sell"
    LEVEL1_BUY = "level1_buy"
    MAINTENANCE = "maintenance"
    BACKGROUND = "background"

    @property
    def rank(self) -> int:
        """Числовой ранг: меньше — выше приоритет.

        Используется очередью Resource Manager. Внутри одного ранга порядок
        определяется ``created_at`` и sequence number (``04_SCHEDULER.md`` §25).
        """
        return _PRIORITY_RANKS[self]


_PRIORITY_RANKS: dict[RequestPriority, int] = {
    RequestPriority.EXECUTION: 0,
    RequestPriority.LEVEL2: 1,
    RequestPriority.LEVEL1_SELL: 2,
    RequestPriority.LEVEL1_BUY: 3,
    RequestPriority.MAINTENANCE: 4,
    RequestPriority.BACKGROUND: 5,
}


class ResourceState(DomainEnum):
    """Состояние ограниченного внешнего ресурса (``05_RESOURCE_MANAGER.md`` §5-10)."""

    AVAILABLE = "available"
    BUSY = "busy"
    RATE_LIMITED = "rate_limited"
    COOLDOWN = "cooldown"
    CIRCUIT_OPEN = "circuit_open"


class CircuitState(DomainEnum):
    """Состояние circuit breaker (``CLAUDE.md`` §33, ``12_RESOURCE_MANAGER.md`` §32-34).

    Circuit breaker отражает временную недоступность и **не изменяет**
    Capability Registry (``05_RESOURCE_MANAGER.md`` §11).
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ResourceResultStatus(DomainEnum):
    """Итог выполнения запроса через Resource Manager (``36_DATA_MODELS.md`` §56)."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
