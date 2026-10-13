"""Which runs are currently executing, so one of them can be cancelled.

The loop's `CancelToken` is cooperative: somebody else has to set it. This is that somebody — a
small registry mapping a run's id to its token, so an HTTP request naming a run can find the
flag and set it.

**Why this is in-process and not in the database.** A cancel has to reach a *running coroutine*
in *this* process. A row written to SQLite would tell some other worker — or a restarted one —
that a run it does not have should stop, and the token it names would not exist there. So this
is deliberately memory, and deliberately lost on restart: a run that a restart killed cannot be
cancelled, because it is already over. That is a property of the mechanism, not a limitation to
be worked around, and the endpoint documents it by answering honestly when the id is unknown.

**Why the registry hands out ids rather than taking them.** The caller supplies its session id
and the registry mints a run id, so a client cannot choose one that collides with a run it does
not own. Cancellation is scoped by the same ``user_id`` rule as every other read of user-owned
data in this service (Stage 2): a cancel for somebody else's run is indistinguishable from a
cancel for an id that does not exist, so the endpoint cannot be used to probe which runs exist.

**Why a token is not removed on cancel.** ``finish`` removes it, and cancel only sets the flag.
Removing the token on cancel would mean a second cancel — a user clicking twice, a client
retrying a timeout — answered with "no such run" for a run that is still stopping, which reads
as a failure when the cancel in fact worked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .budget import CancelToken


@dataclass(frozen=True)
class InflightRun:
    """One run that is currently executing."""

    run_id: str
    session_id: str
    user_id: str
    #: Set from outside to ask the run to stop. See `CancelToken` for why it is a flag.
    token: CancelToken


@dataclass
class InflightRegistry:
    """The runs this process currently has.

    Not thread-shared: the service runs one event loop, and every mutation happens between
    awaits, so no lock is needed and adding one would suggest a concurrency this does not have.
    """

    _runs: dict[str, InflightRun] = field(default_factory=dict, repr=False)

    def start(self, *, session_id: str, user_id: str) -> InflightRun:
        """Register a run and return it, with a fresh token and a minted id.

        The id is minted here rather than accepted so a client cannot name a run it does not
        own — see the module docstring.
        """
        run = InflightRun(
            run_id=f"run-{uuid.uuid4().hex}",
            session_id=session_id,
            user_id=user_id,
            token=CancelToken(),
        )
        self._runs[run.run_id] = run
        return run

    def finish(self, run_id: str) -> None:
        """Forget a run. Idempotent, because a run that ended twice is not an error."""
        self._runs.pop(run_id, None)

    def cancel(self, run_id: str, *, user_id: str) -> bool:
        """Ask a run to stop. Returns whether a run was found.

        Scoped by ``user_id`` on purpose: an unscoped lookup would let any caller stop any run
        by guessing an id, and would report whether an id exists — which is information about
        another user's activity. A run belonging to somebody else is reported exactly as a
        run that does not exist, so the endpoint cannot be used to enumerate them.
        """
        run = self._runs.get(run_id)
        if run is None or run.user_id != user_id:
            return False
        run.token.cancel("cancelled by the user")
        return True

    @property
    def count(self) -> int:
        """How many runs are executing. For a test, and for a health reading."""
        return len(self._runs)

    def __contains__(self, run_id: object) -> bool:
        return run_id in self._runs
