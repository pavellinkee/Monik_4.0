"""Resource Manager: приоритеты, состояния ресурсов, результаты запросов."""

from __future__ import annotations

from monik.domain.enums.base import DomainEnum
from monik.domain.enums.modes import ScanMode
from monik.domain.enums.operations import OperationType


class RequestPriority(DomainEnum):
    """Приоритет запроса к внешнему ресурсу.

    Порядок задаётся двумя правилами, и первое сильнее второго
    (``the_main_rules.md``, правило 13).

    **Между режимами**: ``ann`` > ``fest`` > ``ur``. Вся работа торгового
    режима обслуживается раньше любой работы ``fest``, а вся работа
    ``fest`` — раньше любой работы ``ur``.

    **Внутри режима** порядок свой, потому что состав работы у режимов
    разный. ``ur`` и ``fest`` ищут и подтверждают:

    ``Level 2`` > ``Level 1 SELL`` > ``Level 1 BUY``.

    У ``ann`` нет ни Level 1, ни Level 2. У него три занятия, и порядок
    между ними обратен их последовательности во времени:

    ``продажа`` > ``покупка`` > ``сканирование``.

    Продажа первая, потому что за ней стоят уже потраченные деньги:
    задержка держит купленный токен дольше, чем живёт отклонение, ради
    которого он куплен. Покупка вторая: она деньги тратит, но пока не
    рискует ими. Сканирование последнее — уступив очередь, оно теряет
    один цикл и найдёт то же самое через десять секунд.

    ``MAINTENANCE`` и ``BACKGROUND`` режиму не принадлежат и стоят ниже
    всех: обслуживание и доставка уведомлений подождут.

    Прибыльность возможности **не** влияет на приоритет
    (``04_SCHEDULER.md`` §26): выше ставится род работы, а не её ожидаемый
    доход.

    Приоритет назван целиком, а не парой «режим + работа», намеренно:
    значение путешествует через адаптеры провайдеров как одно поле, и
    новый агрегатор не может забыть передать вторую половину.
    """

    # --- ann: торговый режим, самый приоритетный ------------------------
    #: Продажа: котировка выхода, сборка, проверка узлом, отправка,
    #: квитанция.
    ANN_SELL = "ann_sell"
    #: Покупка: то же самое для входа в сделку.
    ANN_BUY = "ann_buy"
    #: Сканирование торгового режима.
    ANN_SCAN = "ann_scan"

    # --- fest: частый проход по стейблкоинам ----------------------------
    FEST_LEVEL2 = "fest_level2"
    FEST_LEVEL1_SELL = "fest_level1_sell"
    FEST_LEVEL1_BUY = "fest_level1_buy"

    # --- ur: основной проход, самый низкий из режимов -------------------
    UR_LEVEL2 = "ur_level2"
    UR_LEVEL1_SELL = "ur_level1_sell"
    UR_LEVEL1_BUY = "ur_level1_buy"

    # --- работа, не принадлежащая режиму --------------------------------
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
    RequestPriority.ANN_SCAN: 2,
    RequestPriority.FEST_LEVEL2: 3,
    RequestPriority.FEST_LEVEL1_SELL: 4,
    RequestPriority.FEST_LEVEL1_BUY: 5,
    RequestPriority.UR_LEVEL2: 6,
    RequestPriority.UR_LEVEL1_SELL: 7,
    RequestPriority.UR_LEVEL1_BUY: 8,
    RequestPriority.MAINTENANCE: 9,
    RequestPriority.BACKGROUND: 10,
}

#: Приоритет запроса поиска по режиму и направлению.
#:
#: У ``ann`` направления не различаются: сканирование там одно занятие
#: целиком, и делить его на ноги незачем — обе уступают и покупке, и
#: продаже.
_SEARCH_PRIORITIES: dict[tuple[ScanMode, OperationType], RequestPriority] = {
    (ScanMode.ANN, OperationType.BUY): RequestPriority.ANN_SCAN,
    (ScanMode.ANN, OperationType.SELL): RequestPriority.ANN_SCAN,
    (ScanMode.FEST, OperationType.BUY): RequestPriority.FEST_LEVEL1_BUY,
    (ScanMode.FEST, OperationType.SELL): RequestPriority.FEST_LEVEL1_SELL,
    (ScanMode.UR, OperationType.BUY): RequestPriority.UR_LEVEL1_BUY,
    (ScanMode.UR, OperationType.SELL): RequestPriority.UR_LEVEL1_SELL,
}

#: Приоритет подтверждения Level 2 по режиму. ``ann`` сюда не входит:
#: он исполняет находку сам и на второй этап её не передаёт
#: (``the_main_rules.md``, правило 11).
_CONFIRMATION_PRIORITIES: dict[ScanMode, RequestPriority] = {
    ScanMode.FEST: RequestPriority.FEST_LEVEL2,
    ScanMode.UR: RequestPriority.UR_LEVEL2,
}


def search_priority(mode: ScanMode, operation: OperationType) -> RequestPriority:
    """Приоритет запроса поиска в этом режиме."""
    return _SEARCH_PRIORITIES[(mode, operation)]


def confirmation_priority(mode: ScanMode) -> RequestPriority:
    """Приоритет запроса подтверждения Level 2 в этом режиме.

    Для ``ann`` подтверждения не существует, и подставлять ему чужой
    приоритет нельзя: у режима, который не доходит до Level 2, его просто
    нет. Обращение сюда с ``ann`` — ошибка вызывающей стороны, и молчать о
    ней хуже, чем упасть.
    """
    priority = _CONFIRMATION_PRIORITIES.get(mode)
    if priority is None:
        raise ValueError(f"mode {mode.value} has no level 2 confirmation")
    return priority


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
