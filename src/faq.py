"""Pinned, validated public FAQ snapshot loaded outside the caller turn."""

from dataclasses import dataclass
from datetime import date

from errors import PolicyError

KB_VERSION = "faq-v1"
TOPICS = frozenset(
    {
        "office_hours",
        "mailing_address",
        "start_new_claim",
        "what_to_gather",
        "general_process",
        "general_documents",
        "document_submission_general",
        "timing_general",
        "complaint_or_disagreement",
        "emergency",
        "unsupported",
    }
)


@dataclass(frozen=True)
class FaqEntry:
    topic_id: str
    answer_text: str
    effective_date: str


class FaqSnapshot:
    """The whole approved version is captured atomically for one agent session."""

    def __init__(self, entries: dict[str, FaqEntry]) -> None:
        self._entries = entries.copy()

    @classmethod
    def from_rows(cls, rows: list[dict], *, today: date | None = None) -> "FaqSnapshot":
        current = today or date.today()
        entries: dict[str, FaqEntry] = {}
        for row in rows:
            try:
                topic = row["topic_id"]
                version = row["version"]
                answer = row["answer_text"]
                access = row["access_class"]
                effective_from = row["effective_from"]
                effective_to = row["effective_to"]
                active = row["is_active"]
                start = date.fromisoformat(effective_from)
                end = date.fromisoformat(effective_to) if effective_to else None
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyError("VALIDATION_ERROR") from exc
            if (
                not isinstance(topic, str)
                or topic not in TOPICS
                or topic in entries
                or version != KB_VERSION
                or not isinstance(answer, str)
                or not answer.strip()
                or len(answer) > 1000
                or "http" in answer.lower()
                or access != "PUBLIC"
                or active is not True
                or start > current
                or (end is not None and end < current)
            ):
                raise PolicyError("VALIDATION_ERROR")
            entries[topic] = FaqEntry(topic, answer, effective_from)
        if set(entries) != TOPICS:
            raise PolicyError("KB_INCOMPLETE")
        return cls(entries)

    def get_faq(self, topic_id: str) -> dict[str, str]:
        entry = self._entries.get(topic_id)
        if entry is None:
            return {"status": "unknown", "safe_action": "offer_representative"}
        return {
            "status": "found",
            "topic_id": entry.topic_id,
            "answer_text": entry.answer_text,
            "version": KB_VERSION,
            "effective_date": entry.effective_date,
            "access_class": "PUBLIC",
        }
