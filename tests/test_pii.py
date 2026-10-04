from ai_support.pii import contains_pii, mask_pii


def test_card_masked_keeps_last4():
    assert mask_pii("карта 8600 1234 5678 9012") == "карта 8600 **** **** 9012"
    assert mask_pii("8600123456789012") == "8600 **** **** 9012"


def test_phone_and_pinfl_masked():
    out = mask_pii("тел +998 93 069 14 81, ПИНФЛ 31234567890123")
    assert "069" not in out and "31234567890123" not in out
    assert "+998 ** *** ** **" in out


def test_balance_masked():
    assert "1 250 000" not in mask_pii("Balans: 1 250 000 so'm")
    assert "500000" not in mask_pii("мой баланс 500000 сум")


def test_plain_amount_untouched():
    assert mask_pii("оплатил 50 000 сум") == "оплатил 50 000 сум"
    assert not contains_pii("оплатил 50 000 сум")
