"""
Beta 11.12D2: a bounded, deterministic set of IDOR probe ids for the
draft-preview security tests.

Beta 11.4/11.5/11.8/11.10 each proved their preview endpoint fail-closed by
walking ``range(1, foreign.pk + 2)`` - every id from 1 up to just past the
foreign draft. That is correct but unbounded: primary keys come from a
PostgreSQL sequence that is *not* reset between test classes, so by the time
those tests run inside the full suite ``foreign.pk`` is in the thousands and
each of them issues that many admin HTTP requests. Beta 11.12A measured the
result - 8.7 s, 7.4 s and 4.3 s for three of them - and, worse, the cost grows
with every object any earlier test creates.

Walking the whole range never added security value either: the interesting
ids are the foreign draft itself and its immediate neighbourhood, not the
2,000 ids in between that belong to unrelated models' sequence history.

This module therefore answers one question - "which ids should a non-owning
author be denied?" - with a fixed, deduplicated, sorted tuple:

* the **foreign draft's real id** - the only probe that hits an existing row
  the requester must not see, and the reason the whole test exists;
* the **immediate neighbours** of the own and the foreign id (±1) - the
  realistic guessing attempt, and the case where an off-by-one in an
  ownership check would show up;
* ``0`` - accepted by the route (the admin URL uses ``<path:object_id>``, not
  ``<int:...>``, so it is a genuinely reachable branch) and never a real
  primary key;
* one id **provably above every existing row**, derived from the caller's own
  fixture state rather than from a hardcoded constant like ``999999`` that a
  large database could one day actually contain.

The caller's own allowed id is removed, because a probe list that contains it
would assert 404 for a draft the owner may legitimately open. Everything the
function returns must be denied.

Test-only helper: it performs no query, no request, no randomness, and it is
imported exclusively by test modules.
"""

#: How far above the highest known row the "guaranteed absent" probe sits.
#: Large enough not to collide with a neighbour probe, small enough to stay a
#: plain readable integer.
ABSENT_ID_OFFSET = 1000

#: Hard upper bound on the returned tuple, independent of how large the ids
#: are: foreign, own ±1, foreign ±1, ``0`` and the absent id, minus the
#: caller's own id and any duplicates.
MAX_PROBE_IDS = 7


def build_bounded_idor_probe_ids(*, own_id, foreign_id, existing_ids):
    """
    Return the ids a non-owning requester must be denied, as a sorted tuple.

    ``own_id``
        The requester's own draft. Excluded from the result - it is the
        positive control, asserted separately by each test.
    ``foreign_id``
        A real draft belonging to somebody else. Always included.
    ``existing_ids``
        Every id that currently exists for this model, read once by the caller
        (a single ``values_list`` query, never a loop). Only its maximum is
        used, to place the "guaranteed absent" probe above every real row.

    The size of the result never depends on the *magnitude* of the ids, only
    on how much the neighbourhoods overlap - so it is identical for
    ``foreign_id=12`` and ``foreign_id=12_000_000``.
    """
    highest = max([own_id, foreign_id, *existing_ids])

    candidates = {
        # The real foreign draft: existing, forbidden, must not leak.
        foreign_id,
        # Immediate neighbours - the realistic id-guessing attempt.
        own_id - 1,
        own_id + 1,
        foreign_id - 1,
        foreign_id + 1,
        # Accepted by the <path:object_id> route, never a real primary key.
        0,
        # Provably absent: strictly greater than every existing row.
        highest + ABSENT_ID_OFFSET,
    }

    candidates.discard(own_id)
    return tuple(sorted(value for value in candidates if value >= 0))
