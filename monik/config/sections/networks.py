"""Конфигурация сетей."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from monik.config.base import ConfigSection
from monik.domain.value_objects.identity import NetworkId, TokenAddress, TokenSymbol

__all__ = ["NetworkConfig"]


class NetworkConfig(ConfigSection):
    """Параметры одной сети.

    Сеть не зашита в scanner logic и приходит отсюда
    (``01_PROJECT_REQUIREMENTS.md`` §6). Конфигурация одной сети не
    применяется к другой (``17_CONFIGURATION.md`` §24).
    """

    network_id: NetworkId
    name: str = Field(min_length=1, max_length=64)
    chain_id: int = Field(gt=0)
    native_token_symbol: TokenSymbol
    native_token_decimals: int = Field(default=18, ge=0, le=36)
    wrapped_native_address: TokenAddress
    #: Базовый токен round-trip этой сети: вход и выход цикла.
    #:
    #: Принадлежит сети, а не сканеру: адрес токена network-specific
    #: (``01_PROJECT_REQUIREMENTS.md`` §10), а конфигурация одной сети не
    #: применяется к другой (``17_CONFIGURATION.md`` §24). Единая
    #: настройка на весь сканер означала бы, что вторая сеть считает
    #: круг от чужого контракта.
    base_token_address: TokenAddress
    rpc_url: str | None = Field(default=None, max_length=512)
    #: Запасные узлы, в порядке обращения. Основной — :attr:`rpc_url`.
    #:
    #: Узел — внешняя служба, и публичные узлы отказывают. Пока адрес
    #: один, такой отказ останавливает всё, что зависит от цепи, включая
    #: подбор квитанции уже отправленной покупки: деньги потрачены, а
    #: узнать их судьбу нечем.
    rpc_fallback_urls: tuple[str, ...] = ()
    #: Значок сети в уведомлении. Свойство сети, а не формата сообщения.
    emoji: str | None = Field(default=None, min_length=1, max_length=8)
    enabled: bool = True

    #: Надбавка к базовой цене газа, в wei. Свойство **сети**, а не
    #: приложения: устроены сети по-разному.
    #:
    #: В Polygon за место в блоке идёт торг, и транзакция без заметной
    #: надбавки может не попасть в блок вовсе. В Arbitrum торга нет:
    #: все транзакции просят надбавку ноль и платят ровно базовую цену,
    #: а базовая цена там на три порядка ниже. Одно общее значение
    #: означало бы либо застрявшие транзакции в одной сети, либо
    #: многократную переплату в другой.
    #:
    #: Ноль по умолчанию выбран сознательно: сеть, которой надбавка
    #: нужна, обязана назвать её явно. Заниженная надбавка задержит
    #: транзакцию — это видно; завышенная молча потратит деньги.
    priority_fee_wei: int = Field(default=0, ge=0, le=10**13)

    @property
    def rpc_endpoints(self) -> tuple[str, ...]:
        """Узлы сети в порядке обращения: основной первым."""
        if self.rpc_url is None:
            return ()
        return (self.rpc_url, *self.rpc_fallback_urls)

    @model_validator(mode="after")
    def _validate_rpc(self) -> Self:
        """RPC endpoint обязан использовать HTTPS (``32_SECURITY.md``)."""
        for url in (self.rpc_url, *self.rpc_fallback_urls):
            if url is not None and not url.startswith("https://"):
                raise ValueError("rpc_url must use https")
        if self.rpc_fallback_urls and self.rpc_url is None:
            raise ValueError(
                "rpc_fallback_urls are configured without rpc_url: a fallback without a "
                "primary endpoint is a typo, not a configuration"
            )
        if len(set(self.rpc_endpoints)) != len(self.rpc_endpoints):
            raise ValueError("rpc endpoints must be unique: a repeated address is not a fallback")
        return self
