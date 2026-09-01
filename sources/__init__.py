"""Job sources, split by what they are good at.

    models       the Job shape + throttled HTTP helpers (shared)
    ats          Greenhouse / Ashby / Lever board adapters, driven by a registry
    aggregators  the wide sweep -- discovery, feeds the remote registry
    boards_pk    Pakistani job boards via sitemap (Track A)

Two tracks, opposite strategies, because the pools have opposite shapes:
  Track A (Pakistan)      curated company list -- no aggregator indexes PK, and
                          the set of good local employers is small and stable.
  Track B (remote-global) wide sweep -> derived registry -- the takeable roles are
                          a long tail no hand-written list can reach.
"""
from .models import Job, get, get_text, strip_html, iso

__all__ = ["Job", "get", "get_text", "strip_html", "iso"]
