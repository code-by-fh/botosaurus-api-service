import pytest

from app.log_safety import loggable_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a/b", "https://example.com/a/b"),
        ("https://user:secret@example.com/a", "https://example.com/a"),
        ("https://example.com/a?token=abc&x=1", "https://example.com/a?***"),
        ("https://example.com:8443/a#frag", "https://example.com:8443/a"),
        ("http://[2001:db8::1]:8080/p?q=1", "http://[2001:db8::1]:8080/p?***"),
    ],
)
def test_loggable_url_keeps_scheme_host_and_path_only(url, expected):
    assert loggable_url(url) == expected
