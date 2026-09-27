"""Limit the first claim-status tool result to what a caller needs first."""

from errors import PolicyError

DETAIL_TOPICS = frozenset(
    {"next_step", "documents", "document_receipt", "claim_context", "representative"}
)


def claim_headline(claim: dict) -> dict:
    """Expose the stored status without releasing every detail in one turn."""

    return {
        "result": "found",
        "claim_status": claim["status"],
    }


def claim_detail(claim: dict, topic: str) -> dict:
    """Expose only the requested slice of an authorized claim projection."""

    if topic not in DETAIL_TOPICS:
        raise PolicyError("INVALID_INPUT")
    if topic == "next_step":
        detail = {
            "next_action": claim.get("next_action"),
        }
        if claim.get("estimated_next_step_date"):
            detail["estimated_next_step_date"] = claim["estimated_next_step_date"]
    elif topic == "documents":
        detail = {
            "required_documents": claim["required_documents"],
            "submission": claim["submission"],
        }
    elif topic == "document_receipt":
        detail = claim["document_receipt"]
    elif topic == "representative":
        detail = {"assigned_representative": claim.get("assigned_representative")}
    else:
        detail = {
            key: claim[key]
            for key in (
                "claim_id_display",
                "claim_type",
                "loss_date",
                "last_updated_date",
            )
            if claim.get(key) is not None
        }
    return {"result": "found", "topic": topic, "detail": detail}
