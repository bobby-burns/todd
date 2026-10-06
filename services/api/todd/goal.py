"""What kind of goal a prompt is, read from its words (no model call), for the checks that run before the planner.

A goal to make content (a TikTok, reels, a promo video, posts) about something that already exists doesn't put anything
new online: no launch plan, no hosting or app-store accounts, and no social accounts unless it asks to post.
"""

from __future__ import annotations

import re

_SITE = (r"(web ?sites?|web ?apps?|landing pages?|waitlist|web ?pages?|homepage|saas|blog|portfolio|"
         r"online (store|shop)|dashboard|browser game|web game|site|app|apps)")
# A verb making a site or app, with at most two words between: "make a website", "build me a new app".
_BUILDS = re.compile(r"\b(build|make|create|develop|code|ship|design|rebuild|redesign|set up|launch|deploy|start|add|"
                     r"put up)\b\s+"
                     r"(?:(?:a|an|the|my|our|me|new)\s+)*(?:[\w-]+\s+){0,2}?" + _SITE + r"\b", re.I)
_CONTENT = (r"(shorts?|reels?|tiktoks?|videos?|clips?|promos?|trailers?|teasers?|ads?|advert|posts?|threads?|"
            r"carousels?|thumbnails?|newsletters?|captions?|content|marketing|ugc)")
# Making content: "make a TikTok for…", "write three posts", "film a demo video" (not "reply to my TikTok comments").
_MAKES = re.compile(r"\b(make|create|produce|film|shoot|edit|script|write|draft|generate|record|design|do)\b"
                    r"[^.\n]{0,40}?\b" + _CONTENT + r"\b", re.I)
POSTING = re.compile(r"\b(post|publish|upload|schedule|share)\b[^.\n]{0,40}\b(on|to)\b|"
                     r"\b(post|publish|upload) (it|them)\b", re.I)


def builds_something(prompt: str) -> bool:
    """The goal makes (or rebuilds) a site or app, not just something about one."""
    return bool(_BUILDS.search(prompt or ""))


def content_only(prompt: str) -> bool:
    """The goal makes content (videos, posts) about something that already exists, not the thing itself."""
    return bool(_MAKES.search(prompt or "")) and not builds_something(prompt)


def wants_posting(prompt: str) -> bool:
    """The goal asks for the content to be posted somewhere (which needs that account)."""
    return bool(POSTING.search(prompt or ""))
