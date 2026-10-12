import re
from difflib import SequenceMatcher
from itertools import permutations


def phone(value: str) -> str:
    # Extensions are not part of the subscriber's identity.
    value = re.split(r"(?:доб\.?|ext\.?|[;#])", value, flags=re.I)[0]
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError("Некорректный номер телефона")
    return "+" + digits


def contact_phones(contact: dict) -> set[str]:
    result = set()
    for field in contact.get("custom_fields_values") or []:
        if field.get("field_code") == "PHONE":
            for value in field.get("values") or []:
                try:
                    result.add(phone(str(value["value"])))
                except (ValueError, KeyError):
                    pass
    return result


def name_tokens(value: str) -> list[str]:
    return re.findall(r"[а-яa-z]+", value.casefold().replace("ё", "е"))[:4]


def name_score(left: str, right: str) -> float:
    a, b = name_tokens(left), name_tokens(right)
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    best = 0.0
    for order in permutations(b, len(a)):
        scores = [0.88 if min(len(x), len(y)) == 1 and x[0] == y[0]
                  else SequenceMatcher(None, x, y).ratio() for x, y in zip(a, order)]
        best = max(best, sum(scores) / max(len(a), len(b)))
    return best


def name_queries(value: str) -> list[str]:
    words = name_tokens(value)
    variants = {value.strip(), " ".join(words), " ".join(words).upper()}
    variants.update(" ".join(order) for order in permutations(words))
    variants.update(x for x in words if len(x) > 1)
    if len(words) >= 2:
        variants.add(words[0] + " " + " ".join(x[0] for x in words[1:]))
    return sorted(x for x in variants if x)
