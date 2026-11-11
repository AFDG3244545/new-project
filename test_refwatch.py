import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import refwatch
from refwatch import Config, SeenStore, Watcher, extract_codes, retry_delay

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def tweet_id(at: datetime) -> str:
    return str((int(at.timestamp() * 1000) - refwatch.TWITTER_EPOCH_MS) << 22)


def make_tweet(code: str, at: datetime = NOW - timedelta(seconds=7)) -> dict:
    return {
        "id": tweet_id(at),
        "author_id": "1",
        "text": "free week <3 https://t.co/abc",
        "entities": {
            "urls": [{"url": "https://t.co/abc", "expanded_url": f"https://claude.ai/referral/{code}"}]
        },
    }


def page(*tweets: dict) -> dict:
    body: dict = {"meta": {"result_count": len(tweets)}}
    if tweets:
        body["data"] = list(tweets)
        body["meta"]["newest_id"] = max(tweets, key=lambda t: int(t["id"]))["id"]
        body["includes"] = {"users": [{"id": "1", "username": "giver"}]}
    return body


class FakeApis:
    def __init__(self):
        self.pages: list[dict] = []
        self.searches: list[dict] = []
        self.messages: list[dict] = []
        self.telegram_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.x.com":
            assert request.headers["authorization"] == "Bearer x-token"
            self.searches.append(dict(request.url.params))
            return httpx.Response(200, json=self.pages.pop(0) if self.pages else page())
        assert request.url.path == "/bottg-secret/sendMessage"
        self.messages.append(json.loads(request.content))
        return httpx.Response(self.telegram_status, json={"ok": self.telegram_status == 200})


@pytest.fixture
def api():
    return FakeApis()


@pytest.fixture
def watcher(api, tmp_path, monkeypatch):
    monkeypatch.setattr(refwatch, "utcnow", lambda: NOW)
    cfg = Config(
        x_bearer_token="x-token",
        telegram_bot_token="tg-secret",
        telegram_chat_id="42",
        state_file=tmp_path / "state.json",
    )
    client = httpx.Client(transport=httpx.MockTransport(api))
    return Watcher(cfg, client, SeenStore(cfg.state_file), sleep=lambda _: None)


def test_extract_codes_reads_expanded_unwound_and_note_urls():
    tweet = {
        "text": "grab it https://t.co/a https://t.co/b",
        "entities": {
            "urls": [
                {"expanded_url": "https://claude.ai/referral/AAA111"},
                {"expanded_url": "https://bit.ly/x", "unwound_url": "https://claude.ai/referral/BBB-222?utm=1"},
                {"expanded_url": "https://claude.ai/referral/AAA111"},
                {"expanded_url": "https://claude.ai/login"},
            ]
        },
        "note_tweet": {
            "text": "long post",
            "entities": {"urls": [{"expanded_url": "http://www.claude.ai/referral/ccc_333"}]},
        },
    }
    assert extract_codes(tweet) == ["AAA111", "BBB-222", "ccc_333"]
    assert extract_codes({"text": "dm me for claude.ai/referral link"}) == []


def test_poll_notifies_once_per_link_and_advances_since_id(api, watcher):
    newer = make_tweet("NEW1", NOW - timedelta(seconds=7))
    older = make_tweet("OLD1", NOW - timedelta(seconds=30))
    repost = make_tweet("NEW1", NOW - timedelta(seconds=40))
    api.pages.append(page(newer, older, repost))

    assert watcher.poll() == 2
    assert [m["reply_markup"]["inline_keyboard"][0][0]["url"] for m in api.messages] == [
        "https://claude.ai/referral/NEW1",
        "https://claude.ai/referral/OLD1",
    ]
    first = api.messages[0]
    assert first["chat_id"] == "42"
    assert "7 с назад" in first["text"]
    assert "@giver: free week &lt;3" in first["text"]
    assert first["reply_markup"]["inline_keyboard"][0][1]["url"] == f"https://x.com/giver/status/{newer['id']}"

    assert api.searches[0]["start_time"] == "2026-11-11T11:58:00Z"
    assert "since_id" not in api.searches[0]

    assert watcher.poll() == 0
    assert api.searches[1]["since_id"] == newer["id"]
    assert "start_time" not in api.searches[1]


def test_seen_links_survive_restart(api, watcher):
    api.pages.append(page(make_tweet("KEEP1")))
    assert watcher.poll() == 1

    restarted = Watcher(watcher.cfg, watcher.client, SeenStore(watcher.cfg.state_file))
    api.pages.append(page(make_tweet("KEEP1")))
    assert restarted.poll() == 0
    assert len(api.messages) == 1


def test_stale_since_id_falls_back_to_start_time(watcher):
    watcher.since_id = tweet_id(NOW - timedelta(days=6, minutes=1))
    params = watcher.search_params()
    assert "since_id" not in params
    assert params["start_time"] == "2026-11-11T11:58:00Z"


def test_failed_delivery_is_retried_later_and_never_logs_token(api, watcher, caplog):
    api.telegram_status = 500
    api.pages.append(page(make_tweet("LOST1")))
    with caplog.at_level(logging.DEBUG):
        assert watcher.poll() == 0
    assert len(api.messages) == 3
    assert "LOST1" not in watcher.seen
    assert "tg-secret" not in caplog.text

    api.telegram_status = 200
    api.pages.append(page(make_tweet("LOST1")))
    assert watcher.poll() == 1


def test_telegram_client_error_is_not_retried(api, watcher):
    api.telegram_status = 403
    assert watcher.send_telegram("hi") is False
    assert len(api.messages) == 1


def test_retry_delay(monkeypatch):
    monkeypatch.setattr(refwatch, "utcnow", lambda: NOW)
    request = httpx.Request("GET", refwatch.X_SEARCH_URL)

    def status_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
        response = httpx.Response(status, headers=headers, request=request)
        return httpx.HTTPStatusError("boom", request=request, response=response)

    reset = str(int(NOW.timestamp()) + 42)
    assert retry_delay(status_error(429, {"x-rate-limit-reset": reset}), 10) == 42
    assert retry_delay(status_error(429), 10) == 20
    assert retry_delay(status_error(503), 10) == 20
    assert retry_delay(httpx.ConnectError("down"), 200) == refwatch.MAX_BACKOFF


def test_config_from_env():
    with pytest.raises(SystemExit, match="X_BEARER_TOKEN, TELEGRAM_CHAT_ID"):
        Config.from_env({"TELEGRAM_BOT_TOKEN": "t"})

    env = {"X_BEARER_TOKEN": "x", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "POLL_INTERVAL": "5"}
    cfg = Config.from_env(env)
    assert cfg.poll_interval == 5.0
    assert cfg.query == 'url:"claude.ai/referral" -is:retweet'
    assert cfg.lookback == 120
