# Contributing

Thanks for considering a contribution!

## Getting started

1. Fork the repo.
2. Branch off `dev` — `main` is the branch that publishes, and it is protected.
3. Set up a checkout:
   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -e ".[dev]"
   ```
4. Run `pytest` and `ruff check . && ruff format --check .` before pushing.
5. Commit using [conventional commits](https://www.conventionalcommits.org/):
   - `fix:` — bug fix
   - `feat:` — new feature
   - `docs:` — documentation
   - `chore:` — tooling, CI, dependencies
   - `ci:` — workflow changes
6. Push and open a pull request against `dev`.

## Code style

- Python 3.13+, fully typed. New code carries annotations; the package ships
  `py.typed` and that promise has to hold.
- Async throughout. Nothing blocking in the request or download paths.
- Avoid adding dependencies. The browser tier is deliberately an optional
  extra so the common install stays small.
- Keep the tier ladder's rules intact: `AuthenticationError` escalates a tier,
  `RateLimitError` does not — escalating while throttled only burns the next
  credential too.
- Nothing is fully materialised in memory. One page at a time, whatever the
  account size.

## Tests

`pytest`, with `respx` mocking HTTP. Tests must not hit the network or launch
a browser. Add cases next to the behaviour they cover in `tests/`.

## Releasing

Maintainers only. Bump `version` in `pyproject.toml` and merge `dev` into
`main`; the release workflow checks PyPI, publishes if the version is new, then
tags the commit and opens the GitHub release. Never reuse a version number — a
burned version on PyPI cannot be re-uploaded.

## Pull request checklist

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] No new required dependencies (or a strong reason for them)
- [ ] Commit messages follow conventional commits
