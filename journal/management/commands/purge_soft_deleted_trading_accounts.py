from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from journal.models import TradingAccount


class Command(BaseCommand):
    help = (
        "Permanently delete trading accounts that were soft-deleted at least 7 days ago "
        "(and their trades via CASCADE)."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=7)
        qs = TradingAccount.all_objects.filter(
            deleted_at__isnull=False,
            deleted_at__lte=cutoff,
        )
        n = qs.count()
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Purged {n} soft-deleted trading account(s)."))
