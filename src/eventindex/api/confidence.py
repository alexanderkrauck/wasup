"""Shared public visibility policy for confidence-bearing occurrences."""

# ARCHITECTURE §7: tentative hints stay queryable, but the default public
# index does not present them as ordinary events.
DEFAULT_MIN_CONFIDENCE = 0.4

# Staleness is evaluated at read time so a stalled acquisition pipeline fades
# to an honestly empty default surface rather than frozen confident results.
EFFECTIVE_CONFIDENCE_SQL = """
    e.confidence * power(0.9, least(50, greatest(0, floor(
        extract(epoch from now() - o.last_confirmed_at)
        / nullif(extract(epoch from coalesce(e.expected_cadence, interval '7 days')), 0)
    ))))
"""
