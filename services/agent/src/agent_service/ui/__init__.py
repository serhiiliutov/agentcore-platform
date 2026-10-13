"""The front end: one page, and nothing else.

Deliberately not a service. A separate web process would need its own container, its own config
and its own port, and would buy nothing: the page is a client of the contract `/invocations`
already exposes, so serving it from the same app removes a CORS decision and one more thing to
deploy. The assignment asks for a thin page, and the thin thing is to serve it.

Everything visual is in `page.py` as a string. There is no template engine, no build step and no
static mount, so a reviewer can read the whole front end in one file and a built image cannot be
missing a directory it was supposed to copy.
"""

from __future__ import annotations

from .page import INDEX_HTML

__all__ = ["INDEX_HTML"]
