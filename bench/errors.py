"""Domain errors and exit-code policy for the benchmark platform."""


class BenchError(Exception):
    """An expected benchmark-platform failure."""

    exit_code = 1


class ValidationError(BenchError):
    """Invalid user-provided configuration or identifier."""

    exit_code = 2


class ExternalCommandError(BenchError):
    """A required external command failed."""
