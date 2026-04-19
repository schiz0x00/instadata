"""Instagram endpoint constants.

Every Instagram-specific URL, document id and header lives here. When
Instagram rotates a ``doc_id`` — which it does without notice — this is the
only file that changes.

Document ids were captured from a live logged-out session on 2026-07-31.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BASE_URL",
    "DOC_ID_TIMELINE",
    "DOC_ID_TIMELINE_FALLBACKS",
    "GRAPHQL_API_URL",
    "GRAPHQL_QUERY_URL",
    "HIGHLIGHTS_TRAY_URL",
    "MEDIA_INFO_URL",
    "POST_URL_TEMPLATE",
    "PROFILE_URL_TEMPLATE",
    "STORIES_REEL_URL",
    "WEB_PROFILE_INFO_URL",
    "default_headers",
]

BASE_URL: Final = "https://www.instagram.com"

#: Cursored timeline feed. GET, no cookies required.
GRAPHQL_QUERY_URL: Final = f"{BASE_URL}/graphql/query/"

#: Modern POST GraphQL gateway, used by the app for everything else.
GRAPHQL_API_URL: Final = f"{BASE_URL}/api/graphql"

#: Username to profile record, including the numeric id.
WEB_PROFILE_INFO_URL: Final = f"{BASE_URL}/api/v1/users/web_profile_info/"

#: Server-rendered profile page, used as a resolver of last resort.
PROFILE_URL_TEMPLATE: Final = f"{BASE_URL}/{{username}}/"

#: Server-rendered post page.
POST_URL_TEMPLATE: Final = f"{BASE_URL}/p/{{shortcode}}/"

#: Full metadata for one post, keyed by numeric media id. Requires a session:
#: verified on 2026-07-31 that logged-out post pages embed no media JSON at all,
#: rendered or not, so this is the only complete single-post route left.
MEDIA_INFO_URL: Final = f"{BASE_URL}/api/v1/media/{{media_id}}/info/"

#: Stories for one user. Requires an authenticated session.
STORIES_REEL_URL: Final = f"{BASE_URL}/api/v1/feed/reels_media/"

#: Highlight tray for one user. Requires an authenticated session.
HIGHLIGHTS_TRAY_URL: Final = f"{BASE_URL}/api/v1/highlights/{{user_id}}/highlights_tray/"

#: Timeline query verified live: 12 items per page, carousels and reels included.
DOC_ID_TIMELINE: Final = "7950326061742207"

#: Additional timeline document ids, tried in order when the primary stops
#: parsing. Empty by default and deliberately so: an unverified id answers 400
#: and turns one recoverable failure into four wasted tier escalations. Add an
#: id here only after observing it work against live Instagram.
DOC_ID_TIMELINE_FALLBACKS: Final = ()


def default_headers(app_id: str, user_agent: str, referer: str | None = None) -> dict[str, str]:
    """Headers Instagram's web client sends on every XHR.

    ``X-IG-App-ID`` is optional for the timeline query but required by
    ``web_profile_info``; sending it everywhere costs nothing and keeps the
    request shape consistent across tiers.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-IG-App-ID": app_id,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Origin": BASE_URL,
    }
    if referer:
        headers["Referer"] = referer
    return headers
