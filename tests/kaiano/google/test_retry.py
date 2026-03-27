def test_is_retryable_http_error_statuses(as_http_error):
    from kaiano.google._retry import is_retryable_http_error

    assert is_retryable_http_error(as_http_error(status=500)) is True
    assert is_retryable_http_error(as_http_error(status=550)) is True
    assert is_retryable_http_error(as_http_error(status=599)) is True
    assert is_retryable_http_error(as_http_error(status=429)) is True
    assert is_retryable_http_error(as_http_error(status=408)) is True

    # 403 only when message suggests quota / rate limit
    assert (
        is_retryable_http_error(as_http_error(status=403, message="quota exceeded"))
        is True
    )
    assert (
        is_retryable_http_error(
            as_http_error(status=403, message="Rate Limit Exceeded")
        )
        is True
    )
    assert (
        is_retryable_http_error(as_http_error(status=403, message="backendError"))
        is True
    )
    assert (
        is_retryable_http_error(as_http_error(status=403, message="permission denied"))
        is False
    )

    # Non-retryable
    assert is_retryable_http_error(as_http_error(status=404)) is False


def test_execute_with_retry_success_first_try(monkeypatch):
    from kaiano.google._retry import execute_with_retry

    called = {"n": 0}

    def fn():
        called["n"] += 1
        return 123

    assert execute_with_retry(fn, context="unit") == 123
    assert called["n"] == 1


def test_execute_with_retry_retries_http_error(monkeypatch, as_http_error):
    from kaiano.google._retry import RetryConfig, execute_with_retry

    # Make sleep deterministic/fast
    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google._retry.random.random", lambda: 0.0)

    err = as_http_error(status=503)
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise err
        return "ok"

    retry = RetryConfig(max_retries=5, base_delay_s=0.01, max_delay_s=0.02)
    assert execute_with_retry(fn, context="doing thing", retry=retry) == "ok"
    assert state["n"] == 3


def test_execute_with_retry_non_retryable_http_error_raises(monkeypatch, as_http_error):
    from kaiano.google._retry import RetryConfig, execute_with_retry

    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)

    err = as_http_error(status=404, message="not found")

    def fn():
        raise err

    retry = RetryConfig(max_retries=3, base_delay_s=0.01, max_delay_s=0.02)
    try:
        execute_with_retry(fn, context="nope", retry=retry)
    except Exception as e:
        assert e is err
    else:
        raise AssertionError("Expected HttpError")


def test_execute_with_retry_non_http_error_does_not_retry(monkeypatch):
    from kaiano.google._retry import execute_with_retry

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("boom")

    try:
        execute_with_retry(fn, context="unit")
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")

    assert calls["n"] == 1
