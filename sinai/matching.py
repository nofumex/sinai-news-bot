import re


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
