"""Настройки подсистемы исполнения.

Пороги выхода — деньги, а не проценты, и их два. Основной действует на
проверке сразу после покупки: шанс выйти в плюс наивысший именно тогда.
Пониженный — у сделки, ушедшей в ожидание: деньги уже заперты в позиции,
и выйти из них выгоднее, чем ждать прежней прибыли неопределённо долго.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from monik.config.sections.trading import TradingConfig


class TestExitThresholds:
    def test_waiting_threshold_defaults_below_the_main_one(self) -> None:
        config = TradingConfig()

        assert config.min_exit_profit == Decimal("0.01")
        assert config.min_exit_profit_waiting == Decimal("0.005")

    def test_waiting_threshold_may_not_exceed_the_main_one(self) -> None:
        """Порог ожидания — уступка, а не ужесточение.

        Если бы он был выше основного, сделка, не проданная сразу,
        требовала бы для выхода большей прибыли, чем сразу после покупки.
        """
        with pytest.raises(ValidationError, match="must not exceed min_exit_profit"):
            TradingConfig(min_exit_profit=Decimal("0.01"), min_exit_profit_waiting=Decimal("0.02"))

    def test_equal_thresholds_are_allowed(self) -> None:
        """Одинаковые пороги — это «уступки нет», и это допустимо."""
        config = TradingConfig(
            min_exit_profit=Decimal("0.01"), min_exit_profit_waiting=Decimal("0.01")
        )

        assert config.min_exit_profit_waiting == config.min_exit_profit

    def test_zero_waiting_threshold_is_rejected(self) -> None:
        """Ноль означал бы продажу в любой момент, лишь бы не в минус."""
        with pytest.raises(ValidationError):
            TradingConfig(min_exit_profit_waiting=Decimal("0"))


class TestKey:
    def test_enabled_trading_requires_a_key(self) -> None:
        with pytest.raises(ValidationError, match="no private_key reference"):
            TradingConfig(execution_enabled=True)
