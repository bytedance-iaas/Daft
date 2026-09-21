"""Drift guard: contracts change only together with CONTRACTS.lock (reviewed)."""
from curation.contracts.__main__ import check


def test_contracts_match_the_lock():
    problems = check()
    assert not problems, "\n".join(problems + [
        "If the change is intended: bump the contract's version, run "
        "`python -m curation.contracts lock` and include the lock diff in review."])
