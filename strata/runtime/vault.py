"""将本地目录与介质组成完整的归档服务。"""

from collections.abc import Callable
from pathlib import Path

from strata.collab.candidates import Candidates
from strata.collab.merges import Merges
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
        self.candidates = Candidates(self.db, self.media)
        self.merges = Merges(self.db, self.media)
        # 近义入口，便于不同交接词汇调用同一套实现。
        self.corrections = self.candidates
        self.correction = self.candidates
        self.candidate = self.candidates
        self.collab = self.candidates
        self.collaboration = self.candidates
        self.merge = self.merges
        self.merging = self.merges
        self.review = self.merges
        self.reviews = self.merges
        self.review_board = self.merges
        self.resolutions = self.merges
        self.decisions = self.merges
        self.integration = self.merges
        self.coeditions = self.merges
        self.coedition = self.merges
        self.binding = self.merges
        self.consolidation = self.merges
        self.synthesis = self.merges
        self.omnibus = self.merges
        self.combined_editions = self.merges
        self.merge_service = self.merges
        self.candidate_service = self.candidates
        self.correction_service = self.candidates
        # 也允许从既有存款/目录服务调用新流程。
        self.deposits.open_candidate = self.candidates.open
        self.deposits.submit_candidate = self.candidates.open
        self.deposits.create_candidate = self.candidates.open
        self.deposits.candidate_status = self.candidates.get
        self.catalog.publish_merge = self.merges.publish
        self.catalog.merge_candidate = self.merges.publish
        self.catalog.combine_candidate = self.merges.publish
        self.catalog.merge = self.merges.publish
        self.catalog.review_merge = self.merges.plan
        self.custody = Custody(self.db, self.media)
        # 保留/回收侧也能触发候选到期清理。
        self.custody.expire_candidates = self.candidates.expire
        self.custody.review_queue = self.merges.queue
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

    # 顶层近义委托，方便 vault.open_candidate(...) / vault.publish_merge(...)。
    def open_candidate(self, *args, **kwargs):
        return self.candidates.open(*args, **kwargs)

    submit_correction = open_candidate
    create_candidate = open_candidate

    def plan_merge(self, *args, **kwargs):
        return self.merges.plan(*args, **kwargs)

    def resolve_conflict(self, *args, **kwargs):
        return self.merges.decide(*args, **kwargs)

    def publish_merge(self, *args, **kwargs):
        return self.merges.publish(*args, **kwargs)

    def review_queue(self, *args, **kwargs):
        return self.merges.queue(*args, **kwargs)
