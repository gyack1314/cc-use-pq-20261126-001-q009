"""将本地目录与介质组成完整的归档服务。"""

from collections.abc import Callable
from pathlib import Path

from strata.delivery.service import Delivery
from strata.preservation.audit import Audit
from strata.preservation.catalog import Catalog
from strata.preservation.custody import Custody
from strata.preservation.footprint import Footprint
from strata.preservation.integrity import Integrity
from strata.preservation.inspection import Inspection
from strata.runtime.database import Database
from strata.runtime.events import page
from strata.runtime.media import Media
from strata.transfer.deposits import Deposits, collection_row


class Vault:
    def __init__(self, root: Path, clock: Callable[[], int] | None = None):
        self.db = Database(root / "catalog.sqlite3", clock)
        self.media = Media(root / "objects")
        self.catalog = Catalog(self.db, self.media)
        self.deposits = Deposits(self.db, self.media)
        self.custody = Custody(self.db, self.media)
        self.integrity = Integrity(self.db, self.media)
        self.inspection = Inspection(self.db)
        self.audit = Audit(self.db)
        self.footprint = Footprint(self.db)
        self.delivery = Delivery(self.db, self.media, root / "scratch")

    def events(self, collection: str | None, after: int, limit: int) -> dict:
        with self.db.transaction(write=False) as connection:
            if collection is not None:
                collection_row(connection, collection)
            return page(connection, collection, after, limit)
