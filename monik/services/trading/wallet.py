"""Торговый счёт: ключ и адрес.

Ключ живёт ровно в одном месте — в файле секретов службы — и попадает
сюда через тот же механизм ссылок на environment, что и ключи
агрегаторов (``17_CONFIGURATION.md`` §26, ``32_SECURITY.md`` §3).

Наружу класс отдаёт **только адрес**. Сам ключ не возвращается ни одним
методом, не попадает в ``repr`` и не может быть случайно записан в лог:
подпись выполняется внутри.
"""

from __future__ import annotations

from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

from monik.config.secrets import SecretValue
from monik.domain.errors import ConfigurationError

__all__ = ["TradingWallet"]


class TradingWallet:
    """Счёт, от имени которого отправляются сделки."""

    __slots__ = ("_account",)

    def __init__(self, private_key: SecretValue) -> None:
        try:
            account: LocalAccount = Account.from_key(private_key.get())
        except (ValueError, TypeError):
            # Текст исключения библиотеки может содержать сам ключ,
            # поэтому наружу он не выпускается.
            raise ConfigurationError(
                "trading private key is not a valid secp256k1 key",
                code="trading_key_invalid",
            ) from None
        self._account = account

    @property
    def address(self) -> str:
        """Публичный адрес счёта в формате EIP-55."""
        return str(self._account.address)

    def sign_transaction(self, transaction: dict[str, Any]) -> bytes:
        """Подписать транзакцию и вернуть её в виде байтов для отправки.

        Адреса приводятся к контрольному регистру EIP-55. Внутри Monik
        адрес канонически хранится в нижнем регистре — так он сравнивается
        без оговорок, — а библиотека подписи принимает только запись с
        контрольной суммой. Это различие форматов и остаётся здесь, у
        границы с библиотекой, а не расползается по вызывающему коду.
        """
        prepared = dict(transaction)
        for field in ("to", "from"):
            value = prepared.get(field)
            if isinstance(value, str):
                prepared[field] = to_checksum_address(value)
        signed = self._account.sign_transaction(prepared)
        return bytes(signed.raw_transaction)

    def __repr__(self) -> str:
        """Представление без секрета: только адрес."""
        return f"TradingWallet(address={self.address})"
