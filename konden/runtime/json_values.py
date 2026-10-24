"""Encode trusted Runtime values as detached JSON containers."""

from collections.abc import Mapping


def thaw_value(value: object) -> object:
    """Copy frozen objects and arrays without changing scalar values."""
    if isinstance(value, Mapping):
        return {key: thaw_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_value(item) for item in value]
    return value
