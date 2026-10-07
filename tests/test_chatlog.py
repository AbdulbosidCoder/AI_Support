from ai_support.chatlog import ChatLog


def test_messages_saved_in_order_and_masked(tmp_path):
    log = ChatLog(tmp_path / "bot.sqlite3")
    log.add("telegram", "42", "client", "Karta 8600 1234 5678 9012 ishlamayapti", conversation_id=1)
    log.add("telegram", "42", "bot", "Kartalarim bo'limiga kiring.", conversation_id=1)
    log.add("telegram", "42", "client", "", kind="photo", conversation_id=2)
    log.add("telegram", "7", "client", "boshqa mijoz", conversation_id=3)
    assert [m.sender for m in log.for_conversation(1)] == ["client", "bot"]
    assert "8600 1234 5678 9012" not in log.for_conversation(1)[0].text
    mine = log.for_client("telegram", "42")
    assert [m.sender for m in mine] == ["client", "bot", "client"] and mine[-1].kind == "photo"
    assert [m.text for m in log.for_client("telegram", "42", limit=1)] == [""]
    # Survives a restart (same file).
    assert len(ChatLog(tmp_path / "bot.sqlite3").for_client("telegram", "42")) == 3
