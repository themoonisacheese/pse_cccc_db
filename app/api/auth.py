"""Stack Exchange OAuth2 authentication and authorization."""

import re
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer, BadSignature
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.models.clue import User
from app.schemas.clue import UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# Serializer for OAuth state tokens
_state_serializer = lambda: URLSafeSerializer(settings.secret_key, salt="oauth-state")


def _create_state(redirect_after: str = "/") -> str:
    return _state_serializer().dumps({"redirect": redirect_after})


def _verify_state(state: str) -> dict:
    try:
        return _state_serializer().loads(state, max_age=600)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")


def get_oauth_url(state: str) -> str:
    """Build the Stack Exchange OAuth2 authorisation URL."""
    params = {
        "client_id": settings.se_client_id,
        "scope": "",  # SE doesn't require explicit scopes for read
        "redirect_uri": settings.se_oauth_redirect,
        "state": state,
    }
    return f"https://stackexchange.com/oauth?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> str:
    """Exchange the OAuth2 code for an access token (SE returns it in the body)."""
    data = {
        "client_id": settings.se_client_id,
        "client_secret": settings.se_client_secret,
        "code": code,
        "redirect_uri": settings.se_oauth_redirect,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://stackexchange.com/oauth/access_token",
            data=data,
            headers={"Accept": "application/json"},
        )
        # SE returns form-encoded data, not JSON, despite Accept header
        if resp.headers.get("content-type", "").startswith("application/json"):
            token = resp.json().get("access_token")
        else:
            # Parse form-encoded response
            from urllib.parse import parse_qs
            parsed = parse_qs(resp.text)
            token = parsed.get("access_token", [None])[0]
        if not token:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to exchange code for token: {resp.text}",
            )
        return token


async def get_se_user_info(access_token: str) -> dict:
    """Fetch the authenticated user's SE account info.
    
    Returns a dict with user_id, display_name, and profile_link.
    """
    params = {
        "access_token": access_token,
        "key": settings.se_key,
        "site": "puzzling",
        "filter": "default",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.stackexchange.com/2.3/me",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            raise HTTPException(
                status_code=400,
                detail="No user info returned from Stack Exchange",
            )
        user_info = items[0]
        return {
            "user_id": user_info["user_id"],
            "display_name": user_info["display_name"],
            "profile_link": user_info.get("link", ""),
        }


async def check_room_owner(access_token: str, se_user_id: int) -> bool:
    """Check if the user is a room owner of the CCCC chatroom.
    
    Uses the SE chat API to look at room ownership / moderator status.
    Falls back to the configured allowlist if the API call fails.
    """
    # If there's a configured allowlist, use it
    if settings.owner_id_list:
        return se_user_id in settings.owner_id_list

    # Try the chat API: /rooms/{id}/info returns owner info
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://chat.stackexchange.com/rooms/{settings.se_chat_room_id}/info"
            )
            resp.raise_for_status()
            # The room info page lists owners; we need to check if se_user_id
            # appears in the owners list.
            # This is a best-effort HTML parse.
            html = resp.text
            # Room owners are listed with their user IDs in the HTML
            # e.g., class="user-{userId}" or href="/users/{userId}"
            pattern = rf'href="/users/{se_user_id}\b'
            return bool(re.search(pattern, html))
    except Exception:
        return False


def set_session_cookie(response: Response, user_id: int, se_user_id: int):
    """Set a signed session cookie."""
    s = URLSafeSerializer(settings.secret_key, salt="session")
    token = s.dumps({"uid": user_id, "se_uid": se_user_id})
    response.set_cookie(
        "cccc_session",
        token,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
        max_age=86400 * 7,  # 7 days
    )


def clear_session_cookie(response: Response):
    response.delete_cookie("cccc_session")


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """FastAPI dependency: return the authenticated user or None."""
    token = request.cookies.get("cccc_session")
    if not token:
        return None
    s = URLSafeSerializer(settings.secret_key, salt="session")
    try:
        data = s.loads(token, max_age=86400 * 7)
    except BadSignature:
        return None
    result = await db.execute(
        select(User).where(User.id == data["uid"])
    )
    return result.scalar_one_or_none()


# ── Routes ──────────────────────────────────────────────────


@router.get("/login")
async def login(redirect_after: str = "/"):
    """Initiate the Stack Exchange OAuth2 flow."""
    if not settings.se_client_id:
        raise HTTPException(
            status_code=503,
            detail="Stack Exchange OAuth is not configured. Set SE_CLIENT_ID, SE_CLIENT_SECRET, etc.",
        )
    state = _create_state(redirect_after)
    return RedirectResponse(url=get_oauth_url(state))


@router.get("/callback")
async def callback(
    code: str,
    state: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle the OAuth2 callback from Stack Exchange."""
    state_data = _verify_state(state)
    redirect_after = state_data.get("redirect", "/")

    # Exchange code for access token
    access_token = await exchange_code_for_token(code)

    # Get user info
    user_info = await get_se_user_info(access_token)

    # Check room-owner status
    is_owner = await check_room_owner(access_token, user_info["user_id"])

    # Upsert user in DB
    result = await db.execute(
        select(User).where(User.se_user_id == user_info["user_id"])
    )
    user = result.scalar_one_or_none()
    if user:
        user.display_name = user_info["display_name"]
        user.profile_link = user_info["profile_link"]
        user.is_room_owner = is_owner
    else:
        user = User(
            se_user_id=user_info["user_id"],
            display_name=user_info["display_name"],
            profile_link=user_info["profile_link"],
            is_room_owner=is_owner,
            is_admin=is_owner,  # First-time: room owners are admins
        )
        db.add(user)
    await db.commit()
    await db.refresh(user)

    response = RedirectResponse(url=redirect_after)
    set_session_cookie(response, user.id, user.se_user_id)
    return response


@router.get("/logout")
async def logout():
    """Log the user out by clearing the session cookie."""
    response = RedirectResponse(url="/", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/me", response_model=Optional[UserOut])
async def me(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the current authenticated user, or null."""
    user = await get_current_user(request, db)
    if user:
        return UserOut.model_validate(user)
    return None
