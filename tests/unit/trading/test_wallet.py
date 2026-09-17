"""Торговый счёт: адрес наружу, ключ — никогда."""

from __future__ import annotations

import json

import pytest

from monik.config.secrets import SecretValue
from monik.domain.errors import ConfigurationError
from monik.services.observability.redaction import SecretRegistry, redact_text
from monik.services.trading import TradingWallet

#: Ключ из документации ethereum, публично известный и денег не держит.
KEY = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
ADDRESS = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"


def _secret(value: str = KEY) -> SecretValue:
    return SecretValue("MONIK_TRADING_PRIVATE_KEY", value)


class TestAddress:
    def test_address_is_derived_from_the_key(self) -> None:
        assert TradingWallet(_secret()).address == ADDRESS

    def test_invalid_key_is_rejected_as_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="not a valid secp256k1 key"):
            TradingWallet(_secret("не ключ"))

    def test_error_text_never_carries_the_key(self) -> None:
        """Библиотека кладёт ключ в текст исключения — наружу он не идёт."""
        bad = "0x" + "ab" * 31
        with pytest.raises(ConfigurationError) as error:
            TradingWallet(_secret(bad))
        assert bad not in str(error.value)
        assert "ab" * 31 not in str(error.value)


class TestSecrecy:
    def test_repr_shows_only_the_address(self) -> None:
        wallet = TradingWallet(_secret())
        assert repr(wallet) == f"TradingWallet(address={ADDRESS})"
        assert KEY not in repr(wallet)

    def test_key_is_not_reachable_through_attributes(self) -> None:
        """У класса нет ни одного поля, отдающего ключ наружу."""
        wallet = TradingWallet(_secret())
        assert not hasattr(wallet, "__dict__"), "слоты не дают появиться новым полям"
        exposed = json.dumps(
            {
                name: str(getattr(wallet, name, ""))
                for name in dir(wallet)
                if not name.startswith("_")
            }
        )
        assert KEY not in exposed

    def test_registered_key_is_scrubbed_from_any_text(self) -> None:
        """Ключ регистрируется как секрет и вычёркивается из вывода."""
        registry = SecretRegistry()
        registry.register(KEY)
        assert KEY not in redact_text(f"ключ {KEY} в тексте", registry=registry)


class TestSigning:
    def test_signed_transaction_is_bytes_and_hides_nothing_extra(self) -> None:
        wallet = TradingWallet(_secret())
        raw = wallet.sign_transaction(
            {
                "to": ADDRESS,
                "value": 0,
                "gas": 21_000,
                "maxFeePerGas": 1_000_000_000,
                "maxPriorityFeePerGas": 0,
                "nonce": 0,
                "chainId": 42161,
                "data": b"",
            }
        )
        assert isinstance(raw, bytes)
        assert raw, "подписанная транзакция не может быть пустой"
