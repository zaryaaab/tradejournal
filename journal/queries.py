"""
Shared querysets so calendar, day view, and analytics use the same trade scope.
"""
from typing import Optional

from django.contrib.auth.models import User
from django.db.models import QuerySet

from .models import Trade


def trades_for_owner(owner: User, trading_account_id: Optional[int] = None) -> QuerySet:
    """
    Trades for accounts owned by the principal user.
    If trading_account_id is set, restrict to that TradingAccount (must belong to owner).
    """
    qs = Trade.objects.filter(account__owner=owner)
    if trading_account_id is not None:
        qs = qs.filter(account_id=trading_account_id)
    return qs
