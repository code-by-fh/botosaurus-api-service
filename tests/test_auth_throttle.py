from app.auth_throttle import FailedAuthLimiter, ThrottlePolicy

MAX_FAILURES = 3
WINDOW_SECONDS = 60.0
CLIENT = "203.0.113.7"
OTHER_CLIENT = "203.0.113.8"


class ManualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_limiter(clock: ManualClock, max_tracked_clients: int = 100) -> FailedAuthLimiter:
    policy = ThrottlePolicy(MAX_FAILURES, WINDOW_SECONDS, max_tracked_clients)
    return FailedAuthLimiter(policy, clock)


def fail(limiter: FailedAuthLimiter, client: str, times: int) -> None:
    for _ in range(times):
        limiter.record_failure(client)


def test_client_below_the_limit_may_try_again():
    limiter = make_limiter(ManualClock())
    fail(limiter, CLIENT, MAX_FAILURES - 1)

    assert limiter.retry_after_seconds(CLIENT) == 0


def test_client_at_the_limit_is_locked_for_the_rest_of_the_window():
    clock = ManualClock()
    limiter = make_limiter(clock)
    fail(limiter, CLIENT, MAX_FAILURES)
    clock.now += 20

    assert limiter.retry_after_seconds(CLIENT) == 40


def test_lockout_ends_when_the_oldest_failure_leaves_the_window():
    clock = ManualClock()
    limiter = make_limiter(clock)
    fail(limiter, CLIENT, MAX_FAILURES)
    clock.now += WINDOW_SECONDS

    assert limiter.retry_after_seconds(CLIENT) == 0


def test_window_slides_instead_of_resetting():
    clock = ManualClock()
    limiter = make_limiter(clock)
    fail(limiter, CLIENT, MAX_FAILURES - 1)
    clock.now += WINDOW_SECONDS / 2
    fail(limiter, CLIENT, 1)
    clock.now += WINDOW_SECONDS / 2

    assert limiter.retry_after_seconds(CLIENT) == 0


def test_clients_are_counted_separately():
    limiter = make_limiter(ManualClock())
    fail(limiter, CLIENT, MAX_FAILURES)

    assert limiter.retry_after_seconds(OTHER_CLIENT) == 0


def test_oldest_client_is_forgotten_when_the_cap_is_reached():
    limiter = make_limiter(ManualClock(), max_tracked_clients=1)
    fail(limiter, CLIENT, MAX_FAILURES)
    fail(limiter, OTHER_CLIENT, 1)

    assert limiter.retry_after_seconds(CLIENT) == 0
