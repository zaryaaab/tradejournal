from .access import (
    can_manage_trading_accounts,
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
            "user_role":       role,
            "is_super_admin":  role == "super_admin",
            "is_admin":        role in ("admin", "super_admin"),
            "is_assistant":    role == "assistant",
            "is_client":       role == "client",
        }

    return {}


def trading_accounts_sidebar_context(request):
    if not request.user.is_authenticated:
        return {"show_trading_accounts_sidebar": False}
    if not show_trading_accounts_sidebar(request):
        return {"show_trading_accounts_sidebar": False}
    owner = get_principal_owner(request)
    return {
        "show_trading_accounts_sidebar": True,
        "sidebar_trading_accounts": TradingAccount.objects.filter(owner=owner).order_by(
            "name", "account_number"
        ),
        "selected_trading_account_id": resolve_selected_trading_account_id(request, owner),
        "can_manage_trading_accounts": can_manage_trading_accounts(request),
    }