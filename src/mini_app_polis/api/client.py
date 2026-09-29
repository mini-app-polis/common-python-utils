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

import logging as _logging
import os
from functools import cache
from typing import Any, cast

import httpx
from pydantic import BaseModel, TypeAdapter

from ..environment import api_base_url
from .contract import (
    ENDPOINTS_BY_NAME,
    DeejayRunAccepted,
    DeejayRunRequest,
    Envelope,
    IngestResponseData,
    IngestSet,
    LivePlaysIngest,
    LivePlaysResponseData,
    NotificationResult,
    NotifyRequest,
    PipelineEvaluationCreate,
    PipelineEvaluationItem,
    PipelineEvaluationWriteResult,
    SpotifyPlaylistsIngest,
    SpotifyPlaylistsIngestResponse,
    TranscriptionRunAccepted,
    TranscriptionRunRequest,
    WcsSourceCreate,
    WcsSourceItem,
    WcsTranscriptCreate,
    WcsTranscriptItem,
    WcsWikiExportItem,
)
from .errors import KaianoApiError

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
    ):
        self.base_url = (base_url or api_base_url()).rstrip("/")
        self.machine_name = machine_name or os.environ.get("KAIANO_API_MACHINE_NAME")
        self.api_key = api_key or _key_for(self.machine_name)
        self.timeout = timeout
        self.max_retries = max_retries

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
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Make a synchronous POST request to the API.

        Retries up to max_retries times on connection errors.
        Raises KaianoApiError on non-2xx responses.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, json=payload, headers=self._headers())

                if response.status_code >= 400:
                    raise KaianoApiError(
                        status_code=response.status_code,
                        message=response.text,
                        path=path,
                    )

                return response.json()

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                continue

        raise KaianoApiError(
            status_code=0,
            message=f"Connection failed after {self.max_retries} attempts: {last_exc}",
            path=path,
        )

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

        Retries up to max_retries times on connection errors.
        Raises KaianoApiError on non-2xx responses.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        query = params or {}

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
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
                            url, params=query, headers=self._headers()
                        )
                    else:
                        response = client.get(url, headers=self._headers())

                if response.status_code >= 400:
                    raise KaianoApiError(
                        status_code=response.status_code,
                        message=response.text,
                        path=path,
                    )

                return response.json()

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    break
                continue

        raise KaianoApiError(
            status_code=0,
            message=f"Connection failed after {self.max_retries} attempts: {last_exc}",
            path=path,
        )

    # ── Typed methods ─────────────────────────────────────────────────────
    #
    # One per endpoint in mini_app_polis.api.contract.ENDPOINTS. Each takes
    # the request model the API validates against, sends only the fields the
    # caller set — the same body a hand-built dict would have been — and
    # returns the envelope's ``data`` validated against the response model.
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
    ) -> Any:
        endpoint = ENDPOINTS_BY_NAME[endpoint_name]
        if endpoint.method == "POST":
            if body is None:
                raise TypeError(f"{endpoint_name} needs a request body")
            raw = self.post(
                endpoint.path, body.model_dump(mode="json", exclude_unset=True)
            )
        else:
            raw = self.get(endpoint.path, params)
        return _envelope_adapter(endpoint_name).validate_python(raw).data

    def ingest(self, payload: IngestSet) -> IngestResponseData:
        """``POST /v1/ingest`` — one set and its tracks."""
        return cast(IngestResponseData, self._call("ingest", payload))

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
        """``GET /v1/wcs/wiki/export`` — the whole corpus in one response."""
        return cast(WcsWikiExportItem, self._call("export_wcs_wiki"))

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

    def send_notification(self, payload: NotifyRequest) -> NotificationResult:
        """``POST /v1/notify`` — one Discord message, typed.

        :meth:`notify` is the same call taking keyword arguments and returning
        the raw body; it stays as it is for the callers that use it.
        """
        return cast(NotificationResult, self._call("send_notification", payload))
