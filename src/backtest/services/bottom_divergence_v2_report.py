# -*- coding: utf-8 -*-
"""Canonical success/failure reports and content-version hashing."""
from __future__ import annotations

import json
import re
from argparse import Namespace
from pathlib import Path
from typing import Any, Optional

from .bottom_divergence_v2_validation import ValidationInputError


# 报告里声明「这份数字回答的是哪个问题」的两个取值。
#
# `deployed` 的回放保留部署侧的全部过滤（含 L2 主线题材收窄），回答的是
# 「按今天的部署口径，这套参数会选出什么」；`signal_measurement` 跳过题材
# 收窄，回答的是「信号本身有没有预测力」。两者的样本量、期望、胜率没有可比
# 性，报告不写清楚模式，隔几天再看这个 JSON 就无从分辨。
PIPELINE_MODE_DEPLOYED = "deployed"
PIPELINE_MODE_SIGNAL_MEASUREMENT = "signal_measurement"


def resolve_pipeline_mode(config: Any) -> str:
    """Name the question this run answers, from the run's own config.

    刻意用直接属性访问而不是 `getattr(..., False)`：读不到这个字段时兜底成
    `deployed`，等于把一次测量运行标成部署运行——正是这个字段要防的事。
    宁可抛 AttributeError。
    """
    return (
        PIPELINE_MODE_SIGNAL_MEASUREMENT
        if config.signal_research_bypass_l2_theme_filter
        else PIPELINE_MODE_DEPLOYED
    )


def canonical_json_dumps(payload: Any) -> str:
    """Serialize canonical UTF-8 JSON suitable for hashing and audit diffs."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write_report(output: Path, report: dict) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json_dumps(report) + "\n", encoding="utf-8")


def _read_universe_codes(path: Optional[Path]) -> Optional[list[str]]:
    if path is None:
        return None
    raw = path.read_text(encoding="utf-8")
    tokens = [
        token.strip().upper()
        for line in raw.splitlines()
        for token in line.split(",")
        if token.strip()
    ]
    codes = [
        token
        for token in tokens
        if re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,15}", token)
    ]
    if not codes:
        raise ValidationInputError(
            "EMPTY_UNIVERSE",
            "universe code file contains no valid codes",
        )
    return list(dict.fromkeys(codes))


def build_failure_report(
    *,
    status: str,
    error_code: str,
    message: str,
    data_version: Optional[str] = None,
) -> dict[str, Any]:
    """Build a finite, JSON-safe failure payload with stable fields."""
    report: dict[str, Any] = {
        "status": status,
        "eligible": False,
        "passed": False,
        "error_code": error_code,
        "message": message,
        "reasons": [error_code.lower()],
        "selected_parameter_hash": None,
    }
    if data_version:
        report["data_version"] = data_version
    return report


def canonicalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Ensure every success/ineligible result has a canonical status."""
    eligible = bool(report.get("eligible"))
    passed = bool(report.get("passed"))
    normalized = dict(report)
    normalized["status"] = "passed" if eligible and passed else "ineligible"
    normalized["eligible"] = eligible
    normalized["passed"] = passed
    if not eligible:
        reasons = normalized.get("reasons") or []
        error_code = (
            "ZERO_COST_MODEL"
            if "zero_cost_model" in reasons
            else "GATE_FAILED"
        )
        normalized.setdefault("error_code", error_code)
        normalized.setdefault(
            "message",
            "; ".join(str(item) for item in reasons) or "validation ineligible",
        )
    return normalized


def _enrich_report(
    report: dict,
    *,
    args: Namespace,
    replay_service: Any,
    universe: Any,
    parameter_snapshots: dict[str, dict[str, float]],
    config: Any,
) -> dict:
    return canonicalize_report({
        **report,
        "pipeline_mode": resolve_pipeline_mode(config),
        "data_version": replay_service.data_version(),
        "universe_identity": replay_service.universe_identity(universe),
        "date_range": {
            "from": args.date_from.isoformat(),
            "to": args.date_to.isoformat(),
        },
        "market": args.market,
        "parameter_grid": parameter_snapshots,
    })
