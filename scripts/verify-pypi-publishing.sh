#!/usr/bin/env bash
# semantic-release verifyConditions: refuse to start a release PyPI will
# not accept at the end of it.
#
# Why this exists. On 2026-09-05 the first release of this package ran to
# completion through `prepare` — bumped the version, re-locked, committed,
# pushed and tagged — and only then failed at `uv publish`, because no
# trusted publisher existed on PyPI. Both failure and success cost the
# same version number: PyPI has no overwrite, and semantic-release never
# retries a number it has already tagged. Two majors were spent that way
# before anything was ever published.
#
# `verifyConditions` runs before any of that. One HTTP round trip here
# makes the whole class of credential failure free instead of permanent.
#
# This performs the same OIDC exchange `uv publish` performs, and throws
# the minted token away. It never prints it.
set -euo pipefail

if [[ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" || -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]]; then
  echo "::error::No OIDC token is available to this job. The release job needs 'id-token: write' in its permissions block. This is not a PyPI problem." >&2
  exit 1
fi

oidc_token="$(
  curl --silent --show-error --fail \
    --header "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
    "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=pypi" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["value"])'
)"

# PyPI answers a declined exchange with JSON naming the mismatch. It can
# also answer with no JSON at all — an empty body or an HTML error page
# from its CDN during a blip — and that is not a publisher problem. Retry
# those a few times, and if they persist, say what PyPI actually sent
# instead of blaming the publisher configuration.
attempt=1
while :; do
  response="$(
    curl --silent --show-error \
      --write-out '\n%{http_code}' \
      --request POST https://pypi.org/_/oidc/mint-token \
      --data "{\"token\": \"${oidc_token}\"}"
  )" || response=$'\n000'
  status="${response##*$'\n'}"
  mint_response="${response%$'\n'*}"

  if printf '%s' "${mint_response}" | python3 -c 'import json, sys; json.load(sys.stdin)' 2>/dev/null; then
    break
  fi
  if (( attempt >= 4 )); then
    echo "::error::PyPI did not answer the token exchange (HTTP ${status}, not JSON) after ${attempt} attempts. This is PyPI being unavailable, not the publisher configuration; nothing was bumped, so re-run the job. First bytes of the reply: $(printf '%s' "${mint_response}" | head -c 200 | tr '\n' ' ')" >&2
    exit 1
  fi
  sleep $(( attempt * 10 ))
  attempt=$(( attempt + 1 ))
done

# The JSON arrives on stdin and the program comes from -c. Do not be
# tempted to feed the program in on a heredoc as well: two redirections
# both claim stdin, the last one wins, and python silently tries to
# execute the response body as source.
if printf '%s' "${mint_response}" | python3 -c '
import json
import sys

body = json.load(sys.stdin)
if body.get("token"):
    print("PyPI trusted publishing verified — a publishing token was minted.")
    sys.exit(0)

# PyPI returns the reason in `message` and the specific mismatch in
# `errors`. Print both: the message alone says "invalid" without saying
# which of the four bound fields did not match.
print(body.get("message", "PyPI declined to mint a token."), file=sys.stderr)
for error in body.get("errors", []):
    print("  - " + str(error.get("description", error)), file=sys.stderr)
sys.exit(1)
'
then
  exit 0
fi

echo "::error::PyPI refused to mint a publishing token (HTTP ${status}), so this release was stopped before the version was bumped. Check the pending publisher: PyPI project name, owner, repository name, workflow filename (ci.yml) and environment must all match exactly. See ecosystem-standards' playbooks/pypi-package-publishing.md §3." >&2
exit 1
