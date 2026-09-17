"""Кодирование разрешения и его смысл."""

from __future__ import annotations

import pytest

from monik.domain.models.execution import AllowanceKind, AllowanceRequirement
from monik.services.trading import ApprovalRequest, encode_approve, encode_permit2_approve
from monik.services.trading.approvals import (
    ERC20_APPROVE_SELECTOR,
    PERMIT2_APPROVE_SELECTOR,
    PERMIT2_FOREVER,
    PERMIT2_UNLIMITED,
    UNLIMITED,
)
from tests import factories as f

SPENDER = "0x000000000022D473030F116dDEE9F6B43aC78BA3"


class TestEncoding:
    def test_call_is_selector_plus_two_words(self) -> None:
        data = encode_approve(SPENDER, 1)
        assert data.startswith(ERC20_APPROVE_SELECTOR)
        assert len(data) == 2 + 8 + 64 * 2, "селектор и ровно два машинных слова"

    def test_spender_is_left_padded_and_lowercased(self) -> None:
        data = encode_approve(SPENDER, 0)
        word = data[10:74]
        assert word == SPENDER[2:].lower().rjust(64, "0")

    def test_unlimited_is_all_ones(self) -> None:
        data = encode_approve(SPENDER, UNLIMITED)
        assert data[74:] == "f" * 64

    def test_short_address_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="20-byte address"):
            encode_approve("0x1234", 1)

    def test_amount_beyond_uint256_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uint256"):
            encode_approve(SPENDER, UNLIMITED + 1)


ROUTER = "0xA51afAFe0263b40EdaEf0Df8781eA9aa03E381a3"


def _erc20() -> AllowanceRequirement:
    return AllowanceRequirement(
        kind=AllowanceKind.ERC20, contract=str(f.USDT.address), spender=SPENDER
    )


def _permit2() -> AllowanceRequirement:
    return AllowanceRequirement(
        kind=AllowanceKind.PERMIT2, contract=SPENDER, spender=ROUTER
    )


class TestPermit2Encoding:
    """Второе разрешение — особенность Uniswap, а не общее правило."""

    def test_call_is_selector_plus_four_words(self) -> None:
        data = encode_permit2_approve(str(f.USDT.address), ROUTER)
        assert data.startswith(PERMIT2_APPROVE_SELECTOR)
        assert len(data) == 2 + 8 + 64 * 4

    def test_amount_and_expiration_are_at_their_limits(self) -> None:
        data = encode_permit2_approve(str(f.USDT.address), ROUTER)
        amount = int(data[138:202], 16)
        expiration = int(data[202:266], 16)
        assert amount == PERMIT2_UNLIMITED, "предел uint160"
        assert expiration == PERMIT2_FOREVER, "предел uint48 — это и есть «бессрочно»"

    def test_amount_beyond_uint160_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uint160"):
            encode_permit2_approve(str(f.USDT.address), ROUTER, amount=PERMIT2_UNLIMITED + 1)

    def test_expiration_beyond_uint48_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="uint48"):
            encode_permit2_approve(str(f.USDT.address), ROUTER, expiration=PERMIT2_FOREVER + 1)


class TestRequest:
    def test_erc20_request_calls_the_token(self) -> None:
        request = ApprovalRequest(token=f.USDT, requirement=_erc20())
        assert request.to == str(f.USDT.address)
        assert request.calldata == encode_approve(SPENDER, UNLIMITED)

    def test_permit2_request_calls_permit2_not_the_token(self) -> None:
        request = ApprovalRequest(token=f.USDT, requirement=_permit2())
        assert request.to == SPENDER
        assert request.calldata == encode_permit2_approve(str(f.USDT.address), ROUTER)

    def test_description_names_the_token_and_both_sides(self) -> None:
        described = ApprovalRequest(token=f.USDT, requirement=_permit2()).describe()
        assert "USDT" in described
        assert SPENDER in described
        assert ROUTER in described
