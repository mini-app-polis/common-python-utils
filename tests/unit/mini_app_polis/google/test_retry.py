import contextlib


def test_is_retryable_http_error_statuses(as_http_error):
    from mini_app_polis.google._retry import is_retryable_http_error

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
    from mini_app_polis.google._retry import execute_with_retry

    called = {"n": 0}

    def fn():
        called["n"] += 1
        return 123

    assert execute_with_retry(fn, context="unit") == 123
    assert called["n"] == 1


def test_execute_with_retry_retries_http_error(monkeypatch, as_http_error):
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    # Make sleep deterministic/fast
    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("mini_app_polis.google._retry.random.random", lambda: 0.0)

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
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", lambda _s: None)

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
    from mini_app_polis.google._retry import execute_with_retry

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


def test_is_retryable_non_http_error_connection_errors():
    from mini_app_polis.google._retry import is_retryable_non_http_error

    # A dropped keep-alive connection: EPIPE on write, reset or abort otherwise.
    assert is_retryable_non_http_error(BrokenPipeError(32, "Broken pipe")) is True
    assert is_retryable_non_http_error(OSError(32, "Broken pipe")) is True
    assert is_retryable_non_http_error(ConnectionResetError(104, "reset")) is True
    assert is_retryable_non_http_error(ConnectionAbortedError(103, "aborted")) is True

    # Not transport failures.
    assert is_retryable_non_http_error(OSError(2, "No such file")) is False
    assert is_retryable_non_http_error(ValueError("boom")) is False


def test_execute_with_retry_retries_broken_pipe(monkeypatch):
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", lambda _s: None)

    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] == 1:
            raise BrokenPipeError(32, "Broken pipe")
        return "ok"

    retry = RetryConfig(max_retries=3, base_delay_s=0.01, max_delay_s=0.02)
    assert execute_with_retry(fn, context="finding folder", retry=retry) == "ok"
    assert state["n"] == 2


def test_is_retryable_non_http_error_dropped_tls_session():
    import ssl

    from mini_app_polis.google._retry import is_retryable_non_http_error

    # Google closing the TLS session mid-request.
    assert (
        is_retryable_non_http_error(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2437)")
        )
        is True
    )
    assert is_retryable_non_http_error(ssl.SSLZeroReturnError(6, "closed")) is True

    # Certificate and handshake failures are not transient.
    assert is_retryable_non_http_error(ssl.SSLCertVerificationError(1, "bad")) is False
    assert is_retryable_non_http_error(ssl.SSLError(1, "handshake failure")) is False


def test_execute_with_retry_retries_ssl_eof(monkeypatch):
    import ssl

    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", lambda _s: None)

    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] == 1:
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        return "ok"

    retry = RetryConfig(max_retries=3, base_delay_s=0.01, max_delay_s=0.02)
    assert execute_with_retry(fn, context="uploading bytes file", retry=retry) == "ok"
    assert state["n"] == 2


def test_execute_with_retry_expected_error_skips_error_log(as_http_error, monkeypatch):
    import pytest

    from mini_app_polis.google import _retry

    errors: list[str] = []
    monkeypatch.setattr(_retry.log, "error", errors.append)
    boom = as_http_error(status=400, message="invalidSharingRequest")

    def fn():
        raise boom

    with pytest.raises(type(boom)):
        _retry.execute_with_retry(fn, context="unit", expected_error=lambda _e: True)
    assert errors == []

    with pytest.raises(type(boom)):
        _retry.execute_with_retry(fn, context="unit", expected_error=lambda _e: False)
    assert len(errors) == 1


def test_execute_with_retry_expected_error_does_not_stop_retries(
    as_http_error, monkeypatch
):
    from mini_app_polis.google import _retry

    monkeypatch.setattr(_retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise as_http_error(status=503)
        return "ok"

    result = _retry.execute_with_retry(
        fn, context="unit", expected_error=lambda _e: True
    )
    assert result == "ok"
    assert calls["n"] == 2


def test_execute_with_retry_failure_frees_the_callers_frames(as_http_error):
    """A call that fails leaves nothing of its callers behind: their frames,
    and the locals in them (a file's bytes, say), are freed by reference
    counting as soon as the error is handled, without waiting for a garbage
    collection. A worker taking in many files ran out of memory when every
    failed file's bytes stayed alive."""
    import gc
    import weakref

    from googleapiclient.errors import HttpError

    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    class FileBytes:
        pass

    def caller(held: FileBytes) -> None:
        def fn():
            raise as_http_error(status=403, message="insufficientFilePermissions")

        assert held is not None
        execute_with_retry(
            fn, context="moving a file", retry=RetryConfig(max_retries=1)
        )

    held = FileBytes()
    ref = weakref.ref(held)
    gc.disable()
    try:
        with contextlib.suppress(HttpError):
            caller(held)
        del held
        assert ref() is None
    finally:
        gc.enable()
