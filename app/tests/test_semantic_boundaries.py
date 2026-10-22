"""Reproduce routing coverage and distinguish literal fidelity from semantic equivalence."""

import json

import pytest
from notification_service.domain import FailureCategory, ProcessingError
from notification_service.extraction import parse_extraction, validate_semantics


def candidate(message, to="case@example.org", channel="email"):
    return parse_extraction(
        json.dumps({"to": to, "message": message, "type": channel})
    ).notification


@pytest.mark.parametrize(
    "source,to,channel,body",
    [
        (
            "Avisar por SMS al 612-345-678 que el pedido está listo.",
            "612345678",
            "sms",
            "el pedido está listo.",
        ),
        (
            "Enviar un correo a case@example.org que el informe está listo.",
            "case@example.org",
            "email",
            "el informe está listo.",
        ),
        (
            "Manda SMS al 612345678 que confirma que llegas a las 12:30.",
            "612345678",
            "sms",
            "confirma que llegas a las 12:30.",
        ),
    ],
)
def test_explicit_que_clause_verifies_complete_ai_candidate(source, to, channel, body):
    result = candidate(body, to, channel)
    validate_semantics(source, result)
    assert result.message == body


@pytest.mark.parametrize(
    "source,result",
    [
        (
            "Avisar por SMS al 612345678 que el pedido está listo.",
            candidate("el pedido", "612345678", "sms"),
        ),
        ("Enviar correo a case@example.org y other@example.org que listo.", candidate("listo.")),
        (
            "Enviar correo a case@example.org que escribe a other@example.org.",
            candidate("escribe a other@example.org.", "other@example.org"),
        ),
    ],
)
def test_que_clause_does_not_allow_truncation_or_routing_substitution(source, result):
    with pytest.raises(ProcessingError) as error:
        validate_semantics(source, result)
    assert error.value.category == FailureCategory.SEMANTIC


@pytest.mark.parametrize(
    "body", ["Tienes un nuevo mensaje.", "Confirmado!", "cita a las 10:00", 'El código es "AbC12".']
)
def test_case_and_punctuation_are_accepted_when_the_ai_preserves_them(body):
    validate_semantics("Manda un correo a case@example.org: " + body, candidate(body))


@pytest.mark.parametrize(
    "source_body,ai_body",
    [
        ("Tienes un nuevo mensaje.", "tienes un nuevo mensaje."),
        ("Use token AbC12.", "use token abc12."),
        ("No, enviar.", "No enviar."),
        ("cita a las 10:00", "00"),
        ("Importe: 1.500", "Importe: 1500"),
    ],
)
def test_literal_fidelity_policy_does_not_silently_fold_case_or_punctuation(source_body, ai_body):
    with pytest.raises(ProcessingError, match="message_mismatch"):
        validate_semantics("Send email to case@example.org: " + source_body, candidate(ai_body))


def test_supported_aliases_and_formatting_do_not_change_message_text():
    result = parse_extraction(
        '```json\n{"Recipient":"case@example.org","body":"Tienes un nuevo mensaje.",'
        '"channel":"EMAIL"}\n```'
    )
    validate_semantics(
        "Manda un correo a case@example.org: Tienes un nuevo mensaje.", result.notification
    )
    assert result.notification.message == "Tienes un nuevo mensaje."
