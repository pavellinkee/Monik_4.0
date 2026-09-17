"""Конфигурация торговой подсистемы режима ``ann``.

Исполнение — **отдельная подсистема**, а не функция сканера
(``01_PROJECT_REQUIREMENTS.md`` §56 в редакции ``the_main_rules.md``,
правило 11). Поэтому у неё собственный выключатель: поиск в режиме
``ann`` может работать, пока торговля выключена, — это и есть режим
сухого прогона.

Ключ торгового счёта сюда не попадает: как и все секреты, он задаётся
ссылкой на переменную окружения (``17_CONFIGURATION.md`` §26).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.config.secrets import SecretRef
from monik.domain.value_objects.numeric import PositiveDecimal

__all__ = ["TradingConfig"]


class TradingConfig(ConfigSection):
    """Параметры исполнения сделок."""

    #: Разрешено ли отправлять транзакции. Выключено — режим ``ann``
    #: только ищет и записывает найденное в журнал.
    enabled: bool = False
    #: Ссылка на приватный ключ торгового счёта.
    private_key: SecretRef | None = None

    #: Допуск проскальзывания сделки, в процентах.
    #:
    #: Это не косметика, а защита от завышенной котировки. Транзакция
    #: несёт минимальную приемлемую сумму, посчитанную от котировки; если
    #: реальный пул её не даёт, обмен **откатывается сетью**, и мы теряем
    #: только газ вместо исполнения по плохому курсу.
    slippage_percent: PositiveDecimal = Decimal("0.1")

    #: Минимальная чистая прибыль, при которой позицию можно закрывать.
    #: Задаётся в базовом токене, а не в процентах: решение о выходе
    #: принимается деньгами.
    min_exit_profit: PositiveDecimal = Decimal("0.01")

    #: Как часто перепроверять цену продажи у открытой позиции.
    recheck_interval_seconds: int = Field(default=10, ge=1, le=3_600)

    #: Предельный срок ожидания выхода. По его истечении подсистема
    #: **уведомляет оператора** и продолжает ждать: позицию в убыток она
    #: не закрывает никогда — это решение человека.
    max_wait_notice_enabled: bool = True
    max_wait_notice_seconds: int = Field(default=7_200, ge=60, le=2_592_000)

    #: Выдавать роутеру бессрочное разрешение вместо разрешения на каждую
    #: сделку. Бессрочное экономит газ; ограниченное безопаснее, если
    #: контракт роутера окажется скомпрометирован.
    approve_unlimited: bool = True

    #: Период отчёта о сделках в Telegram.
    report_interval_seconds: int = Field(default=1_800, ge=60, le=86_400)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Включённая торговля обязана иметь ключ.

        Иначе подсистема поднялась бы «готовой к работе» и упала бы на
        первой же сделке — то есть ровно тогда, когда возможность уже
        найдена и время дорого.
        """
        if self.enabled and self.private_key is None:
            raise ValueError(
                "trading is enabled but no private_key reference is configured: "
                "execution would fail at the first opportunity"
            )
        if self.min_exit_profit <= Decimal(0):
            raise ValueError("min_exit_profit must be positive")
        return self
