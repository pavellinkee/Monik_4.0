"""Управление сканером во время работы.

Оператор должен уметь остановить и снова запустить сканирование, не
подключаясь к серверу. Управление намеренно устроено просто:

* **остановка** запрещает начинать новые циклы Level 1. Уже принятые
  Level 2 Job доводятся до конца — прерывать безопасную операцию ради
  освобождения ресурса нельзя (``05_RESOURCE_MANAGER.md`` §18);
* **запуск** снова разрешает циклы;
* **перезапуск** — это остановка процесса с отдельным кодом возврата.
  Поднимает процесс обратно менеджер служб, а не приложение: собственного
  механизма перезапуска Monik не заводит.

Состояние живёт только в памяти: после рестарта сканер снова разрешён.
Иначе одна команда «Остановить» тихо выключила бы сканер навсегда.
"""

from __future__ import annotations

import asyncio

from monik.domain.enums.control import ScannerRunState
from monik.services.observability.logging import get_logger, log_fields

__all__ = ["RESTART_EXIT_CODE", "ScannerSwitch"    "TradingSwitch",
]

_LOGGER = get_logger("app.control")

#: Код возврата, которым процесс сообщает о запрошенном перезапуске.
#: Менеджер служб обязан быть настроен на автоматический перезапуск
#: (``24_DEPLOYMENT.md``): без этого команда просто остановит приложение.
RESTART_EXIT_CODE = 3


class ScannerSwitch:
    """Разрешает или запрещает новые циклы сканирования."""

    def __init__(self) -> None:
        self._paused = False
        self._restart_requested = asyncio.Event()

    # --- состояние ---------------------------------------------------------

    def state(self) -> ScannerRunState:
        """Текущее состояние сканирования."""
        if self._restart_requested.is_set():
            return ScannerRunState.RESTARTING
        return ScannerRunState.PAUSED if self._paused else ScannerRunState.RUNNING

    @property
    def is_running(self) -> bool:
        """Разрешено ли начинать новый цикл."""
        return not self._paused and not self._restart_requested.is_set()

    @property
    def restart_requested(self) -> bool:
        """Запрошен ли перезапуск процесса."""
        return self._restart_requested.is_set()

    async def wait_for_restart(self) -> None:
        """Дождаться запроса на перезапуск."""
        await self._restart_requested.wait()

    # --- команды -----------------------------------------------------------

    def start(self) -> bool:
        """Разрешить сканирование. ``True``, если состояние изменилось."""
        if not self._paused:
            return False
        self._paused = False
        _LOGGER.info("scanning resumed", extra=log_fields(state=self.state().value))
        return True

    def stop(self) -> bool:
        """Запретить новые циклы. ``True``, если состояние изменилось."""
        if self._paused:
            return False
        self._paused = True
        _LOGGER.warning("scanning paused by operator", extra=log_fields(state=self.state().value))
        return True

    def request_restart(self) -> None:
        """Запросить перезапуск процесса."""
        if self._restart_requested.is_set():
            return
        self._restart_requested.set()
        _LOGGER.warning("restart requested by operator", extra=log_fields(state="restarting"))


class TradingSwitch:
    """Разрешение подсистемы исполнения тратить деньги.

    Два уровня, и оба обязательны. Конфигурация говорит, **можно ли
    вообще**: выключенная там торговля не включается ничем. Оператор
    говорит, **начинать ли сейчас**.

    После запуска процесса переключатель всегда выключен, даже если
    конфигурация торговлю разрешает. Это сознательно: перезапуск —
    момент, когда состояние счёта и рынка неизвестно, и возобновлять
    траты без ведома человека нельзя. Ведение уже открытых сделок при
    этом не останавливается — иначе перезапуск бросал бы купленные
    токены.
    """

    __slots__ = ("_allowed", "_started")

    def __init__(self, *, allowed: bool) -> None:
        self._allowed = allowed
        self._started = False

    @property
    def allowed(self) -> bool:
        """Разрешена ли торговля конфигурацией."""
        return self._allowed

    @property
    def is_open(self) -> bool:
        """Можно ли открывать новые сделки прямо сейчас."""
        return self._allowed and self._started

    def start(self) -> bool:
        """Разрешить открытие сделок. ``True``, если состояние изменилось."""
        if not self._allowed or self._started:
            return False
        self._started = True
        _LOGGER.warning("trading started by operator", extra=log_fields(state="trading"))
        return True

    def stop(self) -> bool:
        """Запретить открытие новых сделок.

        Уже открытые сделки продолжают вестись: остановка касается трат,
        а не брошенных денег.
        """
        if not self._started:
            return False
        self._started = False
        _LOGGER.warning("trading stopped by operator", extra=log_fields(state="paused"))
        return True

    def state(self) -> str:
        """Состояние для оператора."""
        if not self._allowed:
            return "запрещена конфигурацией"
        return "идёт" if self._started else "разрешена, но не запущена"
