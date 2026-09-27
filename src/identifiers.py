"""Short-lived normalization and keyed lookup tokens for synthetic identifiers."""

import hashlib
import hmac
import re

from errors import PolicyError

_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_TEENS = {
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
}
_TENS = {
    "twenty": "2",
    "thirty": "3",
    "forty": "4",
    "fifty": "5",
    "sixty": "6",
    "seventy": "7",
    "eighty": "8",
    "ninety": "9",
}


def _spoken_digits(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9\s()+.\-]+", value):
        raise PolicyError("INVALID_INPUT")
    tokens = re.findall(r"[A-Za-z]+|\d+", value.lower())
    if not tokens:
        raise PolicyError("INVALID_INPUT")
    parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _DIGIT_WORDS:
            parts.append(_DIGIT_WORDS[token])
        elif token in _TEENS:
            parts.append(_TEENS[token])
        elif token in _TENS:
            next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
            if next_token in _DIGIT_WORDS and next_token not in {"zero", "oh"}:
                parts.append(_TENS[token] + _DIGIT_WORDS[next_token])
                index += 1
            else:
                parts.append(_TENS[token] + "0")
        elif token in {"double", "triple"}:
            next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
            if next_token in _DIGIT_WORDS:
                parts.append(_DIGIT_WORDS[next_token] * (2 if token == "double" else 3))
                index += 1
        elif token.isascii() and token.isdigit():
            parts.append(token)
        index += 1
    digits = "".join(parts)
    if not digits:
        raise PolicyError("INVALID_INPUT")
    return digits


def normalize_phone(value: str) -> str:
    """Return a US demo number in E.164 form; reject ambiguous spoken input."""

    digits = _spoken_digits(value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise PolicyError("INVALID_INPUT")
    return "+1" + digits


def normalize_postal(value: str) -> str:
    digits = _spoken_digits(value)
    if len(digits) != 5:
        raise PolicyError("INVALID_INPUT")
    return digits


def normalize_claim_id(value: str) -> str:
    """Accept six spoken digits with or without a CLM/claim prefix."""

    digits = _spoken_digits(value)
    if len(digits) != 6:
        raise PolicyError("INVALID_INPUT")
    return "CLM-" + digits


def lookup_token(secret: bytes, category: str, normalized_value: str) -> str:
    """Domain-separated HMAC; never persist the source identifier."""

    if len(secret) < 32 or category not in {"phone", "claim", "postal"}:
        raise ValueError("invalid lookup-token configuration")
    message = f"{category}:{normalized_value}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()
