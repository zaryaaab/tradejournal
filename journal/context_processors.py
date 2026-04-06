from .access import (
    can_manage_trading_accounts,
    can_view_trading_accounts,
    get_principal_owner,
    resolve_selected_trading_account_id,
    show_trading_accounts_sidebar,
)
from .models import TradingAccount


def user_role_context(request):
    if request.user.is_authenticated:
        try:
            role = request.user.profile.role
        except Exception:
            role = "client"

        return {
            "user_role": role,
            "is_super_admin": role == "super_admin",
            "is_admin": role in ("admin", "super_admin"),
            "is_assistant": role == "assistant",
            "is_client": role == "client",
            "can_manage_trading_accounts": can_manage_trading_accounts(request),
            "can_view_trading_accounts": can_view_trading_accounts(request),
        }

    return {}


def trading_accounts_sidebar_context(request):
    if not request.user.is_authenticated:
        return {"show_trading_accounts_sidebar": False}
    if not show_trading_accounts_sidebar(request):
        return {
            "show_trading_accounts_sidebar": False,
            "sidebar_trading_accounts_all": [],
        }
    owner = get_principal_owner(request)
    sidebar_qs = (
        TradingAccount.objects.filter(owner=owner, is_archived=False)
        .order_by("name", "account_number")
    )
    sidebar_total = sidebar_qs.count()
    archived_ct = TradingAccount.objects.filter(owner=owner, is_archived=True).count()
    return {
        "show_trading_accounts_sidebar": True,
        "sidebar_trading_accounts_all": sidebar_qs,
        "sidebar_trading_accounts_total": sidebar_total,
        "sidebar_trading_accounts_archived_count": archived_ct,
        "selected_trading_account_id": resolve_selected_trading_account_id(request, owner),
    }