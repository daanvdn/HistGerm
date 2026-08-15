from __future__ import annotations

import json
import os
import stat
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from histgerm.models import LanguageStage
from histgerm.research.discovery_protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    DiscoveryCheckpoint,
    DiscoveryExchange,
    DiscoveryProtocolError,
    ModelElicitationRequest,
    read_checkpoint,
    read_exchange,
    validate_operational_path,
    write_checkpoint,
)
from histgerm.research.discovery_session import (
    apply_exchange,
    checkpoint_config,
    new_checkpoint,
)


def checkpoint(tmp_path: Path) -> DiscoveryCheckpoint:
    value = new_checkpoint(
        category="tool",
        stage=LanguageStage.MHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
        run_on=date(2026, 8, 12),
    )
    return value.model_copy(
        update={
            "revision": 1,
            "pending": [
                ModelElicitationRequest(
                    request_id=f"{value.run_id}:elicitation:one",
                    iteration=1,
                    prompt_kind="broad",
                    prompt="prompt",
                    max_output_chars=100,
                    max_candidates=5,
                )
            ],
        }
    )


def exchange(value: DiscoveryCheckpoint, **updates: object) -> DiscoveryExchange:
    payload: dict[str, object] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "run_id": value.run_id,
        "checkpoint_revision": value.revision,
        "responses": [
            {
                "kind": "model_elicitation",
                "request_id": value.pending[0].request_id,
                "output": '{"candidates":[]}',
            }
        ],
    }
    payload.update(updates)
    return DiscoveryExchange.model_validate(payload)


def test_checkpoint_path_policy_rejects_unsafe_locations(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(Path("relative.json"), option="--checkpoint")
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(
            tmp_path / ".." / "escape.json", option="--checkpoint"
        )
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(
            tmp_path / "missing" / "run.json", option="--checkpoint"
        )
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(tmp_path, option="--checkpoint")
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "target.json")
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(link, option="--checkpoint")
    inside = Path.cwd().resolve() / "run.json"
    with pytest.raises(DiscoveryProtocolError):
        validate_operational_path(inside, option="--checkpoint")
    accepted = tmp_path / "run.json"
    assert validate_operational_path(accepted, option="--checkpoint") == accepted


def test_checkpoint_round_trip_is_atomic_bounded_and_user_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.json"
    original = checkpoint(tmp_path)
    write_checkpoint(path, original)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [item for item in tmp_path.iterdir() if item.name.startswith(".")]
    assert read_checkpoint(path) == original
    assert checkpoint_config(original).stage is LanguageStage.MHG

    path.write_text(
        json.dumps({**original.model_dump(mode="json"), "schema_version": 99}),
        encoding="utf-8",
    )
    with pytest.raises(DiscoveryProtocolError, match="unsupported"):
        read_checkpoint(path)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(DiscoveryProtocolError, match="valid JSON"):
        read_checkpoint(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(DiscoveryProtocolError, match="one JSON object"):
        read_checkpoint(path)


def test_response_file_is_bounded_strict_json(tmp_path: Path) -> None:
    value = checkpoint(tmp_path)
    path = tmp_path / "response.json"
    path.write_text(
        json.dumps(exchange(value).model_dump(mode="json")), encoding="utf-8"
    )
    assert read_exchange(path).run_id == value.run_id
    path.write_bytes(b'{"schema_version":1,"run_id":"\xff"}')
    with pytest.raises(DiscoveryProtocolError, match="UTF-8"):
        read_exchange(path)
    payload = exchange(value).model_dump(mode="json")
    payload["responses"][0]["rationale"] = "chain of thought"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        read_exchange(path)


def test_exchange_consumption_fails_closed(tmp_path: Path) -> None:
    value = checkpoint(tmp_path)
    with pytest.raises(DiscoveryProtocolError, match="run identifier"):
        apply_exchange(value, exchange(value, run_id="0" * 16))
    with pytest.raises(DiscoveryProtocolError, match="revision"):
        apply_exchange(value, exchange(value, checkpoint_revision=99))
    unknown = exchange(
        value,
        responses=[
            {
                "kind": "model_elicitation",
                "request_id": "other",
                "output": '{"candidates":[]}',
            }
        ],
    )
    with pytest.raises(DiscoveryProtocolError, match="not pending"):
        apply_exchange(value, unknown)
    duplicate = exchange(
        value,
        responses=[
            {
                "kind": "model_elicitation",
                "request_id": value.pending[0].request_id,
                "output": '{"candidates":[]}',
            },
            {
                "kind": "model_elicitation",
                "request_id": value.pending[0].request_id,
                "output": '{"candidates":[]}',
            },
        ],
    )
    with pytest.raises(DiscoveryProtocolError, match="answered twice"):
        apply_exchange(value, duplicate)
    mismatched = exchange(
        value,
        responses=[
            {
                "kind": "result_inspection",
                "request_id": value.pending[0].request_id,
                "verdicts": [
                    {"position": 1, "classification": "lead", "reason": "matches"}
                ],
            }
        ],
    )
    with pytest.raises(DiscoveryProtocolError, match="not an inspection"):
        apply_exchange(value, mismatched)

    applied = apply_exchange(value, exchange(value))
    assert applied.pending == []
    assert applied.consumed_request_ids == [value.pending[0].request_id]
    assert len(applied.elicitations) == 1
    with pytest.raises(DiscoveryProtocolError, match="no pending"):
        apply_exchange(applied, exchange(value))
    replayed = applied.model_copy(update={"pending": value.pending})
    with pytest.raises(DiscoveryProtocolError, match="already answered"):
        apply_exchange(replayed, exchange(value))


def test_missing_response_and_tampered_parameters_fail_closed(tmp_path: Path) -> None:
    value = checkpoint(tmp_path)
    second = ModelElicitationRequest(
        request_id=f"{value.run_id}:elicitation:two",
        iteration=2,
        prompt_kind="follow_up",
        prompt="prompt two",
        max_output_chars=100,
        max_candidates=5,
    )
    pending = value.model_copy(update={"pending": [*value.pending, second]})
    with pytest.raises(DiscoveryProtocolError, match="missing"):
        apply_exchange(pending, exchange(value))
    tampered = value.model_copy(
        update={
            "parameters": value.parameters.model_copy(
                update={"max_exclusion_groups": 2}
            )
        }
    )
    with pytest.raises(DiscoveryProtocolError, match="digest"):
        checkpoint_config(tampered)
