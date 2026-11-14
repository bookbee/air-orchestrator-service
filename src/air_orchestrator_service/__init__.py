"""air-orchestrator-service — the AIR estate's conversational front door.

See ``docs/01-hld.md`` for how this service relates to the rest of the estate. The
short version: it orchestrates, and it owns no capability of its own. Retrieval is
air-rag-service's, classification air-classifier's, lookups and changes to business
systems air-tools-gateway's, models air-model-gateway's, and stores air-infra's.
"""

__version__ = "0.1.0"
