"""Stack Exchange OAuth2 authentication and authorization."""

import logging
import re
from typing import Optional
from urllib.parse import urlencode, parse_qs

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
from app.services import se_chat_client

logger = logging.getLogger(__name__)
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

    The app-level API key is sent alongside the token — the SE API
    requires a key when an access_token is present.

    Returns a dict with user_id, display_name, profile_link, and user_type.
    """
    params = {
        "access_token": access_token,
        "key": settings.se_key,
        "site": settings.se_site,
        # user_type is included in the default filter
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.stackexchange.com/2.3/me",
            params=params,
        )
        if resp.status_code != 200:
            logger.error(
                f"SE API /me returned {resp.status_code}: {resp.text[:300]}"
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
            "user_type": user_info.get("user_type", "registered"),
        }


async def check_room_owner(access_token: str, se_user_id: int) -> bool:
    """Check if the user is a room owner of the CCCC chatroom.

    Strategy (in priority order):
    1. If ROOM_OWNER_IDS is configured, check the allowlist.
    2. If SE_BOT_EMAIL is configured, fetch room owners from the chat API
       and cross-reference by display name (the SE API's site→chat ID
       mapping is unreliable; name matching against the room info page
       is simpler and works).
    3. If the user is a site moderator (user_type == "moderator"),
       they are automatically a room owner of all rooms.

    Returns True if the user is a room owner.
    """
    # 1. Allowlist (highest priority, most reliable)
    if settings.owner_id_list:
        return se_user_id in settings.owner_id_list

    # 2. Try the chat API room-owner check
    if settings.se_bot_email and settings.se_bot_password:
        try:
            # Get user info to check moderator status
            user_info = await get_se_user_info(access_token)

            # Site moderators are automatic room owners
            if user_info.get("user_type") == "moderator":
                logger.info(f"User {se_user_id} is a site moderator → room owner")
                return True

            # Fetch room owners from chat and cross-reference by name
            owner_names = await se_chat_client.get_room_owner_names(
                settings.se_chat_room_id
            )
            user_name = user_info.get("display_name", "").lower()
            for owner_name in owner_names.values():
                if owner_name.lower() == user_name:
                    logger.info(
                        f"User {se_user_id} ({user_info['display_name']}) "
                        f"matched room owner by name"
                    )
                    return True

            logger.info(
                f"User {se_user_id} ({user_info.get('display_name')}) "
                f"is not a room owner. Owners: {list(owner_names.values())}"
            )
            return False

        except Exception as e:
            logger.warning(f"Chat API room-owner check failed: {e}")

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
