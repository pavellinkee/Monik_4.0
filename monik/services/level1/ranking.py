"""Ранжирование кандидатов при нехватке ёмкости Level 2.

Если кандидатов больше доступной ёмкости, они ранжируются по ожидаемой
привлекательности (``02_LEVEL1_SCANNER.md`` §49-50). Ранжирование
детерминировано: при равных финансовых показателях порядок задаётся ключом
группы, а не порядком выполнения корутин (``02_LEVEL1_SCANNER.md`` §94).
"""

from __future__ import annotations

from decimal import Decimal

from monik.domain.models.opportunity import Candidate
from monik.services.level1.grouping import CandidateGroup

__all__ = ["rank_candidates", "rank_groups"]

#: Значение для группы без рассчитанной метрики: такая группа не должна
#: вытеснять группу с подтверждённым результатом.
_NO_METRIC = Decimal("-1e30")


def _best_net_roi(group: CandidateGroup) -> Decimal:
    """Максимальный предварительный net ROI внутри группы."""
    values = [
        candidate.preliminary_result.net_roi.value
        for candidate in group.candidates
        if candidate.preliminary_result.net_roi is not None
    ]
    return max(values) if values else _NO_METRIC


def _best_net_profit(group: CandidateGroup) -> Decimal:
    """Максимальная предварительная абсолютная прибыль внутри группы."""
    values = [
        candidate.preliminary_result.net_profit
        for candidate in group.candidates
        if candidate.preliminary_result.net_profit is not None
    ]
    return max(values) if values else _NO_METRIC


def rank_groups(
    groups: tuple[CandidateGroup, ...], *, by_profit: bool = False
) -> tuple[CandidateGroup, ...]:
    """Отсортировать группы по убыванию привлекательности.

    ``by_profit`` меняет главный критерий с доходности в процентах на
    абсолютную прибыль в базовом токене. Это нужно торговому режиму:
    когда проход сканирует несколько сумм сразу, выбирать надо ту, что
    даёт больше денег, а не больший процент — процент на малой сумме
    может быть выше, а заработок меньше (``the_main_rules.md``,
    правило 11).
    """
    if by_profit:
        return tuple(
            sorted(
                groups,
                key=lambda group: (
                    -_best_net_profit(group),
                    -_best_net_roi(group),
                    group.group_key,
                ),
            )
        )
    return tuple(
        sorted(
            groups,
            key=lambda group: (-_best_net_roi(group), -_best_net_profit(group), group.group_key),
        )
    )


def rank_candidates(
    groups: tuple[CandidateGroup, ...], *, by_profit: bool = False
) -> tuple[Candidate, ...]:
    """Кандидаты всех групп подряд, в порядке привлекательности.

    :func:`rank_groups` упорядочивает **группы**, но внутри группы
    кандидаты остаются в порядке сканирования. Для торгового режима это
    не годится: группа — это пара «токен и агрегаторы», а различаются
    кандидаты внутри неё суммой. Доходность в процентах у разных сумм
    почти одинакова, а заработок отличается кратно, и порядок сканирования
    поставил бы меньшую сумму впереди большей.

    Поэтому при выборе по заработку сортируются и кандидаты внутри группы
    (``the_main_rules.md``, правило 11: выбирается наибольший заработок в
    базовом токене).
    """
    ordered = rank_groups(groups, by_profit=by_profit)
    if not by_profit:
        return tuple(candidate for group in ordered for candidate in group.candidates)
    return tuple(
        candidate
        for group in ordered
        for candidate in sorted(group.candidates, key=_candidate_order)
    )


def _candidate_order(candidate: Candidate) -> tuple[Decimal, Decimal, int]:
    """Ключ сортировки кандидата: сначала заработок, потом доходность.

    Сумма входит в ключ последней и только ради определённости: при
    равном заработке и равной доходности порядок не должен зависеть от
    того, в каком порядке пришли ответы.
    """
    result = candidate.preliminary_result
    return (
        -(result.net_profit or Decimal(0)),
        -(result.net_roi.value if result.net_roi is not None else Decimal(0)),
        candidate.buy_quote.input_amount.raw,
    )
