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


def test_registration_saves_normalised_phone_once():
    store = UserStore(":memory:")
    store.touch("telegram", "1", "1")
    assert not store.get("telegram", "1").registered
    store.set_phone("telegram", "1", "998 90 123-45-67")
    u = store.get("telegram", "1")
    assert u.registered and u.phone == "+998901234567" and u.registered_at
    store.set_phone("telegram", "1", "+998911112233")  # a new number keeps the first registration time
    u2 = store.get("telegram", "1")
    assert u2.phone == "+998911112233" and u2.registered_at == u.registered_at
    store.touch("telegram", "1", "1", "new")  # refreshing details keeps the phone
    assert store.get("telegram", "1").phone == "+998911112233"


def test_old_database_gets_phone_columns(tmp_path):
    import sqlite3
    db = tmp_path / "bot.sqlite3"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE users (channel TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                   username TEXT, full_name TEXT, platform_lang TEXT, language TEXT, created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL, PRIMARY KEY (channel, user_id))""")
    con.execute("INSERT INTO users VALUES ('telegram', '1', '1', 'a', 'A', 'ru', 'ru', 'x', 'x')")
    con.commit()
    con.close()
    store = UserStore(db)
    u = store.get("telegram", "1")
    assert u.language == Lang.RU and not u.registered
    store.set_phone("telegram", "1", "998901234567")
    assert store.get("telegram", "1").phone == "+998901234567"
