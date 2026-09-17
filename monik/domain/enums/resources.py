"""Resource Manager: приоритеты, состояния ресурсов, результаты запросов."""

from __future__ import annotations

from monik.domain.enums.base import DomainEnum


class RequestPriority(DomainEnum):
    """Приоритет запроса к внешнему ресурсу.

    Правило приоритета принадлежит **режиму**, а не системе целиком
    (``the_main_rules.md``, правило 13): у режимов разный состав работы, и
    общего порядка для них не существует.

    ``ur`` и ``fest`` ищут и подтверждают, поэтому их порядок прежний
    (``CLAUDE.md`` §15, ``05_RESOURCE_MANAGER.md`` §16-20):

    ``LEVEL2`` > ``LEVEL1_SELL`` > ``LEVEL1_BUY``.

    У ``ann`` нет ни Level 1, ни Level 2. У него три занятия — продажа,
    покупка и сканирование, — и порядок между ними обратен их
    последовательности во времени:

    ``ANN_SELL`` > ``ANN_BUY`` > ``ANN_SCAN``.

    Продажа первая, потому что за ней стоят уже потраченные деньги:
    задержка держит купленный токен дольше, чем живёт отклонение, ради
    которого он куплен. Покупка вторая: она деньги тратит, но пока не
    рискует ими. Сканирование последнее — уступив очередь, оно теряет
    один цикл и найдёт то же самое через десять секунд.

    Между режимами продажа и покупка ``ann`` стоят выше всего: это
    единственная работа, чья задержка стоит денег. Сканирование ``ann``,
    наоборот, поставлено **ниже** поиска ``ur`` и ``fest`` — так их
    обслуживание остаётся ровно таким, каким было до появления ``ann``.

    Прибыльность возможности **не** влияет на приоритет
    (``04_SCHEDULER.md`` §26): выше ставится род работы, а не её ожидаемый
    доход.
    """

    #: Продажа режима ``ann``: котировка выхода, сборка, проверка узлом,
    #: отправка и квитанция.
    ANN_SELL = "ann_sell"
    #: Покупка режима ``ann``: всё то же самое для входа в сделку.
    ANN_BUY = "ann_buy"
    LEVEL2 = "level2"
    LEVEL1_SELL = "level1_sell"
    LEVEL1_BUY = "level1_buy"
    #: Сканирование режима ``ann``.
    ANN_SCAN = "ann_scan"
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
    RequestPriority.ANN_SELL: 0,
    RequestPriority.ANN_BUY: 1,
    RequestPriority.LEVEL2: 2,
    RequestPriority.LEVEL1_SELL: 3,
    RequestPriority.LEVEL1_BUY: 4,
    RequestPriority.ANN_SCAN: 5,
    RequestPriority.MAINTENANCE: 6,
    RequestPriority.BACKGROUND: 7,
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
