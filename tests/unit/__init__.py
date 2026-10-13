"""Hermetic unit tests: no stack, no credential, no network.

Everything under here must pass on a clean checkout with nothing running. If a test
needs the compose stack it belongs in ``tests/integration/`` and carries the
``integration`` marker; if it needs a real model credential it carries ``live`` and
is skipped unless ``--run-live`` is passed.
"""
