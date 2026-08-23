"""Typed failures surfaced by the landing command-line tools."""


class LandingError(RuntimeError):
    """Base class for an expected, actionable policy failure."""


class GitError(LandingError):
    """A Git command or repository invariant failed."""


class EvidenceError(LandingError):
    """Pre-ship review evidence is absent, malformed, or stale."""
