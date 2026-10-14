class PrimeError(RuntimeError):
    """Controller operation failed; never implies a safe automatic retry."""


class PlaybackRefusedError(PrimeError):
    """Runtime confirmed that the candidate navigation method was not invoked."""


class BusyError(PrimeError):
    pass


class NetworkError(PrimeError):
    """A bounded network/configuration failure code, without device log text."""


class UnavailableError(PrimeError):
    pass


class OutcomeUnknownError(PrimeError):
    """Command may have executed; inspect state rather than replaying it."""


class StaleResultError(PrimeError):
    pass


class EvidenceLostError(PrimeError):
    pass


class CompatibilityError(PrimeError):
    pass


class CancelledError(PrimeError):
    """Operation revoked; cancellation does not mean current playback stopped."""
