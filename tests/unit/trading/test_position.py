"""Сделка режима ann: что она знает о себе.

Позиция — единственное место, где Monik держит деньги, поэтому её
инварианты строгие: незавершённая сделка не имеет результата, а
подставлять вместо него ноль нельзя (``CLAUDE.md`` §12).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from monik.domain.enums.providers import ProviderId
from monik.domain.enums.trading import PositionStatus
from monik.domain.models.position import Position
from monik.domain.value_objects.identifiers import TId
from tests import factories as f


def _position(**overrides: object) -> Position:
    base: dict[str, object] = {
        "t_id": TId.from_sequence(1),
        "network_id": f.POLYGON,
        "status": PositionStatus.BUYING,
        "base_token": f.USDT,
        "target_token": f.AAVE,
        "buy_provider_id": ProviderId.UNISWAP,
        "sell_provider_id": ProviderId.UNISWAP,
        "raw_input": 50_000_000,
        "opened_at": f.NOW,
    }
    base.update(overrides)
    return Position(**base)  # type: ignore[arg-type]


class TestLifecycle:
    def test_open_statuses_require_further_action(self) -> None:
        for status in (PositionStatus.BUYING, PositionStatus.HOLDING, PositionStatus.SELLING):
            assert status.is_open
        for status in (PositionStatus.CLOSED, PositionStatus.FAILED):
            assert not status.is_open

    def test_only_two_statuses_hold_tokens(self) -> None:
        holding = {s for s in PositionStatus if s.holds_tokens}
        assert holding == {PositionStatus.HOLDING, PositionStatus.SELLING}

    def test_holding_without_a_known_amount_is_rejected(self) -> None:
        """Держать токен и не знать сколько — недопустимое состояние."""
        with pytest.raises(ValidationError, match="how many were acquired"):
            _position(status=PositionStatus.HOLDING)

    def test_closed_without_a_return_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="how much came back"):
            _position(status=PositionStatus.CLOSED, raw_acquired=1)


class TestResult:
    def test_unfinished_trade_has_no_result(self) -> None:
        """Незавершённая сделка не имеет итога, и ноль ей не подставляется."""
        position = _position(status=PositionStatus.HOLDING, raw_acquired=49_900_000)
        assert position.gross_result_raw is None
        assert position.net_result_raw is None
        assert position.net_result is None

    def test_profit_is_the_difference_in_base_token(self) -> None:
        position = _position(
            status=PositionStatus.CLOSED,
            raw_acquired=49_900_000,
            raw_returned=50_020_000,
            raw_gas_cost=0,
        )
        assert position.gross_result_raw == 20_000
        assert position.net_result_raw == 20_000
        assert position.net_result == Decimal("0.02")

    def test_loss_is_negative(self) -> None:
        position = _position(
            status=PositionStatus.CLOSED,
            raw_acquired=49_900_000,
            raw_returned=49_980_000,
            raw_gas_cost=0,
        )
        assert position.net_result == Decimal("-0.02")

    def test_gas_turns_a_winning_circle_into_a_loss(self) -> None:
        """Выигрыш на цене меньше стоимости пары транзакций — это убыток.

        Круг выиграл 0.015 USDT на курсе и потратил 0.018 на газ. Разница
        токенов положительна, заработок — нет, и итогом считается второе.
        """
        position = _position(
            status=PositionStatus.CLOSED,
            raw_acquired=49_900_000,
            raw_returned=50_015_000,
            raw_gas_cost=18_000,
        )
        assert position.gross_result == Decimal("0.015")
        assert position.net_result == Decimal("-0.003")

    def test_unknown_gas_leaves_the_result_unknown(self) -> None:
        """Неизвестный расход не считается нулём (``CLAUDE.md`` §12)."""
        position = _position(
            status=PositionStatus.CLOSED, raw_acquired=49_900_000, raw_returned=50_020_000
        )
        assert position.gross_result_raw == 20_000
        assert position.net_result_raw is None
        assert position.net_result is None

    def test_hypothetical_exit_is_measured_against_the_input(self) -> None:
        position = _position(status=PositionStatus.HOLDING, raw_acquired=49_900_000)
        assert position.profit_if_sold_for(50_010_000) == 10_000
        assert position.profit_if_sold_for(49_990_000) == -10_000

    def test_hypothetical_exit_subtracts_the_costs_it_is_given(self) -> None:
        position = _position(status=PositionStatus.HOLDING, raw_acquired=49_900_000)
        assert position.profit_if_sold_for(50_010_000, raw_costs=4_000) == 6_000


class TestConsistency:
    def test_tokens_must_belong_to_the_position_network(self) -> None:
        other = f.USDT.model_copy(update={"network_id": f.NetworkId("arbitrum")})
        with pytest.raises(ValidationError, match="another network"):
            _position(base_token=other)

    def test_circle_between_the_same_token_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="two different tokens"):
            _position(target_token=f.USDT)
