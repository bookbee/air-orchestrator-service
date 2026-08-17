"""Pydantic models. No module here may import from ``air_platform.api``.

Dependencies run downward only: schemas are what the HTTP layer serialises, so a
schema that reached back into the HTTP layer would make the response models
unusable anywhere else — including in the turn engine, which produces them
without a request in scope.
"""
