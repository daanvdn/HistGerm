"""Journal-native discovery-run parameters projection.

The scripted capability-exchange state machine that once paused a discovery run
at each model-elicitation or item-inspection boundary and resumed it from a
serialized checkpoint was retired in ``TASK-MIG-010``. Native Copilot
orchestration now drives every run and records it directly in the append-only
run journal (``TASK-MIG-007``).

The single deterministic responsibility that remains is projecting an immutable
:class:`~histgerm.research.discovery_orchestration.DiscoveryConfig` into the
:class:`~histgerm.research.discovery_protocol.RunParameters` whose digest seeds
the journal ``run_started`` event; :func:`journal_adapters.discovery_run_events`
consumes this projection.
"""

from __future__ import annotations

from .discovery_orchestration import DiscoveryConfig
from .discovery_protocol import RunParameters

__all__ = ["run_parameters"]


def run_parameters(config: DiscoveryConfig, run_on: str) -> RunParameters:
    """Project one discovery configuration into its immutable run parameters.

    ``run_on`` is supplied by the caller so a single event stream stamps the
    ``run_started`` payload and its ``parameters_digest`` with the same date.
    """

    return RunParameters(
        category=config.category,
        stage=config.stage.value,
        qualifiers=list(config.qualifiers),
        max_mined_terms=config.max_mined_terms,
        max_exclusion_groups=config.max_exclusion_groups,
        run_on=run_on,
    )
