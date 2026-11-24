import pytest

from shared.redact import RedactionError, redact


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Contact maria.p@example.gr please", "[EMAIL]"),
        ("Call me on 6912345678", "[PHONE]"),
        ("Call me on +30 691 234 5678", "[PHONE]"),
        ("Office 210 123 4567", "[PHONE]"),
        ("Device IMEI 356938035643809", "[IMEI]"),
        ("AFM 123456789", "[AFM]"),
        ("IBAN GR16 0110 1250 0000 0001 2300 695", "[IBAN]"),
    ],
)
def test_personal_data_is_replaced(text: str, label: str) -> None:
    result = redact(text)
    assert label in result


def test_plain_text_is_unchanged() -> None:
    assert redact("Truck stopped reporting after 14:00") == "Truck stopped reporting after 14:00"


def test_non_text_raises() -> None:
    with pytest.raises(RedactionError):
        redact(None)  # type: ignore[arg-type]
