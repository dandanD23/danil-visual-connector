import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import redis.asyncio as redis
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

PINTEREST_API = "https://api.pinterest.com/v5"
PINTEREST_OAUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"

APP_ID = os.getenv("PINTEREST_APP_ID", "").strip()
APP_SECRET = os.getenv("PINTEREST_APP_SECRET", "").strip()
BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://danil-pinterest-api.onrender.com").rstrip("/")
REDIRECT_URI = os.getenv(
    "PINTEREST_REDIRECT_URI",
    f"{BASE_URL}/oauth/pinterest/callback",
).strip()
REDIS_URL = os.getenv("REDIS_URL", "redis://red-dau0crgu01pc73apaff0:6379").strip()

TOKEN_KEY = "pinterest:oauth:token"
STATE_PREFIX = "pinterest:oauth:state:"
SCOPES = "boards:read,pins:read"

rdb = redis.from_url(REDIS_URL, decode_responses=True)


async def _store_token(payload: dict[str, Any]) -> None:
    now = int(time.time())
    stored = dict(payload)
    if stored.get("expires_in") is not None:
        stored["expires_at"] = now + int(stored["expires_in"])
    if stored.get("refresh_token_expires_in") is not None:
        stored["refresh_token_expires_at"] = now + int(stored["refresh_token_expires_in"])
    await rdb.set(TOKEN_KEY, json.dumps(stored))


async def _load_token() -> dict[str, Any] | None:
    raw = await rdb.get(TOKEN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _refresh_token(current: dict[str, Any]) -> dict[str, Any]:
    refresh = str(current.get("refresh_token") or "").strip()
    if not refresh:
        raise RuntimeError("Pinterest refresh token is unavailable; reconnect the account.")

    if not APP_ID or not APP_SECRET:
        raise RuntimeError("Pinterest app credentials are not fully configured.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            PINTEREST_TOKEN_URL,
            auth=(APP_ID, APP_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "scope": SCOPES,
            },
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Pinterest token refresh failed ({resp.status_code}): {resp.text[:700]}")
    payload = resp.json()

    # Some providers rotate refresh tokens; if Pinterest omits one, retain the old token.
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh
        if current.get("refresh_token_expires_at"):
            payload["refresh_token_expires_at"] = current["refresh_token_expires_at"]

    await _store_token(payload)
    return (await _load_token()) or payload


async def _access_token() -> str:
    token = await _load_token()
    if not token:
        raise RuntimeError(
            f"Pinterest is not connected yet. Open {BASE_URL}/oauth/pinterest/start after Trial access is approved."
        )

    expires_at = int(token.get("expires_at") or 0)
    # Refresh early so normal use never gets close to expiration.
    if expires_at and expires_at - int(time.time()) < 7 * 24 * 3600:
        token = await _refresh_token(token)

    value = str(token.get("access_token") or "").strip()
    if not value:
        raise RuntimeError("Stored Pinterest token is invalid; reconnect the account.")
    return value


async def _api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    token = await _access_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{PINTEREST_API}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
        )
    if resp.status_code == 401:
        current = await _load_token()
        if current and current.get("refresh_token"):
            await _refresh_token(current)
            token = await _access_token()
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{PINTEREST_API}{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params=params,
                )
    if resp.status_code >= 400:
        raise RuntimeError(f"Pinterest API {resp.status_code}: {resp.text[:1000]}")
    return resp.json()


def _extract_image_urls(pin: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "url" and isinstance(v, str) and v.startswith("http"):
                    urls.append(v)
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(pin.get("media"))
    # Preserve order but deduplicate.
    return list(dict.fromkeys(urls))[:8]


def _normalize_pin(pin: dict[str, Any], board_name: str | None = None) -> dict[str, Any]:
    return {
        "id": pin.get("id"),
        "title": pin.get("title") or "",
        "description": pin.get("description") or "",
        "link": pin.get("link") or "",
        "dominant_color": pin.get("dominant_color"),
        "board_id": pin.get("board_id"),
        "board_name": board_name,
        "image_urls": _extract_image_urls(pin),
    }


mcp = FastMCP(
    "Danil Pinterest",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def pinterest_connection_status() -> dict[str, Any]:
    """Check whether the owner's Pinterest account is connected and ready for read-only board/Pin access."""
    token = await _load_token()
    return {
        "app_id_configured": bool(APP_ID),
        "app_secret_configured": bool(APP_SECRET),
        "connected": bool(token and token.get("access_token")),
        "scopes": SCOPES,
        "connect_url": f"{BASE_URL}/oauth/pinterest/start",
        "redirect_uri": REDIRECT_URI,
        "mode": "read-only; public boards and Pins only",
    }


@mcp.tool()
async def pinterest_list_boards(
    page_size: int = 100,
    bookmark: str | None = None,
) -> dict[str, Any]:
    """List the connected owner's Pinterest boards. This connector requests only boards:read and excludes secret-board scopes."""
    page_size = max(1, min(page_size, 250))
    params: dict[str, Any] = {"page_size": page_size}
    if bookmark:
        params["bookmark"] = bookmark
    data = await _api_get("/boards", params=params)
    items = []
    for board in data.get("items", []):
        # Defensive filter in case Pinterest ever includes protected boards unexpectedly.
        privacy = str(board.get("privacy") or "").upper()
        if privacy == "SECRET":
            continue
        items.append({
            "id": board.get("id"),
            "name": board.get("name"),
            "description": board.get("description") or "",
            "privacy": board.get("privacy"),
            "pin_count": board.get("pin_count"),
            "follower_count": board.get("follower_count"),
        })
    return {"items": items, "bookmark": data.get("bookmark")}


@mcp.tool()
async def pinterest_list_board_pins(
    board_id: str,
    page_size: int = 100,
    bookmark: str | None = None,
) -> dict[str, Any]:
    """List Pins saved on one Pinterest board owned by the connected account."""
    page_size = max(1, min(page_size, 250))
    params: dict[str, Any] = {"page_size": page_size}
    if bookmark:
        params["bookmark"] = bookmark
    data = await _api_get(f"/boards/{board_id}/pins", params=params)
    return {
        "items": [_normalize_pin(p) for p in data.get("items", [])],
        "bookmark": data.get("bookmark"),
    }


async def _all_public_boards(max_pages: int = 10) -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    bookmark: str | None = None
    for _ in range(max_pages):
        data = await pinterest_list_boards(page_size=250, bookmark=bookmark)
        boards.extend(data["items"])
        bookmark = data.get("bookmark")
        if not bookmark:
            break
    return boards


@mcp.tool()
async def pinterest_find_boards(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Find the owner's boards by name or description, for example 'mockups', 'web design', or 'presentations'."""
    q = query.strip().lower()
    if not q:
        return []
    boards = await _all_public_boards()
    scored: list[tuple[int, dict[str, Any]]] = []
    for b in boards:
        name = str(b.get("name") or "").lower()
        desc = str(b.get("description") or "").lower()
        score = 0
        if q == name:
            score += 20
        if q in name:
            score += 10
        if q in desc:
            score += 4
        for term in q.split():
            if term in name:
                score += 3
            if term in desc:
                score += 1
        if score:
            scored.append((score, b))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("name") or "")))
    return [b for _, b in scored[: max(1, min(max_results, 50))]]


@mcp.tool()
async def pinterest_search_my_pins(
    query: str,
    board_name: str | None = None,
    max_results: int = 30,
    max_boards: int = 40,
    pages_per_board: int = 3,
) -> list[dict[str, Any]]:
    """Search Pins in the owner's public boards using Pin title, description, link, and board name.

    Use board_name when the user knows the likely board (for example 'Mockups').
    The result includes original Pin metadata and image URLs for visual review.
    """
    terms = [t for t in query.strip().lower().split() if t]
    if not terms:
        return []

    boards = await _all_public_boards()
    if board_name:
        bq = board_name.strip().lower()
        preferred = [
            b for b in boards
            if bq in str(b.get("name") or "").lower()
        ]
        if preferred:
            boards = preferred

    boards = boards[: max(1, min(max_boards, 100))]
    results: list[tuple[int, dict[str, Any]]] = []

    for board in boards:
        bookmark: str | None = None
        for _ in range(max(1, min(pages_per_board, 10))):
            data = await pinterest_list_board_pins(
                str(board["id"]),
                page_size=100,
                bookmark=bookmark,
            )
            for raw in data.get("items", []):
                pin = dict(raw)
                pin["board_name"] = board.get("name")
                haystack = " ".join([
                    str(pin.get("title") or ""),
                    str(pin.get("description") or ""),
                    str(pin.get("link") or ""),
                    str(board.get("name") or ""),
                    str(board.get("description") or ""),
                ]).lower()
                score = sum(haystack.count(term) for term in terms)
                title = str(pin.get("title") or "").lower()
                board_text = str(board.get("name") or "").lower()
                score += sum(3 for term in terms if term in title)
                score += sum(2 for term in terms if term in board_text)
                if score > 0:
                    pin["match_score"] = score
                    results.append((score, pin))
            bookmark = data.get("bookmark")
            if not bookmark:
                break

    results.sort(key=lambda x: (-x[0], str(x[1].get("title") or "")))
    return [p for _, p in results[: max(1, min(max_results, 100))]]


async def health(_: Request) -> JSONResponse:
    try:
        await rdb.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return JSONResponse({
        "ok": True,
        "service": "danil-pinterest-api",
        "redis": redis_ok,
        "trial_ready": bool(APP_ID),
        "secret_configured": bool(APP_SECRET),
    })


async def oauth_start(_: Request) -> RedirectResponse | HTMLResponse:
    if not APP_ID:
        return HTMLResponse("Pinterest App ID is not configured.", status_code=503)

    state = secrets.token_urlsafe(32)
    await rdb.setex(STATE_PREFIX + state, 600, "1")
    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return RedirectResponse(f"{PINTEREST_OAUTH_URL}?{urlencode(params)}")


async def oauth_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(f"<h1>Pinterest authorization failed</h1><p>{error}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h1>Missing OAuth code/state</h1>", status_code=400)

    valid = await rdb.get(STATE_PREFIX + state)
    await rdb.delete(STATE_PREFIX + state)
    if not valid:
        return HTMLResponse("<h1>Invalid or expired OAuth state</h1>", status_code=400)

    if not APP_SECRET:
        return HTMLResponse(
            "<h1>Almost ready</h1><p>The Pinterest App Secret has not been configured on the server yet. "
            "Add it to Render and restart the authorization flow.</p>",
            status_code=503,
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            PINTEREST_TOKEN_URL,
            auth=(APP_ID, APP_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )

    if resp.status_code >= 400:
        return HTMLResponse(
            f"<h1>Token exchange failed</h1><pre>{resp.text[:1200]}</pre>",
            status_code=502,
        )

    await _store_token(resp.json())
    return HTMLResponse(
        "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "max-width:680px;margin:80px auto;padding:24px'>"
        "<h1>Pinterest connected ✓</h1>"
        "<p>Danil Visual Connector now has read-only access to your approved public boards and Pins.</p>"
        "<p>You can close this page and return to ChatGPT.</p></body>"
    )


async def oauth_status(_: Request) -> JSONResponse:
    token = await _load_token()
    return JSONResponse({
        "connected": bool(token and token.get("access_token")),
        "app_id_configured": bool(APP_ID),
        "app_secret_configured": bool(APP_SECRET),
        "redirect_uri": REDIRECT_URI,
        "scopes": SCOPES,
    })


app = mcp.streamable_http_app()
app.add_route("/health", health, methods=["GET"])
app.add_route("/oauth/pinterest/start", oauth_start, methods=["GET"])
app.add_route("/oauth/pinterest/callback", oauth_callback, methods=["GET"])
app.add_route("/oauth/pinterest/status", oauth_status, methods=["GET"])
