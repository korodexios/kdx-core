"""The CI workflow is a contract: its shape is asserted, not remembered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def raw() -> str:
    """Return the workflow source, because some failures are textual."""
    return WORKFLOW.read_text(encoding="utf-8")


def parsed() -> dict[str, Any]:
    """Return the workflow as data."""
    data = yaml.safe_load(raw())
    assert isinstance(data, dict), "ci.yml is not a mapping"
    return data


def triggers(data: dict[str, Any]) -> dict[str, Any]:
    """Return the trigger block; YAML reads the bare `on:` key as boolean True."""
    block = data.get("on", data.get(True))
    assert isinstance(block, dict), 'ci.yml has no triggers'
    return block


def jobs(data: dict[str, Any]) -> dict[str, Any]:
    """Return the job mapping."""
    assert isinstance(data['jobs'], dict)
    return data['jobs']


def test_workflow_is_valid_yaml_and_named() -> None:
    assert parsed()['name'] == 'CI'


def test_no_escaped_expression_survives_in_the_source() -> None:
    """A backslashed expression parses as YAML and renders as a literal."""
    assert "\\${{" not in raw()


def test_triggers_cover_branch_pull_and_tag_release() -> None:
    block = triggers(parsed())
    push = block['push']
    assert push['branches'] == ['main']
    assert push['tags'] == ['v*']
    assert 'pull_request' in block
    assert 'workflow_dispatch' in block


def test_concurrency_cancels_stale_runs() -> None:
    concurrency = parsed()['concurrency']
    assert concurrency['cancel-in-progress'] is True


def test_matrix_is_planned_by_the_prepare_job() -> None:
    data = parsed()
    check = jobs(data)['check']
    assert check['needs'] == ['prepare']
    matrix = check['strategy']['matrix']
    assert isinstance(matrix, str) and 'fromJson(needs.prepare.outputs.matrix)' in matrix
    assert check['strategy']['fail-fast'] is False
    prepare = jobs(data)['prepare']
    assert 'matrix' in prepare['outputs']
    body = str(prepare)
    assert 'full_matrix=' in body and 'trimmed_matrix=' in body
    assert 'refs/tags/v*' in body  # a tag runs the full matrix, a branch push the trimmed one


def test_portability_legs_report_without_blocking_the_release() -> None:
    """Linux is the release bar; a macOS or Windows leg is a warning, not a hold.

    A tag used to publish nothing at all the moment one portability host went red,
    so the workflow must say which failures an operator may ship under - and which
    never may.
    """
    check = jobs(parsed())["check"]
    policy = str(check.get("continue-on-error"))
    assert "matrix.os != 'ubuntu-latest'" in policy, "only Linux failures block a release"


def test_no_job_condition_reads_the_matrix() -> None:
    """A job-level `if` cannot see `matrix`; only a step-level one can."""
    for name, job in jobs(parsed()).items():
        condition = str(job.get('if', ''))
        assert 'matrix' not in condition, f'{name} reads matrix in a job-level if'


def test_release_waits_for_checks_and_only_runs_on_tags() -> None:
    release = jobs(parsed())['release']
    assert set(release['needs']) == {'check', 'package'}
    assert "startsWith(github.ref, 'refs/tags/v')" in str(release['if'])
    body = str(release['steps'])
    assert 'uv build' in body
    assert 'gh release create' in body and 'gh release upload' in body
    assert '--verify-tag' in body


def test_package_audit_runs_before_release() -> None:
    data = parsed()
    assert 'package' in jobs(data)
    assert 'package' in jobs(data)['release']['needs']
    body = str(jobs(data)['package'])
    assert 'uv build' in body and 'tar' in body


def test_jobs_that_need_the_tree_check_it_out() -> None:
    """Only the matrix planner runs without a checkout; the rest read the tree."""
    data = parsed()
    for name in ("check", "package", "release"):
        body = str(jobs(data)[name].get('steps', ''))
        assert 'actions/checkout@' in body, name
    prepare = str(jobs(data)['prepare'].get('steps', ''))
    assert 'actions/checkout@' not in prepare

