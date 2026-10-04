import httpx

from scanner import telegram


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, str(payload)

    def json(self):
        return self._payload


def test_find_chats_lists_private_and_group_chats(monkeypatch):
    def fake_get(url, timeout):
        if url.endswith("/getMe"):
            return Resp(200, {"ok": True, "result": {"username": "my_scanner_bot"}})
        return Resp(200, {"ok": True, "result": [
            {"message": {"chat": {"id": 12345, "type": "private", "first_name": "Sam"}}},
            {"message": {"chat": {"id": 12345, "type": "private", "first_name": "Sam"}}},
            {"my_chat_member": {"chat": {"id": -100777, "type": "supergroup", "title": "Alpha"}}},
        ]})
    monkeypatch.setattr(httpx, "get", fake_get)
    username, chats = telegram.find_chats("123:abc")
    assert username == "my_scanner_bot"
    assert sorted(c["id"] for c in chats) == [-100777, 12345]


def test_find_chats_rejects_bad_token(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout: Resp(401, {"ok": False}))
    try:
        telegram.find_chats("bad")
    except ValueError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected ValueError")
