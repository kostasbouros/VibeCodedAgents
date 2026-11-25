"""Tests for shared/redact.py, in Greek and English. All personal data here is made up."""

import pytest

from shared.redact import RedactionError, redact, redact_or_raise


def clean(text: str) -> str:
    """Redact and require success."""
    result = redact(text)
    assert result.ok, result.problems
    return result.redacted_text


# --- Emails -------------------------------------------------------------------------------


def test_email_english() -> None:
    assert clean("Please reply to maria.p@example.gr today") == "Please reply to [EMAIL_1] today"


def test_email_in_greek_sentence() -> None:
    assert clean("Το email μου είναι g.papadopoulos+fleet@company.com.gr") == (
        "Το email μου είναι [EMAIL_1]"
    )


def test_same_email_gets_same_token_and_is_counted_twice() -> None:
    result = redact("Write to a@b.gr. Again: A@B.GR")
    assert result.redacted_text == "Write to [EMAIL_1]. Again: [EMAIL_1]"
    assert result.counts == {"EMAIL": 2}


def test_different_emails_get_different_tokens() -> None:
    assert clean("a@b.gr and c@d.com") == "[EMAIL_1] and [EMAIL_2]"


# --- Phones -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "number",
    ["6912345678", "691 234 5678", "691-234-5678", "69 12 34 56 78", "2101234567", "210 123 4567"],
)
def test_greek_local_phone_formats(number: str) -> None:
    assert clean(f"Τηλέφωνο: {number}.") == "Τηλέφωνο: [PHONE_1]."


def test_greek_phone_with_country_code() -> None:
    assert clean("Call +30 210 123 4567 now") == "Call [PHONE_1] now"


def test_same_phone_written_three_ways_is_one_token() -> None:
    result = redact("+30 6912345678, 0030 691 234 5678 or 6912345678")
    assert result.redacted_text == "[PHONE_1], [PHONE_1] or [PHONE_1]"
    assert result.counts == {"PHONE": 3}


def test_international_phone() -> None:
    assert clean("UK office: +44 20 7946 0958") == "UK office: [PHONE_1]"


def test_dates_times_and_amounts_are_not_phones() -> None:
    text = "Visit on 2024-11-24 10:30, cost €1.250,00, ticket #48213."
    assert clean(text) == text


# --- Vehicles and devices -----------------------------------------------------------------


def test_greek_plate_in_greek_letters() -> None:
    assert clean("Όχημα ΙΚΑ-1234 δεν στέλνει θέση") == "Όχημα [PLATE_1] δεν στέλνει θέση"


def test_same_plate_in_greek_and_latin_letters_is_one_token() -> None:
    assert clean("ΙΚΑ-1234 = IKA1234 = IKA 1234") == "[PLATE_1] = [PLATE_1] = [PLATE_1]"


def test_iso_9001_is_not_a_plate() -> None:
    # S is not one of the 14 Greek plate letters, so this must stay.
    assert clean("We are ISO 9001 certified") == "We are ISO 9001 certified"


def test_vin() -> None:
    assert clean("VIN: WVWZZZ1JZXW000001") == "VIN: [VIN_1]"


def test_imei() -> None:
    assert clean("IMEI 356938035643809 offline") == "IMEI [IMEI_1] offline"


def test_iccid_19_and_20_digits() -> None:
    assert clean("SIM 8930123456789012345 / 89301234567890123456") == ("SIM [ICCID_1] / [ICCID_2]")


# --- Tax and bank ---------------------------------------------------------------------------


def test_afm() -> None:
    assert clean("ΑΦΜ: 123456789, ΔΟΥ Αθηνών") == "ΑΦΜ: [AFM_1], ΔΟΥ Αθηνών"


def test_greek_iban_with_spaces() -> None:
    assert clean("IBAN GR16 0110 1250 0000 0001 2300 695.") == "IBAN [IBAN_1]."


def test_iban_without_spaces() -> None:
    assert clean("GR1601101250000000012300695") == "[IBAN_1]"


# --- Names after greetings ----------------------------------------------------------------


def test_english_greeting_name_and_later_mentions() -> None:
    assert clean("Dear Mr. Smith,\nThe car Smith drives is fine.") == (
        "Dear Mr. [NAME_1],\nThe car [NAME_1] drives is fine."
    )


def test_greek_greeting_with_title() -> None:
    assert clean("Καλημέρα κ. Παπαδόπουλε,\nτο όχημα είναι εντάξει.") == (
        "Καλημέρα κ. [NAME_1],\nτο όχημα είναι εντάξει."
    )


def test_greek_greeting_without_name_is_untouched() -> None:
    assert clean("Καλημέρα σας,\nέχω πρόβλημα.") == "Καλημέρα σας,\nέχω πρόβλημα."


def test_first_name_alone_is_replaced_after_full_name() -> None:
    assert clean("Hi Maria Ioannou,\nMaria, call me.") == "Hi [NAME_1],\n[NAME_2], call me."


# --- Signatures -----------------------------------------------------------------------------


def test_english_signoff_name_tokenised_and_signature_dropped() -> None:
    text = "The tracker is offline.\n\nKind regards,\nJohn Smith\nFleet Manager\nTel 6912345678"
    assert clean(text) == "The tracker is offline.\n\nKind regards,\n[NAME_1]"


def test_greek_signoff_and_signature() -> None:
    text = "Η συσκευή δεν ανάβει.\n\nΜε εκτίμηση,\nΓιώργος Παπαδόπουλος\nΛογιστήριο\n210 1234567"
    assert clean(text) == "Η συσκευή δεν ανάβει.\n\nΜε εκτίμηση,\n[NAME_1]"


def test_signoff_with_name_on_same_line() -> None:
    assert clean("Done.\nThanks, Nikos") == "Done.\nThanks, [NAME_1]"


def test_thanks_that_worked_is_not_a_signature() -> None:
    text = "Thanks, that worked.\nThe device is online again."
    assert clean(text) == text


def test_dash_dash_signature_delimiter() -> None:
    assert clean("See attached.\n-- \nAnna K.\nmobile 6912345678") == "See attached."


def test_sent_from_iphone_removed() -> None:
    assert clean("OK.\nSent from my iPhone") == "OK."


# --- Quoted replies -------------------------------------------------------------------------


def test_quoted_lines_removed() -> None:
    assert clean("Yes, please.\n> Can we visit on Monday?\n> Tel 6912345678") == "Yes, please."


def test_english_reply_chain_removed() -> None:
    text = "Fixed now.\n\nOn Mon, 4 Nov 2024 at 10:00, Maria <m@x.gr> wrote:\nOld text 6912345678"
    assert clean(text) == "Fixed now."


def test_two_line_gmail_reply_header_removed() -> None:
    text = "Fine.\nOn Mon, 4 Nov 2024 at 10:00 Maria Ioannou\n<m@x.gr> wrote:\nold"
    assert clean(text) == "Fine."


def test_greek_reply_chain_removed() -> None:
    text = "Ευχαριστώ για την άμεση απάντηση.\nΣτις 12/11/2024 10:15, ο Νίκος έγραψε:\nπαλιό"
    assert clean(text) == "Ευχαριστώ για την άμεση απάντηση."


def test_outlook_original_message_removed() -> None:
    assert clean("Ok.\n-----Original Message-----\nFrom: a@b.gr") == "Ok."


# --- Safety check and failure handling -----------------------------------------------------


def test_unexplained_long_number_fails_and_blanks_text() -> None:
    result = redact("Reference 123456789012")
    assert result.ok is False
    assert result.redacted_text == ""
    assert "LONG_NUMBER" in result.problems


def test_card_number_fails() -> None:
    assert redact("Card 4111 1111 1111 1111").ok is False


def test_redact_or_raise_raises_when_not_ok() -> None:
    with pytest.raises(RedactionError, match="LONG_NUMBER"):
        redact_or_raise("Reference 123456789012")


def test_redact_or_raise_returns_clean_text() -> None:
    assert redact_or_raise("Call 6912345678") == "Call [PHONE_1]"


def test_non_text_fails_safely() -> None:
    assert redact(None).ok is False  # type: ignore[arg-type]
    with pytest.raises(RedactionError):
        redact_or_raise(12345)  # type: ignore[arg-type]


def test_plain_text_unchanged_with_no_counts() -> None:
    result = redact("Truck stopped reporting after 14:00")
    assert (result.redacted_text, result.counts, result.ok) == (
        "Truck stopped reporting after 14:00",
        {},
        True,
    )


# --- Realistic mixed messages ---------------------------------------------------------------


def test_realistic_greek_ticket() -> None:
    text = (
        "Καλησπέρα κ. Ιωάννου,\n"
        "το φορτηγό ΚΗΒ-4521 (IMEI 356938035643809) δεν εμφανίζεται από χθες.\n"
        "Ο οδηγός Ιωάννου είναι στο 6971234567.\n\n"
        "Με εκτίμηση,\n"
        "Ελένη Γεωργίου\n"
        "Τμήμα Στόλου, e.georgiou@fleet.gr\n\n"
        "Στις 12/11/2024 10:15, ο/η Support έγραψε:\n"
        "> Παρακαλώ στείλτε το IMEI."
    )
    assert clean(text) == (
        "Καλησπέρα κ. [NAME_1],\n"
        "το φορτηγό [PLATE_1] (IMEI [IMEI_1]) δεν εμφανίζεται από χθες.\n"
        "Ο οδηγός [NAME_1] είναι στο [PHONE_1].\n\n"
        "Με εκτίμηση,\n"
        "[NAME_2]"
    )


def test_realistic_english_ticket_counts() -> None:
    text = (
        "Hello Mr. Brown,\nVehicle IKA1234 and ΙΚΑ-1234 are the same truck, VIN WVWZZZ1JZXW000001.\n"
        "Call me at +30 210 123 4567.\n\nBest regards,\nPeter Brown"
    )
    result = redact(text)
    assert result.ok
    assert result.counts == {"NAME": 2, "PLATE": 2, "VIN": 1, "PHONE": 1}
    assert "Brown" not in result.redacted_text


# --- Leaks found in review (each was ok=True with personal data still visible) -------------


@pytest.mark.parametrize(
    "number",
    ["69 1234 5678", "6912 345 678", "2310 123456", "691\u00a0234\u00a05678",
     "691\u200b2345678", "691\u2013234\u20135678"],
)  # fmt: skip
def test_review_phone_formats_and_hidden_characters(number: str) -> None:
    assert clean(f"Call {number} please") == "Call [PHONE_1] please"


def test_review_afm_with_spaces() -> None:
    assert clean("ΑΦΜ 123 456 789") == "ΑΦΜ [AFM_1]"


def test_review_imei_with_dashes() -> None:
    assert clean("IMEI 35-693803-564380-9") == "IMEI [IMEI_1]"


@pytest.mark.parametrize(
    "email", ["maria[at]gmail.com", "maria (at) gmail [dot] com", "maria at gmail dot com"]
)
def test_review_disguised_emails(email: str) -> None:
    assert clean(f"mail {email} now") == "mail [EMAIL_1] now"


def test_review_at_in_normal_sentence_is_not_an_email() -> None:
    text = "We arrive at noon. Then we test."
    assert clean(text) == text


def test_review_lowercase_plate_but_not_the_word_the() -> None:
    assert clean("plate ika-1234, the 2024 contract") == "plate [PLATE_1], the 2024 contract"


def test_review_names_in_capitals() -> None:
    assert clean("Hi MARIA,\nok\nThanks, JOHN") == "Hi [NAME_1],\nok\nThanks, [NAME_2]"


def test_review_hi_all_is_not_a_name() -> None:
    text = "Hi all,\nPlease call all drivers."
    assert clean(text) == text


def test_review_outlook_reply_header_removed() -> None:
    text = "Yes.\nFrom: Maria Ioannou\nSent: Monday\nTo: support\nSubject: x\nold text"
    assert clean(text) == "Yes."


def test_review_greek_outlook_reply_header_removed() -> None:
    assert clean("Εντάξει.\nΑπό: Μαρία\nΣτάλθηκε: Δευτέρα\nπαλιό") == "Εντάξει."


@pytest.mark.parametrize("date", ["2024-11-24 10:30", "24.11.2024 09:15", "24/11/24", "3/4/2025"])
def test_review_dates_survive_wider_phone_rule(date: str) -> None:
    assert clean(f"Visit {date} ok") == f"Visit {date} ok"


def test_review_date_next_to_phone() -> None:
    assert clean("Seen 2024-11-24 6912345678") == "Seen 2024-11-24 [PHONE_1]"


def test_review_long_text_is_fast() -> None:
    import time

    started = time.perf_counter()
    redact("a" * 30000 + " A1" * 10000)
    assert time.perf_counter() - started < 2
