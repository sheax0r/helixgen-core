# Plan: #79(i) — auto-commit honors the user's configured git identity

## Context

Backlog **#79(i)**. The engine's home-repo auto-commit (`src/helixgen/gitops.py`)
injects a hardcoded identity `helixgen <helixgen@localhost>` as
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` env on EVERY commit (constant at
`gitops.py:28-33`, applied at `gitops.py:85` in `ensure_home_repo()` and
`gitops.py:223` in `auto_commit()`). Because those env vars OVERRIDE
`git config user.*`, the user's configured identity is never used — even though
the code comment (lines 26-27) claims it's only a fallback "so git commit works
on a machine with no git config."

**User decision (2026-07-18):** prefer the user's configured git identity; fall
back to `helixgen <helixgen@localhost>` ONLY when no git identity is configured.
Make the implementation match the comment. Repo rules: TDD, stdlib + click only.

### Task 1: make the identity injection a real fallback

- [x] Refactor the two duplicated `commit_env = {**os.environ, **_GIT_IDENTITY_ENV}`
      sites (`gitops.py:85`, `:223`) into ONE helper, e.g. `_commit_env(home)`.
- [x] In the helper, detect whether git has a usable identity for the home repo
      — check `git -C <home> config user.name` AND `git -C <home> config user.email`
      are both set (this respects global/system config too). Inject
      `_GIT_IDENTITY_ENV` ONLY when an identity is NOT configured; otherwise pass
      env through unchanged so git uses the user's identity for author AND
      committer.
- [x] Update the misleading comment at `gitops.py:26-27` to describe the real
      fallback behavior.
- [x] Write failing tests first (in the matching `tests/test_gitops.py` or
      wherever gitops is tested), then implement:
  - [x] with `user.name`/`user.email` configured in the home repo, an
        auto-commit's author AND committer equal the user's identity (assert via
        `git log --format='%an <%ae>' / %cn <%ce>`).
  - [x] with NO git identity configured (unset user.name/user.email in an
        isolated env), the commit author/committer fall back to
        `helixgen <helixgen@localhost>` and the commit still succeeds.
  - [x] both `ensure_home_repo()`'s initial commit and `auto_commit()` use the
        same resolution (cover both sites).

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (`-n auto`). Live
  tests auto-skip without `HELIXGEN_LIVE=1`.
