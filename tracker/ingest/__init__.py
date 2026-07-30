"""Ingest paths. Each produces :class:`~tracker.ingest.records.IngestRecord`
objects and hands them to :func:`tracker.upsert.upsert_record`.

That single seam is what makes "three sources, one normalizer, one upsert path"
real rather than aspirational, and it is the injection point every ingest test
uses to stay offline.
"""

from tracker.ingest.records import (
    EventRecord,
    IngestRecord,
    IngestReport,
    SourceRecord,
)

__all__ = ["EventRecord", "IngestRecord", "IngestReport", "SourceRecord"]
