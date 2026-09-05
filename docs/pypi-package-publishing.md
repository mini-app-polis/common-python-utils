# Playbook: publishing a Python package to PyPI

This is the canonical reference for how the fleet's Python libraries
authenticate when they publish, what the failure modes look like, and how
to get out of each one. It is the Python counterpart to
`common-typescript-utils/docs/npm-package-publishing.md`, and it exists
because that document's §7 asked the right question about the npm path and
the Python path had never been asked it at all.

Two libraries are in scope: `common-python-utils` (published as
`miniapppolis-common-utils`) and `identity` (published as
`miniapppolis-identity`). Before September 2026 neither was published;
consumers pulled them by git ref.

---

## §1 — How it works today

```
  GitHub Actions release job  (permissions: id-token: write)
    └─ npx semantic-release
         ├─ prepare  → uv version <next> && uv lock
         ├─ prepare  → @semantic-release/git commits the bump
         ├─ publish  → uv build
         │            uv publish --trusted-publishing always
         │              └─ exchanges the job's OIDC token for a
         │                 short-lived PyPI credential
         └─ publish  → @semantic-release/github creates the release
```

Three properties of this shape matter when it breaks:

- **There is no stored credential.** No `PYPI_TOKEN` in Doppler, nothing
  to rotate, no expiry. This is the single largest difference from the npm
  path, where every credential has a deadline and the rotation is a
  scheduled event rather than an incident.
- **Publishing runs inside semantic-release's `publish` step**, so it runs
  only when there is an actual release. A push with no releasable commits
  never reaches `uv publish`, which is what keeps PyPI from rejecting a
  duplicate version.
- **`--trusted-publishing always`, not `automatic`.** `automatic` falls
  back to unauthenticated publishing when the OIDC exchange fails, which
  surfaces as a confusing `403` from PyPI. `always` fails at the exchange,
  where the actual fault is.

---

## §2 — Reading the failure

| Symptom | Cause | Go to |
|---|---|---|
| `Failed to obtain token for trusted publishing` | `id-token: write` missing from the job's `permissions`, or the pending publisher was never created | §3 |
| `403 Forbidden` naming the project, on the first ever release | Pending publisher not configured, or configured against the wrong workflow filename | §3 |
| `403 Forbidden` on a project that has published before | The publisher's repo/workflow/environment no longer matches — usually a renamed workflow file | §3 |
| `File already exists` | semantic-release computed a version PyPI already has. Almost always a re-run of a job that already published. Not fixable by retrying: PyPI has no overwrite. | §4 |
| Build succeeds, publish never runs | No releasable commits. Not a fault. | — |

---

## §3 — First-time setup, and the shape of the trusted publisher

PyPI trusted publishing binds four values. All four must match exactly, and
a mismatch in any one produces the same `403`:

| Field | Value |
|---|---|
| PyPI Project Name | `miniapppolis-common-utils` / `miniapppolis-identity` |
| Owner | `mini-app-polis` |
| Repository name | `common-python-utils` / `identity` |
| Workflow filename | `ci.yml` |
| Environment name | *(leave empty)* |

For a project that does not exist on PyPI yet, this is created as a
**pending publisher** — PyPI → *Your projects* → *Publishing* → *Add a
pending publisher*. The project is created by the first successful upload.
There is no token to generate at any point.

The workflow filename is the fragile field. It is matched literally, so
renaming `ci.yml` breaks publishing and the error names PyPI rather than
the rename.

---

## §4 — Version collisions

PyPI does not allow re-uploading a version, and deleting one does not free
the name. If a release job publishes and then fails afterwards, re-running
it will fail at `uv publish` with `File already exists`.

The recovery is to let the next release take the next version, not to
force the current one through. If the GitHub release or the changelog
commit is what failed, fix those by hand; the artifact on PyPI is already
correct.

`uv publish --check-url https://pypi.org/simple/<project>/` makes the
duplicate a skip instead of an error. It is deliberately **not** used here:
a silent skip would let a broken release look successful, which is the
failure mode that cost this fleet two days in September 2026 on a
different code path.

---

## §5 — Why not an API token

PyPI still issues API tokens, and the migration could have used one. It
was rejected:

- A token needs a home. That home is Doppler, which is one more secret to
  rotate and one more place a failure can originate — the npm playbook's
  §3 exists entirely because of that shape.
- PyPI's trusted publishing is more mature than npm's. It has been GA
  since 2023, `uv publish` supports it natively, and there is no
  equivalent of npm's Bypass-2FA trap (npm playbook §5), where `whoami`
  passes and only the final `PUT` is refused.

The npm playbook's §6 lists trusted publishing as *the exit* for the
TypeScript path. The Python path starts there.

---

## §6 — Distribution names are not import names

| Repo | Distribution (PyPI) | Import |
|---|---|---|
| `common-python-utils` | `miniapppolis-common-utils` | `mini_app_polis` |
| `identity` | `miniapppolis-identity` | `identity` |

Three names per library, and they do not agree. That is deliberate but it
is a real cost, so the reasoning is here rather than in a commit message.

**Why the distributions are namespaced.** `identity` was unavailable — an
unrelated Microsoft auth library holds it. That forced a prefix on one
library, and two shared libraries named on different principles is worse
than one awkward name. `common-python-utils` was available, which is
precisely the problem: it is a name anyone could reasonably want, and
claiming it in a public namespace is squatting on a generic term. The npm
playbook closes by noting that `common-typescript-utils` being public and
unscoped "was never a decision, only a default." This is that decision,
made the other way.

**Why the imports did not change.** Renaming `mini_app_polis` would touch
every call site in six repos and buy nothing. The distribution name is what
dependency resolution reads; the import name is what humans read.

**The one live risk.** `identity` as an import name is now shared with a
PyPI package the fleet does not own. Nothing installs both today. If
anything ever does, the top-level module collides, and the fix is to rename
the import — not the distribution.

---

## §7 — What the rename touches

The distribution rename is not confined to the two library repos. Anything
that matches the string `common-python-utils` against a dependency
declaration goes stale the moment a consumer switches, and the failure is
silent in both directions — a check looking for a name nobody declares any
more reports a clean pass over an empty set.

Known sites, all in the conformance path:

| Where | What matches | Effect after the switch |
|---|---|---|
| `evaluator-cog` `python.py` — PY-006 | literal in `pyproject.toml` text | fires on every consumer |
| `evaluator-cog` `config.py` — XSTACK-001 | literal in `pyproject.toml` text | fires on every consumer |
| `evaluator-cog` `packaging.py` — CD-020 | canonicalised requirement name | stops recognising the dep |
| `evaluator-cog` `auth.py` | literal in `auth.py` source text | fires on consumers |
| `evaluator-cog` `crossrepo.py` — XSTACK-007 | registry `id`/`repo` vs declared name | tracks nothing, silently |

XSTACK-007 is the one worth dwelling on. It builds its tracked set from
`ecosystem.yaml` rather than a hardcoded list — deliberately, so a new
library is covered the day it is registered. But it keys on the entry's
`repo`/`id`, and that only worked while the repo name *was* the package
name. Split them and the rule looks for `common-python-utils` in
dependency tables that now say `miniapppolis-common-utils`, matches
nothing, and passes. It is the exact "silently absent rather than visibly
unverified" failure the catalog already has an open note about.

**The fix is one field, not five patches.** Add `package:` to the
`shared-library` entries in `ecosystem.yaml` — the distribution name,
defaulting to `repo`/`id` when absent — and have the checks above resolve
through the registry instead of matching a literal. That is the same
argument XSTACK-007's own description already makes, applied one level
deeper.

---

## §8 — Consumer migration

Consumers cannot switch before the first release lands on PyPI, so this is
strictly two-phase.

Per consumer, the whole change is:

```toml
# before
dependencies = [
    "common-python-utils",
]

[tool.uv.sources]
common-python-utils = { git = "https://github.com/mini-app-polis/common-python-utils.git", rev = "v4.0.0" }

# after
dependencies = [
    "miniapppolis-common-utils>=4.1,<5",
]
```

The `[tool.uv.sources]` table goes away entirely, and with it CD-020 —
the rule that exists to check that the dependency string, the sources
table and the lockfile all agree. Two of those three no longer exist, so
the inconsistency it detects becomes unrepresentable rather than checked.

No import changes. No source changes.

Ranges rather than exact pins is the point of the exercise. `>=4.1,<5`
plus the lockfile means the lockfile is still the exact pin, but
`uv lock --upgrade-package miniapppolis-common-utils` moves it — where a
git `rev` required a hand edit in two places in six repos, which is how
all six ended up on v4.0.0 while the library shipped 4.1.0, and how
`identity` reached v1.3.1 with its only consumer on v1.1.2.

Order:

1. Land the publish-side change in both libraries. Merge to `main`.
2. Confirm both projects exist on PyPI at the expected version.
3. Fix the conformance path (§7) — before consumers switch, not after.
4. Switch consumers one at a time, re-locking each.
5. Delete `allow-direct-references = true` from
   `api-kaianolevine-com`'s `[tool.hatch.metadata]` — it exists only to
   permit the git URLs.

---

## §9 — Still open

- The `package:` field in `ecosystem.yaml` (§7) is described here and not
  yet implemented.
- Both distributions are public. Nothing in either library is sensitive,
  but — as with npm — that is a default and not yet a decision. The
  alternative is a private index, which reintroduces a consume-side
  credential in every repo and every Railway build; that is the trade, and
  it has not been taken.
- The shared security workflow in `mini-app-polis/.github` excludes Python
  from the registry-credential path on the grounds that Python libraries
  are never published. That is no longer true.
