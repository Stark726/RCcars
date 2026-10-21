"""Catalogue hints for read-only inspection ordering, never purchase eligibility."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Product


def normalize_catalogue_time(value) -> str | None:
    """Retain only an explicit, valid timezone-aware catalogue timestamp."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _hint_rank(product: Product) -> int:
    # Whole tag labels avoid treating "renewal" or "New York" as a new release.
    # Normalized separators also cover common labels such as "badge:coming-soon".
    upcoming = {'coming soon', 'upcoming', 'upcoming release', 'pre launch',
                'prelaunch', 'scheduled launch'}
    new = {'new', 'new release', 'new releases', 'new arrival', 'new arrivals'}
    rank = 2
    tags = product.tags
    if isinstance(tags, str):
        tags = tags.split(',')
    if not isinstance(tags, (list, tuple)):
        tags = []
    for raw in tags:
        if not isinstance(raw, str):
            continue
        label = re.sub(r'[^a-z0-9]+', ' ', raw.casefold()).strip()
        label = re.sub(r'^(?:(?:badge|status|label|tag|filter|product) )+', '', label)
        if label in upcoming:
            return 0
        if label in new:
            rank = 1
    return rank


def _catalogue_date(product: Product):
    for field in ('published_at', 'created_at'):
        normalized = normalize_catalogue_time(getattr(product, field, None))
        if normalized:
            return field, datetime.fromisoformat(normalized).timestamp()
    return None, None


def discovery_priority(product: Product) -> tuple:
    """Rank upcoming/new labels, catalogue recency, then RLC and stable identity.

    Catalogue dates describe listings, not release times. Unknown dates sort last
    within each tag class; an ID is only a deterministic tie-breaker, not a date.
    The caller separately keeps never-inspected pages ahead of retry attempts.
    """
    _, timestamp = _catalogue_date(product)
    return (_hint_rank(product), -timestamp if timestamp is not None else math.inf,
            0 if 'rlc' in product.groups else 1, str(product.id))


def discovery_hint(product: Product) -> str:
    """Explain the strongest ordering hint without claiming a verified launch."""
    rank = _hint_rank(product)
    if rank == 0:
        return 'upcoming tag'
    if rank == 1:
        return 'new release tag'
    field, _ = _catalogue_date(product)
    if field == 'published_at':
        return 'publication date'
    if field == 'created_at':
        return 'creation date'
    return 'RLC collection' if 'rlc' in product.groups else 'catalogue fallback'
