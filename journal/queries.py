"""
Shared querysets so calendar, day view, and analytics use the same trade scope.
"""
from django.db.models import QuerySet

from .models import Trade


def trades_for_owner(owner) -> QuerySet:
    """
    All trades for accounts owned by the principal user (dashboard, day list, analytics).
    """
    return Trade.objects.filter(account__owner=owner)
