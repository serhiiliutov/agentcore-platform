"""The stored model provider: one small row, read at startup and written when it changes.

The table existed before this file did. `ProviderSetting` was declared with a scope, a provider, a
model id and a note, and nothing read or wrote it — so the honest statement in its own docstring was
that "changing provider means changing the environment and restarting". That is what this replaces:
a reviewer can now pick a provider on the page, and the choice is still there after a restart.

**One row per scope, and only ``global`` is used.** ``scope`` allows ``session:<id>`` for a
per-conversation override and no caller sets one. It is left in the schema rather than removed
because the migration is cheaper than the argument, and because the column is what makes the
intended shape legible to whoever adds the override.

**Writing replaces; it never appends.** A second row for one scope would make "which provider is in
force" a question with two answers and a tie-break, which is the failure mode `FactRepository`
describes for memory and is no better here.

**No credential is stored, and none could be.** The row holds a provider *name* and a model id.
Keys come from the environment and live inside the provider instance — so a database dump, a
backup or a stray ``SELECT *`` cannot disclose one, which is the property that makes storing the
choice at all uncontroversial.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_service.config import ProviderName
from agent_service.db.base import utc_now
from agent_service.db.models.operations import ProviderSetting

#: The scope of the service-wide selection: the provider every turn uses unless a future
#: per-session override exists. Named rather than inlined so the one string lives in one place.
GLOBAL_SCOPE = "global"


class ProviderSettingsRepository:
    """Reads and writes the selected provider for one scope."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def current(self, *, scope: str = GLOBAL_SCOPE) -> ProviderSetting | None:
        """The stored selection, or ``None`` when it has never been set.

        ``None`` is a meaningful answer and not an error: it means the process follows the
        environment, which is how a fresh installation behaves and how CI stays deterministic
        without anyone having to clear a row.
        """
        return self._session.execute(
            select(ProviderSetting).where(ProviderSetting.scope == scope)
        ).scalar_one_or_none()

    def store(
        self,
        *,
        provider: ProviderName,
        model_id: str = "",
        note: str | None = None,
        scope: str = GLOBAL_SCOPE,
    ) -> ProviderSetting:
        """Record the selection, replacing whatever was there.

        ``updated_at`` is set explicitly on the update path as well as on insert. The column has
        ``onupdate``, which covers a row modified through an attribute — but this method is the only
        writer, and stating the timestamp here means the value is right even if a session is
        configured in a way that skips the default.
        """
        moment = utc_now()
        existing = self.current(scope=scope)

        if existing is not None:
            existing.provider = provider
            existing.model_id = model_id
            existing.note = note
            existing.updated_at = moment
            self._session.flush()
            return existing

        created = ProviderSetting(
            scope=scope,
            provider=provider,
            model_id=model_id,
            note=note,
            updated_at=moment,
        )
        self._session.add(created)
        self._session.flush()
        return created

    def forget(self, *, scope: str = GLOBAL_SCOPE) -> bool:
        """Drop the stored selection, so the environment decides again. Returns whether a row went.

        A hard delete, because the row is a preference rather than a record of something that
        happened: leaving it in place with a flag would make "follow the environment" a third state
        that every reader would have to know about.
        """
        row = self.current(scope=scope)
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True
