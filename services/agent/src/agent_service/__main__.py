"""Run the agent service: ``python -m agent_service``.

`factory.build_app` assembles the service; this module owns the three things a library
deliberately does not — configuring logging, refusing to start when the configured model provider
has nothing to authenticate with, and telling the operator where to look.

**The startup refusal is the reason this module exists.** A provider selected without the key it
needs would otherwise fail on the first turn, inside someone's conversation, as a provider error
with a stack trace. Failing here instead puts it in the terminal the operator just typed into,
before anything has been served — and exits non-zero, so a container orchestrator restarts rather
than routing traffic to a service that cannot answer.

**The assembled app is passed to uvicorn, not an import string.** With an import string uvicorn
re-imports the module per worker, which would build the engine and the provider again in each one;
and with ``workers > 1`` plus the default SQLite file, several processes would open the same file
for writing. Passing the instance makes the single-process assumption explicit rather than
implicit in a flag nobody set.

**``log_config=None`` keeps one logging configuration.** Uvicorn installs its own by default,
which would replace the levels and format set here and would make the service's own records
disappear behind its access log. The access log is still emitted; it is only the *configuration*
that is left alone.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from agent_service.config import get_settings
from agent_service.factory import build_app

#: One format for both the service and the server, so a log line's origin is its logger name and
#: not the shape of the line.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

#: Exit status for "this process was told to run something it cannot". Distinct from 1 so a
#: wrapper script can tell a misconfiguration from a crash, which is the distinction an operator
#: needs at 3am.
EXIT_MISCONFIGURED = 2


def main() -> int:
    """Configure, check, and serve. Returns the process exit status."""
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format=LOG_FORMAT, stream=sys.stderr)
    logger = logging.getLogger(__name__)

    if not settings.selected_provider_is_usable():
        logger.error(
            "provider %r is selected but has nothing to authenticate with; available: %s",
            settings.agent_model_provider,
            ", ".join(settings.configured_providers()),
        )
        return EXIT_MISCONFIGURED

    app = build_app(settings)
    logger.info(
        "provider=%s  database=%s  traces=%s",
        settings.agent_model_provider,
        settings.database_url,
        settings.trace_dir,
    )
    logger.info(
        "serving on http://%s:%d  (page at /, health at /ping)",
        settings.agent_host,
        settings.agent_port,
    )

    uvicorn.run(app, host=settings.agent_host, port=settings.agent_port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
