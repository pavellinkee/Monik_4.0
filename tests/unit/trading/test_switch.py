"""Разрешение тратить деньги: два уровня, и оба обязательны."""

from __future__ import annotations

from monik.app.control import TradingSwitch


class TestTwoLevels:
    def test_configuration_forbids_absolutely(self) -> None:
        """Запрет в конфигурации не снимается командой оператора."""
        switch = TradingSwitch(allowed=False)

        assert switch.start() is False
        assert switch.is_open is False
        assert switch.state() == "запрещена конфигурацией"

    def test_permission_alone_does_not_start_trading(self) -> None:
        """Разрешено — ещё не значит идёт."""
        switch = TradingSwitch(allowed=True)

        assert switch.allowed is True
        assert switch.is_open is False
        assert switch.state() == "разрешена, но не запущена"

    def test_operator_starts_and_stops(self) -> None:
        switch = TradingSwitch(allowed=True)

        assert switch.start() is True
        assert switch.is_open is True
        assert switch.state() == "идёт"
        assert switch.stop() is True
        assert switch.is_open is False

    def test_repeated_commands_change_nothing(self) -> None:
        switch = TradingSwitch(allowed=True)
        switch.start()

        assert switch.start() is False, "повторный запуск не считается изменением"
        switch.stop()
        assert switch.stop() is False


class TestRestartSafety:
    def test_a_fresh_switch_is_always_closed(self) -> None:
        """После перезапуска траты не возобновляются сами.

        Перезапуск — момент, когда состояние счёта и рынка неизвестно.
        """
        assert TradingSwitch(allowed=True).is_open is False
