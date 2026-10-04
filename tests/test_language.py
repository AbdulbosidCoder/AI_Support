import pytest

from ai_support.language import detect_language
from ai_support.models import Lang


@pytest.mark.parametrize("text,lang", [
    ("Pulim yechildi lekin o'tkazma bormadi", Lang.UZ_LATN),
    ("kartam qo'shilmayapti, nima qilish kerak?", Lang.UZ_LATN),
    ("Салом, картам блокланди, нима қилиш керак?", Lang.UZ_CYRL),
    ("Пулим ечилди лекин ўтказма бормади", Lang.UZ_CYRL),
    ("Здравствуйте, не приходит SMS код", Lang.RU),
    ("Деньги списались, а перевод не дошёл", Lang.RU),
    ("Hello, my card is not added to the app", Lang.EN),
    ("Why is my payment pending?", Lang.EN),
])
def test_detect(text, lang):
    assert detect_language(text) == lang


def test_empty_uses_default():
    assert detect_language("", default=Lang.RU) == Lang.RU
