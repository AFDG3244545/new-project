"""Watch X for Claude guest-pass links and push them to Telegram."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

import httpx

log = logging.getLogger("refwatch")
# httpx logs every request URL at INFO, and the Telegram URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
REFERRAL_URL = "https://claude.ai/referral/"
REFERRAL_RE = re.compile(r"claude\.ai/referral/([A-Za-z0-9_-]+)", re.IGNORECASE)
TWITTER_EPOCH_MS = 1288834974657
MAX_SEEN = 10_000
MAX_BACKOFF = 300.0
REQUIRED_ENV = ("X_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tweet_time(tweet_id: str) -> datetime:
    """Creation time encoded in a post's snowflake id."""
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + TWITTER_EPOCH_MS) / 1000, timezone.utc)


@dataclass(frozen=True)
class Config:
    x_bearer_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    query: str = 'url:"claude.ai/referral" -is:retweet'
    poll_interval: float = 10.0
    lookback: int = 120
    state_file: Path = Path("state.json")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        missing = [name for name in REQUIRED_ENV if not env.get(name)]
        if missing:
            raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
        return cls(
            x_bearer_token=env["X_BEARER_TOKEN"],
            telegram_bot_token=env["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=env["TELEGRAM_CHAT_ID"],
            query=env.get("QUERY") or cls.query,
            poll_interval=float(env.get("POLL_INTERVAL") or cls.poll_interval),
            lookback=int(env.get("LOOKBACK") or cls.lookback),
            state_file=Path(env.get("STATE_FILE") or cls.state_file),
        )


def extract_codes(tweet: dict) -> list[str]:
    """Referral codes found in a post, in order, without duplicates."""
    # Long posts keep their full text and entities under note_tweet.
    note = tweet.get("note_tweet") or {}
    haystack = [tweet.get("text"), note.get("text")]
    for entities in (tweet.get("entities"), note.get("entities")):
        for url in (entities or {}).get("urls", []):
            haystack += [url.get("expanded_url"), url.get("unwound_url")]
    return list(dict.fromkeys(code for s in haystack if s for code in REFERRAL_RE.findall(s)))


class SeenStore:
    """Referral codes already sent, persisted so restarts do not repeat them."""

    def __init__(self, path: Path):
        self.path = path
        self._codes: dict[str, None] = {}
        if path.exists():
            try:
                self._codes = dict.fromkeys(json.loads(path.read_text())["seen"])
            except (ValueError, KeyError, TypeError):
                log.warning("State file %s is unreadable, starting empty", path)

    def __contains__(self, code: str) -> bool:
        return code in self._codes

    def add(self, code: str) -> None:
        self._codes[code] = None
        while len(self._codes) > MAX_SEEN:
            del self._codes[next(iter(self._codes))]
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"seen": list(self._codes)}))
        os.replace(tmp, self.path)


def retry_delay(exc: Exception, previous: float) -> float:
    """Seconds to wait after a failed poll."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        reset = exc.response.headers.get("x-rate-limit-reset", "")
        if reset.isdigit():
            return min(max(int(reset) - utcnow().timestamp(), 1.0), 900.0)
    return min(previous * 2, MAX_BACKOFF)


class Watcher:
    def __init__(
        self,
        cfg: Config,
        client: httpx.Client,
        seen: SeenStore,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.client = client
        self.seen = seen
        self.sleep = sleep
        self.since_id: str | None = None

    def search_params(self) -> dict[str, str]:
        params = {
            "query": self.cfg.query,
            "max_results": "100",
            "tweet.fields": "entities,note_tweet",
            "expansions": "author_id",
            "user.fields": "username",
        }
        now = utcnow()
        # The API rejects a since_id older than its 7-day search window.
        if self.since_id and now - tweet_time(self.since_id) < timedelta(days=6):
            params["since_id"] = self.since_id
        else:
            start = now - timedelta(seconds=self.cfg.lookback)
            params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        return params

    def poll(self) -> int:
        """Run one search and notify about unseen links; returns how many were sent."""
        resp = self.client.get(
            X_SEARCH_URL,
            params=self.search_params(),
            headers={"Authorization": f"Bearer {self.cfg.x_bearer_token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        meta = body.get("meta", {})
        if meta.get("newest_id"):
            self.since_id = meta["newest_id"]
        if meta.get("next_token"):
            log.warning("More than 100 new posts in one poll, older ones were skipped")

        users = {u["id"]: u["username"] for u in body.get("includes", {}).get("users", [])}
        sent = 0
        for tweet in body.get("data", []):
            for code in extract_codes(tweet):
                if code in self.seen:
                    continue
                # Marked only after delivery so a repost of the link gets another chance.
                if self.notify(code, tweet, users.get(tweet.get("author_id"))):
                    self.seen.add(code)
                    sent += 1
        return sent

    def notify(self, code: str, tweet: dict, username: str | None) -> bool:
        link = REFERRAL_URL + code
        age = max(0, int((utcnow() - tweet_time(tweet["id"])).total_seconds()))
        post_url = f"https://x.com/{username or 'i/web'}/status/{tweet['id']}"
        log.info("New link %s from @%s, posted %ss ago", link, username or "?", age)
        text = (
            f"🎟 <b>Claude guest pass</b> · {age} с назад\n{link}\n\n"
            f"@{html.escape(username or '?')}: {html.escape(tweet.get('text', '')[:200])}"
        )
        buttons = [{"text": "Активировать", "url": link}, {"text": "Пост в X", "url": post_url}]
        return self.send_telegram(text, buttons)

    def send_telegram(self, text: str, buttons: list[dict] | None = None) -> bool:
        payload: dict = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        url = TELEGRAM_URL.format(token=self.cfg.telegram_bot_token)
        for pause in (1, 3, 0):
            try:
                resp = self.client.post(url, json=payload)
            except httpx.HTTPError as exc:
                # Only the type: the message can carry the URL, which embeds the bot token.
                log.warning("Telegram request failed (%s)", type(exc).__name__)
            else:
                if resp.is_success:
                    return True
                log.warning("Telegram %s: %s", resp.status_code, resp.text[:300])
                if resp.status_code != 429 and resp.status_code < 500:
                    return False
            if pause:
                self.sleep(pause)
        return False

    def run(self) -> None:
        delay = self.cfg.poll_interval
        while True:
            try:
                self.poll()
                delay = self.cfg.poll_interval
            except httpx.HTTPStatusError as exc:
                delay = retry_delay(exc, delay)
                log.error(
                    "X API %s: %s; retry in %.0fs",
                    exc.response.status_code, exc.response.text[:300], delay,
                )
            except (httpx.HTTPError, ValueError) as exc:
                delay = retry_delay(exc, delay)
                log.error("X API request failed (%s); retry in %.0fs", type(exc).__name__, delay)
            self.sleep(delay)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    with httpx.Client(timeout=15) as client:
        watcher = Watcher(cfg, client, SeenStore(cfg.state_file))
        started = f"✅ refwatch запущен\nЗапрос: <code>{html.escape(cfg.query)}</code>"
        if not watcher.send_telegram(started):
            raise SystemExit("Telegram check failed: verify TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        log.info("Watching %r every %.0fs", cfg.query, cfg.poll_interval)
        try:
            watcher.run()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
