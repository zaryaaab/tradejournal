from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q, Sum, Count, Avg
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from datetime import date, datetime
from io import StringIO
import calendar
import pandas as pd

from .access import (
    SELECTED_TRADING_ACCOUNT_SESSION_KEY,
    can_manage_trading_accounts,
    can_view_trading_accounts,
    get_principal_owner,
    resolve_selected_trading_account_id,
    show_trading_accounts_sidebar,
    user_ids_in_principal_org,
)
from .models import Trade, TradingAccount, JournalEntry, Screenshot, UserProfile
from .queries import trades_for_owner
from .time_utils import ensure_chicago_datetime, trade_calendar_date
from .trade_lifecycle import (
    NormalizedFill,
    parse_broker_pnl_cell,
    parse_fill_qty,
    persist_round_trip,
    process_fills_for_symbol,
    rithmic_side_to_is_buy,
    rithmic_symbol_and_mult,
    tradovate_side_to_is_buy,
    tradovate_symbol_and_mult,
)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def _get_admin_user(request):
    """Principal owner for trades/accounts (alias for shared access helper)."""
    return get_principal_owner(request)


def _admin_org_user_ids(request) -> set[int] | None:
    """Principal org member user ids, or None if caller is not a non-super admin."""
    try:
        role = request.user.profile.role
    except Exception:
        return None
    if role == "super_admin":
        return None
    if role != "admin":
        return None
    principal = get_principal_owner(request)
    return user_ids_in_principal_org(principal)


def _admin_may_edit_managed_user(request, target_profile) -> bool:
    if request.user.profile.role == "super_admin":
        return True
    if request.user.profile.role != "admin":
        return False
    org_ids = _admin_org_user_ids(request)
    if org_ids is None or target_profile.user_id not in org_ids:
        return False
    if target_profile.role == "super_admin":
        return False
    if target_profile.role == "admin":
        return False
    return target_profile.role in ("assistant", "client")


TRADING_ACCOUNTS_SORT_FIELDS = {
    "name": "name",
    "-name": "-name",
    "broker": "broker",
    "-broker": "-broker",
    "account_balance": "account_balance",
    "-account_balance": "-account_balance",
    "created_at": "created_at",
    "-created_at": "-created_at",
    "status": "status",
    "-status": "-status",
    "account_number": "account_number",
    "-account_number": "-account_number",
}


def _safe_redirect_path(request, raw_next: str) -> str:
    raw_next = (raw_next or "").strip()
    if not raw_next.startswith("/"):
        return reverse("dashboard")
    allowed = {request.get_host()}
    allowed.update(settings.ALLOWED_HOSTS)
    allowed.discard("*")
    if url_has_allowed_host_and_scheme(
        url=raw_next,
        allowed_hosts=allowed,
        require_https=request.is_secure(),
    ):
        return raw_next
    return reverse("dashboard")


@login_required
@require_POST
def select_trading_account(request):
    if not show_trading_accounts_sidebar(request):
        return redirect("dashboard")
    owner = get_principal_owner(request)
    next_url = _safe_redirect_path(request, request.POST.get("next", ""))
    aid = request.POST.get("trading_account_id", "").strip()
    if aid in ("", "all"):
        request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)
    else:
        try:
            iid = int(aid)
        except ValueError:
            return redirect(next_url)
        if TradingAccount.objects.filter(
            pk=iid, owner=owner, is_archived=False
        ).exists():
            request.session[SELECTED_TRADING_ACCOUNT_SESSION_KEY] = iid
        else:
            request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)
    return redirect(next_url)


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


def _user_can_reset_target_password(request, target_profile) -> bool:
    """Super admin: any user. Admin: assistants/clients in the same principal org."""
    try:
        role = request.user.profile.role
    except Exception:
        return False
    if role == "super_admin":
        return True
    if role == "admin":
        return _admin_may_edit_managed_user(request, target_profile)
    return False


# ─────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────

@login_required
def dashboard(request):
    owner = _get_admin_user(request)
    selected_aid = resolve_selected_trading_account_id(request, owner)

    month = request.GET.get("month")
    year  = request.GET.get("year")

    if month and year:
        month = int(month)
        year  = int(year)
    else:
        today = timezone.localdate()
        month = today.month
        year  = today.year

    if selected_aid is not None:
        account_obj = TradingAccount.objects.filter(pk=selected_aid, owner=owner).first()
    else:
        account_obj = TradingAccount.objects.filter(
            owner=owner, is_archived=False
        ).first()
    account_balance = account_obj.account_balance if account_obj else 50000.0

    owner_trades = trades_for_owner(owner, selected_aid)
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
    for seq, (_, row) in enumerate(all_rows.iterrows()):
        symbol, mult = rithmic_symbol_and_mult(row["Symbol"])
        ts = ensure_chicago_datetime(row["Update Time (CST)"])
        is_buy = rithmic_side_to_is_buy(row["Buy/Sell"])
        qty = parse_fill_qty(row["Qty To Fill"])
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
            seq=seq,
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
    for seq, (_, row) in enumerate(rows.iterrows()):
        symbol, mult = tradovate_symbol_and_mult(row["Product"])
        ts = ensure_chicago_datetime(row["Fill Time"])
        is_buy = tradovate_side_to_is_buy(row["B/S"])
        qty = parse_fill_qty(row["filledQty"])
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
            seq=seq,
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

def _csv_text_from_upload(csv_file) -> str:
    raw = csv_file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("latin-1")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _get_or_create_import_account(request, owner, account_number: str, name: str, broker: str):
    """
    Find or create a trading account for CSV import.
    Returns (account, None) or (None, error_message).
    """
    an = str(account_number).strip()
    if not an:
        return None, "Skipped rows with an empty Account id."
    account = (
        TradingAccount.objects.filter(owner=owner, account_number=an)
        .order_by("id")
        .first()
    )
    if account is not None:
        if account.is_archived:
            return None, (
                f'Account "{an}" is archived. Unarchive it on the Accounts page '
                "before importing to it again."
            )
        if account.uploaded_by_id != request.user.id:
            account.uploaded_by = request.user
            account.save(update_fields=["uploaded_by"])
        return account, None
    try:
        account = TradingAccount.objects.create(
            owner=owner,
            account_number=an,
            name=name,
            broker=broker,
            user=owner,
            uploaded_by=request.user,
            status=TradingAccount.STATUS_ACTIVE,
            is_archived=False,
        )
    except IntegrityError:
        return None, (
            f'Could not create account for "{an}" — that name may already be in use '
            "for another active account. Rename the existing account or fix the CSV."
        )
    return account, None


@login_required
def upload_csv(request):
    role = getattr(getattr(request.user, "profile", None), "role", "client")

    # Only admin, super_admin, and assistant can upload
    if role == "client":
        messages.error(request, "You do not have permission to upload files.")
        return redirect("dashboard")

    owner = _get_admin_user(request)

    if request.method == "POST":
        csv_file = request.FILES.get("file")
        if not csv_file:
            messages.error(request, "No file selected.")
            return redirect("upload_csv")

        try:
            text = _csv_text_from_upload(csv_file)
        except Exception:
            messages.error(request, "Could not read the uploaded file.")
            return redirect("upload_csv")

        import_errors: list[str] = []
        any_import_ok = False

        try:
            if "Completed Orders" in text:
                df = load_rithmic_csv(text)
                for account_number, group in df.groupby("Account"):
                    an = str(account_number).strip()
                    account, err = _get_or_create_import_account(
                        request,
                        owner,
                        an,
                        f"Account {an}",
                        "Rithmic",
                    )
                    if err:
                        import_errors.append(err)
                        continue
                    parse_rithmic(group.copy(), account)
                    any_import_ok = True

            elif "B/S" in text[:500]:
                df = pd.read_csv(StringIO(text))
                for account_number, group in df.groupby("Account"):
                    an = str(account_number).strip()
                    account, err = _get_or_create_import_account(
                        request,
                        owner,
                        an,
                        f"Account {an}",
                        "Tradovate",
                    )
                    if err:
                        import_errors.append(err)
                        continue
                    parse_tradovate(group.copy(), account)
                    any_import_ok = True

            elif "buyPrice" in text and "sellPrice" in text:
                df = pd.read_csv(StringIO(text))
                account, err = _get_or_create_import_account(
                    request,
                    owner,
                    "performance",
                    "Performance Import",
                    "Tradovate",
                )
                if err:
                    messages.error(request, err)
                    return redirect("upload_csv")
                parse_performance(df.copy(), account)
                any_import_ok = True

            else:
                messages.error(request, "Unrecognized CSV format.")
                return redirect("upload_csv")
        except Exception:
            messages.error(
                request,
                "Import failed while processing this file. Check the format or re-export from your broker.",
            )
            return redirect("upload_csv")

        if any_import_ok:
            messages.success(request, "CSV imported successfully.")
            for msg in dict.fromkeys(import_errors):
                messages.warning(request, msg)
            return redirect("dashboard")

        if import_errors:
            messages.error(request, "No data was imported.")
            for msg in dict.fromkeys(import_errors):
                messages.warning(request, msg)
        else:
            messages.warning(
                request,
                "No trades were imported from this file (check account numbers and file contents).",
            )
        return redirect("upload_csv")

    return render(request, "upload.html")


# ─────────────────────────────────────────
# DAY VIEW
# ─────────────────────────────────────────

@login_required
def day_view(request, date):
    owner = _get_admin_user(request)
    selected_aid = resolve_selected_trading_account_id(request, owner)

    day = datetime.strptime(date, "%Y-%m-%d").date()

    trades = (
        trades_for_owner(owner, selected_aid)
        .filter(date=day)
        .select_related("account")
        .prefetch_related("screenshots")
    )

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
    owner = _get_admin_user(request)
    selected_aid = resolve_selected_trading_account_id(request, owner)
    user_trades = trades_for_owner(owner, selected_aid)

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

    if selected_aid is not None:
        account_obj = TradingAccount.objects.filter(pk=selected_aid, owner=owner).first()
    else:
        account_obj = TradingAccount.objects.filter(
            owner=owner, is_archived=False
        ).first()
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
# TRADING ACCOUNTS (management page)
# ─────────────────────────────────────────


@login_required
def trading_accounts_list(request):
    if not can_view_trading_accounts(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    manage = can_manage_trading_accounts(request)
    owner = _get_admin_user(request)
    qs = TradingAccount.objects.filter(owner=owner)

    show_archived = request.GET.get("archived") == "1"
    if not show_archived:
        qs = qs.filter(is_archived=False)

    q_raw = request.GET.get("q", "").strip()
    if q_raw:
        qs = qs.filter(
            Q(name__icontains=q_raw)
            | Q(account_number__icontains=q_raw)
            | Q(broker__icontains=q_raw)
        )

    broker_f = request.GET.get("broker", "").strip()
    if broker_f:
        qs = qs.filter(broker=broker_f)

    st = request.GET.get("status", "").strip()
    valid_status = {c[0] for c in TradingAccount.STATUS_CHOICES}
    if st in valid_status:
        qs = qs.filter(status=st)

    tag_f = request.GET.get("tag", "").strip()
    if tag_f:
        qs = qs.filter(tags__contains=[tag_f])

    sort_param = request.GET.get("sort", "name")
    order = TRADING_ACCOUNTS_SORT_FIELDS.get(sort_param, "name")
    qs = qs.order_by(order, "id")

    try:
        per_page = int(request.GET.get("per_page", 15) or 15)
    except ValueError:
        per_page = 15
    per_page = max(10, min(per_page, 20))

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))

    brokers_qs = (
        TradingAccount.objects.filter(owner=owner)
        .values_list("broker", flat=True)
        .distinct()
        .order_by("broker")
    )

    tag_choices: list[str] = []
    for row in (
        TradingAccount.objects.filter(owner=owner)
        .values_list("tags", flat=True)
        .iterator()
    ):
        if isinstance(row, list):
            for t in row:
                if isinstance(t, str) and t.strip() and t not in tag_choices:
                    tag_choices.append(t)
    tag_choices.sort(key=str.lower)

    params = request.GET.copy()
    params.pop("page", None)
    querystring = params.urlencode()

    return render(
        request,
        "trading_accounts_list.html",
        {
            "page_obj": page_obj,
            "broker_choices": brokers_qs,
            "tag_choices": tag_choices,
            "status_choices": TradingAccount.STATUS_CHOICES,
            "current_q": q_raw,
            "current_broker": broker_f,
            "current_status": st,
            "current_tag": tag_f,
            "current_sort": sort_param,
            "show_archived": show_archived,
            "per_page": per_page,
            "querystring": querystring,
            "can_manage_trading_accounts": manage,
        },
    )


@login_required
def edit_trading_account(request, account_id):
    if not can_manage_trading_accounts(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    owner = _get_admin_user(request)
    account = get_object_or_404(TradingAccount, pk=account_id, owner=owner)
    valid_status = {c[0] for c in TradingAccount.STATUS_CHOICES}

    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:100]
        broker = request.POST.get("broker", "").strip()[:50]
        account_number = request.POST.get("account_number", "").strip()[:100]
        if not all([name, broker, account_number]):
            messages.error(request, "Name, broker, and account number are required.")
        else:
            try:
                bal = float(request.POST.get("account_balance", "") or 0)
            except ValueError:
                messages.error(request, "Invalid balance.")
            else:
                new_status = request.POST.get("status", "").strip()
                if new_status in valid_status:
                    account.status = new_status
                tags_raw = request.POST.get("tags", "").strip()
                tag_list = [t.strip() for t in tags_raw.split(",") if t.strip()][:30]
                account.name = name
                account.broker = broker
                account.account_number = account_number
                account.account_balance = bal
                account.tags = tag_list
                try:
                    account.save()
                except IntegrityError:
                    messages.error(
                        request,
                        "Another active account already uses this name for your organization.",
                    )
                else:
                    messages.success(request, "Account updated.")
                    return redirect("trading_accounts_list")

    return render(
        request,
        "edit_trading_account.html",
        {
            "account": account,
            "status_choices": TradingAccount.STATUS_CHOICES,
        },
    )


@login_required
@require_POST
def archive_trading_account(request, account_id):
    if not can_manage_trading_accounts(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    owner = _get_admin_user(request)
    account = get_object_or_404(TradingAccount, pk=account_id, owner=owner)
    account.is_archived = not account.is_archived
    account.archived_at = timezone.now() if account.is_archived else None
    account.save(update_fields=["is_archived", "archived_at"])

    sel = request.session.get(SELECTED_TRADING_ACCOUNT_SESSION_KEY)
    if sel == account.id:
        request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)

    if account.is_archived:
        messages.success(request, f'Account "{account.name}" archived.')
    else:
        messages.success(request, f'Account "{account.name}" unarchived.')

    return redirect("trading_accounts_list")


# ─────────────────────────────────────────
# RETIRE TRADING ACCOUNT (blown + archive, no data removal)
# ─────────────────────────────────────────

@login_required
def retire_trading_account(request, account_id):
    if not can_manage_trading_accounts(request):
        messages.error(request, "Only administrators can retire accounts.")
        return redirect("dashboard")

    owner = _get_admin_user(request)
    account = get_object_or_404(TradingAccount, id=account_id, owner=owner)

    if request.method == "POST":
        if (request.POST.get("confirm_retire") or "").strip() != "RETIRE":
            messages.error(request, "Type RETIRE exactly to confirm.")
            return redirect("retire_trading_account", account_id=account_id)
        sel = request.session.get(SELECTED_TRADING_ACCOUNT_SESSION_KEY)
        if sel == account.id:
            request.session.pop(SELECTED_TRADING_ACCOUNT_SESSION_KEY, None)
        UserProfile.objects.filter(default_import_trading_account=account).update(
            default_import_trading_account=None
        )
        UserProfile.objects.filter(last_import_trading_account=account).update(
            last_import_trading_account=None
        )
        account.status = TradingAccount.STATUS_BLOWN
        account.is_archived = True
        account.archived_at = timezone.now()
        account.save(update_fields=["status", "is_archived", "archived_at"])
        messages.success(
            request,
            "Account retired (marked Blown and archived). All trades and history stay in the journal for reporting.",
        )
        return redirect("trading_accounts_list")

    trades = Trade.objects.filter(account=account).order_by("-date", "-entry_time")
    trade_count = trades.count()
    total_pnl = round(trades.aggregate(t=Sum("pnl"))["t"] or 0, 2)
    return render(
        request,
        "retire_trading_account_confirm.html",
        {
            "account": account,
            "trades": trades,
            "trade_count": trade_count,
            "total_pnl": total_pnl,
        },
    )


@login_required
def change_password(request):
    """Password changes are handled on User Management; keep URL as a redirect."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")
    return redirect(f"{reverse('user_management')}#your-password")

# ─────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────

@login_required
def user_management(request):
    """Admin-only: list all users created under this admin."""
    if not _require_admin(request):
        messages.error(request, "Access denied.")
        return redirect("dashboard")

    password_form = PasswordChangeForm(request.user)
    if request.method == "POST" and request.POST.get("action") == "change_own_password":
        password_form = PasswordChangeForm(request.user, request.POST)
        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password was successfully updated!")
            return redirect(f"{reverse('user_management')}#your-password")
        for error in password_form.errors.values():
            messages.error(request, error)

    principal = get_principal_owner(request)
    org_ids = user_ids_in_principal_org(principal)
    base = (
        UserProfile.objects.filter(user_id__in=org_ids)
        .exclude(user=request.user)
        .select_related("user", "created_by")
    )
    if request.user.profile.role == "super_admin":
        managed_users = base.order_by("role", "user__username")
    else:
        managed_users = base.exclude(role="super_admin").order_by(
            "role", "user__username"
        )

    context = {
        "managed_users": managed_users,
        "password_form": password_form,
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

    if not _admin_may_edit_managed_user(request, target_profile):
        messages.error(request, "You cannot edit this user.")
        return redirect("user_management")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_details":
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            username = request.POST.get("username", "").strip()
            email = request.POST.get("email", "").strip()
            if not first_name:
                messages.error(request, "First name is required.")
            elif not username:
                messages.error(request, "Username is required.")
            elif User.objects.filter(username=username).exclude(pk=target_user.pk).exists():
                messages.error(request, f"Username '{username}' is already taken.")
            else:
                target_user.first_name = first_name[:150]
                target_user.last_name = last_name[:150]
                target_user.username = username[:150]
                target_user.email = email[:254]
                target_user.save()
                messages.success(request, "Name, username, and email updated.")

        elif action == "change_role":
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
            if not _user_can_reset_target_password(request, target_profile):
                messages.error(request, "You cannot reset this user's password.")
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
        "can_reset_password": _user_can_reset_target_password(request, target_profile),
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

    if request.user.profile.role != "super_admin":
        org_ids = _admin_org_user_ids(request)
        if (
            org_ids is None
            or target_profile.user_id not in org_ids
            or target_profile.role not in ("assistant", "client")
        ):
            messages.error(request, "You cannot delete this user.")
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