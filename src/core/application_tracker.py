"""Private follow-up notes, stored apart from the pipeline's changing status."""
from datetime import UTC, date, datetime

OUTCOMES = {"", "waiting", "interview", "offer", "rejected", "withdrawn"}


def validate(body: dict) -> dict:
    notes = body.get("notes", "")
    outcome = body.get("outcome", "")
    follow_up = body.get("follow_up", "")
    if not isinstance(notes, str) or len(notes) > 10000:
        raise ValueError("Notes must be text, up to 10,000 characters.")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError("Choose a valid application outcome.")
    if not isinstance(follow_up, str):
        raise ValueError("Choose a valid follow-up date.")
    if follow_up:
        try:
            if date.fromisoformat(follow_up).isoformat() != follow_up:
                raise ValueError()
        except ValueError:
            raise ValueError("Choose a valid follow-up date.") from None
    return {"notes": notes, "outcome": outcome, "follow_up": follow_up,
            "saved_at": datetime.now(UTC).isoformat()}
