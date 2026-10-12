class ScopeError(Exception):
    """A bounded, user-visible failure; never silently retry instrument commands."""


class ProtocolError(ScopeError):
    pass


class IdentityError(ScopeError):
    pass


class BusyError(ScopeError):
    pass


class MeasurementPreflightError(ScopeError):
    """A rejected measurement request before any setting/arming command."""
    writes_sent = 0
