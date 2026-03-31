from django.db import models
from django.contrib.auth.models import User


class TradingAccount(models.Model):
    # ── owner is ALWAYS the admin/super-admin (Dana).
    #    Trades uploaded by an Assistant are still stored under Dana's account.
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="owned_accounts",
        null=True, blank=True,          # nullable so existing rows don't break
    )

    # legacy field kept for migration safety — no longer used for filtering
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="uploaded_accounts",
        null=True, blank=True,
    )

    # who physically uploaded the CSV (assistant or admin) — audit trail only
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="csv_uploads",
        null=True, blank=True,
    )

    name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=100)
    broker = models.CharField(max_length=50)

    # Used to calculate daily/monthly return percentages on the calendar
    account_balance = models.FloatField(default=50000.0)

    def __str__(self):
        return f"{self.name} ({self.account_number})"


class Trade(models.Model):
    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE)

    date = models.DateField()
    entry_time = models.DateTimeField()
    exit_time = models.DateTimeField()

    symbol = models.CharField(max_length=20)
    side = models.CharField(max_length=10)  # B or S

    quantity = models.IntegerField()

    entry_price = models.FloatField()
    exit_price = models.FloatField()

    pnl = models.FloatField()

    is_auto_liq = models.BooleanField(default=False)

    # Per-trade journal fields (filled in manually on day view)
    is_a_plus = models.BooleanField(default=False)
    htf_confirmation = models.BooleanField(default=False)
    displacement = models.BooleanField(default=False)
    fresh_ifvg = models.BooleanField(default=False)
    entry_fill_50 = models.BooleanField(default=False)
    ignition_move = models.BooleanField(default=False)

    stop_points = models.FloatField(null=True, blank=True)
    target_points = models.FloatField(null=True, blank=True)

    # WIN / LOSS / BE
    result = models.CharField(max_length=10, blank=True)

    # Trade Grade
    GRADE_CHOICES = [
        ("A+", "A+"),
        ("A",  "A"),
        ("B",  "B"),
        ("C",  "C"),
        ("F",  "F"),
    ]
    trade_grade = models.CharField(max_length=2, choices=GRADE_CHOICES, blank=True)

    emotion = models.CharField(max_length=100, blank=True)
    trade_notes = models.TextField(blank=True)

    risk_per_trade = models.FloatField(null=True, blank=True)

    STRATEGY_CHOICES = [
        ("ignition",               "Ignition"),
        ("vwap_rejection",         "VWAP Rejection"),
        ("momentum_continuation",  "Momentum Continuation"),
        ("opening_drive",          "Opening Drive"),
        ("liquidity_sweep",        "Liquidity Sweep"),
        ("ifvg",                   "IFVG"),
        ("fvg",                    "FVG"),
        ("ob",                     "Order Block"),
        ("bos",                    "Break of Structure"),
        ("vwap",                   "VWAP Reclaim"),
        ("other",                  "Other"),
    ]
    strategy_setup = models.CharField(max_length=30, choices=STRATEGY_CHOICES, blank=True)

    TIME_OF_DAY_CHOICES = [
        ("open",        "Open (9:30–10:00)"),
        ("mid_morning", "Mid-Morning (10:00–11:30)"),
        ("lunch",       "Lunch (11:30–13:00)"),
        ("afternoon",   "Afternoon (13:00–15:30)"),
        ("close",       "Close (15:30–16:00)"),
    ]
    time_of_day_category = models.CharField(max_length=20, choices=TIME_OF_DAY_CHOICES, blank=True)

    def __str__(self):
        return f"{self.date} {self.symbol} {self.side} pnl={self.pnl}"


class Screenshot(models.Model):
    """Screenshot attached to a specific trade."""
    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name="screenshots", null=True, blank=True)
    image = models.ImageField(upload_to="screenshots/")
    caption = models.CharField(max_length=200, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True, null=True)

    def __str__(self):
        return f"Screenshot for {self.trade}"


class JournalEntry(models.Model):

    BIAS_CHOICES = [
        ("bullish", "Bullish"),
        ("bearish", "Bearish"),
        ("range", "Range"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    date = models.DateField()

    class Meta:
        unique_together = ["user", "date"]

    # --- Pre-trade prep ---
    session_bias = models.CharField(max_length=20, choices=BIAS_CHOICES, blank=True)
    htf_bias_reason = models.TextField(blank=True)
    a_plus_setup = models.BooleanField(default=False)

    # Timeframes checked
    tf_1m = models.BooleanField(default=False)
    tf_15m = models.BooleanField(default=False)
    tf_30m = models.BooleanField(default=False)
    tf_60m = models.BooleanField(default=False)
    tf_4h = models.BooleanField(default=False)

    # Setup checklist
    vwap_confirmed = models.BooleanField(default=False)
    ema_confluence = models.BooleanField(default=False)
    sma_confluence = models.BooleanField(default=False)
    trend_aligned = models.BooleanField(default=False)
    above_8_20_50 = models.BooleanField(default=False)
    stop_loss_placed = models.BooleanField(default=False)

    # Key levels
    globex_high = models.FloatField(null=True, blank=True)
    globex_low = models.FloatField(null=True, blank=True)
    asia_high = models.FloatField(null=True, blank=True)
    asia_low = models.FloatField(null=True, blank=True)
    london_high = models.FloatField(null=True, blank=True)
    london_low = models.FloatField(null=True, blank=True)
    key_support = models.FloatField(null=True, blank=True)
    key_resistance = models.FloatField(null=True, blank=True)

    # --- End of day review ---
    how_day_turned_out = models.TextField(blank=True)

    # Mistake tracker
    mistake_chased = models.BooleanField(default=False)
    mistake_revenge = models.BooleanField(default=False)
    mistake_countertrend = models.BooleanField(default=False)
    mistake_broke_time_rules = models.BooleanField(default=False)
    mistake_skipped_aplus = models.BooleanField(default=False)
    mistake_traded_news = models.BooleanField(default=False)

    # Emotional check-in (1-10)
    emotion_fear = models.IntegerField(null=True, blank=True)
    emotion_confidence = models.IntegerField(null=True, blank=True)
    emotion_focus = models.IntegerField(null=True, blank=True)
    emotion_discipline = models.IntegerField(null=True, blank=True)
    dominant_emotion = models.CharField(max_length=100, blank=True)
    followed_plan = models.BooleanField(default=False)
    improvement_tomorrow = models.TextField(blank=True)

    notes = models.TextField(blank=True)

    SESSION_TIME_CHOICES = [
        ("asia",     "Asia"),
        ("london",   "London"),
        ("new_york", "New York"),
        ("midday",   "Midday"),
    ]

    QUALITY_CHOICES = [
        ("A", "A – Perfect discipline"),
        ("B", "B – Minor mistakes"),
        ("C", "C – Emotional trading"),
    ]

    session_time       = models.CharField(max_length=20, choices=SESSION_TIME_CHOICES, blank=True)
    instruments_traded = models.CharField(max_length=200, blank=True)

    emotion_before      = models.CharField(max_length=150, blank=True)
    emotion_after       = models.CharField(max_length=150, blank=True)
    followed_risk_rules = models.BooleanField(null=True, blank=True)
    stopped_at_max_loss = models.BooleanField(null=True, blank=True)

    what_did_well = models.TextField(blank=True)
    mistakes_made = models.TextField(blank=True)

    trade_quality_rating = models.CharField(max_length=1, choices=QUALITY_CHOICES, blank=True)

    def __str__(self):
        return f"{self.user.username} - {self.date}"


# ─────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────

from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):

    ROLE_CHOICES = [
        ("super_admin", "Super Admin"),
        ("admin",       "Admin"),
        ("assistant",   "Assistant"),
        ("client",      "Client"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    role = models.CharField(max_length=15, choices=ROLE_CHOICES, default="client")

    # Which super-admin/admin "owns" this user (set when admin creates the account)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="created_users",
    )

    def is_super_admin(self):
        return self.role == "super_admin"

    def is_admin(self):
        return self.role in ("admin", "super_admin")

    def is_assistant(self):
        return self.role == "assistant"

    def is_client(self):
        return self.role == "client"

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, "profile"):
        instance.profile.save()