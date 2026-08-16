"""Immutable discovery-run parameters and their canonical digest.

Native Copilot orchestration records every discovery run in the append-only run
journal (``TASK-MIG-007``). The run's immutable scope and bounds are captured by
:class:`RunParameters`, whose :meth:`RunParameters.digest` seeds the journal
``run_started`` event so a replay can prove which run it belongs to.

``TASK-MIG-010`` retired the scripted capability-exchange and checkpoint models
this module once also held; only the journal-native run identity remains.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RunParameters(BaseModel):
    """Immutable run scope and bounds that identify one journalled run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    category: Literal["corpus", "tool", "dictionary"]
    stage: str
    qualifiers: list[str]
    max_mined_terms: int
    max_exclusion_groups: int
    run_on: str

    def digest(self) -> str:
        """Return a canonical digest of the immutable run parameters."""

        return _digest(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["RunParameters"]
