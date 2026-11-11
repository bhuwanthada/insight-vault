"""Pulling tickets from a connected tool into an organization's store, one resumable page at a time."""
import logging

from ..secrets_box import SecretsUnavailable
from ..tenancy import QuotaExceeded, UnknownOrg, ensure_room
from . import KINDS, ConnectorError, HttpClient

log = logging.getLogger("insight_vault.sync")
LEASE_SECONDS = 900  # a crashed worker's claim expires after this long


class SyncService:
    def __init__(self, tenants, repo, box, clock, settings, http_factory=HttpClient):
        self.tenants, self.repo, self.box, self.clock = tenants, repo, box, clock
        self.settings, self.http_factory = settings, http_factory

    def build(self, row):
        """The live connector for a stored configuration. Raises ConnectorError / SecretsUnavailable."""
        secrets = self.box.decrypt(row["secrets_enc"])
        return KINDS[row["kind"]](row["config"], secrets, row["mapping"], self.http_factory(), self.clock(),
                                  self.settings.sync_initial_days, self.settings.connector_extra_hosts)

    def run(self, row, max_pages=None):
        """Sync one connector (its lease must already be held). Always releases the lease and records the outcome.

        Progress is saved after every page, so a long sync that is interrupted resumes where it stopped."""
        limit = max_pages or self.settings.sync_max_pages
        fetched = pages = 0
        more, status, error = False, "ok", None
        try:
            ctx = self.tenants.context(row["org_id"])
            connector = self.build(row)
            for tickets, cursor in connector.pages(row["cursor"]):
                ids = [t["id"] for t in tickets]
                with ctx.store.lock:
                    ensure_room(ctx.quotas, "tickets", ctx.store.ticket_count(), len(set(ids) - ctx.store.existing_ticket_ids(ids)))
                    ctx.store.upsert_tickets(tickets)
                self.repo.save_cursor(row["id"], cursor)
                fetched, pages = fetched + len(tickets), pages + 1
                if pages >= limit:
                    more = True  # there may be more: the next run continues from the saved cursor
                    break
        except (QuotaExceeded, ConnectorError, SecretsUnavailable, UnknownOrg) as exc:
            status, error = "error", str(exc) or type(exc).__name__
        except Exception as exc:  # a vendor payload we did not expect must never take down the worker
            log.exception("connector %s crashed", row["id"])
            status, error = "error", f"unexpected response from the tool ({type(exc).__name__}); see the server log"
        now = self.clock()
        self.repo.finish(row["id"], status, error, fetched, now, 0 if more else row["interval_minutes"])
        try:
            self.tenants.factory.for_org(row["org_id"]).append_audit(
                {"at": now.isoformat(), "actor": f"connector:{row['kind']}", "action": "connector_sync",
                 "detail": f"{status}: {fetched} tickets" + (f" ({error})" if error else "")}, self.settings.audit_retention)
        except Exception:  # the organization may have been deleted while the sync ran
            log.warning("could not audit the sync of connector %s", row["id"], exc_info=True)
        return {"status": status, "error": error, "fetched": fetched, "pages": pages, "more": more}
