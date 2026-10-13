"""Cross-service and contract tests for the whole repository.

Why this is a package rather than a bare directory: the contract suite shares a
loader and a set of recorded facts about the ticketing mock, and it shares them by
importing a module. A relative import needs a package, and a package also gives
``mypy`` an unambiguous module name for every test file — without it, two files
with the same basename in different directories collide as duplicate modules.

Test layout:

``tests/unit/``       hermetic, fast, no stack and no credential. Run in CI by default.
``tests/contract/``   the ticketing mock's behaviour, pinned. Also hermetic: the mock
                      is loaded in-process it needs no container.
``tests/integration/``  (later) tests that need the compose stack running.
"""
