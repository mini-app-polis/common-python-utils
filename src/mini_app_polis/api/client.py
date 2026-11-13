"""HTTP client for Kaiano internal APIs.

**Auth:** Sends ``Authorization: Bearer <key>`` using this caller's own named
API key. No token exchange, no issuer on the request path — the key is the
credential and the receiving service matches it against configuration.

**Identity:** a cog presents its own named API key as a bearer credential.
The key identifies the machine — the receiving service matches it against the
keys it holds in configuration — so nothing is asserted by the caller, no
token is minted, and no identity provider sits on the request path.

The key proves *who* the caller is and never what it may do. Permissions are
decided by the receiving service from its own declaration, and nothing sent
here can widen them.

The shared Clerk machine secret this replaced is gone. Every cog holding one
key indistinguishable from every other cog's was the reason the API could tell
that *a* cog called it and never which one; keeping it as a fallback would
have kept that ambiguity available.

Pass ``machine_name`` and the client finds that cog's key by convention:
``transcription-cog`` -> ``TRANSCRIPTION_COG_API_KEY``. The same convention is
used by the receiving service to derive the variable it checks against, so
there is one rule rather than a mapping to keep in step on both sides.

That derivation lives here, in the shared client, rather than in each cog. A
helper copied into five repos is five things to change when the convention
does, and four of them will be missed.

**Notifications:** ``notify()`` wraps ``POST /v1/notify`` so the path and
body shape live here rather than in each cog. See
:mod:`mini_app_polis.pipeline_status` for the best-effort layer above it.

**Env vars:**
  KAIANO_API_BASE_URL             — base URL of the target API service, in
                                    production. Non-production reads
                                    KAIANO_API_BASE_URL_DEV instead; see
                                    mini_app_polis.environment.env_var. There
                                    is no fallback between the two.
  <MACHINE_NAME>_API_KEY          — this cog's own key (from machine_name)
  KAIANO_API_KEY                  — key for a caller that declares no name
"""

from __future__ import annotations

import datetime as dt
import logging as _logging
import os
import random
import time
import uuid
from functools import cache
from typing import Any, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter

from ..deadline import _ensure_time_left, _wait_crosses_deadline
from ..environment import api_base_url
from .contract import (
    ENDPOINTS_BY_NAME,
    AgentResultAccepted,
    AgentRun,
    AgentRunCreate,
    AgentRunFailure,
    AgentRunResult,
    AgentSpend,
    CatalogDisplay,
    CatalogDisplayRequest,
    CatalogFillUp,
    CatalogFillUpResult,
    CatalogLookupRequest,
    CatalogMatch,
    DeejayRunAccepted,
    DeejayRunRequest,
    DuplicateReport,
    DuplicateReportResult,
    Envelope,
    FeatureFlagItem,
    IngestResponseData,
    IngestSet,
    LibraryEntryCreate,
    LibraryEntryCreated,
    LibraryFile,
    LibraryFileEdited,
    LibraryFileEditResult,
    LibraryFileRegister,
    LibraryFilesAbsentRequest,
    LibraryFilesAbsentResult,
    LibraryFilesSynced,
    LibraryFilesSyncedResult,
    LibraryIdentification,
    LivePlaysIngest,
    LivePlaysResponseData,
    NotificationResult,
    NotifyRequest,
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    PipelineEvaluationWriteResult,
    ReconcileApprovals,
    ReconcileApprovalsSet,
    ReconcileReport,
    ReconcileReportResult,
    RefiningBudgetSpentMarked,
    RefiningBudgetSpentRequest,
    RefiningClaimReleased,
    RefiningClaimRequest,
    RefiningClaimResult,
    RefiningJobAccepted,
    RefiningJobRequest,
    RefiningProposal,
    RefiningProposalAccepted,
    RefiningRecord,
    RefiningRecordWrite,
    RefiningReturnRate,
    RefiningRunCreate,
    RefiningRunRecorded,
    RefiningSpend,
    RotationConfig,
    RotationEvent,
    RotationEventCreate,
    RotationIncident,
    RotationIncidentAccepted,
    RotationIncidentEntryStatus,
    RotationIncidentRegistration,
    RotationIncidentRequest,
    RotationIncidentTaskRequested,
    RotationRequest,
    RotationRequestAccepted,
    SecretChange,
    SetListItem,
    SpotifyPlaylistsIngest,
    SpotifyPlaylistsIngestResponse,
    TaskCreateAccepted,
    TaskCreateRequest,
    TaskSweepAccepted,
    TaskSweepRequest,
    TranscriptionRunAccepted,
    TranscriptionRunRequest,
    WcsSourceCreate,
    WcsSourceItem,
    WcsTranscriptCreate,
    WcsTranscriptItem,
    WcsWikiExportItem,
    WcsWikiFacetsState,
    WcsWikiJobsAccepted,
    WcsWikiJobsRequest,
    WcsWikiLinksState,
    WcsWikiPagesWrite,
    WcsWikiPagesWriteResult,
    WcsWikiProseState,
    WcsWikiStateWriteResult,
    WcsWikiThinState,
    WcsWikiThinWrite,
    WcsWikiThinWriteResult,
)
from .errors import ApiUnavailable, KaianoApiError, error_for, is_edge_rate_limited

_log = _logging.getLogger(__name__)


@cache
def _envelope_adapter(endpoint_name: str) -> TypeAdapter[Any]:
    """Validator for one catalog endpoint's full response body.

    Built once per endpoint: parametrising ``Envelope`` creates a new model
    class, which is not something to do on every call.
    """
    response = ENDPOINTS_BY_NAME[endpoint_name].response
    return TypeAdapter(Envelope[response])  # type: ignore[valid-type]


def machine_key_env_var(machine_name: str) -> str:
    """Environment variable holding a machine's key.

    ``deejay-cog`` -> ``DEEJAY_COG_API_KEY``. Derived from the name so the
    caller and the receiving service agree without either one carrying a
    mapping.
    """
    return f"{machine_name.upper().replace('-', '_')}_API_KEY"


def _key_for(machine_name: str | None) -> str | None:
    """This caller's key: its own variable first, then the generic one.

    A variable that exists but is blank counts as unset. It should behave like
    an absent one rather than an empty credential that fails on first use.
    """
    if machine_name:
        own = (os.environ.get(machine_key_env_var(machine_name)) or "").strip()
        if own:
            return own
    return (os.environ.get("KAIANO_API_KEY") or "").strip() or None


# ── Retrying without piling on ───────────────────────────────────────────
#
# A retry sent the moment the last attempt failed lands on a service that is
# still busy with the request it gave up on. A slow GET whose timeout fires
# while the server is still building the answer would, retried at once,
# start a second build beside the first, then a third. A GET is idempotent, so it is retried on a
# gateway error and on 429 as well as on a lost connection, but only after a
# wait that grows each attempt, jittered so callers that failed together do
# not come back together. A ``Retry-After`` from the server is used instead.

#: The first wait between attempts, in seconds; it doubles each attempt.
_BACKOFF_BASE_S = 2.0
#: No one wait is longer than this, whatever ``Retry-After`` asks for.
_BACKOFF_MAX_S = 30.0

#: Patched in tests.
_sleep = time.sleep

#: POST paths retried on the API's own 429: upserts keyed by Drive id, so a
#: repeat is safe. A 429 from Cloudflare's edge is retried on any path: the
#: request never reached the API (errors.is_edge_rate_limited).
_RETRY_ON_429_POSTS = frozenset({"/v1/catalog/library/files"})


def _retry_after_s(response: httpx.Response) -> float | None:
    """The wait a response asks for, in seconds, or None if it names none."""
    headers = getattr(response, "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    if not isinstance(raw, str):
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None  # an HTTP date: rare, and the backoff serves as well


def _backoff_s(attempt: int, retry_after: float | None) -> float:
    """How long to wait after failed attempt ``attempt`` (1-based)."""
    if retry_after is not None:
        return min(_BACKOFF_MAX_S, retry_after)
    wait = _BACKOFF_BASE_S * 2 ** (attempt - 1) * random.uniform(0.5, 1.0)
    return min(_BACKOFF_MAX_S, wait)


# ── Conditional reads ────────────────────────────────────────────────────
#
# An endpoint whose response is large and changes rarely is read with its
# ETag: this process keeps the last body, sends the ETag back, and an
# unchanged response comes back as an empty 304. A warm Lambda keeps module
# state between invocations, so that holds across jobs. The server checks
# every read, so a stale body is never used.

#: Endpoint name -> its request timeout in seconds. The export is the whole
#: corpus; built cold it takes long enough that the default timeout gave up
#: on it while the API kept building, and the retry asked for another.
_CONDITIONAL_READS: dict[str, float] = {"export_wcs_wiki": 120.0}

#: URL -> (ETag, body) of the last 200 for each conditional read.
_ETAG_CACHE: dict[str, tuple[str, bytes]] = {}


class KaianoApiClient:
    """
    HTTP client for calling Kaiano's internal FastAPI services.

    Reads configuration from environment variables:
      KAIANO_API_BASE_URL             — base URL of the target service
                                        (KAIANO_API_BASE_URL_DEV outside
                                        production)
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        api_key: str | None = None,
        machine_name: str | None = None,
        user_agent: str | None = None,
    ):
        """``user_agent`` names the caller in every request's User-Agent
        header (``mini-app-polis-rotator/1``); without it the HTTP library's
        default is sent."""
        self.base_url = (base_url or api_base_url()).rstrip("/")
        self.machine_name = machine_name or os.environ.get("KAIANO_API_MACHINE_NAME")
        self.api_key = api_key or _key_for(self.machine_name)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = (user_agent or "").strip() or None

    @classmethod
    def from_env(cls, machine_name: str | None = None) -> KaianoApiClient:
        """Build a client from the environment.

        Pass ``machine_name`` so this caller presents its own key rather than
        the shared fleet credential — that is what makes the receiving
        service's audit trail name which cog called.
        """
        return cls(machine_name=machine_name)

    def _headers(self) -> dict[str, str]:
        """Auth headers for API requests.

        The key is used directly — no token exchange, so no network call
        before the call you wanted to make, and nothing to cache or refresh.
        """
        if not self.api_key:
            raise KaianoApiError(
                status_code=0,
                message=(
                    "No API key: set this machine's key (see machine_key_env_var) "
                    "or KAIANO_API_KEY"
                ),
                path="",
            )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return headers

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Make a synchronous POST request to the API.

        Retries up to max_retries times on connection errors, and with GET's
        backoff on a 429: on any path when Cloudflare's edge answered it (the
        request never reached the API), and on the API's own 429 only for
        the paths in ``_RETRY_ON_429_POSTS``.
        Raises KaianoApiError on non-2xx responses, ApiUnavailable when the
        API cannot be reached.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        rate_limited: KaianoApiError | None = None
        err: KaianoApiError | None = None

        # Cleared on the way out: a caught exception left in a local closes a
        # cycle through this frame that keeps every caller's frame, and its
        # locals, alive until a full garbage collection (see
        # mini_app_polis.google._retry.execute_with_retry).
        try:
            for attempt in range(1, self.max_retries + 1):
                if rate_limited is not None:
                    _ensure_time_left(f"writing {path}", rate_limited)
                try:
                    with httpx.Client(timeout=self.timeout) as client:
                        response = client.post(
                            url, json=payload, headers=self._headers()
                        )

                    if response.status_code >= 400:
                        err = error_for(response.status_code, response.text, path)
                        if response.status_code != 429 or (
                            path not in _RETRY_ON_429_POSTS
                            and not is_edge_rate_limited(
                                response.status_code, response.text
                            )
                        ):
                            raise err
                        wait = _backoff_s(attempt, _retry_after_s(response))
                        if attempt == self.max_retries or _wait_crosses_deadline(wait):
                            raise err
                        rate_limited = err
                        _log.warning(
                            "POST %s answered 429; attempt %d of %d, retrying in %.1fs",
                            path,
                            attempt,
                            self.max_retries,
                            wait,
                        )
                        _sleep(wait)
                        continue

                    return response.json()

                except httpx.TransportError as exc:
                    last_exc = exc
                    if attempt == self.max_retries:
                        break
                    continue

            raise ApiUnavailable(
                status_code=0,
                message=f"Connection failed after {self.max_retries} attempts: {last_exc}",
                path=path,
            )
        finally:
            last_exc = rate_limited = err = None

    def notify(
        self,
        content: str | None = None,
        *,
        embeds: list[dict[str, Any]] | None = None,
        username: str | None = None,
    ) -> dict[str, Any]:
        """Send one Discord notification via ``POST /v1/notify``.

        The one place in the fleet that knows the notification path and its
        body shape. A cog calling ``post("/v1/notify", {...})`` by hand works
        today and breaks silently the day the route or the payload changes;
        the whole argument for deriving the key variable in this client
        rather than in five cogs applies here unchanged.

        Requires ``notify.messages.send``, which every declared machine holds.
        Build the client with ``machine_name`` so the API's audit trail names
        which cog sent the message rather than only that one did.

        Returns the response body, a ``NotificationResult``. Every outcome of
        the Discord send answers 200, never a 5xx: ``forwarded`` true with
        reason ``forwarded`` when it went straight out; ``forwarded`` false
        with reason ``queued`` when the API accepted it for later delivery,
        which is not a failure; ``forwarded`` false with reason
        ``delivery_failed`` when Discord refused it outright and it will not
        be retried. Outside production an unmarked message answers
        ``forwarded`` false with reason ``development_not_reported``.

        Raises :class:`KaianoApiError` like every other verb here — the caller
        decides whether a missed notification matters. Callers for which it
        does not should use :mod:`mini_app_polis.pipeline_status`, which is
        best-effort by contract.
        """
        if not content and not embeds:
            raise KaianoApiError(
                status_code=0,
                message="notify requires content or embeds",
                path="/v1/notify",
            )

        payload: dict[str, Any] = {}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        if username:
            payload["username"] = username

        return self.post("/v1/notify", payload)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Make a synchronous GET request to the API.

        Makes up to max_retries attempts, waiting longer before each one,
        when the connection fails, a gateway answers 502, 503 or 504, or the
        API answers 429.
        Raises KaianoApiError on non-2xx responses, ApiUnavailable when the
        API cannot be reached.
        """
        return self._get_response(path, params).json()

    def _get_response(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """A GET's response: any 2xx or 3xx, after retries; raises otherwise.

        Inside :func:`mini_app_polis.deadline.run_deadline`, no attempt starts
        past the deadline, and a wait that would end at or past it is skipped
        and the error that prompted it raised instead.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        last_error: Exception | None = None
        err: KaianoApiError | None = None
        query = params or {}
        request_headers = {**self._headers(), **(headers or {})}
        attempts = 0

        # Cleared on the way out: a caught exception left in a local closes a
        # cycle through this frame that keeps every caller's frame, and its
        # locals, alive until a full garbage collection (see
        # mini_app_polis.google._retry.execute_with_retry).
        try:
            for attempt in range(1, self.max_retries + 1):
                _ensure_time_left(f"reading {path}", last_error)
                attempts = attempt
                final = attempt == self.max_retries
                try:
                    with httpx.Client(timeout=timeout or self.timeout) as client:
                        # `params` is passed only when there is something to
                        # pass. httpx *replaces* a URL's query string whenever
                        # the kwarg is supplied at all, so the previous
                        # unconditional `params=query` — where query was `{}`
                        # for every caller that had no params — silently
                        # discarded any query string written into `path`.
                        #
                        # A caller asking for `/v1/evaluations?repo=x&limit=1`
                        # got `/v1/evaluations`: no filter, default limit, the
                        # newest row across every repo. It returned 200 with
                        # plausible data, so nothing looked wrong anywhere.
                        if query:
                            response = client.get(
                                url, params=query, headers=request_headers
                            )
                        else:
                            response = client.get(url, headers=request_headers)
                except httpx.TransportError as exc:
                    last_exc = last_error = exc
                    wait = _backoff_s(attempt, None)
                    if final or _wait_crosses_deadline(wait):
                        break
                    _sleep(wait)
                    continue

                if response.status_code < 400:
                    return response
                err = last_error = error_for(response.status_code, response.text, path)
                wait = _backoff_s(attempt, _retry_after_s(response))
                if (
                    final
                    or not (
                        isinstance(err, ApiUnavailable) or response.status_code == 429
                    )
                    or _wait_crosses_deadline(wait)
                ):
                    raise err
                _log.warning(
                    "GET %s answered %d; attempt %d of %d, retrying in %.1fs",
                    path,
                    response.status_code,
                    attempt,
                    self.max_retries,
                    wait,
                )
                _sleep(wait)

            raise ApiUnavailable(
                status_code=0,
                message=f"Connection failed after {attempts} attempts: {last_exc}",
                path=path,
            ) from last_exc
        finally:
            last_exc = last_error = err = None

    # ── Typed methods ─────────────────────────────────────────────────────
    #
    # One per endpoint in mini_app_polis.api.contract.ENDPOINTS. Each takes
    # the request model the API validates against, sends only the fields the
    # caller set — the same body a hand-built dict would have been — under
    # their wire names, and returns the envelope's ``data`` validated against
    # the response model.
    #
    # Wire names matter: a field whose name is a Python keyword is declared
    # with an alias (``from_`` is ``"from"`` on the wire), and the API reads
    # the alias. Dumped by field name it arrives missing and is a 422.
    #
    # A response that does not match raises pydantic's ValidationError rather
    # than KaianoApiError: the call succeeded and the API answered, but not
    # in the shape the contract says, and that should read differently from
    # a refusal. get() and post() stay for anything outside the catalog.

    def _call(
        self,
        endpoint_name: str,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        path_params: dict[str, str] | None = None,
    ) -> Any:
        endpoint = ENDPOINTS_BY_NAME[endpoint_name]
        path = endpoint.path
        if path_params:
            path = path.format(
                **{key: quote(value, safe="") for key, value in path_params.items()}
            )
        if endpoint.method == "POST":
            if body is None:
                raise TypeError(f"{endpoint_name} needs a request body")
            raw = self.post(
                path,
                body.model_dump(mode="json", exclude_unset=True, by_alias=True),
            )
        elif endpoint_name in _CONDITIONAL_READS:
            body_bytes = self._conditional_get(
                path, params, _CONDITIONAL_READS[endpoint_name]
            )
            return _envelope_adapter(endpoint_name).validate_json(body_bytes).data
        else:
            raw = self.get(path, params)
        return _envelope_adapter(endpoint_name).validate_python(raw).data

    def _conditional_get(
        self, path: str, params: dict[str, Any] | None, timeout: float
    ) -> bytes:
        """A GET's body, revalidated by ETag against this process's last copy.

        A 304 returns the copy kept from the last 200. The caller validates
        the bytes afresh each time, so it gets its own model and can never
        alter the copy the next call reads.
        """
        key = f"{self.base_url}{path}"
        cached = _ETAG_CACHE.get(key)
        response = self._get_response(
            path,
            params,
            headers={"If-None-Match": cached[0]} if cached else None,
            timeout=max(self.timeout, timeout),
        )
        if response.status_code == 304:
            if cached is None:
                raise KaianoApiError(
                    status_code=304,
                    message="304 Not Modified, but no ETag was sent",
                    path=path,
                )
            return cached[1]
        body = response.content
        etag = response.headers.get("ETag")
        if etag:
            _ETAG_CACHE[key] = (etag, body)
        else:
            _ETAG_CACHE.pop(key, None)
        return body

    def lookup_catalog(
        self, payload: CatalogLookupRequest
    ) -> list[CatalogMatch | None]:
        """``POST /v1/catalog/lookup`` — the catalog entry each track belongs to.

        One answer per track, in order: the entry, or None when the catalog
        has no song it is a play of. Tracks are matched as a set import
        matches its plays, and nothing is recorded.
        """
        return cast(list[CatalogMatch | None], self._call("lookup_catalog", payload))

    def register_library_file(self, payload: LibraryFileRegister) -> LibraryFile:
        """``POST /v1/catalog/library/files`` — register a library file, or
        bring its row up to date, keyed by its Drive id.

        The file is present again and its ``last_seen`` is now. Optional
        fields not set on ``payload`` are not sent, so they keep their value.
        Raises :class:`OtherLibraryError` when ``catalog_id`` is an entry of
        the other library.
        """
        return cast(LibraryFile, self._call("register_library_file", payload))

    def mark_library_files_absent(
        self, payload: LibraryFilesAbsentRequest
    ) -> LibraryFilesAbsentResult:
        """``POST /v1/catalog/library/files/absent`` — files gone from the
        library are marked not present; their rows and links are kept."""
        return cast(
            LibraryFilesAbsentResult,
            self._call("mark_library_files_absent", payload),
        )

    def list_library_files(
        self,
        *,
        catalog_id: uuid.UUID | None = None,
        identification_status: LibraryIdentification | None = None,
        present: bool | None = None,
        library_root_id: str | None = None,
        trash_requested: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        after: uuid.UUID | None = None,
    ) -> list[LibraryFile]:
        """``GET /v1/catalog/library/files`` — one page of registered files.

        Filter by the catalog entry they link to, their identification
        status, whether they are present, their library root, or whether
        the owner asked for them to go to Drive's trash. Unset
        filters are not sent, so the API's own defaults apply (``limit``
        100, at most 500). Page with ``offset`` until a page comes back
        shorter than ``limit``, or with ``after``, the ``id`` of the last
        row of the page before: rows that join or leave the filter earlier
        in the order then neither repeat nor skip a row.
        """
        params = {
            key: value
            for key, value in {
                "catalog_id": str(catalog_id) if catalog_id else None,
                "identification_status": identification_status,
                "present": None if present is None else str(present).lower(),
                "library_root_id": library_root_id,
                "trash_requested": (
                    None if trash_requested is None else str(trash_requested).lower()
                ),
                "limit": limit,
                "offset": offset,
                "after": str(after) if after else None,
            }.items()
            if value is not None
        }
        return cast(list[LibraryFile], self._call("list_library_files", params=params))

    def list_library_files_needing_sync(
        self,
        *,
        library_root_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[LibraryFile]:
        """``GET /v1/catalog/library/files/needing-sync`` — one page of the
        present, linked files whose catalog entry, or the display rules,
        changed after the catalog's values were last written to them (or
        that never had them written).

        Unset arguments are not sent, so the API's defaults apply (``limit``
        100, at most 500). Page with ``offset`` until a page comes back
        shorter than ``limit``.
        """
        params = {
            key: value
            for key, value in {
                "library_root_id": library_root_id,
                "limit": limit,
                "offset": offset,
            }.items()
            if value is not None
        }
        return cast(
            list[LibraryFile],
            self._call("list_library_files_needing_sync", params=params),
        )

    def mark_library_files_synced(
        self, payload: LibraryFilesSynced
    ) -> LibraryFilesSyncedResult:
        """``POST /v1/catalog/library/files/synced`` — the files named now
        carry the catalog's values, as of ``payload.synced_at``."""
        return cast(
            LibraryFilesSyncedResult,
            self._call("mark_library_files_synced", payload),
        )

    def report_library_file_edited(
        self, payload: LibraryFileEdited
    ) -> LibraryFileEditResult:
        """``POST /v1/catalog/library/files/edited`` — a linked file's owned
        fields as read after it changed outside the pipeline; each field it
        changed and the catalog did not goes up to its entry."""
        return cast(
            LibraryFileEditResult,
            self._call("report_library_file_edited", payload),
        )

    def report_duplicate_groups(
        self, payload: DuplicateReport
    ) -> DuplicateReportResult:
        """``POST /v1/catalog/library/duplicates`` — the duplicate groups a
        report run found under one root; they replace the open groups, and
        groups the owner already resolved are kept."""
        return cast(
            DuplicateReportResult, self._call("report_duplicate_groups", payload)
        )

    def create_library_entry(self, payload: LibraryEntryCreate) -> LibraryEntryCreated:
        """``POST /v1/catalog/library/entries`` — a catalog entry, from the
        library and with no plays, for a song that was never played.

        When the catalog already has the song's entry, that entry comes back
        with ``created`` false and nothing is made. With ``preview`` set,
        nothing is saved: a song the catalog lacks comes back as the entry
        that would be made, with ``previewed`` true. Only entries of
        ``payload.library`` (main unless set) are matched or made.
        """
        return cast(LibraryEntryCreated, self._call("create_library_entry", payload))

    def read_catalog_display(
        self, payload: CatalogDisplayRequest
    ) -> list[CatalogDisplay | None]:
        """``POST /v1/catalog/display`` — each entry's owned fields as a file
        should carry them, in the order asked; None for an id the catalog
        does not have."""
        return cast(
            list[CatalogDisplay | None], self._call("read_catalog_display", payload)
        )

    def report_reconcile_differences(
        self, payload: ReconcileReport
    ) -> ReconcileReportResult:
        """``POST /v1/catalog/library/reconcile/differences`` — the files a
        reconcile run compared and their differences, which replace what the
        API holds for those files."""
        return cast(
            ReconcileReportResult,
            self._call("report_reconcile_differences", payload),
        )

    def read_reconcile_approvals(self) -> ReconcileApprovals:
        """``GET /v1/catalog/library/reconcile/approvals`` — what the owner
        approved for the reconcile's write runs (empty, with no
        ``updated_at``, until any are set)."""
        return cast(ReconcileApprovals, self._call("read_reconcile_approvals"))

    def seed_reconcile_approvals(
        self, payload: ReconcileApprovalsSet
    ) -> ReconcileApprovals:
        """``POST /v1/catalog/library/reconcile/approvals/seed`` — approvals
        given before the API held them; refused (409) once any are set."""
        return cast(ReconcileApprovals, self._call("seed_reconcile_approvals", payload))

    def fill_up_catalog_entry(
        self, catalog_id: str | uuid.UUID, payload: CatalogFillUp
    ) -> CatalogFillUpResult:
        """``POST /v1/catalog/{catalog_id}/fill-up`` — a file's values for the
        fields the entry leaves empty; a value the catalog has is kept."""
        return cast(
            CatalogFillUpResult,
            self._call(
                "fill_up_catalog_entry",
                payload,
                path_params={"catalog_id": str(catalog_id)},
            ),
        )

    def ingest(self, payload: IngestSet) -> IngestResponseData:
        """``POST /v1/ingest`` — one set and its tracks."""
        return cast(IngestResponseData, self._call("ingest", payload))

    def list_sets(
        self,
        *,
        date_from: dt.date | None = None,
        date_to: dt.date | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[SetListItem]:
        """``GET /v1/sets`` — one page of sets, filtered by date.

        Unset filters are not sent, so the API's own defaults apply
        (``limit`` 50, at most 200). Page with ``offset`` until a page
        comes back shorter than ``limit``.
        """
        params = {
            key: value
            for key, value in {
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "limit": limit,
                "offset": offset,
            }.items()
            if value is not None
        }
        return cast(list[SetListItem], self._call("list_sets", params=params))

    def ingest_live_plays(self, payload: LivePlaysIngest) -> LivePlaysResponseData:
        """``POST /v1/live-plays`` — a batch of plays from the live history."""
        return cast(LivePlaysResponseData, self._call("ingest_live_plays", payload))

    def ingest_spotify_playlists(
        self, payload: SpotifyPlaylistsIngest
    ) -> SpotifyPlaylistsIngestResponse:
        """``POST /v1/spotify/playlists`` — the playlists a set was published to."""
        return cast(
            SpotifyPlaylistsIngestResponse,
            self._call("ingest_spotify_playlists", payload),
        )

    def create_wcs_transcript(self, payload: WcsTranscriptCreate) -> WcsTranscriptItem:
        """``POST /v1/wcs/transcripts`` — store one raw transcript."""
        return cast(WcsTranscriptItem, self._call("create_wcs_transcript", payload))

    def create_wcs_source(self, payload: WcsSourceCreate) -> WcsSourceItem:
        """``POST /v1/wcs/sources`` — ingest one source and its extraction."""
        return cast(WcsSourceItem, self._call("create_wcs_source", payload))

    def export_wcs_wiki(self) -> WcsWikiExportItem:
        """``GET /v1/wcs/wiki/export`` — the whole corpus in one response.

        The API labels each export with an ETag that changes whenever the
        corpus does. This process keeps the last body it read and sends that
        ETag back, so an unchanged corpus costs the API one small query and
        comes back as an empty 304. The body is validated afresh on every
        call, so callers get their own model and can never alter the copy
        the next call reads.
        """
        return cast(WcsWikiExportItem, self._call("export_wcs_wiki"))

    # The wiki curator's state. A GET is the whole document; a POST upserts
    # the entries it carries and leaves the rest alone, so a long import is
    # sent in chunks and can be re-sent.

    def get_wcs_wiki_links(self) -> WcsWikiLinksState:
        """``GET /v1/wcs/wiki/state/links`` — every linked statement."""
        return cast(WcsWikiLinksState, self._call("get_wcs_wiki_links"))

    def put_wcs_wiki_links(self, payload: WcsWikiLinksState) -> WcsWikiStateWriteResult:
        """``POST /v1/wcs/wiki/state/links`` — upsert linked statements."""
        return cast(WcsWikiStateWriteResult, self._call("put_wcs_wiki_links", payload))

    def get_wcs_wiki_facets(self) -> WcsWikiFacetsState:
        """``GET /v1/wcs/wiki/state/facets`` — every facet decision."""
        return cast(WcsWikiFacetsState, self._call("get_wcs_wiki_facets"))

    def put_wcs_wiki_facets(
        self, payload: WcsWikiFacetsState
    ) -> WcsWikiStateWriteResult:
        """``POST /v1/wcs/wiki/state/facets`` — upsert facet decisions."""
        return cast(WcsWikiStateWriteResult, self._call("put_wcs_wiki_facets", payload))

    def get_wcs_wiki_thin(self) -> WcsWikiThinState:
        """``GET /v1/wcs/wiki/state/thin`` — every thin entity."""
        return cast(WcsWikiThinState, self._call("get_wcs_wiki_thin"))

    def put_wcs_wiki_thin(self, payload: WcsWikiThinWrite) -> WcsWikiThinWriteResult:
        """``POST /v1/wcs/wiki/state/thin`` — upsert thin entities, remove the rest."""
        return cast(WcsWikiThinWriteResult, self._call("put_wcs_wiki_thin", payload))

    def put_wcs_wiki_pages(self, payload: WcsWikiPagesWrite) -> WcsWikiPagesWriteResult:
        """``POST /v1/wcs/wiki/pages`` — upsert page documents, remove pages gone."""
        return cast(WcsWikiPagesWriteResult, self._call("put_wcs_wiki_pages", payload))

    def enqueue_wcs_wiki_jobs(self, payload: WcsWikiJobsRequest) -> WcsWikiJobsAccepted:
        """``POST /v1/wcs/wiki/jobs`` — put wiki jobs on the curator's queue."""
        return cast(WcsWikiJobsAccepted, self._call("enqueue_wcs_wiki_jobs", payload))

    def get_wcs_wiki_prose(self) -> WcsWikiProseState:
        """``GET /v1/wcs/wiki/state/prose`` — every entity's prose and history."""
        return cast(WcsWikiProseState, self._call("get_wcs_wiki_prose"))

    def put_wcs_wiki_prose(self, payload: WcsWikiProseState) -> WcsWikiStateWriteResult:
        """``POST /v1/wcs/wiki/state/prose`` — upsert entities' prose."""
        return cast(WcsWikiStateWriteResult, self._call("put_wcs_wiki_prose", payload))

    def request_deejay_run(self, payload: DeejayRunRequest) -> DeejayRunAccepted:
        """``POST /v1/deejay/runs`` — enqueue a deejay-cog run.

        Answered 202 when enqueued and 200 when deduplicated; both come back
        here as the same model, with ``deduplicated`` telling them apart.
        """
        return cast(DeejayRunAccepted, self._call("request_deejay_run", payload))

    def request_transcription_run(
        self, payload: TranscriptionRunRequest
    ) -> TranscriptionRunAccepted:
        """``POST /v1/transcription/runs`` — enqueue one transcription-cog job.

        Answered 202 when enqueued and 200 when deduplicated; both come back
        here as the same model, with ``deduplicated`` telling them apart.
        """
        return cast(
            TranscriptionRunAccepted,
            self._call("request_transcription_run", payload),
        )

    def request_task(self, payload: TaskCreateRequest) -> TaskCreateAccepted:
        """``POST /v1/tasks`` — ask the task manager for one task on the board.

        Answered 202 once the job is queued; the task is created afterwards.
        Asking twice for one ``external_id`` makes one task.
        """
        return cast(TaskCreateAccepted, self._call("request_task", payload))

    def request_sweep(self, payload: TaskSweepRequest) -> TaskSweepAccepted:
        """``POST /v1/tasks/sweeps`` — ask the task manager for a sweep now.

        Answered 202 once the sweep is queued. ``refine`` and ``agents`` make
        it count as the sweep for those times of day; every switch and cap
        still applies. Asking twice runs two sweeps.
        """
        return cast(TaskSweepAccepted, self._call("request_sweep", payload))

    def list_refinings(self) -> list[RefiningRecord]:
        """``GET /v1/tasks/refinings`` — every task's refining record."""
        return cast(list[RefiningRecord], self._call("list_refinings"))

    def record_refining(self, payload: RefiningRecordWrite) -> RefiningRecord:
        """``POST /v1/tasks/refinings`` — write one task's refining record.

        Keyed by ``task_gid``: writing a task's record again replaces it.
        """
        return cast(RefiningRecord, self._call("record_refining", payload))

    def record_refining_run(self, payload: RefiningRunCreate) -> RefiningRunRecorded:
        """``POST /v1/tasks/refinings/runs`` — record one paid refining run."""
        return cast(RefiningRunRecorded, self._call("record_refining_run", payload))

    def refining_spend(self) -> RefiningSpend:
        """``GET /v1/tasks/refinings/spend`` — this month's paid refining."""
        return cast(RefiningSpend, self._call("refining_spend"))

    def refining_return_rate(self, days: int | None = None) -> RefiningReturnRate:
        """``GET /v1/tasks/refinings/return-rate`` — how often a task moved to
        Ready came back out, over the last ``days`` (the API's default, 30,
        when None)."""
        params = {"days": days} if days is not None else None
        return cast(
            RefiningReturnRate, self._call("refining_return_rate", params=params)
        )

    def request_refining(self, payload: RefiningJobRequest) -> RefiningJobAccepted:
        """``POST /v1/tasks/refinings/jobs`` — queue one task for the refiner."""
        return cast(RefiningJobAccepted, self._call("request_refining", payload))

    def submit_refining_proposal(
        self, payload: RefiningProposal
    ) -> RefiningProposalAccepted:
        """``POST /v1/tasks/refinings/proposals`` — hand a proposal back.

        The API queues it for the task manager, which applies it.
        """
        return cast(
            RefiningProposalAccepted, self._call("submit_refining_proposal", payload)
        )

    def claim_refining(self, payload: RefiningClaimRequest) -> RefiningClaimResult:
        """``POST /v1/tasks/refinings/claims`` — claim one job before refining it.

        ``claimed`` is false when the job is already held; skip it.
        """
        return cast(RefiningClaimResult, self._call("claim_refining", payload))

    def release_refining_claim(
        self, payload: RefiningClaimRequest
    ) -> RefiningClaimReleased:
        """``POST /v1/tasks/refinings/claims/release`` — give a claim back.

        For a run that failed, so the next send can refine the same content.
        """
        return cast(
            RefiningClaimReleased, self._call("release_refining_claim", payload)
        )

    def mark_refining_budget_spent(
        self, payload: RefiningBudgetSpentRequest
    ) -> RefiningBudgetSpentMarked:
        """``POST /v1/tasks/refinings/budget-spent`` — report a budget stop.

        For a run stopped because the month's refining budget is spent, so
        the sweep sends the same content again. A later won claim clears it.
        """
        return cast(
            RefiningBudgetSpentMarked,
            self._call("mark_refining_budget_spent", payload),
        )

    def record_agent_run(self, payload: AgentRunCreate) -> AgentRun:
        """``POST /v1/tasks/agent-runs`` — record a run about to be started.

        The record is the lock: a repository with a run in flight is given
        no second one.
        """
        return cast(AgentRun, self._call("record_agent_run", payload))

    def list_agent_runs(
        self, *, status: str | None = None, repository: str | None = None
    ) -> list[AgentRun]:
        """``GET /v1/tasks/agent-runs`` — runs, newest first.

        ``status`` and ``repository`` narrow the list: the runs in flight are
        ``status="dispatched"``.
        """
        params = {
            key: value
            for key, value in (("status", status), ("repository", repository))
            if value is not None
        }
        return cast(
            list[AgentRun], self._call("list_agent_runs", params=params or None)
        )

    def agent_spend(self) -> AgentSpend:
        """``GET /v1/tasks/agent-runs/spend`` — this month's agent runs."""
        return cast(AgentSpend, self._call("agent_spend"))

    def fail_agent_run(self, payload: AgentRunFailure) -> AgentRun:
        """``POST /v1/tasks/agent-runs/failures`` — mark a dispatched run failed.

        For a run that could not be started, or that never reported.
        """
        return cast(AgentRun, self._call("fail_agent_run", payload))

    def submit_agent_result(self, payload: AgentRunResult) -> AgentResultAccepted:
        """``POST /v1/tasks/agent-runs/results`` — how a run ended.

        Accepted once per run. The API queues it for the task manager,
        which moves the task.
        """
        return cast(AgentResultAccepted, self._call("submit_agent_result", payload))

    def create_evaluation(
        self, payload: PipelineEvaluationCreate
    ) -> PipelineEvaluationWriteResult:
        """``POST /v1/evaluations`` — record one graded finding.

        Raises like every other verb here. For best-effort posting from a
        flow, use :func:`mini_app_polis.pipeline_status.post_findings`.
        """
        return cast(
            PipelineEvaluationWriteResult, self._call("create_evaluation", payload)
        )

    def list_evaluations(
        self,
        *,
        repo: str | None = None,
        dimension: str | None = None,
        severity: str | None = None,
        source: str | None = None,
        run_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[PipelineEvaluationItem]:
        """``GET /v1/evaluations`` — findings, newest first.

        ``repo``, ``dimension``, ``severity`` and ``source`` take one value
        or a comma-separated list. Unset filters are not sent, so the API's
        own defaults apply (``limit`` 50, at most 500).
        """
        params = {
            key: value
            for key, value in {
                "repo": repo,
                "dimension": dimension,
                "severity": severity,
                "source": source,
                "run_id": run_id,
                "limit": limit,
                "offset": offset,
            }.items()
            if value is not None
        }
        return cast(
            list[PipelineEvaluationItem],
            self._call("list_evaluations", params=params),
        )

    def list_flags(self) -> list[FeatureFlagItem]:
        """``GET /v1/flags`` — every feature flag, with its state and value.

        A flag's ``value`` is None when it is a plain on/off flag.
        """
        return cast(list[FeatureFlagItem], self._call("list_flags"))

    def record_rotation_event(self, payload: RotationEventCreate) -> RotationEvent:
        """``POST /v1/secrets/rotation-events`` — record one rotation.

        Keyed on name, config and minted: writing the same key again updates
        the event, as when its old value is retired.
        """
        return cast(RotationEvent, self._call("record_rotation_event", payload))

    def list_rotation_events(
        self,
        *,
        name: str | None = None,
        config: RotationConfig | None = None,
        latest: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        incident_id: str | None = None,
    ) -> list[RotationEvent]:
        """``GET /v1/secrets/rotation-events`` — events, newest minted first.

        ``name``, ``config`` and ``incident_id`` (sent as ``incident``)
        narrow the list. ``latest=True`` returns only the newest event for
        each name and config, which is what the daily sweep reads. Unset
        filters are not sent, so the API's own defaults apply.
        """
        params = {
            key: value
            for key, value in {
                "name": name,
                "config": config,
                "incident": incident_id,
                "latest": "true" if latest else None,
                "limit": limit,
                "offset": offset,
            }.items()
            if value is not None
        }
        return cast(
            list[RotationEvent],
            self._call("list_rotation_events", params=params or None),
        )

    def list_secret_changes(
        self,
        *,
        config: RotationConfig | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> list[SecretChange]:
        """``GET /v1/secrets/changes`` — names Doppler reported changed, oldest first.

        ``config`` narrows the list to one config. ``since`` and ``until``
        bound when the API received the change (``since`` inclusive,
        ``until`` exclusive). Unset filters are not sent.
        Names and times only: Doppler's webhook carries no values.
        """
        params = {
            key: value.isoformat() if isinstance(value, dt.datetime) else value
            for key, value in {"config": config, "since": since, "until": until}.items()
            if value is not None
        }
        return cast(
            list[SecretChange],
            self._call("list_secret_changes", params=params or None),
        )

    def request_rotation(self, payload: RotationRequest) -> RotationRequestAccepted:
        """``POST /v1/secrets/rotations`` — ask for one secret to be rotated now.

        The API queues it for the rotator and answers 202; the rotation is
        recorded as an event once it has run.
        """
        return cast(RotationRequestAccepted, self._call("request_rotation", payload))

    def start_rotation_incident(
        self, payload: RotationIncidentRequest
    ) -> RotationIncidentAccepted:
        """``POST /v1/secrets/rotation-incidents`` — rotate every secret now.

        Mints an incident, or resumes the one ``payload.incident_id`` names,
        and queues it for the rotator. The answer carries the incident id
        every job, event and task of it will carry.
        """
        return cast(
            RotationIncidentAccepted, self._call("start_rotation_incident", payload)
        )

    def register_rotation_incident(
        self, incident_id: str, payload: RotationIncidentRegistration
    ) -> RotationIncident:
        """``POST /v1/secrets/rotation-incidents/{id}/entries`` — the
        incident's entries, as the rotator built them from its inventory.

        Registering again keeps what is already recorded for an entry.
        Returns the incident as it now stands.
        """
        return cast(
            RotationIncident,
            self._call(
                "register_rotation_incident",
                payload,
                path_params={"incident_id": incident_id},
            ),
        )

    def mark_rotation_incident_task_requested(
        self, incident_id: str, payload: RotationIncidentTaskRequested
    ) -> RotationIncidentEntryStatus:
        """``POST /v1/secrets/rotation-incidents/{id}/task-requested`` — the
        board task for one entry has been asked for."""
        return cast(
            RotationIncidentEntryStatus,
            self._call(
                "mark_rotation_incident_task_requested",
                payload,
                path_params={"incident_id": incident_id},
            ),
        )

    def get_rotation_incident(self, incident_id: str) -> RotationIncident:
        """``GET /v1/secrets/rotation-incidents/{id}`` — each entry's state,
        and what is still outstanding."""
        return cast(
            RotationIncident,
            self._call(
                "get_rotation_incident", path_params={"incident_id": incident_id}
            ),
        )

    def send_notification(self, payload: NotifyRequest) -> NotificationResult:
        """``POST /v1/notify`` — one Discord message, typed.

        :meth:`notify` is the same call taking keyword arguments and returning
        the raw body; it stays as it is for the callers that use it.

        Every outcome of the Discord send answers 200 with a
        :class:`NotificationResult`; see its docstring for the reasons. A
        result with ``forwarded`` false and reason ``queued`` was accepted and
        will be sent. A 5xx is not an expected answer for any Discord outcome,
        and raises :class:`KaianoApiError` like any other.
        """
        return cast(NotificationResult, self._call("send_notification", payload))
