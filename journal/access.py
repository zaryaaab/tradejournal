"""
Principal owner resolution and trading-account sidebar permissions (shared by views + context processor).
"""
from __future__ import annotations

from django.contrib.auth.models import User

from .models import TradingAccount, UserProfile

SELECTED_TRADING_ACCOUNT_SESSION_KEY = "selected_trading_account_id"


def get_principal_owner(request) -> User:
    """
    User whose trades and trading accounts are shown (super_admin → self;
    admin → first super_admin; assistant/client → creator chain / fallbacks).
    """
    try:
        role = request.user.profile.role
    except Exception:
        role = "client"

    if role == "super_admin":
        return request.user

    if role == "admin":
        super_admin_user = (
            UserProfile.objects.filter(role="super_admin")
            .select_related("user")
            .first()
        )
        if super_admin_user:
            return super_admin_user.user
        return request.user

    try:
        creator = request.user.profile.created_by
        if creator:
            if creator.profile.role == "admin":
                super_admin_user = (
                    UserProfile.objects.filter(role="super_admin")
                    .select_related("user")
                    .first()
                )
                if super_admin_user:
                    return super_admin_user.user
            return creator
    except Exception:
        pass

    super_admin = (
        UserProfile.objects.filter(role="super_admin")
        .select_related("user")
        .first()
    )
    if super_admin:
        return super_admin.user

    admin = (
        UserProfile.objects.filter(role="admin")
        .select_related("user")
        .first()
    )
    return admin.user if admin else request.user


def show_trading_accounts_sidebar(request) -> bool:
    if not request.user.is_authenticated:
        return False
    try:
        role = request.user.profile.role
    except Exception:
        return False
    return role in ("super_admin", "admin", "assistant")


def can_manage_trading_accounts(request) -> bool:
    if not request.user.is_authenticated:
        return False
    try:
        return request.user.profile.role in ("admin", "super_admin")
    except Exception:
        return False


def can_view_trading_accounts(request) -> bool:
    """Read-only broker list + import picker (assistant, admin, super_admin)."""
    if not request.user.is_authenticated:
        return False
    try:
        return request.user.profile.role in ("super_admin", "admin", "assistant")
    except Exception:
        return False


def user_ids_in_principal_org(principal_user: User) -> set[int]:
    """BFS over created_by graph starting at principal (includes principal id)."""
    # Always query auth.User — never use type(principal_user): request.user can be a
    # SimpleLazyObject, whose type is not the concrete User model.
    seen: set[int] = {principal_user.pk}
    stack = [principal_user]
    while stack:
        u = stack.pop()
        for child in User.objects.filter(profile__created_by=u).select_related("profile"):
            if child.id not in seen:
                seen.add(child.id)
                stack.append(child)
    return seen


def resolve_selected_trading_account_id(request, owner: User) -> int | None:
    """
    Validated session id for filtering trades, or None = all accounts.
    Clears session if invalid.
    """
    raw = request.session.get(SELECTED_TRADING_ACCOUNT_SESSION_KEY)
    if raw is None or raw == "" or raw == "all":
        return None
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)
        return None
    if not TradingAccount.objects.filter(
        pk=aid, owner=owner, is_archived=False
    ).exists():
        request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)
        return None
    return aid
