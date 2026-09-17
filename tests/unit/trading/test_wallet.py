"""Торговый счёт: адрес наружу, ключ — никогда.

Ключ для тестов создаётся **во время прогона**, а не записывается в файл:
в репозитории не должно быть ключевого материала ни в каком виде, даже
заведомо пустого (``32_SECURITY.md`` §68). За этим следит отдельный тест
``test_repository_contains_no_secret_values``.
"""

from __future__ import annotations

import json

import pytest
from eth_account import Account

from monik.config.secrets import SecretValue
from monik.domain.errors import ConfigurationError
from monik.services.observability.redaction import SecretRegistry, redact_text
from monik.services.trading import TradingWallet


@pytest.fixture
def key() -> str:
    """Свежий ключ, существующий только в памяти прогона."""
    return str(Account.create().key.hex())


def _secret(value: str) -> SecretValue:
    return SecretValue("MONIK_TRADING_PRIVATE_KEY", value)


class TestAddress:
    def test_address_is_derived_from_the_key(self, key: str) -> None:
        assert TradingWallet(_secret(key)).address == Account.from_key(key).address

    def test_address_is_stable_for_the_same_key(self, key: str) -> None:
        assert TradingWallet(_secret(key)).address == TradingWallet(_secret(key)).address

    def test_invalid_key_is_rejected_as_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="not a valid secp256k1 key"):
            TradingWallet(_secret("не ключ"))

    def test_error_text_never_carries_the_key(self) -> None:
        """Библиотека кладёт ключ в текст исключения — наружу он не идёт."""
        malformed = "0x" + "ab" * 31  # на байт короче настоящего
        with pytest.raises(ConfigurationError) as error:
            TradingWallet(_secret(malformed))
        assert "ab" * 31 not in str(error.value)


class TestSecrecy:
    def test_repr_shows_only_the_address(self, key: str) -> None:
        wallet = TradingWallet(_secret(key))
        assert repr(wallet) == f"TradingWallet(address={wallet.address})"
        assert key.removeprefix("0x") not in repr(wallet)

    def test_key_is_not_reachable_through_attributes(self, key: str) -> None:
        """У класса нет ни одного поля, отдающего ключ наружу."""
        wallet = TradingWallet(_secret(key))
        assert not hasattr(wallet, "__dict__"), "слоты не дают появиться новым полям"
        exposed = json.dumps(
            {
                name: str(getattr(wallet, name, ""))
                for name in dir(wallet)
                if not name.startswith("_")
            }
        )
        assert key.removeprefix("0x") not in exposed

    def test_registered_key_is_scrubbed_from_any_text(self, key: str) -> None:
        """Ключ регистрируется как секрет и вычёркивается из вывода."""
        registry = SecretRegistry()
        registry.register(key)
        assert key not in redact_text(f"ключ {key} в тексте", registry=registry)


class TestSigning:
    def test_signed_transaction_is_bytes(self, key: str) -> None:
        wallet = TradingWallet(_secret(key))
        raw = wallet.sign_transaction(
            {
                "to": wallet.address,
                "value": 0,
                "gas": 21_000,
                "maxFeePerGas": 1_000_000_000,
                "maxPriorityFeePerGas": 0,
                "nonce": 0,
                "chainId": 137,
                "data": b"",
            }
        )
        assert isinstance(raw, bytes)
        assert raw, "подписанная транзакция не может быть пустой"

    def test_signature_is_recoverable_to_our_address(self, key: str) -> None:
        """Подпись действительно принадлежит нашему счёту."""
        wallet = TradingWallet(_secret(key))
        transaction = {
            "to": wallet.address,
            "value": 0,
            "gas": 21_000,
            "maxFeePerGas": 1_000_000_000,
            "maxPriorityFeePerGas": 0,
            "nonce": 7,
            "chainId": 137,
            "data": b"",
        }
        raw = wallet.sign_transaction(transaction)
        assert Account.recover_transaction(raw) == wallet.address
