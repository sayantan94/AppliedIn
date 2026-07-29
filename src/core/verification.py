"""One-time verification codes, handed to a waiting browser session.

Oracle emails a validation code the first time you apply from an account, and the
form will not go on without it. Nothing in the pipeline can read that email, so
the code has to come from the owner — while the session is still open, because
the half-filled form and the account's own state live in that browser. Ending the
session to ask and starting a new one to answer loses both, and the code expires
before the second run gets there.

So the session waits. It cannot poll a file: its tools are the browser plus Write,
deliberately, so an unattended run cannot read anything on disk. It CAN read a
page, which is the whole trick here — it opens a page this module serves and
reloads it until the code appears.

A code is short lived by design (Oracle's lasts about ten minutes), so it is
stored with a TTL rather than kept: an old code left lying around would be typed
into a later application and rejected, which looks like the form failing rather
than the code being stale.
"""

from __future__ import annotations

import time
from typing import Any

from .logging import get_logger

log = get_logger(__name__)

# Oracle's code lasts about ten minutes. Matching that exactly means a code that
# is still in here is still worth typing.
CODE_TTL_S = 600

# How long a session is willing to sit waiting. Slightly under the code's life:
# waiting longer than the code can survive only burns browser time to type
# something that will be rejected.
WAIT_TTL_S = 540

_CODE = "verify:code"      # hash pk -> code
_CODE_AT = "verify:code:at"    # hash pk -> unix seconds the code was given
_WAIT = "verify:waiting"   # hash pk -> unix seconds the session started waiting
_SEEN = "verify:seen"      # hash pk -> "<unix seconds last read>|<read count>"


class Verification:
    """Codes in flight between the owner and a waiting browser session."""

    def __init__(self, client: Any) -> None:
        self.r = client

    # --- the session's side ------------------------------------------------
    def start_waiting(self, pk: str, company: str = "") -> None:
        """Called when the session opens the wait page. Registering the wait HERE,
        rather than asking the session to announce it separately, means the
        dashboard learns about it from the one action that always happens."""
        # FIRST read only. The session reloads this page every twenty seconds, so
        # rewriting the timestamp each time meant the clock never advanced: the
        # nine minute cap could never fire and the owner was told "about 9 min
        # left" forever while the code quietly expired behind it.
        # Every read updates a SEPARATE last-seen record. The start time must not
        # move (the cap depends on it), but without knowing when the session last
        # looked there is no way to tell a session still watching from one that
        # wandered off — and those need opposite responses: wait, or start again.
        self.r.hset(_SEEN, pk, f"{time.time()}|{int(self.polls(pk)) + 1}")
        if self.r.hget(_WAIT, pk):
            return
        self.r.hset(_WAIT, pk, f"{time.time()}|{company}")
        log.info("waiting for a verification code for %s", pk)

    def polls(self, pk: str) -> int:
        raw = self.r.hget(_SEEN, pk)
        try:
            return int(str(raw).split("|")[1]) if raw else 0
        except (IndexError, ValueError):
            return 0

    def last_seen(self, pk: str) -> float:
        """Unix seconds when the session last read the page, 0 if never."""
        raw = self.r.hget(_SEEN, pk)
        try:
            return float(str(raw).split("|")[0]) if raw else 0.0
        except (TypeError, ValueError):
            return 0.0

    def code_for(self, pk: str) -> str:
        """The code, or empty while none has been given or it has expired."""
        raw = self.r.hget(_CODE, pk)
        if not raw:
            return ""
        try:
            given = float(self.r.hget(_CODE_AT, pk) or 0)
        except (TypeError, ValueError):
            given = 0.0
        if time.time() - given > CODE_TTL_S:
            self.clear(pk)
            log.info("verification code for %s expired before it was read", pk)
            return ""
        return str(raw)

    def waited_too_long(self, pk: str) -> bool:
        raw = self.r.hget(_WAIT, pk)
        if not raw:
            return False
        try:
            since = float(str(raw).split("|")[0])
        except (TypeError, ValueError):
            return False
        return (time.time() - since) > WAIT_TTL_S

    # --- the owner's side --------------------------------------------------
    def give(self, pk: str, code: str) -> bool:
        """Hand a code to whichever session is waiting on this job."""
        code = (code or "").strip()
        if not code:
            return False
        self.r.hset(_CODE, pk, code)
        self.r.hset(_CODE_AT, pk, str(time.time()))
        log.info("verification code received for %s", pk)
        return True

    def pending(self) -> list[dict]:
        """Jobs whose session is waiting for a code right now, freshest first.

        Waits that have aged out are dropped as they are read: the session behind
        them has already given up, and offering the owner a box that can no longer
        reach anything is worse than showing nothing.
        """
        now, out = time.time(), []
        for pk, raw in (self.r.hgetall(_WAIT) or {}).items():
            parts = str(raw).split("|", 1)
            try:
                since = float(parts[0])
            except (TypeError, ValueError):
                self.r.hdel(_WAIT, pk)
                continue
            if now - since > WAIT_TTL_S:
                self.r.hdel(_WAIT, pk)
                continue
            seen = self.last_seen(pk)
            out.append({"pk": pk, "company": parts[1] if len(parts) > 1 else "",
                        "waiting_s": int(now - since),
                        "polls": self.polls(pk),
                        # How long since the session last looked. Large means it has
                        # stopped watching and the code will never be typed.
                        "unseen_s": int(now - seen) if seen else -1,
                        "answered": bool(self.r.hget(_CODE, pk))})
        return sorted(out, key=lambda d: d["waiting_s"])

    def clear(self, pk: str) -> None:
        """Forget everything about this job's code. Called once it has been typed,
        so the next application starts from nothing rather than reusing it."""
        self.r.hdel(_CODE, pk)
        self.r.hdel(_CODE_AT, pk)
        self.r.hdel(_WAIT, pk)
        self.r.hdel(_SEEN, pk)
