from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from security import scope_policy
from tools import delegate_tool


def test_dbm_allowed_paths_accept_absolute_normalized_vault_path(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    target = vault / "02-Areas" / "dbm" / "cockpit" / "hunter.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\nscope: dbm\n---\n", encoding="utf-8")
    monkeypatch.setattr(scope_policy, "VAULT_ROOT", vault.resolve())
    monkeypatch.setattr(
        scope_policy,
        "load_policy",
        lambda path=scope_policy.DEFAULT_SCOPES_PATH: {
            "scopes": {
                "dbm": {
                    "tools": ["read_file"],
                    "allowed_paths": ["02-Areas/dbm/**"],
                    "excluded_paths": [],
                }
            }
        },
    )

    ok, info = scope_policy.check_tool_call("dbm", "read_file", {"path": str(target)})

    assert ok is True
    assert info == {"status": "allowed"}


def test_dbm_delegate_child_receives_dbm_api_toolset_even_if_parent_enabled_toolsets_are_generic(monkeypatch):
    captured = {}

    class DummyAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs.get("session_id", "child")
            self.valid_tool_names = {"dbm_prospects_search"}
            self.enabled_toolsets = kwargs.get("enabled_toolsets")
            self.model = kwargs.get("model")
            self.provider = kwargs.get("provider")
            self.base_url = kwargs.get("base_url")
            self.api_mode = kwargs.get("api_mode")
            self.api_key = kwargs.get("api_key")
            self.platform = kwargs.get("platform")

    parent = SimpleNamespace(
        scope="dbm",
        enabled_toolsets=["file", "delegation"],
        valid_tool_names={"read_file", "write_file", "delegate_task"},
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_key="k",
        platform="api_server",
        session_id="parent-session",
        quiet_mode=True,
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
    )

    with patch("run_agent.AIAgent", DummyAgent):
        child = delegate_tool._build_child_agent(
            parent_agent=parent,
            goal="search prospects T3 limit 1",
            context="",
            toolsets=None,
            model=None,
            max_iterations=3,
            task_index=0,
            task_count=1,
            role="leaf",
        )

    assert child is not None
    assert captured["scope"] == "dbm"
    assert "dbm-api" in captured["enabled_toolsets"]
    assert "file" in captured["enabled_toolsets"]
