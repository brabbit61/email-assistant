"""Real-API classifier fixtures. Opt-in, paid, majority-vote.

Runs the *real* classifier against the *real* rubric.md so a prompt/model/parse
regression fails loudly. Each fixture is called N times; the majority answer must
match the expected label — retry-until-pass would hide a broken rubric, so we
vote instead. Category is always asserted; priority only where a fixture pins it.

Run:  EMAIL_ANTHROPIC_API_KEY=<key> uv run pytest -m fixtures
(Skips, not fails, when the key is unset — see conftest-free `client` below.)

To prove the suite bites: invert a tie-break in src/assistant/rubric.md (e.g.
send recruiter mail to Work), run the command above, watch the relevant fixture
go red, then revert.
"""

import collections
import os
from pathlib import Path

import pytest
import yaml

from assistant import classify

pytestmark = pytest.mark.fixtures

# ponytail: mirror config.toml [models].classifier; bump the two together.
MODEL = "claude-haiku-4-5-20251001"
N = 3  # majority vote — odd so there's always a winner

_FIXTURES = yaml.safe_load(
    (Path(__file__).parent / "fixtures" / "emails.yaml").read_text()
)["fixtures"]


@pytest.fixture(scope="session")
def client():
    key = os.environ.get("EMAIL_ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("EMAIL_ANTHROPIC_API_KEY not set — real-API fixtures skipped")
    return classify.make_client(key)


def _mode(values: list) -> object:
    return collections.Counter(values).most_common(1)[0][0]


@pytest.mark.parametrize("fx", _FIXTURES, ids=lambda f: f["name"])
def test_fixture(client, fx):
    email = classify.Email(fx["sender"], fx["subject"], fx["body"])
    verdicts = [classify.classify(client, MODEL, email)[0] for _ in range(N)]

    cats = [v.category for v in verdicts]
    assert _mode(cats) == fx["expected_category"], f"{fx['name']}: categories={cats}"

    if "expected_priority" in fx:
        pris = [v.priority for v in verdicts]
        assert _mode(pris) == fx["expected_priority"], (
            f"{fx['name']}: priorities={pris}"
        )
