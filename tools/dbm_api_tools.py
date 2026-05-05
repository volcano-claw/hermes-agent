"""DirectBookingManager REST tools for Hermes DBM scope.

Hermes never connects to DBM Postgres directly. These tools call DBM's
Next.js cockpit API with a process-local HERMES_AGENT_API_KEY. The token is
never part of tool schemas, tool args, prompts, or tool results.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, Tuple

from tools.registry import registry

DBM_API_BASE_URL = os.getenv("DBM_API_BASE_URL", "https://www.directbookingmanager.com").rstrip("/")
DBM_USER_AGENT = "HERMES-Agent/dbm-scope"
HTTP_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
READ_LIMIT_PER_MINUTE = int(os.getenv("HERMES_DBM_READ_RATE_LIMIT_PER_MINUTE", "30"))
WRITE_LIMIT_PER_MINUTE = int(os.getenv("HERMES_DBM_WRITE_RATE_LIMIT_PER_MINUTE", "10"))
BURST_LIMIT = int(os.getenv("HERMES_DBM_RATE_LIMIT_BURST", "5"))
CIRCUIT_BREAK_SECONDS = 60
CIRCUIT_ERROR_THRESHOLD = 5

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "x-api-key",
    "api-key",
    "api_key",
}
ALLOWED_HTTP_METHODS = {"GET", "POST"}
WRITE_TOOLS = {
    "dbm_prospects_update",
    "dbm_prospects_insert",
    "dbm_outreach_send",
    "dbm_outreach_mark_sent",
}

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain"}
BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_rate_lock = threading.Lock()
_rate_windows: dict[str, deque[float]] = defaultdict(deque)
_circuit: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "blocked_until": 0.0})


def _dbm_agent_token() -> str:
    """Return the outbound DBM token from runtime env or a mounted file.

    The token must never be supplied by the model. `HERMES_AGENT_API_KEY_FILE`
    lets the gateway container receive the credential without placing the
    secret value in prompts, tool args, or compose diffs.
    """
    token = os.getenv("HERMES_AGENT_API_KEY", "")
    if token:
        return token.strip()
    token_file = os.getenv("HERMES_AGENT_API_KEY_FILE", "")
    if not token_file:
        return ""
    try:
        return open(token_file, "r", encoding="utf-8").read().strip()
    except Exception:
        return ""


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in (headers or {}).items():
        if str(key).lower() in SENSITIVE_HEADER_NAMES:
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


def _redact_error(text: str) -> str:
    token = _dbm_agent_token()
    if token:
        text = text.replace(token, "[REDACTED]")
    return text


def _session_key(tool: str, kind: str) -> str:
    try:
        from security.scope_policy import current_session_id
        sid = current_session_id() or "no-session"
    except Exception:
        sid = "no-session"
    return f"{sid}:{tool}:{kind}"


def _rate_check(tool: str, kind: str) -> tuple[bool, Dict[str, Any]]:
    limit = WRITE_LIMIT_PER_MINUTE if kind == "write" else READ_LIMIT_PER_MINUTE
    key = _session_key(tool, kind)
    now = time.monotonic()
    with _rate_lock:
        state = _circuit[key]
        if state.get("blocked_until", 0.0) > now:
            return False, {"error": "circuit_open", "retry_after_seconds": int(state["blocked_until"] - now) + 1}
        window = _rate_windows[key]
        while window and now - window[0] >= 60:
            window.popleft()
        # Token-bucket-ish conservative burst: allow only BURST_LIMIT in first 10s.
        recent_10s = sum(1 for t in window if now - t < 10)
        if recent_10s >= BURST_LIMIT:
            return False, {"error": "rate_limited", "retry_after_seconds": 10}
        if len(window) >= limit:
            retry = max(1, int(60 - (now - window[0])))
            return False, {"error": "rate_limited", "retry_after_seconds": retry}
        window.append(now)
    return True, {"status": "allowed"}


def _record_http_result(tool: str, kind: str, status: int | None) -> None:
    key = _session_key(tool, kind)
    now = time.monotonic()
    with _rate_lock:
        state = _circuit[key]
        if status == 429 or (status is not None and 500 <= status <= 599):
            state["count"] = int(state.get("count", 0)) + 1
            if state["count"] >= CIRCUIT_ERROR_THRESHOLD:
                state["blocked_until"] = now + CIRCUIT_BREAK_SECONDS
                state["count"] = 0
        elif status is not None and status < 500:
            state["count"] = 0
            state["blocked_until"] = 0.0


def _validate_method(method: str) -> str:
    method = (method or "GET").upper()
    if method not in ALLOWED_HTTP_METHODS:
        raise ValueError("method_not_allowed")
    return method


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return any(addr in net for net in BLOCKED_IP_NETWORKS)


def _resolve_and_validate_host(hostname: str) -> list[str]:
    if not hostname:
        raise ValueError("missing_hostname")
    host = hostname.strip().strip("[]").lower().rstrip(".")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError("blocked_hostname")
    try:
        literal = ipaddress.ip_address(host)
        if _is_blocked_ip(str(literal)):
            raise ValueError("blocked_private_ip")
        return [str(literal)]
    except ValueError as exc:
        if str(exc) == "blocked_private_ip":
            raise
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("dns_resolution_failed")
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise ValueError("dns_resolution_empty")
    for ip in ips:
        if _is_blocked_ip(ip):
            raise ValueError("blocked_private_ip")
    return ips


def _validate_public_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https":
        raise ValueError("https_required")
    if parsed.username or parsed.password:
        raise ValueError("url_credentials_forbidden")
    _resolve_and_validate_host(parsed.hostname or "")
    return urllib.parse.urlunparse(parsed)


def _merge_query(url: str, query: Dict[str, Any] | None) -> str:
    if not query:
        return url
    clean = {k: v for k, v in query.items() if v is not None}
    if not clean:
        return url
    parsed = urllib.parse.urlparse(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    encoded = urllib.parse.urlencode(existing + list(clean.items()), doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=encoded))


def _request(
    *,
    tool: str,
    method: str,
    url: str,
    body: Dict[str, Any] | None = None,
    headers: Dict[str, Any] | None = None,
    auth: bool = False,
    dry_run: bool = False,
    kind: str = "read",
) -> str:
    try:
        method = _validate_method(method)
        url = _validate_public_https_url(url)
        ok, deny = _rate_check(tool, kind)
        if not ok:
            return _json(deny)

        final_headers = {
            "User-Agent": DBM_USER_AGENT,
            "Accept": "application/json",
        }
        if method == "POST":
            final_headers["Content-Type"] = "application/json"
        for key, value in (headers or {}).items():
            lk = str(key).lower()
            if lk in SENSITIVE_HEADER_NAMES:
                try:
                    from security.scope_policy import current_scope, current_session_id, log_scope_violation
                    log_scope_violation(
                        current_session_id(),
                        current_scope(),
                        tool,
                        {"url": url, "method": method, "headers": headers or {}},
                        "DENIED",
                        f"header_forbidden:{key}",
                    )
                except Exception:
                    pass
                raise ValueError(f"header_forbidden:{key}")
            if lk == "user-agent":
                continue
            final_headers[str(key)] = str(value)
        if auth:
            token = _dbm_agent_token()
            if not token and not dry_run:
                return _json({"error": "missing_env", "env": "HERMES_AGENT_API_KEY"})
            final_headers["Authorization"] = f"Bearer {token or '[DRY_RUN_TOKEN]'}"

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        if dry_run:
            return _json({
                "dry_run": True,
                "method": method,
                "url": url,
                "headers": _redact_headers(final_headers),
                "body": body,
                "timeout_seconds": HTTP_TIMEOUT_SECONDS,
                "max_response_bytes": MAX_RESPONSE_BYTES,
            })

        current_url = url
        for redirect_index in range(MAX_REDIRECTS + 1):
            current_url = _validate_public_https_url(current_url)
            req = urllib.request.Request(current_url, data=data, headers=final_headers, method=method)
            opener = urllib.request.build_opener(_NoRedirectHandler)
            try:
                with opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    status = int(getattr(resp, "status", 200))
                    raw = resp.read(MAX_RESPONSE_BYTES + 1)
                    _record_http_result(tool, kind, status)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        return _json({"error": "response_too_large", "max_bytes": MAX_RESPONSE_BYTES})
                    text = raw.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(text) if text else None
                    except Exception:
                        parsed = None
                    return _json({"status": status, "url": current_url, "json": parsed, "text": text if parsed is None else None})
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                if status in {301, 302, 303, 307, 308}:
                    if redirect_index >= MAX_REDIRECTS:
                        _record_http_result(tool, kind, status)
                        return _json({"error": "too_many_redirects", "status": status})
                    location = exc.headers.get("Location")
                    if not location:
                        return _json({"error": "redirect_without_location", "status": status})
                    current_url = urllib.parse.urljoin(current_url, location)
                    if status == 303:
                        method = "GET"
                        data = None
                    continue
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
                _record_http_result(tool, kind, status)
                text = _redact_error(raw.decode("utf-8", errors="replace"))
                if len(raw) > MAX_RESPONSE_BYTES:
                    text = text[:1000] + "[truncated]"
                try:
                    parsed = json.loads(text) if text else None
                except Exception:
                    parsed = None
                return _json({"status": status, "error": "http_error", "json": parsed, "text": text if parsed is None else None})
        return _json({"error": "too_many_redirects"})
    except Exception as exc:
        return _json({"error": _redact_error(str(exc))})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _dbm_url(path: str) -> str:
    return f"{DBM_API_BASE_URL}{path}"


def _dbm_post(tool: str, path: str, body: Dict[str, Any], dry_run: bool, kind: str = "read") -> str:
    return _request(tool=tool, method="POST", url=_dbm_url(path), body=body, auth=True, dry_run=dry_run, kind=kind)


def _dbm_get(tool: str, path: str, query: Dict[str, Any], dry_run: bool, kind: str = "read") -> str:
    return _request(tool=tool, method="GET", url=_merge_query(_dbm_url(path), query), auth=True, dry_run=dry_run, kind=kind)


def _require_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 20:
        raise ValueError(f"invalid_{field}")
    return value


def _pick(data: Dict[str, Any], allowed: Iterable[str]) -> Dict[str, Any]:
    allowed_set = set(allowed)
    return {k: v for k, v in (data or {}).items() if k in allowed_set and v is not None}


SEARCH_FIELDS = {
    "tier", "category", "status", "score_min", "score_max", "department_code",
    "region_code", "city_contains", "has_email", "has_linkedin", "source_prefix",
    "assigned_to_agent", "limit", "offset", "order_by", "order_direction",
}
AUDIT_LOG_FIELDS = {
    "prospect_id", "endpoint", "from_ts", "to_ts", "caller_type", "errors_only", "limit",
}
UPDATE_FIELDS = {
    "score", "status", "intel", "intel_strategy", "notes_replace", "notes_append",
    "assigned_to_agent", "contact_email", "contact_phone", "contact_linkedin", "website",
    "contact_name", "region_code", "department_code", "city", "postal_code", "source", "source_url",
}
INSERT_FIELDS = {
    "tier", "category", "name", "contact_name", "contact_email", "contact_phone",
    "contact_linkedin", "website", "region_code", "department_code", "city", "postal_code",
    "source", "source_url", "intel", "score", "status", "notes", "assigned_to_agent",
}
INBOX_FIELDS = {"identity_id", "since", "status", "limit", "offset", "include_processed"}

DEPARTMENT_TO_REGION_INSEE = {
    # Auvergne-Rhône-Alpes
    "01": "84", "03": "84", "07": "84", "15": "84", "26": "84", "38": "84",
    "42": "84", "43": "84", "63": "84", "69": "84", "73": "84", "74": "84",
    # Bourgogne-Franche-Comté
    "21": "27", "25": "27", "39": "27", "58": "27", "70": "27", "71": "27", "89": "27", "90": "27",
    # Bretagne
    "22": "53", "29": "53", "35": "53", "56": "53",
    # Centre-Val de Loire
    "18": "24", "28": "24", "36": "24", "37": "24", "41": "24", "45": "24",
    # Corse
    "2A": "94", "2B": "94",
    # Grand Est
    "08": "44", "10": "44", "51": "44", "52": "44", "54": "44", "55": "44",
    "57": "44", "67": "44", "68": "44", "88": "44",
    # Hauts-de-France
    "02": "32", "59": "32", "60": "32", "62": "32", "80": "32",
    # Île-de-France
    "75": "11", "77": "11", "78": "11", "91": "11", "92": "11", "93": "11", "94": "11", "95": "11",
    # Normandie
    "14": "28", "27": "28", "50": "28", "61": "28", "76": "28",
    # Nouvelle-Aquitaine
    "16": "75", "17": "75", "19": "75", "23": "75", "24": "75", "33": "75", "40": "75",
    "47": "75", "64": "75", "79": "75", "86": "75", "87": "75",
    # Occitanie
    "09": "76", "11": "76", "12": "76", "30": "76", "31": "76", "32": "76", "34": "76",
    "46": "76", "48": "76", "65": "76", "66": "76", "81": "76", "82": "76",
    # Pays de la Loire
    "44": "52", "49": "52", "53": "52", "72": "52", "85": "52",
    # Provence-Alpes-Côte d'Azur
    "04": "93", "05": "93", "06": "93", "13": "93", "83": "93", "84": "93",
    # DROM region codes (for completeness when a 3-digit department appears)
    "971": "01", "972": "02", "973": "03", "974": "04", "976": "06",
}
REGION_INSEE_CODES = set(DEPARTMENT_TO_REGION_INSEE.values())
_POSTAL_CITY_RE = re.compile(r"(?P<city>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<postal>(?:\d{2}|2a|2b)\d{3})", re.IGNORECASE)

# Conservative suffix lexicon for Hunter/OEC canonical URLs where the profile slug
# may be "firm-name-city-06000-..." instead of "/city-06000/firm-name".
# Without a commune gazetteer, the only safe signal in that pattern is a known
# commune slug suffix immediately before the postal code.
_KNOWN_CITY_SLUG_SUFFIXES = tuple(
    sorted(
        {
            # Day-1 / validation examples
            "nice",
            "cagnes-sur-mer",
            "saint-laurent-du-var",
            # Day-2 PACA departments 13 / 83 / 84 — common OEC target cities
            "aix-en-provence",
            "allauch",
            "arles",
            "aubagne",
            "cassis",
            "berre-l-etang",
            "chateaurenard",
            "gardanne",
            "istres",
            "la-ciotat",
            "les-pennes-mirabeau",
            "marignane",
            "marseille",
            "martigues",
            "miramas",
            "port-de-bouc",
            "salon-de-provence",
            "vitrolles",
            "brignoles",
            "draguignan",
            "frejus",
            "hyeres",
            "la-garde",
            "la-seyne-sur-mer",
            "le-pradet",
            "saint-raphael",
            "sainte-maxime",
            "six-fours-les-plages",
            "toulon",
            "avignon",
            "bollene",
            "cavaillon",
            "carpentras",
            "l-isle-sur-la-sorgue",
            "orange",
            "pertuis",
            "sorgues",
            "vedene",
        },
        key=len,
        reverse=True,
    )
)


def _titlecase_slug(slug: str) -> str:
    words = [w for w in re.split(r"[-_]+", slug or "") if w]
    return " ".join(word.capitalize() for word in words)


def _city_slug_from_postal_candidate(candidate: str) -> str:
    candidate = (candidate or "").strip("/-_").lower()
    if not candidate:
        return ""
    # Prefer the URL path segment when source has the clean /city-06000/... shape.
    if "/" in candidate:
        candidate = candidate.rsplit("/", 1)[-1]
    for city_slug in _KNOWN_CITY_SLUG_SUFFIXES:
        if candidate == city_slug or candidate.endswith("-" + city_slug):
            return city_slug
    return candidate


def parse_city_postal_department_from_url(url: str) -> Dict[str, str]:
    """Extract city, postal_code and department_code from canonical OEC-style URLs.

    Rule: use the slug segment immediately before the postal code in a
    ``city-06000`` token, never the cabinet-name slug.
    """
    path = urllib.parse.urlparse(str(url or "")).path
    for match in _POSTAL_CITY_RE.finditer(path):
        city_slug = _city_slug_from_postal_candidate(match.group("city"))
        postal_code = match.group("postal").upper()
        if postal_code[:2].upper() in {"2A", "2B"}:
            department_code = postal_code[:2].upper()
        else:
            department_code = postal_code[:2]
        return {
            "city": _titlecase_slug(city_slug),
            "postal_code": postal_code,
            "department_code": department_code,
        }
    return {}


def region_code_for_department(department_code: Any) -> str:
    dep = str(department_code or "").strip().upper()
    return DEPARTMENT_TO_REGION_INSEE.get(dep, "")


def _intel_dict(prospect: Dict[str, Any]) -> Dict[str, Any]:
    intel = (prospect or {}).get("intel") or {}
    if isinstance(intel, str):
        try:
            intel = json.loads(intel)
        except Exception:
            return {}
    return intel if isinstance(intel, dict) else {}


def _clean_city(value: Any) -> str:
    return str(value or "").strip().title()


def _normalize_prospect_geo(prospect: Dict[str, Any]) -> Dict[str, Any]:
    prospect = dict(prospect or {})
    parsed = parse_city_postal_department_from_url(str(prospect.get("source_url") or ""))
    intel = _intel_dict(prospect)

    # Hunter/OEC precedence: DOM/scraped intel.city is authoritative. The URL
    # parser is only a fallback because OEC slugs can be "34226-firm-city-13260".
    intel_city = _clean_city(intel.get("city"))
    if intel_city:
        prospect["city"] = intel_city
    elif not _clean_city(prospect.get("city")) and parsed.get("city"):
        prospect["city"] = parsed["city"]
    elif parsed.get("city") and _clean_city(prospect.get("city"))[:1].isdigit():
        prospect["city"] = parsed["city"]
    elif parsed.get("city") and _clean_city(prospect.get("city")) == _clean_city(prospect.get("name")):
        prospect["city"] = parsed["city"]
    elif parsed.get("city") and not prospect.get("city"):
        prospect["city"] = parsed["city"]

    for field in ("postal_code", "department_code"):
        if parsed.get(field) and not prospect.get(field):
            prospect[field] = parsed[field]
    dep = str(prospect.get("department_code") or "").strip().upper()
    if dep:
        prospect["department_code"] = dep
        region = region_code_for_department(dep)
        if region:
            prospect["region_code"] = region
    region_code = str(prospect.get("region_code") or "").strip()
    if region_code and region_code not in REGION_INSEE_CODES:
        prospect.pop("region_code", None)
    return prospect


def _handle_dbm_prospects_search(args: Dict[str, Any], **_) -> str:
    filters = _pick(args.get("filters") or {}, SEARCH_FIELDS)
    return _dbm_post("dbm_prospects_search", "/api/cockpit/prospects/search", filters, bool(args.get("dry_run")), "read")


def _handle_dbm_prospects_update(args: Dict[str, Any], **_) -> str:
    body = {"prospect_id": _require_uuid(args.get("id") or args.get("prospect_id"), "prospect_id"), "fields": _pick(args.get("fields") or {}, UPDATE_FIELDS)}
    return _dbm_post("dbm_prospects_update", "/api/cockpit/prospects/update", body, bool(args.get("dry_run")), "write")


def _handle_dbm_prospects_insert(args: Dict[str, Any], **_) -> str:
    prospect = _normalize_prospect_geo(_pick(args.get("prospect") or {}, INSERT_FIELDS))
    for field in ("tier", "category", "name"):
        if prospect.get(field) in (None, ""):
            return _json({"error": f"missing_required_field:{field}"})
    return _dbm_post("dbm_prospects_insert", "/api/cockpit/prospects/insert", prospect, bool(args.get("dry_run")), "write")


def _handle_dbm_audit_log_search(args: Dict[str, Any], **_) -> str:
    body = _pick(args, AUDIT_LOG_FIELDS)
    if "limit" not in body:
        body["limit"] = 50
    return _dbm_post("dbm_audit_log_search", "/api/cockpit/audit-log/search", body, bool(args.get("dry_run")), "read")


def _handle_dbm_outreach_draft(args: Dict[str, Any], **_) -> str:
    body = {"prospect_id": _require_uuid(args.get("prospect_id"), "prospect_id"), "channel": args.get("channel")}
    if body["channel"] not in {"email", "linkedin"}:
        return _json({"error": "invalid_channel"})
    return _dbm_post("dbm_outreach_draft", "/api/cockpit/outreach/draft", body, bool(args.get("dry_run")), "read")


def _handle_dbm_outreach_send(args: Dict[str, Any], **_) -> str:
    body = _pick(args, {"prospect_id", "identity_id", "sequence_step_id", "subject", "body_text", "approved_by"})
    body["prospect_id"] = _require_uuid(body.get("prospect_id"), "prospect_id")
    body["identity_id"] = _require_uuid(body.get("identity_id"), "identity_id")
    return _dbm_post("dbm_outreach_send", "/api/cockpit/outreach/send", body, bool(args.get("dry_run")), "write")


def _handle_dbm_outreach_mark_sent(args: Dict[str, Any], **_) -> str:
    body = _pick(args, {"prospect_id", "channel", "identity_id", "sequence_step_id", "subject", "body_text", "sent_via", "approved_by"})
    body["prospect_id"] = _require_uuid(body.get("prospect_id"), "prospect_id")
    return _dbm_post("dbm_outreach_mark_sent", "/api/cockpit/outreach/mark-sent", body, bool(args.get("dry_run")), "write")


def _handle_dbm_outreach_inbox_list(args: Dict[str, Any], **_) -> str:
    query = _pick(args, INBOX_FIELDS)
    return _dbm_get("dbm_outreach_inbox_list", "/api/cockpit/outreach/inbox", query, bool(args.get("dry_run")), "read")


def _handle_http_fetch(args: Dict[str, Any], **_) -> str:
    method = _validate_method(args.get("method") or "GET")
    body = args.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            body = {"text": body}
    if body is not None and not isinstance(body, dict):
        return _json({"error": "body_must_be_object_or_string"})
    return _request(
        tool="http_fetch",
        method=method,
        url=args.get("url", ""),
        body=body,
        headers=args.get("headers") or {},
        auth=False,
        dry_run=bool(args.get("dry_run")),
        kind="read",
    )


DRY_RUN_PROP = {"type": "boolean", "description": "If true, validate and return the prepared request without sending it."}


def _schema(name: str, description: str, properties: Dict[str, Any], required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {**properties, "dry_run": DRY_RUN_PROP},
            "required": required or [],
            "additionalProperties": False,
        },
    }


registry.register(
    name="dbm_prospects_search",
    toolset="dbm-api",
    schema=_schema("dbm_prospects_search", "Search DBM prospects via cockpit API.", {"filters": {"type": "object", "additionalProperties": True}}, ["filters"]),
    handler=_handle_dbm_prospects_search,
    emoji="🏨",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_prospects_update",
    toolset="dbm-api",
    schema=_schema("dbm_prospects_update", "Update whitelisted DBM prospect fields.", {"id": {"type": "string"}, "fields": {"type": "object", "additionalProperties": True}}, ["id", "fields"]),
    handler=_handle_dbm_prospects_update,
    emoji="🏨",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_prospects_insert",
    toolset="dbm-api",
    schema=_schema("dbm_prospects_insert", "Insert a DBM prospect via cockpit API.", {"prospect": {"type": "object", "additionalProperties": True}}, ["prospect"]),
    handler=_handle_dbm_prospects_insert,
    emoji="🏨",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_audit_log_search",
    toolset="dbm-api",
    schema=_schema(
        "dbm_audit_log_search",
        "Search sanitized DBM agent API audit logs via cockpit API.",
        {
            "prospect_id": {"type": "string"},
            "endpoint": {"type": "string"},
            "from_ts": {"type": "string"},
            "to_ts": {"type": "string"},
            "caller_type": {"type": "string"},
            "errors_only": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        [],
    ),
    handler=_handle_dbm_audit_log_search,
    emoji="🔎",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_outreach_draft",
    toolset="dbm-api",
    schema=_schema("dbm_outreach_draft", "Draft DBM outreach for a prospect.", {"prospect_id": {"type": "string"}, "channel": {"type": "string", "enum": ["email", "linkedin"]}}, ["prospect_id", "channel"]),
    handler=_handle_dbm_outreach_draft,
    emoji="✉️",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_outreach_send",
    toolset="dbm-api",
    schema=_schema("dbm_outreach_send", "Send approved DBM outreach using current DBM transport.", {"prospect_id": {"type": "string"}, "identity_id": {"type": "string"}, "sequence_step_id": {"type": "string"}, "subject": {"type": "string", "minLength": 1, "maxLength": 500}, "body_text": {"type": "string", "minLength": 1, "maxLength": 50000}, "approved_by": {"type": "string"}}, ["prospect_id", "identity_id", "subject", "body_text"]),
    handler=_handle_dbm_outreach_send,
    emoji="✉️",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_outreach_mark_sent",
    toolset="dbm-api",
    schema=_schema("dbm_outreach_mark_sent", "Mark DBM outreach as sent.", {"prospect_id": {"type": "string"}, "channel": {"type": "string"}, "identity_id": {"type": "string"}, "sequence_step_id": {"type": "string"}, "subject": {"type": "string"}, "body_text": {"type": "string"}, "sent_via": {"type": "string", "minLength": 1, "maxLength": 100}, "approved_by": {"type": "string"}}, ["prospect_id", "channel", "body_text", "sent_via"]),
    handler=_handle_dbm_outreach_mark_sent,
    emoji="✉️",
    max_result_size_chars=100_000,
)
registry.register(
    name="dbm_outreach_inbox_list",
    toolset="dbm-api",
    schema=_schema("dbm_outreach_inbox_list", "List DBM outreach inbox items.", {"identity_id": {"type": "string"}, "since": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}, "include_processed": {"type": "boolean"}}, []),
    handler=_handle_dbm_outreach_inbox_list,
    emoji="📥",
    max_result_size_chars=100_000,
)
registry.register(
    name="http_fetch",
    toolset="dbm-api",
    schema=_schema("http_fetch", "Strict HTTPS fetch for DBM Hunter research. Blocks localhost/private/cloud metadata IPs, forbids auth cookies/headers, max 5MB, 30s timeout, GET/POST only.", {"url": {"type": "string"}, "method": {"type": "string", "enum": ["GET", "POST"]}, "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "body": {"description": "JSON object or string body for POST."}}, ["url"]),
    handler=_handle_http_fetch,
    emoji="🌐",
    max_result_size_chars=100_000,
)
