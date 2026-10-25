"""ingest/core/http.py: the user agent, per-host pacing, retries with backoff
and Retry-After, and robots.txt. A fake clock stands in for time, so every
wait is exact and none is slept."""

import asyncio

import httpx
import pytest

from backend.ingest.core import http

UA = "TailrdBot/2.0 (+https://www.tailrd.ca)"


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(http, "_now", fake.time)
    monkeypatch.setattr(http, "_sleep", fake.sleep)
    monkeypatch.setattr(http, "_next_slot", {})
    monkeypatch.setattr(http, "_robots", {})
    return fake


class Server:
    """A MockTransport handler answering from a script per URL: each request
    takes the next answer, a (status, body[, headers]) tuple or an exception
    to raise; the last answer repeats. Records every request."""

    def __init__(self, script: dict):
        self.script = {url: list(answers) for url, answers in script.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answers = self.script.get(str(request.url), [(404, "")])
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body, *rest = answer
        return httpx.Response(status, text=body, headers=rest[0] if rest else {})

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def _client(server: Server, **kw) -> httpx.AsyncClient:
    return http.make_client(transport=httpx.MockTransport(server), **kw)


# --- make_client --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_make_client_identifies_follows_redirects_and_waits_30s():
    server = Server({
        "https://a.test/old": [(301, "", {"Location": "https://a.test/new"})],
        "https://a.test/new": [(200, "ok")],
    })
    async with _client(server, headers={"Accept": "application/json",
                                        "User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get("https://a.test/old")
        assert client.timeout == httpx.Timeout(30.0)

    assert response.status_code == 200
    assert server.urls() == ["https://a.test/old", "https://a.test/new"]
    # A caller's other headers stay; the user agent is always ours.
    assert all(r.headers["user-agent"] == UA for r in server.requests)
    assert server.requests[0].headers["accept"] == "application/json"


# --- request: retries -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_429_is_retried_after_its_retry_after_and_then_succeeds(clock):
    server = Server({"https://api.a.test/jobs": [(429, "slow down", {"Retry-After": "7"}),
                                                 (200, "[]")]})

    async with _client(server) as client:
        response = await http.request(client, "GET", "https://api.a.test/jobs", rate_limit=0)

    assert response.status_code == 200
    assert len(server.requests) == 2
    assert all(r.headers["user-agent"] == UA for r in server.requests)
    assert clock.sleeps == [7.0]  # the host's own Retry-After, longer than the backoff


@pytest.mark.asyncio
async def test_the_user_agent_is_set_on_any_client_not_only_ours(clock):
    server = Server({"https://api.a.test/jobs": [(200, "[]")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        await http.request(client, "GET", "https://api.a.test/jobs",
                           headers={"User-Agent": "python-httpx"})

    assert server.requests[0].headers["user-agent"] == UA


@pytest.mark.asyncio
async def test_5xx_backs_off_exponentially_and_raises_the_last_error(clock):
    server = Server({"https://api.a.test/jobs": [(503, "down")]})

    async with _client(server) as client:
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await http.request(client, "GET", "https://api.a.test/jobs", rate_limit=0, retries=3)

    assert raised.value.response.status_code == 503
    assert len(server.requests) == 4  # the first try and three retries
    # Exponential with jitter: retry n waits between half and all of 2**n seconds.
    assert len(clock.sleeps) == 3
    for n, wait in enumerate(clock.sleeps):
        assert 2 ** n / 2 <= wait <= 2 ** n


@pytest.mark.asyncio
async def test_a_timeout_is_retried_and_the_last_one_raised(clock):
    url = "https://api.a.test/jobs"
    server = Server({url: [httpx.ReadTimeout("slow"), (200, "[]")]})
    async with _client(server) as client:
        assert (await http.request(client, "GET", url, rate_limit=0)).status_code == 200
    assert len(server.requests) == 2

    server = Server({url: [httpx.ConnectTimeout("slow")]})
    async with _client(server) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await http.request(client, "GET", url, rate_limit=0, retries=2)
    assert len(server.requests) == 3


@pytest.mark.asyncio
async def test_a_status_that_is_not_retried_comes_back_at_once(clock):
    server = Server({"https://api.a.test/jobs/9": [(404, "gone")]})

    async with _client(server) as client:
        response = await http.request(client, "GET", "https://api.a.test/jobs/9")

    assert response.status_code == 404
    assert len(server.requests) == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_a_retry_after_too_long_to_wait_out_fails_at_once(clock):
    server = Server({"https://api.a.test/jobs": [(429, "", {"Retry-After": "3600"}), (200, "[]")]})

    async with _client(server) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await http.request(client, "GET", "https://api.a.test/jobs", rate_limit=0)

    assert len(server.requests) == 1
    assert clock.sleeps == []


# --- request: pacing ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_requests_to_one_host_are_paced_and_other_hosts_are_not_held(clock):
    server = Server({
        "https://api.a.test/1": [(200, "")], "https://api.a.test/2": [(200, "")],
        "https://api.a.test/3": [(200, "")], "https://b.test/1": [(200, "")],
    })

    async with _client(server) as client:
        for url in ("https://api.a.test/1", "https://api.a.test/2", "https://b.test/1",
                    "https://api.a.test/3"):
            await http.request(client, "GET", url, rate_limit=2.0)

    # a.test: one request every 2 s (the b.test request in between cost no time).
    assert clock.sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_a_failure_holds_back_the_next_request_to_that_host(clock):
    server = Server({
        "https://api.a.test/busy": [(503, "", {"Retry-After": "5"})],
        "https://api.a.test/other": [(200, "")],
        "https://b.test/x": [(200, "")],
    })

    async with _client(server) as client:
        with pytest.raises(httpx.HTTPStatusError):  # gave up at once: no retries
            await http.request(client, "GET", "https://api.a.test/busy", rate_limit=0, retries=0)
        await http.request(client, "GET", "https://b.test/x", rate_limit=0)
        assert clock.sleeps == []  # another host is not held
        await http.request(client, "GET", "https://api.a.test/other", rate_limit=0)

    # The next board on the overloaded host waited out its Retry-After.
    assert clock.sleeps == [5.0]


@pytest.mark.asyncio
async def test_a_backoff_set_while_a_request_waits_its_turn_is_waited_out_too(clock):
    server = Server({"https://api.a.test/jobs": [(200, "")]})
    http._next_slot["api.a.test"] = 1002.0  # the host's next free slot

    async def overload_reported_meanwhile():
        http._defer("api.a.test", 10.0)  # another request just met a 503

    async with _client(server) as client:
        await asyncio.gather(
            http.request(client, "GET", "https://api.a.test/jobs", rate_limit=0),
            overload_reported_meanwhile(),
        )

    assert clock.sleeps == [2.0, 10.0]
    assert len(server.requests) == 1


# --- robots.txt -----------------------------------------------------------------------

ROBOTS = "User-agent: *\nDisallow: /private\n\nUser-agent: TailrdBot\nDisallow: /no-bots\n"


@pytest.mark.asyncio
async def test_robots_rules_apply_and_are_read_once_per_host(clock):
    server = Server({"https://a.test/robots.txt": [(200, ROBOTS)]})

    async with _client(server) as client:
        assert await http.robots_allowed(client, "https://a.test/jobs/1") is True
        assert await http.robots_allowed(client, "https://a.test/no-bots/1") is False
        assert await http.robots_allowed(client, "https://a.test/jobs/2") is True

    assert server.urls() == ["https://a.test/robots.txt"]
    assert server.requests[0].headers["user-agent"] == UA


@pytest.mark.asyncio
async def test_robots_rules_for_every_agent_apply_to_us(clock):
    server = Server({"https://a.test/robots.txt": [(200, "User-agent: *\nDisallow: /private\n")]})

    async with _client(server) as client:
        assert await http.robots_allowed(client, "https://a.test/private/x") is False
        assert await http.robots_allowed(client, "https://a.test/jobs/1") is True


@pytest.mark.asyncio
async def test_concurrent_first_asks_share_one_robots_fetch(clock):
    server = Server({"https://a.test/robots.txt": [(200, ROBOTS)]})

    async def answer_after_a_turn(request):
        await asyncio.sleep(0)  # let the other asks run while this fetch is out
        return server(request)

    async with http.make_client(transport=httpx.MockTransport(answer_after_a_turn)) as client:
        answers = await asyncio.gather(*(
            http.robots_allowed(client, f"https://a.test/jobs/{i}") for i in range(5)
        ))

    assert answers == [True] * 5
    assert server.urls() == ["https://a.test/robots.txt"]


@pytest.mark.asyncio
async def test_a_robots_txt_that_cannot_be_fetched_allows(clock):
    server = Server({
        "https://missing.test/robots.txt": [(404, "")],
        "https://broken.test/robots.txt": [(503, "")],
        "https://down.test/robots.txt": [httpx.ConnectError("refused")],
    })

    async with _client(server) as client:
        for host in ("missing.test", "broken.test", "down.test"):
            assert await http.robots_allowed(client, f"https://{host}/jobs/1") is True
