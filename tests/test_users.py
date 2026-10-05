from ai_support.models import Lang
from ai_support.users import DEFAULT_LANGUAGE, UserStore


def test_new_user_has_no_language_and_defaults_to_uzbek():
    store = UserStore(":memory:")
    u = store.touch("telegram", "1", "1", "ali", "Ali Valiyev", "ru")
    assert u.language is None and u.lang == DEFAULT_LANGUAGE == Lang.UZ_LATN
    assert u.platform_lang == "ru" and u.created_at


def test_language_is_kept_when_details_refresh():
    store = UserStore(":memory:")
    store.touch("telegram", "1", "1", "old")
    store.set_language("telegram", "1", Lang.RU)
    u = store.touch("telegram", "1", "1", "new")
    assert u.language == Lang.RU and u.username == "new"
    assert store.count() == 1


def test_users_persist_in_file(tmp_path):
    path = tmp_path / "db" / "bot.sqlite3"
    store = UserStore(path)
    store.touch("telegram", "1", "1")
    store.set_language("telegram", "1", Lang.EN)
    store.close()
    again = UserStore(path)
    assert again.get("telegram", "1").language == Lang.EN


def test_channels_are_separate():
    store = UserStore(":memory:")
    store.touch("telegram", "1", "1")
    store.touch("mobile", "1", "device-1")
    store.set_language("mobile", "1", Lang.EN)
    assert store.get("telegram", "1").language is None
    assert store.get("mobile", "1").language == Lang.EN


def test_by_language_for_notifications():
    store = UserStore(":memory:")
    for uid, lang in (("1", Lang.RU), ("2", None), ("3", Lang.UZ_LATN), ("4", Lang.EN)):
        store.touch("telegram", uid, uid)
        if lang:
            store.set_language("telegram", uid, lang)
    assert [u.user_id for u in store.by_language("telegram", Lang.UZ_LATN)] == ["2", "3"]  # unchosen = Uzbek
    assert [u.user_id for u in store.by_language("telegram", Lang.RU)] == ["1"]
