"""The signed-in user's console preferences (the GUI redesign, BRIEF §4d): `GET` and `PUT /api/me/preferences`.

Installed by app/main.py next to the pins (app/rapps.py); stored by app/db.py's `UserPreference`, one JSON object per user. Every field is checked
against a closed set (an enum or a bounded number), so nothing the browser sends ends up in the page unvalidated: the SPA puts `theme`, `accent` and
`size` straight onto `<html>` as attributes. A field left out of a `PUT` keeps its default; an unknown field is refused (422), so a typo does not
silently vanish. Any signed-in user may read and change their own preferences, and only their own: there is no username in the path.
"""

import json
from typing import Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, Field

START_PAGES = ("/", "/flows", "/rapps", "/approvals", "/decisions", "/safeguards", "/aiml", "/policy", "/alarms", "/kpis", "/topology",
               "/configuration", "/software", "/infrastructure", "/data")


class Preferences(BaseModel):
    """One user's console preferences. `theme` "system" follows the browser's `prefers-color-scheme`; `size` is the root text size (s 90 %,
    m 100 %, l 112 %, xl 125 %); `accent` the accent colour; `startPage` where the console opens after sign-in; `rowsPerPage` the default page size
    of every server table; `timeZone` "local" or "UTC"; `clock` 12- or 24-hour; `reduceMotion` turns off animation; `alarmSound` a sound on a new
    critical alarm; `language` the language of the console's words (GUI-6.3: `en`, or `de`)."""
    model_config = ConfigDict(extra="forbid")

    theme: Literal["dark", "light", "system"] = "dark"
    size: Literal["s", "m", "l", "xl"] = "m"
    accent: Literal["volt", "blue", "teal", "amber", "radisys"] = "volt"
    startPage: Literal[START_PAGES] = "/"            # type: ignore[valid-type]
    rowsPerPage: Literal[25, 50, 100] = 50
    timeZone: Literal["local", "UTC"] = "local"
    clock: Literal["12h", "24h"] = "24h"
    reduceMotion: bool = False
    alarmSound: bool = Field(default=False, description="a short sound when a new critical alarm arrives")
    language: Literal["en", "de"] = "en"


DEFAULTS = Preferences()


def install(app: FastAPI, *, current_session) -> None:
    """Add the two routes to `app`; `current_session` is main.py's session dependency (it also checks the CSRF token on the `PUT`)."""

    @app.get("/api/me/preferences", response_model=Preferences)
    def my_preferences(session=Depends(current_session)):
        """The user's saved preferences, or the defaults when they never saved any. A stored value that no longer validates (a field this
        release dropped) falls back to the defaults for the fields it cannot read."""
        stored = app.state.db.preferences(session.user.username)
        if stored is None:
            return DEFAULTS
        try:
            raw = json.loads(stored)
        except ValueError:
            return DEFAULTS
        known = {k: v for k, v in raw.items() if k in Preferences.model_fields} if isinstance(raw, dict) else {}
        try:
            return Preferences(**known)
        except ValueError:
            return DEFAULTS

    @app.put("/api/me/preferences", response_model=Preferences)
    def save_my_preferences(body: Preferences, session=Depends(current_session)):
        """Replace the user's preferences with `body` (fields left out take their default); answers what was stored. 422 for an unknown field
        or a value outside its set."""
        app.state.db.save_preferences(session.user.username, body.model_dump_json())
        return body
