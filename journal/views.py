from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from datetime import date, datetime
from io import StringIO
import calendar
import pandas as pd

from .models import Trade, TradingAccount, JournalEntry, Screenshot, UserProfile
from .queries import trades_for_owner
from .time_utils import ensure_chicago_datetime, trade_calendar_date
from .trade_lifecycle import (
    NormalizedFill,
    parse_broker_pnl_cell,
    persist_round_trip,
    process_fills_for_symbol,
    rithmic_symbol_and_mult,
    tradovate_symbol_and_mult,
)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def _get_admin_user(request):
    """
    Return the 'owner' user whose trades should be displayed.
    - Super Admin → themselves (superadmin)
    - Admin → superadmin (so admins see all trades)
    - Assistant / Client → find the super_admin who created them
    """
    try:
        role = request.user.profile.role
    except Exception:
        role = "client"

    # Super admin sees their own trades
    if role == "super_admin":
        return request.user

    # Admin should see superadmin's trades (so they see everything)
    if role == "admin":
        # Get the first super_admin user
        super_admin_user = (
            UserProfile.objects.filter(role="super_admin")
            .select_related("user")
            .first()
        )
        if super_admin_user:
            return super_admin_user.user

        # Fallback: return request user if no superadmin exists
        return request.user

    # For assistant / client: use the super_admin who created them
    try:
        creator = request.user.profile.created_by
        if creator:
            # If creator is admin, find the superadmin
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

    # Fallback: first super_admin in the system
    super_admin = (
        UserProfile.objects.filter(role="super_admin")
        .select_related("user")
        .first()
    )
    if super_admin:
        return super_admin.user

    # Last resort: first admin
    admin = (
        UserProfile.objects.filter(role="admin")
        .select_related("user")
        .first()
    )
    return admin.user if admin else request.user



def _require_admin(request):
    """Return True if user is admin or super_admin."""
    try:
        return request.user.profile.role in ("admin", "super_admin")
    except Exception:
        return False


def _require_super_admin(request):
    try:
        return request.user.profile.role == "super_admin"
    except Exception:
        return False


# ─────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────

@login_required
def dashboard(request):
    role = getattr(getattr(request.user, "profile", None), "role", "client")

    # Assistants go straight to upload page — they have no dashboard
    if role == "assistant":
        return redirect("upload_csv")

    # The admin owner whose trades we display
    owner = _get_admin_user(request)

    month = request.GET.get("month")
    year  = request.GET.get("year")

    if month and year:
        month = int(month)
        year  = int(year)
    else:
        today = timezone.localdate()
        month = today.month
        year  = today.year

    # Account balance (first owned account, default 50k)
    account_obj     = TradingAccount.objects.filter(owner=owner).first()
    account_balance = account_obj.account_balance if account_obj else 50000.0

    # All daily summaries for calendar grid — same queryset as analytics / day view
    owner_trades = trades_for_owner(owner)
    trades = owner_trades.values("date").annotate(
        pnl=Sum("pnl"),
        trades=Count("id"),
        contracts=Sum("quantity"),
    )

    trade_map  = {t["date"]: t for t in trades}
    cal = calendar.Calendar(firstweekday=6)
    month_days = []

    for day in cal.itermonthdates(year, month):
        data    = trade_map.get(day)
        day_pnl = round(data["pnl"], 2) if data else 0

        pnl_pct = round((day_pnl / account_balance) * 100, 2) if (data and account_balance) else 0

        if data and day_pnl > 0:
            color_class = "win-strong" if pnl_pct > 1 else "win-light"
        elif data and day_pnl < 0:
            color_class = "loss"
        else:
            color_class = ""

        month_days.append({
            "date":          day,
            "pnl":           day_pnl,
            "pnl_pct":       pnl_pct,
            "color_class":   color_class,
            "trades":        data["trades"] if data else 0,
            "contracts":     data["contracts"] if data else 0,
            "current_month": day.month == month,
        })

    import calendar as cal_mod
    from datetime import date as date_cls

    monthly_days      = [d for d in month_days if d["current_month"] and d["trades"] > 0]
    monthly_pnl       = sum(d["pnl"] for d in monthly_days)
    win_days          = [d for d in monthly_days if d["pnl"] > 0]
    loss_days         = [d for d in monthly_days if d["pnl"] < 0]
    total_trade_days  = len(monthly_days)

    win_rate      = round((len(win_days) / total_trade_days) * 100) if total_trade_days else None
    gross_profit  = sum(d["pnl"] for d in win_days)
    gross_loss    = abs(sum(d["pnl"] for d in loss_days))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    monthly_return = round((monthly_pnl / account_balance) * 100, 2) if account_balance else None

    first_day    = date_cls(year, month, 1)
    last_day     = date_cls(year, month, cal_mod.monthrange(year, month)[1])
    total_trades = owner_trades.filter(date__range=(first_day, last_day)).count()

    if month == 1:
        prev_month, prev_year = 12, year - 1
    else:
        prev_month, prev_year = month - 1, year

    if month == 12:
        next_month, next_year = 1, year + 1
    else:
        next_month, next_year = month + 1, year

    context = {
        "month_days":      month_days,
        "month":           month,
        "year":            year,
        "prev_month":      prev_month,
        "prev_year":       prev_year,
        "next_month":      next_month,
        "next_year":       next_year,
        "monthly_pnl":     round(monthly_pnl, 2),
        "monthly_return":  monthly_return,
        "win_rate":        win_rate,
        "profit_factor":   profit_factor,
        "total_trades":    total_trades,
        "account_balance": account_balance,
        # Only show accounts list to admin roles
        "accounts": TradingAccount.objects.filter(owner=owner) if _require_admin(request) else [],
    }

    return render(request, "dashboard.html", context)


# ─────────────────────────────────────────
# CSV PARSING — RITHMIC
# ─────────────────────────────────────────


def load_rithmic_csv(text):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "Completed Orders":
            start = i + 1
            break
    if start is None:
        raise Exception("Completed Orders section not found")
    csv_data = "\n".join(lines[start:])
    df = pd.read_csv(StringIO(csv_data))
    return df



def parse_rithmic(df, account):
    print(f"\n{'='*60}")
    print(f"[RITHMIC PARSER] Account: {account}")
    print(f"[RITHMIC PARSER] Total rows: {len(df)}")
    print(f"{'='*60}")

    # Filter for filled orders
    filled = df[df["Status"].str.strip() == "Filled"].copy()

    auto_liq = filled[filled["Remarks"].str.contains("Auto Liquidation", na=False)].copy()
    rows = filled[~filled["Remarks"].str.contains("Auto Liquidation", na=False)].copy()

    print(f"[FILTER] Normal filled rows: {len(rows)}")
    print(f"[FILTER] Auto-liq rows: {len(auto_liq)}")

    # Combine and sort
    all_rows = pd.concat([rows, auto_liq]).sort_values("Update Time (CST)").reset_index(drop=True)

    print(f"\n[SORTED] All filled rows:")
    for idx, r in all_rows.iterrows():
        tag = " [AUTO-LIQ]" if "Auto Liquidation" in str(r.get("Remarks", "")) else ""
        print(f"  [{idx}] {r['Update Time (CST)']} | {r['Buy/Sell']} | {r['Qty To Fill']} x {r['Symbol']} @ {r['Avg Fill Price']}{tag}")

    print(f"\n[LIFECYCLE] Building fills by symbol (FIFO round trips)...")
    fills_by_sym = {}
    for _, row in all_rows.iterrows():
        symbol, mult = rithmic_symbol_and_mult(row["Symbol"])
        ts = ensure_chicago_datetime(row["Update Time (CST)"])
        side = row["Buy/Sell"].strip()
        is_buy = side in ("B", "Buy", "b", "buy")
        qty = int(row["Qty To Fill"])
        price = float(row["Avg Fill Price"])
        bpnl = parse_broker_pnl_cell(row.get("P&L", "") or row.get("PnL", ""))
        auto_liq = "Auto Liquidation" in str(row.get("Remarks", ""))
        nf = NormalizedFill(
            ts=ts,
            is_buy=is_buy,
            qty=qty,
            price=price,
            symbol=symbol,
            multiplier=mult,
            broker_pnl=bpnl,
            is_auto_liq=auto_liq,
        )
        fills_by_sym.setdefault(symbol, []).append(nf)

    trades_created = 0
    skipped = 0
    for sym, flist in sorted(fills_by_sym.items()):
        completed, tail_net = process_fills_for_symbol(flist)
        if tail_net != 0:
            print(
                f"  [WARN] {sym}: import ended with open net qty={tail_net} "
                f"(no round-trip row emitted for remainder)"
            )
        for c in completed:
            _, created = persist_round_trip(account, c)
            if created:
                trades_created += 1
                print(
                    f"  [SAVE] {sym} round-trip pnl={c.pnl} qty={c.quantity} "
                    f"{c.entry_time} → {c.exit_time}"
                )
            else:
                skipped += 1
                print(f"  [SKIP] Duplicate trade {sym} {c.entry_time}")

    print(f"\n{'='*60}")
    print(f"[RITHMIC PARSER] DONE — created={trades_created} skipped={skipped}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────
# CSV PARSING — TRADOVATE
# ─────────────────────────────────────────

def parse_tradovate(df, account):

    print(f"\n{'='*60}")
    print(f"[TRADOVATE PARSER] Account: {account}")
    print(f"[TRADOVATE PARSER] Total rows: {len(df)}")
    print(f"{'='*60}")

    # Only filled orders
    rows = df[df["Status"].str.strip() == "Filled"].copy()
    print(f"[FILTER] Filled rows: {len(rows)}")

    rows = rows.sort_values("Fill Time").reset_index(drop=True)

    print(f"\n[SORTED] All filled rows:")
    for idx, r in rows.iterrows():
        print(f"  [{idx}] {r['Fill Time']} | {r['B/S'].strip()} | {r['filledQty']} x {r['Product']} @ {r['avgPrice']} | PnL: {r.get('pnl', 'N/A')}")

    print(f"\n[LIFECYCLE] Building fills by symbol (FIFO round trips)...")
    fills_by_sym = {}
    for _, row in rows.iterrows():
        symbol, mult = tradovate_symbol_and_mult(row["Product"])
        ts = ensure_chicago_datetime(row["Fill Time"])
        side = row["B/S"].strip()
        is_buy = side == "Buy"
        qty = int(row["filledQty"])
        price = float(row["avgPrice"])
        bpnl = parse_broker_pnl_cell(row.get("pnl", ""))
        nf = NormalizedFill(
            ts=ts,
            is_buy=is_buy,
            qty=qty,
            price=price,
            symbol=symbol,
            multiplier=mult,
            broker_pnl=bpnl,
            is_auto_liq=False,
        )
        fills_by_sym.setdefault(symbol, []).append(nf)

    trades_created = 0
    skipped = 0
    for sym, flist in sorted(fills_by_sym.items()):
        completed, tail_net = process_fills_for_symbol(flist)
        if tail_net != 0:
            print(
                f"  [WARN] {sym}: import ended with open net qty={tail_net} "
                f"(no round-trip row emitted for remainder)"
            )
        for c in completed:
            _, created = persist_round_trip(account, c)
            if created:
                trades_created += 1
                print(
                    f"  [SAVE] {sym} round-trip pnl={c.pnl} qty={c.quantity} "
                    f"{c.entry_time} → {c.exit_time}"
                )
            else:
                skipped += 1
                print(f"  [SKIP] Duplicate trade {sym} {c.entry_time}")

    print(f"\n{'='*60}")
    print(f"[TRADOVATE PARSER] DONE — created={trades_created} skipped={skipped}")
    print(f"{'='*60}\n")

def parse_performance(df, account):

    print("\n[PERFORMANCE PARSER] Starting...")
    print(f"[DEBUG] DataFrame shape: {df.shape}")
    print(f"[DEBUG] Columns: {list(df.columns)}")
    print("-" * 80)

    trades_created = 0

    for idx, row in df.iterrows():
        print(f"\n[DEBUG] Processing row {idx}:")
        print(f"[DEBUG] Raw row data:")
        print(f"  - symbol: {row['symbol']}")
        print(f"  - buyPrice: {row['buyPrice']}")
        print(f"  - sellPrice: {row['sellPrice']}")
        print(f"  - qty: {row['qty']}")
        print(f"  - pnl RAW: '{row['pnl']}' (type: {type(row['pnl'])})")

        entry_price = float(row["buyPrice"])
        exit_price  = float(row["sellPrice"])
        qty         = int(row["qty"])
        symbol      = str(row["symbol"]).strip()

        # FIXED: Handle CSV PnL with parentheses for negative values
        raw_pnl = str(row["pnl"]).strip()
        print(f"[DEBUG PnL] Step 1 - Raw string: '{raw_pnl}'")

        # Check if it's negative - handle both ($100.00) and $(100.00) formats
        is_negative = False
        if raw_pnl.startswith("(") and raw_pnl.endswith(")"):
            is_negative = True
        elif raw_pnl.startswith("$(") and raw_pnl.endswith(")"):
            is_negative = True
        print(f"[DEBUG PnL] Step 2 - Is negative?: {is_negative}")

        # Remove $, commas, and parentheses
        cleaned = raw_pnl.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
        print(f"[DEBUG PnL] Step 3 - Cleaned string: '{cleaned}'")

        try:
            pnl = float(cleaned)
            print(f"[DEBUG PnL] Step 4 - Parsed float: {pnl}")
            if is_negative:
                pnl = -pnl
                print(f"[DEBUG PnL] Step 5 - Applied negative sign: {pnl}")
            else:
                print(f"[DEBUG PnL] Step 5 - Keeping positive: {pnl}")
        except Exception as e:
            print(f"[DEBUG PnL] ERROR parsing: {e}")
            pnl = 0.0
            print(f"[DEBUG PnL] Set to 0.0")

        print(f"[DEBUG PnL] FINAL PnL value: {pnl}")

        # Determine side from timestamps: whichever happened first is the entry
        bought_ts = pd.to_datetime(row["boughtTimestamp"])
        sold_ts   = pd.to_datetime(row["soldTimestamp"])

        if bought_ts <= sold_ts:
            # Bought first → long trade
            side = "B"
            entry_raw = bought_ts
            exit_raw  = sold_ts
        else:
            # Sold first → short trade
            side = "S"
            entry_raw = sold_ts
            exit_raw  = bought_ts

        print(f"[DEBUG] Original times - entry: {entry_raw}, exit: {exit_raw}")

        entry_time = ensure_chicago_datetime(entry_raw)
        exit_time = ensure_chicago_datetime(exit_raw)
        journal_date = trade_calendar_date(entry_time)

        print(f"[DEBUG] Timezone aware - entry: {entry_time}, exit: {exit_time}")
        print(f"[DEBUG] Journal date (CST): {journal_date}")
        print(f"[DEBUG] Determined side: {side}")

        # Check if trade already exists
        print(f"[DEBUG] Checking for existing trade...")

        trade, created = Trade.objects.get_or_create(
            account=account,
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            defaults={
                "side": side,
                "quantity": qty,
                "pnl": pnl,
                "date": journal_date,
                "is_auto_liq": False,
            },
        )

        if created:
            trades_created += 1
            print(f"[DEBUG] ✓ Trade CREATED with pnl={pnl}")
        else:
            print(f"[DEBUG] ✗ Trade ALREADY EXISTS, pnl would be: {pnl} (existing: {trade.pnl})")

    print("\n" + "=" * 80)
    print(f"[PERFORMANCE PARSER] DONE — created={trades_created} out of {len(df)} rows")
    print("=" * 80)
# ─────────────────────────────────────────
# UPLOAD CSV
# ─────────────────────────────────────────

@login_required
def upload_csv(request):
    role = getattr(getattr(request.user, "profile", None), "role", "client")

    # Only admin, super_admin, and assistant can upload
    if role == "client":
        messages.error(request, "You do not have permission to upload files.")
        return redirect("dashboard")

    if request.method == "POST":
        csv_file = request.FILES.get("file")
        if not csv_file:
            messages.error(request, "No file selected.")
            return render(request, "upload.html")

        text = csv_file.read().decode("utf-8")

        # ── Determine the OWNER of these trades ─────────────────────
        # Always the admin/super_admin, never the assistant themselves.
        owner = _get_admin_user(request)

        if "Completed Orders" in text:
            df = load_rithmic_csv(text)
            for account_number, group in df.groupby("Account"):
                account, _ = TradingAccount.objects.get_or_create(
                    owner=owner,
                    account_number=account_number,
                    defaults={
                        "name":        f"Account {account_number}",
                        "broker":      "Rithmic",
                        "user":        owner,          # legacy field
                        "uploaded_by": request.user,
                    },
                )
                # Update uploaded_by on re-uploads
                if account.uploaded_by != request.user:
                    account.uploaded_by = request.user
                    account.save()
                parse_rithmic(group.copy(), account)

        elif "B/S" in text[:500]:
            df = pd.read_csv(StringIO(text))
            for account_number, group in df.groupby("Account"):
                account, _ = TradingAccount.objects.get_or_create(
                    owner=owner,
                    account_number=account_number,
                    defaults={
                        "name":        f"Account {account_number}",
                        "broker":      "Tradovate",
                        "user":        owner,
                        "uploaded_by": request.user,
                    },
                )
                if account.uploaded_by != request.user:
                    account.uploaded_by = request.user
                    account.save()
                parse_tradovate(group.copy(), account)

        elif "buyPrice" in text and "sellPrice" in text:
            df = pd.read_csv(StringIO(text))
            account, _ = TradingAccount.objects.get_or_create(
                owner=owner,
                account_number="performance",
                defaults={
                    "name":        "Performance Import",
                    "broker":      "Tradovate",
                    "user":        owner,
                    "uploaded_by": request.user,
                },
            )
            parse_performance(df.copy(), account)

        else:
            messages.error(request, "Unrecognized CSV format.")
            return render(request, "upload.html")

        messages.success(request, "CSV imported successfully.")
        return redirect("dashboard")

    return render(request, "upload.html")


# ─────────────────────────────────────────
# DAY VIEW
# ─────────────────────────────────────────

@login_required
def day_view(request, date):
    role  = getattr(getattr(request.user, "profile", None), "role", "client")
    owner = _get_admin_user(request)

    # Assistants cannot access day view
    if role == "assistant":
        return redirect("upload_csv")

    day = datetime.strptime(date, "%Y-%m-%d").date()

    trades = trades_for_owner(owner).filter(date=day).prefetch_related("screenshots")

    # Journal entry always belongs to the admin user (Dana)
    journal, _ = JournalEntry.objects.get_or_create(
        user=owner,
        date=day,
    )

    if request.method == "POST":
        # Only admins can save
        if not _require_admin(request):
            messages.error(request, "You do not have permission to save journal entries.")
            return redirect("day_view", date=date)

        # Pre-trade prep
        journal.session_bias    = request.POST.get("session_bias", "")
        journal.htf_bias_reason = request.POST.get("htf_bias_reason", "")
        journal.a_plus_setup    = bool(request.POST.get("a_plus_setup"))

        journal.tf_1m  = bool(request.POST.get("tf_1m"))
        journal.tf_15m = bool(request.POST.get("tf_15m"))
        journal.tf_30m = bool(request.POST.get("tf_30m"))
        journal.tf_60m = bool(request.POST.get("tf_60m"))
        journal.tf_4h  = bool(request.POST.get("tf_4h"))

        journal.vwap_confirmed  = bool(request.POST.get("vwap_confirmed"))
        journal.ema_confluence  = bool(request.POST.get("ema_confluence"))
        journal.sma_confluence  = bool(request.POST.get("sma_confluence"))
        journal.trend_aligned   = bool(request.POST.get("trend_aligned"))
        journal.above_8_20_50   = bool(request.POST.get("above_8_20_50"))
        journal.stop_loss_placed = bool(request.POST.get("stop_loss_placed"))

        journal.globex_high   = request.POST.get("globex_high") or None
        journal.globex_low    = request.POST.get("globex_low") or None
        journal.asia_high     = request.POST.get("asia_high") or None
        journal.asia_low      = request.POST.get("asia_low") or None
        journal.london_high   = request.POST.get("london_high") or None
        journal.london_low    = request.POST.get("london_low") or None
        journal.key_support   = request.POST.get("key_support") or None
        journal.key_resistance = request.POST.get("key_resistance") or None

        journal.how_day_turned_out = request.POST.get("how_day_turned_out", "")

        journal.mistake_chased           = bool(request.POST.get("mistake_chased"))
        journal.mistake_revenge          = bool(request.POST.get("mistake_revenge"))
        journal.mistake_countertrend     = bool(request.POST.get("mistake_countertrend"))
        journal.mistake_broke_time_rules = bool(request.POST.get("mistake_broke_time_rules"))
        journal.mistake_skipped_aplus    = bool(request.POST.get("mistake_skipped_aplus"))
        journal.mistake_traded_news      = bool(request.POST.get("mistake_traded_news"))

        journal.emotion_fear       = request.POST.get("emotion_fear") or None
        journal.emotion_confidence = request.POST.get("emotion_confidence") or None
        journal.emotion_focus      = request.POST.get("emotion_focus") or None
        journal.emotion_discipline = request.POST.get("emotion_discipline") or None
        journal.dominant_emotion   = request.POST.get("dominant_emotion", "")
        journal.followed_plan      = (
            bool(request.POST.get("followed_plan")) or
            request.POST.get("followed_plan_radio") == "yes"
        )
        journal.improvement_tomorrow = request.POST.get("improvement_tomorrow", "")
        journal.notes                = request.POST.get("notes", "")

        journal.session_time       = request.POST.get("session_time", "")
        journal.instruments_traded = request.POST.get("instruments_traded", "")

        def resolve_emotion(field, custom_field):
            custom   = request.POST.get(custom_field, "").strip()
            dropdown = request.POST.get(field, "").strip()
            return custom if custom else dropdown

        journal.emotion_before = resolve_emotion("emotion_before", "emotion_before_custom")
        journal.emotion_after  = resolve_emotion("emotion_after",  "emotion_after_custom")

        def tri_bool(key):
            val = request.POST.get(key)
            if val == "yes":  return True
            if val == "no":   return False
            return None

        journal.followed_risk_rules = tri_bool("followed_risk_rules")
        journal.stopped_at_max_loss = tri_bool("stopped_at_max_loss")

        journal.what_did_well        = request.POST.get("what_did_well", "")
        journal.mistakes_made        = request.POST.get("mistakes_made", "")
        journal.trade_quality_rating = request.POST.get("trade_quality_rating", "")

        journal.save()

        # Per-trade fields
        for trade in trades:
            prefix = f"trade_{trade.id}_"
            trade.is_a_plus            = bool(request.POST.get(f"{prefix}is_a_plus"))
            trade.htf_confirmation     = bool(request.POST.get(f"{prefix}htf_confirmation"))
            trade.displacement         = bool(request.POST.get(f"{prefix}displacement"))
            trade.fresh_ifvg           = bool(request.POST.get(f"{prefix}fresh_ifvg"))
            trade.entry_fill_50        = bool(request.POST.get(f"{prefix}entry_fill_50"))
            trade.ignition_move        = bool(request.POST.get(f"{prefix}ignition_move"))
            trade.stop_points          = request.POST.get(f"{prefix}stop_points") or None
            trade.target_points        = request.POST.get(f"{prefix}target_points") or None
            trade.result               = request.POST.get(f"{prefix}result", "")
            trade.trade_grade          = request.POST.get(f"{prefix}trade_grade", "")
            trade.emotion              = request.POST.get(f"{prefix}emotion", "")
            trade.trade_notes          = request.POST.get(f"{prefix}trade_notes", "")
            trade.risk_per_trade       = request.POST.get(f"{prefix}risk_per_trade") or None
            trade.strategy_setup       = request.POST.get(f"{prefix}strategy_setup", "")
            trade.time_of_day_category = request.POST.get(f"{prefix}time_of_day_category", "")
            trade.save()

            screenshot_file = request.FILES.get(f"{prefix}screenshot")
            if screenshot_file:
                Screenshot.objects.create(
                    trade=trade,
                    image=screenshot_file,
                    caption=request.POST.get(f"{prefix}screenshot_caption", "")
                )

        return redirect("day_view", date=date)

    EMOTION_CHOICES = [
        ("calm",          "Calm & focused"),
        ("confident",     "Confident"),
        ("anxious",       "Anxious"),
        ("fearful",       "Fearful"),
        ("fomo",          "FOMO"),
        ("overconfident", "Overconfident"),
        ("frustrated",    "Frustrated"),
        ("disciplined",   "Disciplined"),
        ("impulsive",     "Impulsive"),
        ("neutral",       "Neutral"),
    ]
    emotion_choice_values = [v for v, _ in EMOTION_CHOICES]

    context = {
        "date":                  day,
        "trades":                trades,
        "journal":               journal,
        "emotion_choices":       EMOTION_CHOICES,
        "emotion_choice_values": emotion_choice_values,
        "grade_choices":         ["A+", "A", "B", "C", "F"],
        # Clients see trades but NOT journal notes
        "show_journal":          _require_admin(request),
    }

    return render(request, "day.html", context)


# ─────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────

@login_required
def analytics(request):
    role = getattr(getattr(request.user, "profile", None), "role", "client")
    if role == "assistant":
        return redirect("upload_csv")

    owner = _get_admin_user(request)
    user_trades = trades_for_owner(owner)

    total_trades = user_trades.count()
    if total_trades == 0:
        return render(request, "analytics.html", {"no_data": True})

    winning_trades = user_trades.filter(pnl__gt=0)
    losing_trades  = user_trades.filter(pnl__lt=0)

    total_pnl    = round(user_trades.aggregate(t=Sum("pnl"))["t"] or 0, 2)
    win_count    = winning_trades.count()
    loss_count   = losing_trades.count()
    win_rate     = round((win_count / total_trades) * 100, 1) if total_trades else 0

    avg_win  = round(winning_trades.aggregate(a=Avg("pnl"))["a"] or 0, 2)
    avg_loss = round(losing_trades.aggregate(a=Avg("pnl"))["a"] or 0, 2)

    gross_profit  = winning_trades.aggregate(s=Sum("pnl"))["s"] or 0
    gross_loss    = abs(losing_trades.aggregate(s=Sum("pnl"))["s"] or 0)
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    daily    = user_trades.values("date").annotate(day_pnl=Sum("pnl")).order_by("day_pnl")
    best_day  = daily.last()
    worst_day = daily.first()

    setup_stats = []
    for key, label in Trade.STRATEGY_CHOICES:
        setup_trades = user_trades.filter(strategy_setup=key)
        count        = setup_trades.count()
        if count == 0:
            continue
        s_pnl     = round(setup_trades.aggregate(s=Sum("pnl"))["s"] or 0, 2)
        s_wins    = setup_trades.filter(pnl__gt=0).count()
        s_avg_pnl = round(setup_trades.aggregate(a=Avg("pnl"))["a"] or 0, 2)
        s_winrate = round((s_wins / count) * 100, 1)
        setup_stats.append({
            "label":     label,
            "count":     count,
            "total_pnl": s_pnl,
            "avg_pnl":   s_avg_pnl,
            "win_rate":  s_winrate,
        })
    setup_stats.sort(key=lambda x: x["total_pnl"], reverse=True)

    grade_stats = []
    for key, label in Trade.GRADE_CHOICES:
        grade_trades = user_trades.filter(trade_grade=key)
        count        = grade_trades.count()
        if count == 0:
            continue
        g_pnl     = round(grade_trades.aggregate(s=Sum("pnl"))["s"] or 0, 2)
        g_winrate = round((grade_trades.filter(pnl__gt=0).count() / count) * 100, 1)
        grade_stats.append({
            "label":     label,
            "count":     count,
            "total_pnl": g_pnl,
            "win_rate":  g_winrate,
        })

    account_obj     = TradingAccount.objects.filter(owner=owner).first()
    account_balance = account_obj.account_balance if account_obj else 50000.0
    total_return    = round((total_pnl / account_balance) * 100, 2) if account_balance else None

    context = {
        "no_data":        False,
        "total_trades":   total_trades,
        "total_pnl":      total_pnl,
        "total_return":   total_return,
        "win_rate":       win_rate,
        "win_count":      win_count,
        "loss_count":     loss_count,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "profit_factor":  profit_factor,
        "best_day":       best_day,
        "worst_day":      worst_day,
        "setup_stats":    setup_stats,
        "grade_stats":    grade_stats,
        "account_balance": account_balance,
    }
    return render(request, "analytics.html", context)


# ─────────────────────────────────────────
# DELETE TRADING ACCOUNT
# ─────────────────────────────────────────

@login_required
def delete_account(request, account_id):
    if not _require_admin(request):
        messages.error(request, "Only admins can delete accounts.")
        return redirect("dashboard")

    owner   = _get_admin_user(request)
    account = get_object_or_404(TradingAccount, id=account_id, owner=owner)

    if request.method == "POST":
        account.delete()
        return redirect("dashboard")

    trades = Trade.objects.filter(account=account).order_by("-date", "-entry_time")
    return render(request, "delete_account_confirm.html", {
        "account":     account,
        "trades":      trades,
        "trade_count": trades.count(),
    })


@login_required
def change_password(request):
    # ONLY super admin can change their own password
    if not _require_super_admin(request):
        messages.error(request, "Only Super Administrators can change passwords.")
        return redirect("dashboard")

    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # Keep user logged in
            messages.success(request, "Your password was successfully updated!")
            return redirect("user_management")
        else:
            for error in form.errors.values():
                messages.error(request, error)
    else:
        form = PasswordChangeForm(request.user)

    return render(request, "change_password.html", {"form": form})

# ─────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────

@login_required
def user_management(request):
    """Admin-only: list all users created under this admin."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    owner = _get_admin_user(request)

    # Super admin sees ALL users; admin sees users they created
    if request.user.profile.role == "super_admin":
        managed_users = UserProfile.objects.exclude(
            user=request.user
        ).select_related("user", "created_by").order_by("role", "user__username")
    else:
        managed_users = UserProfile.objects.filter(
            created_by=request.user
        ).select_related("user").order_by("role", "user__username")

    context = {
        "managed_users": managed_users,
        "accounts":      TradingAccount.objects.filter(owner=owner),
    }
    return render(request, "user_management.html", context)


@login_required
def create_user(request):
    """Admin creates a new assistant or client account."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    if request.method == "POST":
        first_name = request.POST.get("first_name", "").strip()
        last_name  = request.POST.get("last_name", "").strip()
        username   = request.POST.get("username", "").strip()
        email      = request.POST.get("email", "").strip()
        password   = request.POST.get("password", "")
        role       = request.POST.get("role", "client")

        # Super admin can create any role; regular admin can only create assistant/client
        allowed_roles = (
            ["admin", "assistant", "client"]
            if request.user.profile.role == "super_admin"
            else ["assistant", "client"]
        )

        if role not in allowed_roles:
            messages.error(request, "You cannot assign that role.")
            return render(request, "create_user.html", {"allowed_roles": allowed_roles})

        if not all([first_name, username, password]):
            messages.error(request, "First name, username and password are required.")
            return render(request, "create_user.html", {"allowed_roles": allowed_roles})

        if len(password) < 6:
            messages.error(request, "Password must be at least 6 characters.")
            return render(request, "create_user.html", {"allowed_roles": allowed_roles})

        if User.objects.filter(username=username).exists():
            messages.error(request, f"Username '{username}' is already taken.")
            return render(request, "create_user.html", {"allowed_roles": allowed_roles})

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        profile            = user.profile
        profile.role       = role
        profile.created_by = request.user
        profile.save()

        messages.success(request, f"User '{username}' created successfully as {role.title()}.")
        return redirect("user_management")

    allowed_roles = (
        ["admin", "assistant", "client"]
        if request.user.profile.role == "super_admin"
        else ["assistant", "client"]
    )
    return render(request, "create_user.html", {"allowed_roles": allowed_roles})


@login_required
def edit_user(request, user_id):
    """Admin edits role or resets password for a managed user."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    target_user    = get_object_or_404(User, id=user_id)
    target_profile = target_user.profile

    # Admins can only edit users they created; super_admin can edit anyone
    if request.user.profile.role != "super_admin":
        if target_profile.created_by != request.user:
            messages.error(request, "You can only edit users you created.")
            return redirect("user_management")

        # Regular admins cannot reset passwords (only change roles)
        if request.method == "POST" and request.POST.get("action") == "reset_password":
            messages.error(request, "Only Super Administrators can reset passwords.")
            return redirect("user_management")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "change_role":
            new_role = request.POST.get("role", "client")
            allowed_roles = (
                ["admin", "assistant", "client"]
                if request.user.profile.role == "super_admin"
                else ["assistant", "client"]
            )
            if new_role in allowed_roles:
                target_profile.role = new_role
                target_profile.save()
                messages.success(request, f"Role updated to {new_role.title()}.")
            else:
                messages.error(request, "Invalid role.")

        elif action == "reset_password":
            # Only super_admin can reset passwords
            if request.user.profile.role != "super_admin":
                messages.error(request, "Only Super Administrators can reset passwords.")
            else:
                new_password = request.POST.get("new_password", "")
                if len(new_password) < 6:
                    messages.error(request, "Password must be at least 6 characters.")
                else:
                    target_user.set_password(new_password)
                    target_user.save()
                    messages.success(request, f"Password for '{target_user.username}' has been reset.")

        return redirect("user_management")

    allowed_roles = (
        ["admin", "assistant", "client"]
        if request.user.profile.role == "super_admin"
        else ["assistant", "client"]
    )
    return render(request, "edit_user.html", {
        "target_user":    target_user,
        "target_profile": target_profile,
        "allowed_roles":  allowed_roles,
        "can_reset_password": request.user.profile.role == "super_admin",  # Add this flag
    })
@login_required
def delete_user(request, user_id):
    """Admin deletes a managed user account."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    target_user    = get_object_or_404(User, id=user_id)
    target_profile = target_user.profile

    # Prevent deleting yourself
    if target_user == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("user_management")

    # Super admin can delete any non-super-admin
    if request.user.profile.role != "super_admin":
        if target_profile.created_by != request.user:
            messages.error(request, "You can only delete users you created.")
            return redirect("user_management")

    if target_profile.role == "super_admin":
        messages.error(request, "Super Admin accounts cannot be deleted.")
        return redirect("user_management")

    if request.method == "POST":
        username = target_user.username
        target_user.delete()
        messages.success(request, f"User '{username}' has been deleted.")
        return redirect("user_management")

    return render(request, "delete_user_confirm.html", {
        "target_user":    target_user,
        "target_profile": target_profile,
    })