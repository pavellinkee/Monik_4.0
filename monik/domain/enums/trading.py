"""Состояния сделки режима ``ann``."""

from __future__ import annotations

from monik.domain.enums.base import DomainEnum

__all__ = ["PositionStatus"]


class PositionStatus(DomainEnum):
    """Где находится сделка.

    Сделка живёт дольше одного цикла и обязана пережить перезапуск:
    позиция, существующая только в памяти, после падения превратилась бы
    в токены на счёте, о которых система не знает.
    """

    #: Покупка отправлена, квитанции ещё нет.
    BUYING = "buying"
    #: Токен куплен. Ждём цены, при которой продажа выгодна.
    HOLDING = "holding"
    #: Продажа отправлена, квитанции ещё нет.
    SELLING = "selling"
    #: Круг завершён.
    CLOSED = "closed"
    #: Покупка не состоялась: сеть отвергла транзакцию. Денег это не
    #: стоило, кроме газа, и позиция закрыта, не начавшись.
    FAILED = "failed"

    @property
    def is_open(self) -> bool:
        """Требует ли сделка дальнейших действий."""
        return self in (PositionStatus.BUYING, PositionStatus.HOLDING, PositionStatus.SELLING)

    @property
    def holds_tokens(self) -> bool:
        """Лежит ли сейчас на счёте промежуточный токен."""
        return self in (PositionStatus.HOLDING, PositionStatus.SELLING)
