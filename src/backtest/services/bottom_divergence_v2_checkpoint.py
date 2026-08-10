# -*- coding: utf-8 -*-
"""Canonical validation checkpoint identity, integrity, and recovery."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.indicators.causal_bottom_divergence_detector import (
    ALGORITHM_VERSION as CAUSAL_ALGORITHM_VERSION,
)
from src.indicators.resistance_zone_detector import (
    ALGORITHM_VERSION as ZONE_ALGORITHM_VERSION,
)

from .bottom_divergence_v2_report import canonical_json_dumps


VALIDATION_REPLAY_ALGORITHM_VERSION = "validation-replay-v2"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_V1_STRATEGY_PATH = (
    ROOT / "strategies" / "bottom_divergence_double_breakout.yaml"
)
DEFAULT_V2_STRATEGY_PATH = (
    ROOT / "strategies" / "bottom_divergence_layered_entry_v2.yaml"
)


class CheckpointMismatchError(ValueError):
    """Raised when a checkpoint belongs to different validation inputs."""


class CheckpointCorruptionError(ValueError):
    """Raised when no valid atomic checkpoint copy can be recovered."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validation_checkpoint_config_hash(
    *,
    config: Any,
    date_from: Any,
    date_to: Any,
    market: str,
    trading_dates: Sequence[Any],
    universe_identity: Mapping[str, Any],
    data_version: str,
    costs: Mapping[str, Any],
    parameter_snapshots: Mapping[str, Any],
    v1_strategy_path: Optional[Path] = None,
    v2_strategy_path: Optional[Path] = None,
) -> str:
    """Hash all result-affecting inputs, excluding workers and progress."""
    resolved_v1_path = v1_strategy_path or DEFAULT_V1_STRATEGY_PATH
    resolved_v2_path = v2_strategy_path or DEFAULT_V2_STRATEGY_PATH
    identity = {
        "algorithm_versions": {
            "replay": VALIDATION_REPLAY_ALGORITHM_VERSION,
            "causal_detector": CAUSAL_ALGORITHM_VERSION,
            "resistance_zone": ZONE_ALGORITHM_VERSION,
        },
        "config": asdict(config),
        "costs": dict(costs),
        "data_version": data_version,
        "date_from": str(date_from),
        "date_to": str(date_to),
        "market": market,
        "parameter_snapshots": dict(parameter_snapshots),
        "strategy_versions": {"v1": "v1", "v2": "v2"},
        "strategy_yaml_sha256": {
            "v1": _file_sha256(resolved_v1_path),
            "v2": _file_sha256(resolved_v2_path),
        },
        "trading_dates": [str(item) for item in trading_dates],
        "universe": dict(universe_identity),
    }
    return hashlib.sha256(
        canonical_json_dumps(identity).encode("utf-8")
    ).hexdigest()


class CanonicalCheckpointStore:
    """Canonical JSON checkpoint with identity validation and atomic replace."""

    def __init__(
        self,
        path: Path,
        *,
        data_version: str,
        config_hash: str,
    ) -> None:
        self.path = Path(path)
        self.data_version = data_version
        self.config_hash = config_hash
        if self.path.exists():
            try:
                payload = self._read_valid_payload(self.path)
            except (OSError, ValueError, json.JSONDecodeError):
                temporary = self._temporary_path()
                try:
                    payload = self._read_valid_payload(temporary)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise CheckpointCorruptionError(
                        "checkpoint and atomic temporary copy are invalid"
                    ) from exc
                os.replace(temporary, self.path)
            if (
                payload.get("data_version") != data_version
                or payload.get("config_hash") != config_hash
            ):
                raise CheckpointMismatchError(
                    "checkpoint identity does not match validation input"
                )
            self._payload = payload
        else:
            self._payload = {
                "schema_version": 2,
                "data_version": data_version,
                "config_hash": config_hash,
                "parameters": {},
            }

    def _temporary_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".tmp")

    @staticmethod
    def _payload_hash(payload: Mapping[str, Any]) -> str:
        content = {
            key: value
            for key, value in payload.items()
            if key != "content_hash"
        }
        return hashlib.sha256(
            canonical_json_dumps(content).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _read_valid_payload(cls, path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = payload.get("content_hash")
        if expected is not None and expected != cls._payload_hash(payload):
            raise ValueError("checkpoint content hash mismatch")
        return payload

    def save_partition(
        self,
        *,
        parameter_hash: str,
        partition: str,
        payload: Mapping[str, Any],
    ) -> None:
        parameters = self._payload.setdefault("parameters", {})
        entry = parameters.setdefault(parameter_hash, {"partitions": {}})
        entry["partitions"][partition] = dict(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._payload["content_hash"] = self._payload_hash(self._payload)
        temporary = self._temporary_path()
        temporary.write_text(
            canonical_json_dumps(self._payload),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def completed_partitions(self, parameter_hash: str) -> tuple[str, ...]:
        partitions = (
            self._payload.get("parameters", {})
            .get(parameter_hash, {})
            .get("partitions", {})
        )
        return tuple(sorted(partitions))

    def load_partition(
        self,
        parameter_hash: str,
        partition: str,
    ) -> Optional[dict[str, Any]]:
        value = (
            self._payload.get("parameters", {})
            .get(parameter_hash, {})
            .get("partitions", {})
            .get(partition)
        )
        return dict(value) if value is not None else None
