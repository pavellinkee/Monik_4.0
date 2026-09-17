"""Разрешения роутеру списывать токены.

Контракт биржи не может взять токен сам: владелец заранее разрешает ему
списание. Это отдельная транзакция, и без неё обмен откатывается —
именно так выглядела первая симуляция.

Вызов ``approve`` стандартен для ERC-20 и особенностью агрегатора не
является, поэтому живёт здесь, а не в адаптере. Особенность агрегатора —
**кому** выдавать разрешение: у Uniswap это Permit2, а не сам роутер.
Этот адрес приходит вместе с транзакцией обмена
(:attr:`SwapTransaction.spender`), и общая логика о нём не знает.
"""

from __future__ import annotations

from dataclasses import dataclass

from monik.domain.models.execution import AllowanceKind, AllowanceRequirement
from monik.domain.models.token import Token

__all__ = [
    "ERC20_APPROVE_SELECTOR",
    "PERMIT2_APPROVE_SELECTOR",
    "UNLIMITED",
    "ApprovalRequest",
    "encode_approve",
    "encode_permit2_approve",
]

#: Селектор ``approve(address,uint256)``.
ERC20_APPROVE_SELECTOR = "0x095ea7b3"

#: Селектор ``approve(address,address,uint160,uint48)`` контракта Permit2.
PERMIT2_APPROVE_SELECTOR = "0x87517c45"

#: Предел суммы у Permit2: аргумент объявлен как ``uint160``.
PERMIT2_UNLIMITED = (1 << 160) - 1

#: Предел срока у Permit2: аргумент объявлен как ``uint48``. Это и есть
#: «бессрочно» в его понятиях — дальше числа не бывает.
PERMIT2_FOREVER = (1 << 48) - 1

#: Разрешение без ограничения суммы: 2**256 - 1.
#:
#: Бессрочное разрешение экономит газ — иначе транзакция ``approve``
#: понадобилась бы перед каждой сделкой. Плата за это: если контракт
#: получателя окажется скомпрометирован, он сможет забрать весь остаток
#: этого токена. Поэтому режим работает на отдельном счёте.
UNLIMITED = (1 << 256) - 1


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Что именно разрешается и кому."""

    token: Token
    requirement: AllowanceRequirement

    @property
    def to(self) -> str:
        """Контракт, которому адресуется вызов."""
        return self.requirement.contract

    @property
    def calldata(self) -> str:
        """Закодированный вызов нужного вида."""
        if self.requirement.kind is AllowanceKind.PERMIT2:
            return encode_permit2_approve(
                str(self.token.address), self.requirement.spender
            )
        return encode_approve(self.requirement.spender, UNLIMITED)

    def describe(self) -> str:
        """Строка для журнала и для подтверждения оператором."""
        return f"{self.token.symbol}: {self.requirement.describe()}, без ограничения"


def encode_approve(spender: str, amount: int) -> str:
    """Собрать вызов ``approve(address,uint256)``.

    Кодирование стандартное и намеренно сделано вручную: ради одного
    вызова с двумя аргументами тянуть ABI-кодировщик незачем, а ошибиться
    здесь негде — оба аргумента укладываются в машинное слово.
    """
    if not spender.startswith("0x") or len(spender) != 42:
        raise ValueError("spender must be a 20-byte address")
    if amount < 0 or amount > UNLIMITED:
        raise ValueError("approve amount does not fit into uint256")
    return (
        ERC20_APPROVE_SELECTOR
        + spender[2:].lower().rjust(64, "0")
        + format(amount, "x").rjust(64, "0")
    )


def encode_permit2_approve(
    token: str,
    spender: str,
    amount: int = PERMIT2_UNLIMITED,
    expiration: int = PERMIT2_FOREVER,
) -> str:
    """Собрать вызов ``approve(address,address,uint160,uint48)`` у Permit2.

    Второе разрешение — особенность Uniswap: токен разрешает списание
    контракту Permit2, а Permit2 отдельно разрешает списание роутеру. Без
    него обмен откатывается с ``AllowanceExpired(0)``.
    """
    for address in (token, spender):
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("permit2 approve needs 20-byte addresses")
    if not 0 <= amount <= PERMIT2_UNLIMITED:
        raise ValueError("permit2 amount does not fit into uint160")
    if not 0 <= expiration <= PERMIT2_FOREVER:
        raise ValueError("permit2 expiration does not fit into uint48")
    return (
        PERMIT2_APPROVE_SELECTOR
        + token[2:].lower().rjust(64, "0")
        + spender[2:].lower().rjust(64, "0")
        + format(amount, "x").rjust(64, "0")
        + format(expiration, "x").rjust(64, "0")
    )
