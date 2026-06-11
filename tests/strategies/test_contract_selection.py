from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from options_quant.data.models import (
    OptionChain,
    OptionContract,
    OptionGreek,
    OptionImpliedVolatility,
    OptionType,
)
from options_quant.strategies import (
    ContractSelectionEngine,
    OptionSelectionCandidate,
    OptionSelectionQuery,
)

AS_OF = date(2026, 6, 10)
OBSERVED_AT = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
SPOT = Decimal("100")


def make_contract(
    *,
    expiration: date,
    strike: Decimal,
    option_type: OptionType = OptionType.PUT,
) -> OptionContract:
    return OptionContract(
        underlying_symbol="AAPL",
        expiration=expiration,
        strike=strike,
        option_type=option_type,
    )


PUT_30_DTE = make_contract(expiration=date(2026, 7, 10), strike=Decimal("95"))
PUT_40_DTE = make_contract(expiration=date(2026, 7, 20), strike=Decimal("90"))
PUT_45_DTE = make_contract(expiration=date(2026, 7, 25), strike=Decimal("88"))
PUT_60_DTE = make_contract(expiration=date(2026, 8, 9), strike=Decimal("80"))
CALL_45_DTE = make_contract(
    expiration=date(2026, 7, 25),
    strike=Decimal("110"),
    option_type=OptionType.CALL,
)


def make_engine() -> ContractSelectionEngine:
    chain = OptionChain(
        underlying_symbol="AAPL",
        timestamp=OBSERVED_AT,
        contracts=(PUT_30_DTE, PUT_40_DTE, PUT_45_DTE, PUT_60_DTE, CALL_45_DTE),
    )
    greeks = [
        OptionGreek(contract=PUT_30_DTE, timestamp=OBSERVED_AT, delta=Decimal("-0.20")),
        OptionGreek(contract=PUT_40_DTE, timestamp=OBSERVED_AT, delta=Decimal("-0.12")),
        OptionGreek(contract=PUT_45_DTE, timestamp=OBSERVED_AT, delta=Decimal("-0.11")),
        OptionGreek(contract=PUT_60_DTE, timestamp=OBSERVED_AT, delta=Decimal("-0.08")),
        OptionGreek(contract=CALL_45_DTE, timestamp=OBSERVED_AT, delta=Decimal("0.20")),
    ]
    ivs = [
        OptionImpliedVolatility(
            contract=PUT_30_DTE,
            timestamp=OBSERVED_AT,
            implied_volatility=Decimal("0.35"),
        ),
        OptionImpliedVolatility(
            contract=PUT_40_DTE,
            timestamp=OBSERVED_AT,
            implied_volatility=Decimal("0.42"),
        ),
        OptionImpliedVolatility(
            contract=PUT_45_DTE,
            timestamp=OBSERVED_AT,
            implied_volatility=Decimal("0.45"),
        ),
        OptionImpliedVolatility(
            contract=PUT_60_DTE,
            timestamp=OBSERVED_AT,
            implied_volatility=Decimal("0.55"),
        ),
        OptionImpliedVolatility(
            contract=CALL_45_DTE,
            timestamp=OBSERVED_AT,
            implied_volatility=Decimal("0.30"),
        ),
    ]
    return ContractSelectionEngine(
        chain,
        SPOT,
        as_of_date=AS_OF,
        greeks=greeks,
        implied_volatilities=ivs,
    )


def test_candidates_return_strongly_typed_selection_metadata() -> None:
    candidates = make_engine().candidates()

    assert all(isinstance(candidate, OptionSelectionCandidate) for candidate in candidates)
    assert candidates[0].contract == PUT_30_DTE
    assert candidates[0].dte == 30
    assert candidates[0].strike_distance == Decimal("-5")
    assert candidates[0].strike_distance_pct == Decimal("-0.05")
    assert candidates[0].delta == Decimal("-0.20")
    assert candidates[0].implied_volatility == Decimal("0.35")


def test_find_nearest_45_dte_put_with_delta_closest_to_negative_point_10() -> None:
    selected = make_engine().find_nearest_dte_with_delta(
        target_dte=45,
        target_delta=Decimal("-0.10"),
        option_type=OptionType.PUT,
    )

    assert selected is not None
    assert selected.contract == PUT_45_DTE
    assert selected.dte == 45
    assert selected.delta == Decimal("-0.11")


def test_find_contracts_between_30_and_45_dte() -> None:
    selected = make_engine().find_contracts_between_dte(
        30,
        45,
        option_type=OptionType.PUT,
    )

    assert [candidate.contract for candidate in selected] == [PUT_30_DTE, PUT_40_DTE, PUT_45_DTE]
    assert [candidate.dte for candidate in selected] == [30, 40, 45]


def test_find_contract_closest_to_specified_strike() -> None:
    selected = make_engine().find_closest_to_strike(
        Decimal("89"),
        option_type=OptionType.PUT,
    )

    assert selected is not None
    assert selected.contract == PUT_40_DTE


def test_select_filters_by_strike_distance_from_spot() -> None:
    selected = make_engine().select(
        OptionSelectionQuery(
            option_type=OptionType.PUT,
            min_strike_distance=Decimal("-12"),
            max_strike_distance=Decimal("-5"),
        )
    )

    assert [candidate.contract for candidate in selected] == [PUT_30_DTE, PUT_40_DTE, PUT_45_DTE]


def test_select_filters_by_implied_volatility_range() -> None:
    selected = make_engine().select(
        OptionSelectionQuery(
            option_type=OptionType.PUT,
            min_implied_volatility=Decimal("0.40"),
            max_implied_volatility=Decimal("0.50"),
        )
    )

    assert [candidate.contract for candidate in selected] == [PUT_40_DTE, PUT_45_DTE]


def test_select_can_combine_dte_delta_strike_and_iv_filters() -> None:
    selected = make_engine().select(
        OptionSelectionQuery(
            option_type=OptionType.PUT,
            min_dte=30,
            max_dte=45,
            target_delta=Decimal("-0.10"),
            target_strike=Decimal("89"),
            min_implied_volatility=Decimal("0.40"),
            max_implied_volatility=Decimal("0.50"),
        )
    )

    assert [candidate.contract for candidate in selected] == [PUT_45_DTE, PUT_40_DTE]


def test_best_returns_none_when_filters_match_no_contracts() -> None:
    selected = make_engine().best(
        OptionSelectionQuery(
            option_type=OptionType.PUT,
            min_dte=90,
            max_dte=120,
        )
    )

    assert selected is None


def test_target_delta_excludes_contracts_without_delta() -> None:
    chain = OptionChain(
        underlying_symbol="AAPL",
        timestamp=OBSERVED_AT,
        contracts=(PUT_30_DTE, PUT_40_DTE),
    )
    engine = ContractSelectionEngine(
        chain,
        SPOT,
        as_of_date=AS_OF,
        greeks=[
            OptionGreek(contract=PUT_40_DTE, timestamp=OBSERVED_AT, delta=Decimal("-0.12")),
        ],
    )

    selected = engine.select(OptionSelectionQuery(target_delta=Decimal("-0.10")))

    assert [candidate.contract for candidate in selected] == [PUT_40_DTE]


def test_iv_filter_excludes_contracts_without_implied_volatility() -> None:
    chain = OptionChain(
        underlying_symbol="AAPL",
        timestamp=OBSERVED_AT,
        contracts=(PUT_30_DTE, PUT_40_DTE),
    )
    engine = ContractSelectionEngine(
        chain,
        SPOT,
        as_of_date=AS_OF,
        implied_volatilities=[
            OptionImpliedVolatility(
                contract=PUT_40_DTE,
                timestamp=OBSERVED_AT,
                implied_volatility=Decimal("0.42"),
            ),
        ],
    )

    selected = engine.select(OptionSelectionQuery(min_implied_volatility=Decimal("0.40")))

    assert [candidate.contract for candidate in selected] == [PUT_40_DTE]


def test_greek_implied_volatility_is_used_when_dedicated_iv_is_missing() -> None:
    chain = OptionChain(underlying_symbol="AAPL", timestamp=OBSERVED_AT, contracts=(PUT_30_DTE,))
    engine = ContractSelectionEngine(
        chain,
        SPOT,
        as_of_date=AS_OF,
        greeks=[
            OptionGreek(
                contract=PUT_30_DTE,
                timestamp=OBSERVED_AT,
                implied_volatility=Decimal("0.41"),
            ),
        ],
    )

    selected = engine.select(OptionSelectionQuery(min_implied_volatility=Decimal("0.40")))

    assert selected[0].contract == PUT_30_DTE
    assert selected[0].implied_volatility == Decimal("0.41")


def test_dedicated_iv_overrides_greek_implied_volatility() -> None:
    chain = OptionChain(underlying_symbol="AAPL", timestamp=OBSERVED_AT, contracts=(PUT_30_DTE,))
    engine = ContractSelectionEngine(
        chain,
        SPOT,
        as_of_date=AS_OF,
        greeks=[
            OptionGreek(
                contract=PUT_30_DTE,
                timestamp=OBSERVED_AT,
                implied_volatility=Decimal("0.41"),
            ),
        ],
        implied_volatilities=[
            OptionImpliedVolatility(
                contract=PUT_30_DTE,
                timestamp=OBSERVED_AT,
                implied_volatility=Decimal("0.44"),
            )
        ],
    )

    assert engine.candidates()[0].implied_volatility == Decimal("0.44")


def test_query_rejects_invalid_dte_range() -> None:
    with pytest.raises(ValidationError, match="min_dte must be less than or equal to max_dte"):
        OptionSelectionQuery(min_dte=45, max_dte=30)


def test_query_rejects_invalid_strike_distance_range() -> None:
    with pytest.raises(
        ValidationError,
        match="min_strike_distance must be less than or equal to max_strike_distance",
    ):
        OptionSelectionQuery(
            min_strike_distance=Decimal("5"),
            max_strike_distance=Decimal("-5"),
        )


def test_query_rejects_invalid_iv_range() -> None:
    with pytest.raises(
        ValidationError,
        match="min_implied_volatility must be less than or equal to max_implied_volatility",
    ):
        OptionSelectionQuery(
            min_implied_volatility=Decimal("0.50"),
            max_implied_volatility=Decimal("0.40"),
        )
