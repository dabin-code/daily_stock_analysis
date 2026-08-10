# -*- coding: utf-8 -*-
"""Command-level canonical failure-report tests."""
from __future__ import annotations

import json
from argparse import Namespace

from scripts import validate_bottom_divergence_v2 as command
from src.backtest.services import bottom_divergence_v2_cli_service as cli_service
from src.backtest.services.bottom_divergence_v2_report import (
    _read_universe_codes,
)
from src.backtest.services.bottom_divergence_v2_validation import (
    ValidationInputError,
)
from src.config import Config
from src.storage import DatabaseManager


def _argv(output, universe=None) -> list[str]:
    values = [
        "--date-from",
        "2024-01-01",
        "--date-to",
        "2024-12-31",
        "--market",
        "cn",
        "--output",
        str(output),
    ]
    if universe is not None:
        values.extend(["--universe-codes", str(universe)])
    return values


def test_empty_universe_file_writes_canonical_ineligible_report(
    tmp_path,
    monkeypatch,
) -> None:
    universe = tmp_path / "universe.txt"
    universe.write_text("@@@,\n,,\n", encoding="utf-8")
    output = tmp_path / "report.json"

    def run(args):
        _read_universe_codes(args.universe_codes)
        raise AssertionError("unreachable")

    monkeypatch.setattr(command, "run_validation_cli", run)
    assert command.main(_argv(output, universe)) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "ineligible"
    assert report["eligible"] is False
    assert report["error_code"] == "EMPTY_UNIVERSE"


def test_expected_data_error_is_exit_one_and_json_safe(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "report.json"

    def fail(_args):
        raise ValidationInputError(
            "INVALID_INPUT",
            "non-finite cost",
            data_version="sha256-fixture",
        )

    monkeypatch.setattr(command, "run_validation_cli", fail)
    assert command.main(_argv(output)) == 1
    payload = output.read_text(encoding="utf-8")
    assert "NaN" not in payload
    report = json.loads(payload)
    assert report["error_code"] == "INVALID_INPUT"
    assert report["data_version"] == "sha256-fixture"


def test_unexpected_error_is_sanitized_exit_two_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "report.json"

    def explode(_args):
        raise RuntimeError("secret database path")

    monkeypatch.setattr(command, "run_validation_cli", explode)
    assert command.main(_argv(output)) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert report["error_code"] == "UNEXPECTED_ERROR"
    assert "secret database path" not in json.dumps(report)
    assert "Traceback" not in capsys.readouterr().err


def test_universe_option_absent_remains_distinct_from_empty_file(
    tmp_path,
) -> None:
    assert _read_universe_codes(None) is None
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    try:
        _read_universe_codes(empty)
    except ValidationInputError as exc:
        assert exc.error_code == "EMPTY_UNIVERSE"
    else:
        raise AssertionError("empty universe file must not fall back")


def test_zero_cost_exits_before_database_copy_or_future_checks(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "zero-cost.json"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("zero-cost validation must not access the database")

    monkeypatch.setattr(DatabaseManager, "get_instance", forbidden)
    monkeypatch.setattr(cli_service, "isolated_replay_database", forbidden)
    exit_code, report = cli_service.run_validation_cli(
        Namespace(
            date_from=command._parse_date("2024-01-01"),
            date_to=command._parse_date("2024-01-02"),
            market="cn",
            output=output,
            universe_codes=None,
        ),
        base_config=Config(
            backtest_buy_cost_bps=0.0,
            backtest_sell_cost_bps=0.0,
            backtest_slippage_bps=0.0,
        ),
    )

    assert exit_code == 1
    assert report["error_code"] == "ZERO_COST_MODEL"
    assert json.loads(output.read_text(encoding="utf-8")) == report
