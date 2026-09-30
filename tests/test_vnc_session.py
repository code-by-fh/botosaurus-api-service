from starlette.requests import Request

from app.vnc_session import SESSION_TTL_SECONDS, SessionSigner, is_https, is_same_origin

START = 1_700_000_000.0


class ManualClock:
    def __init__(self):
        self.now = START

    def __call__(self) -> float:
        return self.now


def request_with(headers: dict[str, str], scheme: str = "http") -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    scope = {"type": "http", "scheme": scheme, "server": ("svc", 80), "path": "/", "headers": raw}
    return Request(scope)


def test_issued_token_is_valid():
    signer = SessionSigner(ManualClock())

    assert signer.is_valid(signer.issue())


def test_token_expires_after_the_ttl():
    clock = ManualClock()
    signer = SessionSigner(clock)
    token = signer.issue()
    clock.now += SESSION_TTL_SECONDS

    assert not signer.is_valid(token)


def test_extended_expiry_breaks_the_signature():
    signer = SessionSigner(ManualClock())
    _, signature = signer.issue().split(".")
    forged = f"{int(START) + 10 * SESSION_TTL_SECONDS}.{signature}"

    assert not signer.is_valid(forged)


def test_token_of_another_process_is_rejected():
    clock = ManualClock()

    assert not SessionSigner(clock).is_valid(SessionSigner(clock).issue())


def test_malformed_tokens_are_rejected():
    signer = SessionSigner(ManualClock())

    assert not signer.is_valid(None)
    assert not signer.is_valid("")
    assert not signer.is_valid("not-a-token")
    assert not signer.is_valid("١٢.abc")
    assert not signer.is_valid("1" * 200)


def test_forwarded_proto_https_marks_the_request_secure():
    assert is_https(request_with({"X-Forwarded-Proto": "https"}))


def test_plain_http_request_is_not_secure():
    assert not is_https(request_with({}))


def test_same_origin_matches_the_host_header():
    request = request_with({"Host": "render.example.com", "Origin": "https://render.example.com"})

    assert is_same_origin(request)


def test_foreign_origin_is_rejected():
    request = request_with({"Host": "render.example.com", "Origin": "https://evil.example"})

    assert not is_same_origin(request)


def test_missing_or_null_origin_is_rejected():
    assert not is_same_origin(request_with({"Host": "render.example.com"}))
    assert not is_same_origin(request_with({"Host": "render.example.com", "Origin": "null"}))


def test_forwarded_host_is_accepted_for_host_rewriting_proxies():
    headers = {
        "Host": "127.0.0.1:8000",
        "X-Forwarded-Host": "render.example.com",
        "Origin": "https://render.example.com",
    }

    assert is_same_origin(request_with(headers))
