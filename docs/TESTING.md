# Feature regression tests

Run the automated feature regression suite from the repository root before
merging any change:

```powershell
.\.venv-local\Scripts\python.exe -m unittest discover -s tests -v
```

For a clean environment, install only the lightweight test/runtime dependencies
first:

```powershell
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

The suite in `tests/test_feature_regression.py` uses a fresh temporary SQLite
database per test. It checks candidate sign-in and readiness boundaries, normal
exam availability, independent readiness results when availability is closed
or unavailable, one-face diagnostics, exam start/resume/autosave, answer-key privacy, result
publication visibility and candidate-scoped PDF downloads before and after a
fresh candidate sign-in, admin authentication and CSRF, question management,
report/evidence review, monitoring access, and Cloud Exam roster/session
boundaries. Supabase behavior is mocked or disabled; no live service, browser
camera, real candidate profile, saved exam database, or secret is used by these
tests.

GitHub Actions runs this suite on every push and pull request. The workflow can
also be started manually from the repository's Actions page.

These are regression checks, not a substitute for testing hardware- or
deployment-dependent behavior. For changes to face recognition, browser
camera permissions, Supabase migrations/storage/RLS, or hosting configuration,
also follow the relevant setup guide and run a separate test with authorized
synthetic data in the intended environment. Never point automated tests at the
live candidate database or paste credentials into test output.
