from keshro_cli import execution_policies as ep


def test_normalize_execution_policies_accept_known_values():
    assert ep.normalize_worktree_policy("always") == "always"
    assert ep.normalize_worktree_policy("CODE_CHANGES_ONLY") == "code_changes_only"
    assert ep.normalize_pr_policy("manual") == "manual"
    assert ep.normalize_pr_policy("DISABLED") == "disabled"


def test_should_use_isolated_worktree_honors_policy_and_task_shape():
    code_task = {
        "title": "Update workflow",
        "related_files": ["infra/main.tf", "README.md"],
    }
    docs_task = {
        "title": "Write cutover runbook",
        "related_files": ["docs/cutover.md"],
    }

    assert ep.should_use_isolated_worktree(code_task, policy="always") is True
    assert (
        ep.should_use_isolated_worktree(code_task, policy="code_changes_only") is True
    )
    assert (
        ep.should_use_isolated_worktree(docs_task, policy="code_changes_only") is False
    )
