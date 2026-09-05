# [5.0.0](https://github.com/mini-app-polis/common-python-utils/compare/v4.2.1...v5.0.0) (2026-09-05)


* feat!: publish to PyPI as miniapppolis-common-utils ([bd1b5d0](https://github.com/mini-app-polis/common-python-utils/commit/bd1b5d026ee3af19fe751663d7d95073cd6034d8))


### Bug Fixes

* **git:** never three-way merge a lockfile ([d763070](https://github.com/mini-app-polis/common-python-utils/commit/d763070263baabb2a5c73ac54d8731ab4c33146d))


### BREAKING CHANGES

* the distribution is named miniapppolis-common-utils.
Anything declaring common-python-utils, including by git ref, must be
updated. See docs/pypi-package-publishing.md.

## [4.2.1](https://github.com/mini-app-polis/common-python-utils/compare/v4.2.0...v4.2.1) (2026-09-04)


### Bug Fixes

* **deps:** bump mypy from 1.20.2 to 2.3.1 ([1de6340](https://github.com/mini-app-polis/common-python-utils/commit/1de634093b6dedc3404eabb166cc01f9e65aef5d))

# [4.2.0](https://github.com/mini-app-polis/common-python-utils/compare/v4.1.0...v4.2.0) (2026-09-04)


### Bug Fixes

* **api:** stop discarding a query string written into the path ([3b8474b](https://github.com/mini-app-polis/common-python-utils/commit/3b8474b3707dcf37cf2da0530a9e7a4887442716))


### Features

* **deps:** automate dependency updates ([3914533](https://github.com/mini-app-polis/common-python-utils/commit/3914533120b6375787960cda39f59ace45a39ef5))

# [4.1.0](https://github.com/mini-app-polis/common-python-utils/compare/v4.0.2...v4.1.0) (2026-09-03)


### Features

* **security:** call the shared security workflow, clear 22 advisories ([f4cc791](https://github.com/mini-app-polis/common-python-utils/commit/f4cc791a5671e916b2969b5bdb78937c78ae1189))

## [4.0.2](https://github.com/mini-app-polis/common-python-utils/compare/v4.0.1...v4.0.2) (2026-09-02)


### Bug Fixes

* **release:** re-lock as part of the release ([4f4e60e](https://github.com/mini-app-polis/common-python-utils/commit/4f4e60e3089e015ca0649c20341ba860b003327e))

## [4.0.1](https://github.com/mini-app-polis/common-python-utils/compare/v4.0.0...v4.0.1) (2026-09-02)


### Bug Fixes

* **deps:** re-lock after the 4.0.0 release ([2694b5f](https://github.com/mini-app-polis/common-python-utils/commit/2694b5fb13f3685c5b07d0f47b30645cc44e49ec))

# [4.0.0](https://github.com/mini-app-polis/common-python-utils/compare/v3.2.0...v4.0.0) (2026-09-02)


### Features

* **api:** remove Clerk M2M token minting ([ac5a009](https://github.com/mini-app-polis/common-python-utils/commit/ac5a009102a1c48351623b38434d4690db8a2007))


### BREAKING CHANGES

* **api:** KaianoApiClient no longer accepts machine_secret and no
longer reads KAIANO_API_CLERK_MACHINE_SECRET. Callers must pass machine_name
(or api_key). A cog without a key now fails rather than falling back to the
shared fleet identity — that fallback is what made every cog
indistinguishable in the first place.

# [3.2.0](https://github.com/mini-app-polis/common-python-utils/compare/v3.1.1...v3.2.0) (2026-09-02)


### Features

* **api:** derive each machine's API key from its name ([b156cda](https://github.com/mini-app-polis/common-python-utils/commit/b156cda02b9120b42142f517a06fe58c10597670))

## [3.1.1](https://github.com/mini-app-polis/common-python-utils/compare/v3.1.0...v3.1.1) (2026-09-02)


### Bug Fixes

* finalizing identity procresses ([dac566d](https://github.com/mini-app-polis/common-python-utils/commit/dac566da76563ca90d3d5c5f89f36b5ea61fc9bb))

# [3.1.0](https://github.com/mini-app-polis/common-python-utils/compare/v3.0.0...v3.1.0) (2026-08-19)


### Bug Fixes

* tests ([b7c5ee6](https://github.com/mini-app-polis/common-python-utils/commit/b7c5ee6a45d9138132f7c369e17ceb03ec88a56c))


### Features

* **pipeline-status:** allow CRITICAL as a self-reported severity ([5ea69f7](https://github.com/mini-app-polis/common-python-utils/commit/5ea69f7520592bbc24f9a9bc77a8612217904c6d))

# [3.0.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.6.1...v3.0.0) (2026-05-27)


### Bug Fixes

* pipeline status ([ce10f25](https://github.com/mini-app-polis/common-python-utils/commit/ce10f25f632231bfa0da6e0711e5a96ba75a27ab))
* pipeline status ([1eb932a](https://github.com/mini-app-polis/common-python-utils/commit/1eb932a6343be57882cee517027bb35bf4d5ae2d))


### Features

* configurable max_tokens with truncation detection in LLM clients ([131fbd7](https://github.com/mini-app-polis/common-python-utils/commit/131fbd7765abfb76225650c986fdf8a9fb2784cc))


### BREAKING CHANGES

* callers that previously caught LLMValidationError for
truncation will need to catch LLMTruncationError instead. The base
LLMError still catches both. The default max_tokens has doubled from
8192 to 16384, which may affect API costs for callers that were
relying on the implicit cap.

Co-authored-by: Cursor <cursoragent@cursor.com>

## [2.6.1](https://github.com/mini-app-polis/common-python-utils/compare/v2.6.0...v2.6.1) (2026-05-17)


### Bug Fixes

* updated for transcript cof eval support ([b04e024](https://github.com/mini-app-polis/common-python-utils/commit/b04e024ce49014937612d100d698490c6ed6fbfc))

# [2.6.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.5.0...v2.6.0) (2026-05-17)


### Features

* common pipeline eval structure moving to common ([d15df57](https://github.com/mini-app-polis/common-python-utils/commit/d15df575557bb1ff6c3b71195a27c2cb59f2cce4))

# [2.5.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.4.2...v2.5.0) (2026-04-18)


### Features

* finalizing on opaque m2m tokens ([93fac4d](https://github.com/mini-app-polis/common-python-utils/commit/93fac4d82f47d402cbda57da8864f55b08db628c))

## [2.4.2](https://github.com/mini-app-polis/common-python-utils/compare/v2.4.1...v2.4.2) (2026-04-17)


### Bug Fixes

* debug warning ([9363d91](https://github.com/mini-app-polis/common-python-utils/commit/9363d9115dbea4986c650cf53178434d3ed2a0cb))

## [2.4.1](https://github.com/mini-app-polis/common-python-utils/compare/v2.4.0...v2.4.1) (2026-04-17)


### Bug Fixes

* debug ([6c9b474](https://github.com/mini-app-polis/common-python-utils/commit/6c9b474602e6fe6f546e270e58fc387753b25a92))

# [2.4.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.3.0...v2.4.0) (2026-04-17)


### Features

* finishing the proper implementation of auth through clerk ([5ec090e](https://github.com/mini-app-polis/common-python-utils/commit/5ec090ec2c63fe6907a4f5ecf144e263d7379498))

# [2.3.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.2.0...v2.3.0) (2026-04-17)


### Features

* updating m2m auth ([bdafc21](https://github.com/mini-app-polis/common-python-utils/commit/bdafc218ae267f73c6685e438d884b0bfc4ddc8e))

# [2.2.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.1.3...v2.2.0) (2026-04-17)


### Features

* upgrading auth to m2m clerk pattern ([9512b0c](https://github.com/mini-app-polis/common-python-utils/commit/9512b0c4c7ad8c28405389ba1795f84f809eb0ea))

## [2.1.3](https://github.com/mini-app-polis/common-python-utils/compare/v2.1.2...v2.1.3) (2026-04-02)


### Bug Fixes

* docstrings, explicit CI coverage, and package identity docs (DOC-006, DOC-009, TEST-006) ([e8e2322](https://github.com/mini-app-polis/common-python-utils/commit/e8e2322bc3b9976dfe82aa6b6fa7d04c00e6152f))
* remove literal URL from docstring to clear evaluator false positive ([b796e6b](https://github.com/mini-app-polis/common-python-utils/commit/b796e6b4cae304c0d4d635e652a94aadcf598e6b))

## [2.1.2](https://github.com/mini-app-polis/common-python-utils/compare/v2.1.1...v2.1.2) (2026-04-02)


### Bug Fixes

* latest ([0557c55](https://github.com/mini-app-polis/common-python-utils/commit/0557c5546c6509322710250dfa8925e9a833957c))

## [2.1.1](https://github.com/mini-app-polis/common-python-utils/compare/v2.1.0...v2.1.1) (2026-03-28)


### Bug Fixes

* fix ([768a508](https://github.com/mini-app-polis/common-python-utils/commit/768a508cad7b423e7b4cd70b2f4571eeb070de32))

# [2.1.0](https://github.com/mini-app-polis/common-python-utils/compare/v2.0.0...v2.1.0) (2026-03-27)


### Features

* add music normalization module ([9ca9bfb](https://github.com/mini-app-polis/common-python-utils/commit/9ca9bfbd15d75f1ca7277b21924b280b46857e47))

# [2.0.0](https://github.com/mini-app-polis/common-python-utils/compare/v1.3.0...v2.0.0) (2026-03-27)


* feat!: rename import namespace from kaiano to mini_app_polis ([642cafa](https://github.com/mini-app-polis/common-python-utils/commit/642cafa2ebe37e96704a350ebec1708abd4dd54b))


### BREAKING CHANGES

* all imports change from `from kaiano.x` to `from mini_app_polis.x`

Made-with: Cursor

# [2.0.0] — Breaking: import namespace renamed kaiano → mini_app_polis

All imports change from `from kaiano.x` to `from mini_app_polis.x`.

# [1.3.0](https://github.com/mini-app-polis/common-python-utils/compare/v1.2.0...v1.3.0) (2026-03-27)


### Bug Fixes

* build ([f0573b3](https://github.com/mini-app-polis/common-python-utils/commit/f0573b31d7265d744b114396db5341e07f8c7f8a))


### Features

* reverting auth ([6e6d013](https://github.com/mini-app-polis/common-python-utils/commit/6e6d013b0e6abb90920f85d77073165891da9338))

# [1.2.0](https://github.com/mini-app-polis/common-python-utils/compare/v1.1.2...v1.2.0) (2026-03-27)


### Features

* adding clerk functionality ([02bb6bf](https://github.com/mini-app-polis/common-python-utils/commit/02bb6bf3aee963570c10282374b8ec784c6431d4))
* adding clerk functionality ([fb5abe6](https://github.com/mini-app-polis/common-python-utils/commit/fb5abe6871b689afa613a63548f230cd76bd5c2b))

## [1.1.2](https://github.com/mini-app-polis/common-python-utils/compare/v1.1.1...v1.1.2) (2026-03-27)


### Bug Fixes

* removing warning ([cfe5206](https://github.com/mini-app-polis/common-python-utils/commit/cfe52063457137b3d0058804c3a125e4eb70db0f))

## [1.1.1](https://github.com/mini-app-polis/common-python-utils/compare/v1.1.0...v1.1.1) (2026-03-27)


### Bug Fixes

* removing warning ([baa2475](https://github.com/mini-app-polis/common-python-utils/commit/baa2475e8d19e3e4eca3c56c0078fabef6844db3))

# [1.1.0](https://github.com/mini-app-polis/common-python-utils/compare/v1.0.0...v1.1.0) (2026-03-27)


### Features

* cleanup and align with ecosystem standards ([c43bdbf](https://github.com/mini-app-polis/common-python-utils/commit/c43bdbfecad43558bedf240673f73dc9060f03fd))

# 1.0.0 (2026-03-27)


### Features

* breaking change: cleaning up for build ([2552043](https://github.com/mini-app-polis/common-python-utils/commit/25520430dc22508ea98b351b1d9c7c73c2e739cf))
