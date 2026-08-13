# -*- coding: utf-8 -*-
"""Time-series parameter selection and non-inferiority orchestration."""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import date
from typing import Any, Dict, Mapping, Optional, Sequence

from .bottom_divergence_v2_models import (
    CandidateEventEvidence,
    ValidationInputError,
    ValidationSample,
    _require_finite,
    build_parameter_snapshots,
    chronological_split,
    fit_tertile_boundaries,
)
from .bottom_divergence_v2_metrics import (
    _conversion_lower,
    _partition_by_dates,
    deduplicate_validation_samples,
    evaluate_noninferiority_gates,
    summarize_validation_samples,
)


_FORWARD_LABEL_DAYS = 20

_SELECTION_SPLIT_RATIOS = (("train", 0.6), ("validation", 0.2))


def minimum_trading_days() -> int:
    """能让两个选参切分都熬过前瞻清洗的最短交易日数。

    验证段是紧约束：它只占 20%，而清洗要从每段末尾砍掉
    `_FORWARD_LABEL_DAYS` 天。用循环而不是 `ceil((F+1)/r)` 直接算，
    是因为 0.2 在二进制里不精确，`21 / 0.2` 落在 105 的哪一侧不该由
    浮点表示决定——这里的判据必须和 `chronological_split` 里那个
    `math.floor(len(dates) * ratio)` 逐位一致。
    """
    needed = 1
    for _, ratio in _SELECTION_SPLIT_RATIOS:
        count = 1
        while math.floor(count * ratio) <= _FORWARD_LABEL_DAYS:
            count += 1
        needed = max(needed, count)
    return needed


def purge_would_empty(split: Any) -> Optional[str]:
    """返回第一个会被前瞻清洗清空的选参切分名，没有则返回 None。

    只看日历长度，因子还没算就能定。放在这里而不是调用方，是为了让它
    和 `_selection_dates_and_purge_report` 的清洗深度共用同一个常量，
    改一处不会漏掉另一处。
    """
    for name, _ in _SELECTION_SPLIT_RATIOS:
        if len(getattr(split, f"{name}_dates")) <= _FORWARD_LABEL_DAYS:
            return name
    return None


def _selection_dates_and_purge_report(
    split: Any,
) -> tuple[dict[str, tuple[date, ...]], dict[str, Dict[str, Any]]]:
    raw_dates = {
        "train": tuple(split.train_dates),
        "validation": tuple(split.validation_dates),
        "test": tuple(split.test_dates),
    }
    selection_dates = {
        "train": raw_dates["train"][:-_FORWARD_LABEL_DAYS],
        "validation": raw_dates["validation"][:-_FORWARD_LABEL_DAYS],
        "test": raw_dates["test"],
    }
    report: dict[str, Dict[str, Any]] = {}
    for name, dates in raw_dates.items():
        effective = selection_dates[name]
        report[name] = {
            "raw_date_count": len(dates),
            "purged_count": len(dates) - len(effective),
            "effective_date_range": {
                "from": effective[0].isoformat() if effective else None,
                "to": effective[-1].isoformat() if effective else None,
            },
        }
    return selection_dates, report


def _filter_samples(
    samples: Sequence[ValidationSample],
    dates: Sequence[date],
    *,
    label_end: Optional[date] = None,
) -> tuple[ValidationSample, ...]:
    date_set = set(dates)
    return tuple(
        item
        for item in samples
        if item.signal_date in date_set
        and (
            label_end is None
            or not item.future_trade_dates_20d
            or item.future_trade_dates_20d[-1] <= label_end
        )
    )


class BottomDivergenceV2Validator:
    """Select on train/validation, then evaluate the locked test sample once."""

    @staticmethod
    def evaluate(
        *,
        v1_samples: Sequence[ValidationSample],
        v2_samples_by_parameter_hash: Dict[
            str,
            Sequence[ValidationSample],
        ],
        parameter_snapshots: Dict[str, Dict[str, float]],
        buy_cost_bps: float,
        sell_cost_bps: float,
        slippage_bps: float,
        trading_dates: Optional[Sequence[date]] = None,
        opportunity_counts: Optional[Mapping[Any, int]] = None,
        selection_event_evidence_by_parameter_hash: Optional[
            Mapping[str, Sequence[CandidateEventEvidence]]
        ] = None,
        selection_contract: Optional[Mapping[str, Any]] = None,
        test_maturation_evidence: Sequence[CandidateEventEvidence] = (),
        test_observation_dates: Optional[Sequence[date]] = None,
        train_ratio: float = 0.6,
        validation_ratio: float = 0.2,
        test_ratio: float = 0.2,
    ) -> Dict[str, Any]:
        if (train_ratio, validation_ratio, test_ratio) != (0.6, 0.2, 0.2):
            raise ValueError("split ratios are fixed at 0.6/0.2/0.2")
        costs = {
            "buy_cost_bps": buy_cost_bps,
            "sell_cost_bps": sell_cost_bps,
            "slippage_bps": slippage_bps,
            "round_trip_cost_bps": (
                buy_cost_bps + sell_cost_bps + 2.0 * slippage_bps
            ),
        }
        for name, value in costs.items():
            _require_finite(name, value)
        if any(value < 0.0 for value in costs.values()):
            raise ValidationInputError(
                "INVALID_INPUT",
                "costs must be non-negative",
            )
        if costs["round_trip_cost_bps"] == 0.0:
            return {
                "eligible": False,
                "passed": False,
                "reasons": ["zero_cost_model"],
                "selected_parameter_hash": None,
                "cost_model": costs,
            }
        if not trading_dates:
            raise ValidationInputError(
                "NO_TRADING_DATES",
                "trading_dates must be provided as a non-empty calendar",
            )
        calendar = tuple(sorted(set(trading_dates)))
        if len(calendar) != len(trading_dates):
            raise ValidationInputError(
                "INVALID_INPUT",
                "trading_dates must be unique",
            )
        calendar_set = set(calendar)
        if not opportunity_counts or set(opportunity_counts) != calendar_set:
            raise ValidationInputError(
                "INVALID_OPPORTUNITY_COUNTS",
                "opportunity_counts must match trading_dates exactly",
            )
        for trade_date, count in opportunity_counts.items():
            if type(count) is not int or count < 0:
                raise ValidationInputError(
                    "INVALID_OPPORTUNITY_COUNTS",
                    "daily opportunity counts must be integers >= 0",
                )

        expected_snapshots = build_parameter_snapshots()
        if parameter_snapshots != expected_snapshots:
            raise ValueError("parameter_snapshots must match preregistered grid")
        if set(v2_samples_by_parameter_hash) != set(expected_snapshots):
            raise ValueError("v2 sample mapping must cover preregistered grid")
        if selection_event_evidence_by_parameter_hash is None:
            selection_evidence = {
                parameter_hash: ()
                for parameter_hash in expected_snapshots
            }
        else:
            if set(selection_event_evidence_by_parameter_hash) != set(
                expected_snapshots
            ):
                raise ValueError(
                    "selection event evidence mapping must cover "
                    "preregistered grid"
                )
            selection_evidence = {
                parameter_hash: tuple(evidence)
                for parameter_hash, evidence
                in selection_event_evidence_by_parameter_hash.items()
            }
        if any(item.strategy_version != "v1" for item in v1_samples):
            raise ValueError("v1_samples contains a non-v1 strategy sample")
        for parameter_hash, samples in v2_samples_by_parameter_hash.items():
            if any(item.strategy_version != "v2" for item in samples):
                raise ValueError(
                    f"v2 sample mapping {parameter_hash} contains non-v2 sample"
                )
        v1_samples = deduplicate_validation_samples(v1_samples)
        v2_samples_by_parameter_hash = {
            parameter_hash: deduplicate_validation_samples(samples)
            for parameter_hash, samples in v2_samples_by_parameter_hash.items()
        }

        all_samples = [
            *v1_samples,
            *(
                sample
                for samples in v2_samples_by_parameter_hash.values()
                for sample in samples
            ),
        ]
        outside_dates = sorted(
            {item.signal_date for item in all_samples} - calendar_set
        )
        if outside_dates:
            raise ValidationInputError(
                "SAMPLE_DATE_OUTSIDE_CALENDAR",
                "sample signal_date is not present in trading_dates",
            )
        split = chronological_split(v1_samples, date_universe=calendar)
        selection_dates, purge_report = _selection_dates_and_purge_report(split)
        raw_split_opportunities = {
            "train": sum(
                int(opportunity_counts[item]) for item in split.train_dates
            ),
            "validation": sum(
                int(opportunity_counts[item])
                for item in split.validation_dates
            ),
            "test": sum(
                int(opportunity_counts[item]) for item in split.test_dates
            ),
        }
        split_opportunities = {
            name: sum(int(opportunity_counts[item]) for item in dates)
            for name, dates in selection_dates.items()
        }
        invalid_opportunities = [
            name for name, count in split_opportunities.items() if count <= 0
        ]
        if invalid_opportunities:
            return {
                "eligible": False,
                "passed": False,
                "reasons": [
                    f"invalid_opportunity_count:{name}"
                    for name in sorted(invalid_opportunities)
                ],
                "selected_parameter_hash": None,
                "cost_model": costs,
                "purge": purge_report,
                "raw_opportunity_counts": raw_split_opportunities,
                "purged_opportunity_counts": split_opportunities,
            }
        raw_v1_partitions = _partition_by_dates(v1_samples, split)
        v1_partitions = {
            name: _filter_samples(
                raw_v1_partitions[name],
                dates,
                label_end=(
                    None
                    if name == "test"
                    else {
                        "train": split.train_dates[-1],
                        "validation": split.validation_dates[-1],
                    }[name]
                ),
            )
            for name, dates in selection_dates.items()
        }
        boundaries = fit_tertile_boundaries(v1_partitions["train"])

        def summarize(
            samples: Sequence[ValidationSample],
            split_name: str,
            conversion_samples: Sequence[ValidationSample],
            *,
            observation_dates: Optional[Sequence[date]] = None,
            maturation_evidence: Sequence[CandidateEventEvidence] = (),
        ) -> Dict[str, Any]:
            split_dates = set(selection_dates[split_name])
            default_observation_dates = {
                "train": split.train_dates,
                "validation": split.validation_dates,
                "test": split.test_dates,
            }[split_name]
            return summarize_validation_samples(
                samples,
                boundaries=boundaries,
                opportunity_count=split_opportunities[split_name],
                conversion_samples=conversion_samples,
                conversion_dates=split_dates,
                conversion_observation_dates=(
                    observation_dates
                    if observation_dates is not None
                    else default_observation_dates
                ),
                maturation_evidence=maturation_evidence,
                buy_cost_bps=buy_cost_bps,
                sell_cost_bps=sell_cost_bps,
                slippage_bps=slippage_bps,
            )

        if selection_contract is not None:
            frozen_contract = deepcopy(dict(selection_contract))
            selected_hash = frozen_contract.get("selected_parameter_hash")
            if selected_hash not in expected_snapshots:
                raise ValueError(
                    "selection_contract selected hash is not preregistered"
                )
            expected_split = {
                "train_dates": [item.isoformat() for item in split.train_dates],
                "validation_dates": [
                    item.isoformat() for item in split.validation_dates
                ],
                "test_dates": [item.isoformat() for item in split.test_dates],
            }
            if frozen_contract.get("split") != expected_split:
                raise ValueError("selection_contract split does not match inputs")
            if frozen_contract.get("purge") != purge_report:
                raise ValueError("selection_contract purge does not match inputs")
            if frozen_contract.get("cost_model") != costs:
                raise ValueError(
                    "selection_contract cost model does not match inputs"
                )
            boundaries = {
                name: tuple(values)
                for name, values in frozen_contract["tertile_boundaries"].items()
            }
            selected_partitions = _partition_by_dates(
                v2_samples_by_parameter_hash[selected_hash],
                split,
            )
            v1_test_metrics = summarize(
                v1_partitions["test"],
                "test",
                v1_partitions["test"],
                observation_dates=test_observation_dates,
            )
            v2_test_metrics = summarize(
                selected_partitions["test"],
                "test",
                selected_partitions["test"],
                observation_dates=test_observation_dates,
                maturation_evidence=test_maturation_evidence,
            )
            test_gates = evaluate_noninferiority_gates(
                v1_test_metrics,
                v2_test_metrics,
                train_conversion_lower=float(
                    frozen_contract["train_conversion_lower"]
                ),
            )
            return {
                **test_gates,
                "selected_parameter_hash": selected_hash,
                "selected_parameter_snapshot": parameter_snapshots[
                    selected_hash
                ],
                "selection_contract": frozen_contract,
                "cost_model": costs,
                "parameter_grid": parameter_snapshots,
                "split": expected_split,
                "tertile_boundaries": boundaries,
                "opportunity_counts": split_opportunities,
                "raw_opportunity_counts": raw_split_opportunities,
                "purged_opportunity_counts": split_opportunities,
                "purge": purge_report,
                "parameter_selection": frozen_contract[
                    "parameter_selection"
                ],
                "metrics": {
                    "train": frozen_contract["metrics"]["train"],
                    "validation": frozen_contract["metrics"]["validation"],
                    "test": {
                        "v1": v1_test_metrics,
                        "v2": v2_test_metrics,
                    },
                },
            }

        v1_train_metrics = summarize(
            v1_partitions["train"],
            "train",
            v1_partitions["train"],
        )
        v1_validation_metrics = summarize(
            v1_partitions["validation"],
            "validation",
            v1_partitions["validation"],
        )
        candidates = []
        candidate_partitions: Dict[
            str,
            Dict[str, tuple[ValidationSample, ...]],
        ] = {}
        for parameter_hash in sorted(expected_snapshots):
            partitions = _partition_by_dates(
                v2_samples_by_parameter_hash[parameter_hash],
                split,
            )
            partitions = {
                name: _filter_samples(
                    partitions[name],
                    dates,
                    label_end=(
                        None
                        if name == "test"
                        else {
                            "train": split.train_dates[-1],
                            "validation": split.validation_dates[-1],
                        }[name]
                    ),
                )
                for name, dates in selection_dates.items()
            }
            candidate_partitions[parameter_hash] = partitions
            train_metrics = summarize(
                partitions["train"],
                "train",
                partitions["train"],
                maturation_evidence=selection_evidence[parameter_hash],
            )
            validation_metrics = summarize(
                partitions["validation"],
                "validation",
                partitions["validation"],
                maturation_evidence=selection_evidence[parameter_hash],
            )
            train_gates = evaluate_noninferiority_gates(
                v1_train_metrics,
                train_metrics,
                train_conversion_lower=_conversion_lower(train_metrics),
                minimum_stage_samples=1,
            )
            validation_gates = evaluate_noninferiority_gates(
                v1_validation_metrics,
                validation_metrics,
                train_conversion_lower=_conversion_lower(train_metrics),
                minimum_stage_samples=1,
            )
            reasons = [
                f"train:{reason}" for reason in train_gates["reasons"]
            ] + [
                f"validation:{reason}"
                for reason in validation_gates["reasons"]
            ]
            candidates.append(
                {
                    "parameter_hash": parameter_hash,
                    "eligible": not reasons,
                    "reasons": reasons,
                    "validation_expectancy_10d": validation_metrics[
                        "overall"
                    ]["mean_net_return_10d"],
                    "train_metrics": train_metrics,
                    "validation_metrics": validation_metrics,
                }
            )

        eligible_candidates = [item for item in candidates if item["eligible"]]
        if not eligible_candidates:
            return {
                "eligible": False,
                "passed": False,
                "reasons": ["no_parameter_passed_train_validation"],
                "selected_parameter_hash": None,
                "cost_model": costs,
                "parameter_selection": candidates,
            }
        selected = min(
            eligible_candidates,
            key=lambda item: (
                -float(item["validation_expectancy_10d"]),
                item["parameter_hash"],
            ),
        )
        selected_hash = selected["parameter_hash"]
        train_conversion = selected["train_metrics"]["early"]["conversion"]
        train_conversion_lower = _conversion_lower(
            selected["train_metrics"]
        )
        selection_contract_payload = {
            "selected_parameter_hash": selected_hash,
            "cost_model": deepcopy(costs),
            "train_conversion_rate": train_conversion["early_to_r1"],
            "train_conversion_lower": train_conversion_lower,
            "split": {
                "train_dates": [item.isoformat() for item in split.train_dates],
                "validation_dates": [
                    item.isoformat() for item in split.validation_dates
                ],
                "test_dates": [item.isoformat() for item in split.test_dates],
            },
            "tertile_boundaries": deepcopy(boundaries),
            "opportunity_counts": deepcopy(split_opportunities),
            "raw_opportunity_counts": deepcopy(raw_split_opportunities),
            "purged_opportunity_counts": deepcopy(split_opportunities),
            "purge": deepcopy(purge_report),
            "parameter_selection": deepcopy(candidates),
            "metrics": {
                "train": {
                    "v1": deepcopy(v1_train_metrics),
                    "v2": deepcopy(selected["train_metrics"]),
                },
                "validation": {
                    "v1": deepcopy(v1_validation_metrics),
                    "v2": deepcopy(selected["validation_metrics"]),
                },
            },
        }

        v1_test_metrics = summarize(
            v1_partitions["test"],
            "test",
            v1_partitions["test"],
        )
        v2_test_metrics = summarize(
            candidate_partitions[selected_hash]["test"],
            "test",
            candidate_partitions[selected_hash]["test"],
            observation_dates=test_observation_dates,
            maturation_evidence=test_maturation_evidence,
        )
        test_gates = evaluate_noninferiority_gates(
            v1_test_metrics,
            v2_test_metrics,
            train_conversion_lower=train_conversion_lower,
        )
        return {
            **test_gates,
            "selected_parameter_hash": selected_hash,
            "selected_parameter_snapshot": parameter_snapshots[selected_hash],
            "selection_contract": selection_contract_payload,
            "cost_model": costs,
            "parameter_grid": parameter_snapshots,
            "split": {
                "train_dates": [item.isoformat() for item in split.train_dates],
                "validation_dates": [
                    item.isoformat() for item in split.validation_dates
                ],
                "test_dates": [item.isoformat() for item in split.test_dates],
            },
            "tertile_boundaries": boundaries,
            "opportunity_counts": split_opportunities,
            "raw_opportunity_counts": raw_split_opportunities,
            "purged_opportunity_counts": split_opportunities,
            "purge": purge_report,
            "parameter_selection": candidates,
            "metrics": {
                "train": {
                    "v1": v1_train_metrics,
                    "v2": selected["train_metrics"],
                },
                "validation": {
                    "v1": v1_validation_metrics,
                    "v2": selected["validation_metrics"],
                },
                "test": {
                    "v1": v1_test_metrics,
                    "v2": v2_test_metrics,
                },
            },
        }
