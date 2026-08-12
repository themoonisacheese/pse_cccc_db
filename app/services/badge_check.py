"""Stack Exchange badge checking for the CCCC moderator catch-up page.

The CCCC project awards two badges on Puzzling SE:
  - CCCContributor (badge id 212): first clue authored
  - CCCChampion   (badge id 213): 50th clue authored

This service answers the question "has this author actually been granted
their badge yet?" It does this by:

  1. Resolving clue authors (stored as display names) to Puzzling SE user
     IDs via the `/users?inname=` search, or via the local `users` table
     when the author has logged in (the `se_user_id` column).
  2. Fetching each user's granted badges from `/users/{ids}/badges` and
     checking whether the CCCC badges (matched by *name*, which is more
     robust than hard-coded IDs) appear in the list.

Known Stack Exchange API quirks handled here:
  - The user-badges route caps at `pagesize` <= 50 and pagination beyond
    page 1 is unreliable, so we fetch per-user and merge pages carefully.
  - Custom badges such as the CCCC ones may not surface while the API is
    in an odd state; callers should treat a lookup that returns *no* data
    as "unknown" rather than "not granted".

The exact badge names/IDs are configurable so the mapping can be corrected
if the API's badge enumeration changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.clue import User

logger = logging.getLogger(__name__)

# The CCCC badges, keyed by the slug used in their help-page URL.
# Each entry maps slug -> (badge_id, canonical display name, label).
CCC_BADGES: dict[str, tuple[int, str, str]] = {
    "contributor": (212, "ccccontributor", "CCCContributor"),
    "champion": (213, "cccchampion", "CCCChampion"),
}

SE_API_BASE = "https://api.stackexchange.com/2.3"
_DEFAULT_HEADERS = {"User-Agent": "CCCC-Badge-Check/1.0"}
# The user-badges route caps at 50 and pagination is flaky past page 1.
_MAX_BADGES_PAGE = 50


def _authed_params(settings, **extra) -> dict:
    """Build query params with the app key (and passed extras) for authed hooks."""
    params = {"key": settings.se_key, "site": settings.se_site}
    params.update(extra)
    return params


class SEBadgeAPIError(RuntimeError):
    """Raised when the Stack Exchange API errors in a blocking way."""


@dataclass
class AuthorBadgeStatus:
    """Grant status for a single author for one badge."""

    badge_slug: str
    badge_label: str
    author: str
    se_user_id: Optional[int]
    clues: int
    should_have: bool       # author has authored enough clues to qualify
    granted: bool           # SE reports the badge as granted
    unknown: bool = False   # API gave no usable answer (treat as unknown)


@dataclass
class BadgeCheckResult:
    """Aggregated result of checking one badge across authors."""

    badge_slug: str
    badge_label: str
    threshold_clues: int
    checked_at: str = ""
    entries: list[AuthorBadgeStatus] = field(default_factory=list)

    @property
    def missed(self) -> list[AuthorBadgeStatus]:
        """Authors who qualify but have NOT been granted the badge."""
        return [e for e in self.entries if e.should_have and not e.granted and not e.unknown]

    @property
    def granted(self) -> list[AuthorBadgeStatus]:
        return [e for e in self.entries if e.should_have and e.granted]

    @property
    def unknown(self) -> list[AuthorBadgeStatus]:
        return [e for e in self.entries if e.should_have and e.unknown]


# ── Name → SE user id resolution ─────────────────────────────


async def resolve_se_user_ids(
    names: list[str],
    db: AsyncSession,
) -> dict[str, Optional[int]]:
    """Map display names to Puzzling SE user IDs.

    Prefers the local `users` table (authoritative when someone has logged
    in), falling back to the SE API `/users?inname=` search.
    """
    settings = get_settings()
    result: dict[str, Optional[int]] = {n: None for n in names}

    # 1) Local users table first (exact, no API quota cost).
    if names:
        known = (await db.execute(
            select(User).where(User.display_name.in_(names))
        )).scalars().all()
        for u in known:
            result[u.display_name] = u.se_user_id

    # 2) For unresolved, query the SE API (exact display-name match preferred).
    unresolved = [n for n in names if result.get(n) is None]
    if not unresolved:
        return result

    async with httpx.AsyncClient(timeout=20.0, headers=_DEFAULT_HEADERS) as client:
        for name in unresolved:
            try:
                r = await client.get(
                    f"{SE_API_BASE}/users",
                    params=_authed_params(settings, inname=name, pagesize=20),
                )
                if r.status_code != HTTPStatus.OK:
                    logger.warning("SE /users inname=%r -> %s", name, r.status_code)
                    continue
                items = r.json().get("items", [])
                # Prefer an exact display-name match.
                exact = [it for it in items if it.get("display_name", "").lower() == name.lower()]
                pick = exact[0] if exact else (items[0] if items else None)
                if pick:
                    result[name] = int(pick["user_id"])
                    logger.info("Resolved %r -> SE user %s", name, pick["user_id"])
            except httpx.HTTPError as e:
                logger.warning("SE /users inname=%r failed: %s", name, e)

    return result


# ── Granted CCCC badge lookup ────────────────────────────────


async def fetch_granted_ccbc_badges(
    se_user_ids: list[int],
) -> dict[int, set[str]]:
    """Fetch, per SE user id, the set of granted CCCC badge slugs.

    Returns a dict of {se_user_id: {"contributor", "champion"} ...}. A user
    missing from the dict means the API gave no usable answer (unknown),
    NOT that they were not granted the badge.
    """
    settings = get_settings()
    # Map canonical lowercase name -> slug
    slug_by_name = {label.lower(): slug for slug, (_, label, _) in CCC_BADGES.items()}

    granted: dict[int, set[str]] = {}

    async with httpx.AsyncClient(timeout=25.0, headers=_DEFAULT_HEADERS) as client:
        # The user-badges route accepts up to 100 ids per call, but the
        # response caps at pagesize 50. We query per user to avoid flaky
        # multi-page coalescing, batching the HTTP over a connection pool.
        for uid in se_user_ids:
            found: set[str] = set()
            got_usable = False
            page = 1
            try:
                while True:
                    r = await client.get(
                        f"{SE_API_BASE}/users/{uid}/badges",
                        params=_authed_params(settings, pagesize=_MAX_BADGES_PAGE, page=page),
                    )
                    if r.status_code != HTTPStatus.OK:
                        logger.warning("SE /users/%s/badges page %d -> %s", uid, page, r.status_code)
                        break
                    # A 200 on page 1 is our signal that the lookup is usable.
                    if page == 1:
                        got_usable = True
                    body = r.json()
                    items = body.get("items", [])
                    for it in items:
                        name = (it.get("name") or "").lower()
                        if name in slug_by_name:
                            found.add(slug_by_name[name])
                    if not body.get("has_more"):
                        break
                    page += 1
                # Only record a user as checked if we got a usable page 1.
                if got_usable:
                    granted[uid] = found
            except httpx.HTTPError as e:
                logger.warning("SE /users/%s/badges failed: %s", uid, e)

    return granted


async def check_badge_for_authors(
    badge_slug: str,
    authors: dict[str, int],
    se_user_ids: dict[str, Optional[int]],
    db: AsyncSession,
) -> BadgeCheckResult:
    """Check grant status of one badge across authors.

    `authors` maps display name -> cumulative authored count.
    `se_user_ids` maps display name -> SE user id (or None if unresolved).
    """
    if badge_slug not in CCC_BADGES:
        raise ValueError(f"Unknown badge slug: {badge_slug}")
    _, _, label = CCC_BADGES[badge_slug]
    threshold = 1 if badge_slug == "contributor" else 50

    # Collect the SE user ids we need to look up.
    eligible = {n: c for n, c in authors.items() if c >= threshold}
    ids_to_check = sorted({
        sid for n, sid in se_user_ids.items()
        if n in eligible and sid is not None
    })

    granted_map = {}
    if ids_to_check:
        granted_map = await fetch_granted_ccbc_badges(ids_to_check)

    entries: list[AuthorBadgeStatus] = []
    for name, count in authors.items():
        if count < threshold:
            continue  # below threshold, not relevant for this badge
        sid = se_user_ids.get(name)
        grants = granted_map.get(sid, None) if sid is not None else None
        if sid is None or grants is None:
            # Could not resolve the user or no usable API answer -> unknown
            entries.append(AuthorBadgeStatus(
                badge_slug=badge_slug, badge_label=label, author=name,
                se_user_id=sid, clues=count, should_have=True,
                granted=False, unknown=True,
            ))
            continue
        entries.append(AuthorBadgeStatus(
            badge_slug=badge_slug, badge_label=label, author=name,
            se_user_id=sid, clues=count, should_have=True,
            granted=badge_slug in grants, unknown=False,
        ))

    return BadgeCheckResult(
        badge_slug=badge_slug, badge_label=label, threshold_clues=threshold,
        entries=entries,
    )
