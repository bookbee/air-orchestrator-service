"""Clients for the other AIR services.

Two rules shape this package, and both are structural rather than conventional:

* **``tools.py`` never grows an execute-shaped method.** The read/write split is a
  property of this module surface — there is no code path from a synthesis step to
  a mutation, because the read-path client cannot express one (docs/01-hld.md §6).
* **Only ``infra.py`` is mandatory.** Every other client treats its service being
  absent as a missing capability rather than an error, which is what makes most of
  the estate being unbuilt a supported deployment.
"""
