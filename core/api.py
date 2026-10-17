from __future__ import annotations

import requests

from .credentials import redact_token
from .models import Channel, Project, Site


class PuApiError(Exception):
    """Base class for all Productsup API errors."""


class PuAuthError(PuApiError):
    """Raised when the token is missing, invalid, or lacks permission."""


class PuHttpError(PuApiError):
    """Raised for any other non-2xx HTTP response or transport error."""


class PuApiClient:
    """
    Thin wrapper around /platform/v2 endpoints.

    Usage:
        client = PuApiClient(token, base_url)
        projects = client.list_projects()
        for p in projects:
            for s in client.list_sites(p.id):
                channels = client.list_channels(s, p)
    """

    def __init__(
        self,
        token: str,
        base_url: str,
        timeout: tuple[int, int] = (5, 30),
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be a non-empty string")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self._token = token.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = timeout

    # -- internal ---------------------------------------------------------

    def _get(self, path: str) -> dict:
        """GET a JSON endpoint. Raises PuAuthError / PuHttpError."""
        url = f"{self._base_url}{path}"
        try:
            response = requests.get(
                url,
                headers={"X-Auth-Token": self._token},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise PuHttpError(
                redact_token(f"Network error calling {url}: {exc}")
            ) from exc

        if response.status_code in (401, 403):
            raise PuAuthError("Token is invalid or lacks permission.")
        if response.status_code >= 400:
            raise PuHttpError(redact_token(f"HTTP {response.status_code} from {url}"))

        try:
            data = response.json()
        except ValueError as exc:
            raise PuHttpError(redact_token(f"Non-JSON response from {url}")) from exc

        if not isinstance(data, dict):
            raise PuHttpError(
                redact_token(
                    f"Unexpected response shape from {url}: "
                    f"expected JSON object, got {type(data).__name__}"
                )
            )
        return data

    # -- public -----------------------------------------------------------

    def list_projects(self) -> list[Project]:
        data = self._get("/platform/v2/projects")
        raw = data.get("Projects") or []
        if not isinstance(raw, list):
            raise PuHttpError("/platform/v2/projects: 'Projects' is not a list")

        result: list[Project] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            pid = _to_str(entry.get("id"))
            if not pid:
                continue
            result.append(Project(id=pid, name=_to_str(entry.get("name"))))
        return result

    def list_sites(self, project_id: str) -> list[Site]:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        project_id = project_id.strip()

        data = self._get(f"/platform/v2/projects/{project_id}/sites")
        raw = data.get("Sites") or []
        if not isinstance(raw, list):
            raise PuHttpError(
                f"/platform/v2/projects/{project_id}/sites: " "'Sites' is not a list"
            )

        result: list[Site] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            sid = _to_str(entry.get("id"))
            if not sid:
                continue
            result.append(
                Site(
                    id=sid,
                    title=_to_str(entry.get("title")),
                    project_id=project_id,
                )
            )
        return result

    def list_channels(
        self,
        site: Site,
        project: Project,
    ) -> list[Channel]:
        """
        Return all channels of a site, active ones first.

        Inactive channels are included: they still have a dataflow
        page and can be parsed. Whether a channel has a feed URL is
        also not a filter condition; it is captured in Channel.url
        for display only.
        """
        if not isinstance(site, Site):
            raise ValueError("site must be a Site instance")
        if not isinstance(project, Project):
            raise ValueError("project must be a Project instance")
        if not site.id:
            raise ValueError("site.id must be non-empty")

        data = self._get(f"/platform/v2/sites/{site.id}/channels")
        raw = data.get("Channels") or []
        if not isinstance(raw, list):
            raise PuHttpError(
                f"/platform/v2/sites/{site.id}/channels: "
                "'Channels' is not a list"
            )

        result: list[Channel] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if not is_usable(entry):
                continue
            cid = _to_str(entry.get("id"))
            if not cid:
                continue
            url = extract_feed_url(entry) or ""
            result.append(
                Channel(
                    id=cid,
                    name=_to_str(entry.get("name")),
                    site_id=site.id,
                    site_name=site.title,
                    project_id=project.id,
                    project_name=project.name,
                    url=url,
                    fmt=detect_url_format(url),
                    active=bool(entry.get("active")),
                )
            )

        result.sort(key=lambda c: not c.active)
        return result


# ---------------------------------------------------------------------------
# Pure helpers (mirror export_selector)
# ---------------------------------------------------------------------------


def extract_feed_url(channel: object) -> str | None:
    """
    Return the first non-empty URL of the first active feed destination,
    or None. Defensive against malformed structures.
    """
    if not isinstance(channel, dict):
        return None

    destinations = channel.get("feed_destinations")
    if not isinstance(destinations, dict):
        return None

    for dest in destinations.values():
        if not isinstance(dest, dict):
            continue
        if not dest.get("active"):
            continue
        urls = dest.get("urls")
        if not isinstance(urls, list) or not urls:
            continue
        first = urls[0]
        if isinstance(first, str):
            cleaned = first.strip()
            if cleaned:
                return cleaned
        # Silently skip non-string first entries (None, dict, ...).
    return None


def is_usable(channel: object) -> bool:
    """
    A channel is usable when it is a dict with a non-empty id.

    Active state and feed URL are deliberately NOT checked. Inactive
    channels still expose a dataflow page, and a channel with no feed
    destination can still be parsed for its mapping.
    """
    if not isinstance(channel, dict):
        return False
    return bool(_to_str(channel.get("id")))


def detect_url_format(url: str) -> str:
    """Return 'xml' when the URL path ends with .xml, else 'csv'."""
    path = (url or "").split("?", 1)[0].lower()
    return "xml" if path.endswith(".xml") else "csv"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_str(value: object) -> str:
    """Best-effort stringify that treats None as empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()
