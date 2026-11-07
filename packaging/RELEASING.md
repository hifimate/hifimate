# Releasing HiFiMate

A release is a tag `vX.Y.Z` on `main`. `.github/workflows/release.yml` then runs the CI checks, builds the sdist and
wheel (recording the commit in `hifimate/_commit.txt`), publishes them to PyPI with Trusted Publishing (no token
is stored anywhere), and makes a GitHub release with `install.sh`, the one-line installer pinned to that version.

## Once, before the first release

1. PyPI → *Your account → Publishing → Add a pending publisher*: project `hifimate`, owner `hifimate`,
   repository `hifimate`, workflow `release.yml`, environment `pypi`. Do the same on TestPyPI with environment `testpypi`.
2. GitHub → repository *Settings → Environments*: create `pypi` (optionally with required reviewers) and `testpypi`.

## Each release

1. Set `__version__` in `hifimate/__init__.py` (the only place the version is written) and commit.
2. Rehearse: *Actions → Release → Run workflow* on `main`. It runs the checks and publishes to TestPyPI only; try it with
   `pipx install --index-url https://test.pypi.org/simple/ --pip-args='--extra-index-url https://pypi.org/simple/' hifimate`.
   TestPyPI never accepts the same version twice, so a failed rehearsal needs a new version number.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. The workflow refuses a tag that does not match `__version__`.

The installer is then at `https://github.com/hifimate/hifimate/releases/latest/download/install.sh` (run with `| sudo sh`).

## Locally

```sh
git rev-parse HEAD > hifimate/_commit.txt && python3 -m pip wheel --no-deps -w dist . && rm hifimate/_commit.txt
python3 -B packaging/check_wheel.py dist/hifimate-*.whl
```
