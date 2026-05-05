"""Scope/ACL policy for Hermes API Server per-caller tool isolation."""
from __future__ import annotations

import fnmatch
import hmac
import json
import os
import sqlite3
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

CURRENT_SCOPE: ContextVar[str] = ContextVar("hermes_current_scope", default="full")
CURRENT_SESSION_ID: ContextVar[str] = ContextVar("hermes_current_session_id", default="")

SCOPE_ORDER = {"dbm": 1, "full": 2}
DEFAULT_SCOPES_PATH = Path(os.getenv("HERMES_SCOPES_PATH", "/opt/data/scopes.yml"))
DEFAULT_AUDIT_LOG = Path(os.getenv("HERMES_SCOPE_AUDIT_LOG", "/opt/data/scope-violations.log"))
VAULT_ROOT = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/vault")).resolve()


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_policy(path: Path | str = DEFAULT_SCOPES_PATH) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"scopes": {"full": {"tools": ["*"], "allowed_paths": ["**"]}}}
    if yaml is None:
        # Minimal safe fallback for runtimes without PyYAML: keep full uncapped
        # and enforce the initial DBM scope from /opt/data/scopes.yml.
        return {
            "scopes": {
                "full": {"tools": ["*"], "allowed_paths": ["**"]},
                "dbm": {
                    "tools": [
                        "read_file", "write_file", "search_files", "search_mcp", "delegate_task",
                        "dbm_prospects_search", "dbm_prospects_update", "dbm_prospects_insert",
                        "dbm_outreach_draft", "dbm_outreach_send", "dbm_outreach_mark_sent",
                        "dbm_outreach_inbox_list", "http_fetch",
                    ],
                    "denied_tools": [
                        "terminal", "process", "execute_code", "cronjob", "patch",
                        "browser_*", "memory", "session_search", "skill_*",
                        "vision_analyze", "image_generate", "todo", "docker",
                    ],
                    "allowed_paths": [
                        "01-Inbox/dbm-*", "02-Areas/dbm/**", "02-Areas/cockpit/**",
                        "03-Resources/dbm/**", "04-Archive/dbm/**",
                    ],
                    "excluded_paths": ["**/_private/**", "**/.confidential/**"],
                    "require_frontmatter": {"scope": ["dbm", "public"]},
                },
            }
        }
    data = yaml.safe_load(p.read_text()) or {}
    data.setdefault("scopes", {})
    data["scopes"].setdefault("full", {"tools": ["*"], "allowed_paths": ["**"]})
    return data


def min_scope(*scopes: Optional[str]) -> str:
    clean = [s for s in scopes if s in SCOPE_ORDER]
    if not clean:
        return "full"
    return min(clean, key=lambda s: SCOPE_ORDER[s])


def scope_from_token(token: str, full_key: str = "") -> str:
    token = token or ""
    dbm_key = os.getenv("API_SERVER_DBM_KEY", "")
    full_candidates = [full_key or "", os.getenv("API_SERVER_KEY", "")]
    if dbm_key and hmac.compare_digest(token, dbm_key):
        return "dbm"
    for key in full_candidates:
        if key and hmac.compare_digest(token, key):
            return "full"
    return ""


def extract_bearer(headers: Any) -> str:
    auth = headers.get("Authorization", "") if headers else ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def effective_scope_for_request(headers: Any, full_key: str = "") -> str:
    token_scope = scope_from_token(extract_bearer(headers), full_key=full_key)
    header_scope = (headers.get("X-Hermes-Scope", "") if headers else "").strip().lower()
    if not token_scope:
        # Header-only scoping is intentionally not trusted when API auth is enabled.
        # In unauthenticated local/dev mode, keep Hermes full rather than accepting
        # caller-declared self-demotion/elevation as an authority source.
        return "full"
    if header_scope not in SCOPE_ORDER:
        return token_scope
    final = min_scope(token_scope, header_scope)
    if SCOPE_ORDER.get(header_scope, 0) > SCOPE_ORDER.get(token_scope, 0):
        log_scope_violation(
            "",
            token_scope,
            "scope_header",
            {"requested_scope": header_scope, "effective_scope": final},
            "DENIED",
            "scope_elevation_denied",
        )
    return final


def init_session_scope_table(db_path: Path | str) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS session_scopes ("
            "session_id TEXT PRIMARY KEY, "
            "scope TEXT NOT NULL, "
            "source TEXT, "
            "created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        con.commit()
    finally:
        con.close()


def attach_session_scope(db_path: Path | str, session_id: str, requested_scope: str, source: str = "api_server") -> str:
    if not session_id:
        return requested_scope or "full"
    requested_scope = requested_scope if requested_scope in SCOPE_ORDER else "full"
    init_session_scope_table(db_path)
    now = time.time()
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT scope FROM session_scopes WHERE session_id=?", (session_id,)).fetchone()
        if row:
            final = min_scope(row[0], requested_scope)
            if final != row[0]:
                con.execute("UPDATE session_scopes SET scope=?, updated_at=? WHERE session_id=?", (final, now, session_id))
        else:
            final = requested_scope
            con.execute(
                "INSERT INTO session_scopes(session_id, scope, source, created_at, updated_at) VALUES(?,?,?,?,?)",
                (session_id, final, source, now, now),
            )
        con.commit()
        return final
    finally:
        con.close()


def get_session_scope(db_path: Path | str, session_id: str, default: str = "full") -> str:
    if not session_id:
        return default
    try:
        init_session_scope_table(db_path)
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute("SELECT scope FROM session_scopes WHERE session_id=?", (session_id,)).fetchone()
            return row[0] if row else default
        finally:
            con.close()
    except Exception:
        return default


def set_current_scope(scope: str, session_id: str = ""):
    tok1 = CURRENT_SCOPE.set(scope if scope in SCOPE_ORDER else "full")
    tok2 = CURRENT_SESSION_ID.set(session_id or "")
    return tok1, tok2


def reset_current_scope(tokens) -> None:
    try:
        CURRENT_SCOPE.reset(tokens[0])
        CURRENT_SESSION_ID.reset(tokens[1])
    except Exception:
        pass


def current_scope() -> str:
    return CURRENT_SCOPE.get("full")


def current_session_id() -> str:
    return CURRENT_SESSION_ID.get("")


def _scope_cfg(scope: str) -> Dict[str, Any]:
    return (load_policy().get("scopes", {}) or {}).get(scope, {}) or {}


def tool_is_allowed(scope: str, tool: str) -> tuple[bool, str]:
    if scope == "full":
        return True, "allowed"
    cfg = _scope_cfg(scope)
    denied = set(cfg.get("denied_tools") or [])
    allowed = set(cfg.get("tools") or [])
    if _tool_matches(tool, list(denied)):
        return False, "tool_denied"
    if "*" in allowed or _tool_matches(tool, list(allowed)):
        return True, "allowed"
    return False, "tool_not_allowed"


def filter_tool_schemas_for_scope(tools: list, scope: str) -> list:
    if scope == "full" or not tools:
        return tools
    out = []
    for t in tools:
        name = ((t or {}).get("function") or {}).get("name")
        ok, _ = tool_is_allowed(scope, name or "")
        if ok:
            out.append(t)
    return out


def _to_vault_rel(raw: str) -> tuple[Optional[Path], str]:
    if not raw:
        return None, "missing_path"
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = VAULT_ROOT / p
    try:
        resolved = p.resolve(strict=False)
    except Exception:
        return None, "path_resolve_failed"
    try:
        rel = resolved.relative_to(VAULT_ROOT)
    except ValueError:
        return None, "outside_vault"
    return rel, "ok"


def _matches_any(value: str, patterns: list[str]) -> bool:
    value = value.strip("/")
    for pat in patterns or []:
        pat = str(pat).strip("/")
        if fnmatch.fnmatch(value, pat):
            return True
        if pat.endswith("/**"):
            base = pat[:-3].rstrip("/")
            if value == base or value.startswith(base + "/"):
                return True
    return False


def _tool_matches(tool: str, patterns: list[str]) -> bool:
    return _matches_any(tool or "", patterns or [])


def _frontmatter_scope_ok(abs_path: Path, allowed: list[str]) -> bool:
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    fm = text[3:end]
    for line in fm.splitlines():
        if line.strip().startswith("scope:"):
            val = line.split(":", 1)[1].strip().strip('"\'')
            return val in set(allowed or [])
    return False


def _extract_tool_paths(tool: str, args: Dict[str, Any]) -> list[str]:
    args = args or {}
    if tool in {"read_file", "write_file", "patch"}:
        return [args.get("path", "")]
    if tool == "search_files":
        return [args.get("path") or "."]
    return []


def check_tool_call(scope: str, tool: str, args: Dict[str, Any], session_id: str = "") -> tuple[bool, Dict[str, Any]]:
    ok, reason = tool_is_allowed(scope, tool)
    if not ok:
        info = {"error": "out_of_scope", "tool": tool, "scope": scope, "reason": reason}
        log_scope_violation(session_id, scope, tool, args, "DENIED", reason)
        return False, info
    if scope == "full":
        return True, {"status": "allowed"}

    cfg = _scope_cfg(scope)
    allowed_paths = cfg.get("allowed_paths") or []
    excluded_paths = cfg.get("excluded_paths") or []
    for raw in _extract_tool_paths(tool, args):
        rel, status = _to_vault_rel(str(raw or ""))
        if status != "ok" or rel is None:
            info = {"error": "out_of_scope", "tool": tool, "path": raw, "scope": scope, "reason": status}
            log_scope_violation(session_id, scope, tool, args, "DENIED", status)
            return False, info
        rel_s = rel.as_posix()
        if _matches_any(rel_s, excluded_paths):
            info = {"error": "out_of_scope", "tool": tool, "path": rel_s, "scope": scope, "reason": "path_excluded"}
            log_scope_violation(session_id, scope, tool, {**(args or {}), "path": rel_s}, "DENIED", "path_excluded")
            return False, info
        if not _matches_any(rel_s, allowed_paths):
            info = {"error": "out_of_scope", "tool": tool, "path": rel_s, "scope": scope, "reason": "path_not_allowed"}
            log_scope_violation(session_id, scope, tool, {**(args or {}), "path": rel_s}, "DENIED", "path_not_allowed")
            return False, info
    return True, {"status": "allowed"}


_SENSITIVE_LOG_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "key",
    "password",
    "secret",
    "bearer",
    "x-api-key",
    "api-key",
    "api_key",
}
_TRUNCATE_LOG_KEYS = {"content", "old_string", "new_string", "patch", "command", "code", "body"}
_MAX_LOG_VALUE_CHARS = 200


def _is_sensitive_log_key(key: Any) -> bool:
    lk = str(key).lower()
    return any(s in lk for s in _SENSITIVE_LOG_KEYS)


def _truncate_log_value(value: Any) -> str:
    text = str(value)
    if len(text) > _MAX_LOG_VALUE_CHARS:
        return text[:_MAX_LOG_VALUE_CHARS] + "[truncated]"
    return text


def _safe_log_value(value: Any, key: Any = "") -> Any:
    if _is_sensitive_log_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _safe_log_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_log_value(v, key) for v in value]
    if isinstance(value, tuple):
        return [_safe_log_value(v, key) for v in value]
    if str(key).lower() in _TRUNCATE_LOG_KEYS:
        return _truncate_log_value(value)
    if isinstance(value, str) and len(value) > _MAX_LOG_VALUE_CHARS:
        return value[:_MAX_LOG_VALUE_CHARS] + "[truncated]"
    return value


def _safe_args_for_log(args: Dict[str, Any]) -> Dict[str, Any]:
    return _safe_log_value(args or {})


def log_scope_violation(session_id: str, scope: str, tool: str, args: Dict[str, Any], action: str, reason: str) -> None:
    rec = {
        "ts": _now(),
        "session": session_id or current_session_id(),
        "caller_scope": scope or current_scope(),
        "tool": tool,
        "path": (args or {}).get("path"),
        "action": action,
        "reason": reason,
        "args": _safe_args_for_log(args or {}),
    }
    try:
        DEFAULT_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEFAULT_AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            DEFAULT_AUDIT_LOG.chmod(0o600)
        except Exception:
            pass
    except Exception:
        pass


def read_scope_audit(since: str = "", limit: int = 100) -> list[Dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 1000))
    p = DEFAULT_AUDIT_LOG
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    items = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if since and str(rec.get("ts", "")) < since:
            continue
        items.append(rec)
        if len(items) >= limit:
            break
    return list(reversed(items))
