"""Known conflicts at the user authorization boundary."""


class AuthorizationTargetNotFound(LookupError):
    """The requested command does not belong to this session."""


class AuthorizationConflict(Exception):
    """The requested authorization decision is no longer available."""
