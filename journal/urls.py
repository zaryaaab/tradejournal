from django.urls import path
from . import views
from . import auth_views

urlpatterns = [

    # ── Auth ─────────────────────────────────────────────────────────
    path("login/",  auth_views.login_view,  name="login"),
    #path("signup/", auth_views.signup_view, name="signup"),
    path("logout/", auth_views.logout_view, name="logout"),

    # ── Core app ─────────────────────────────────────────────────────
    path("",                views.dashboard,  name="dashboard"),
    path("upload/",         views.upload_csv, name="upload_csv"),
    path(
        "select-trading-account/",
        views.select_trading_account,
        name="select_trading_account",
    ),
    path("day/<str:date>/", views.day_view,   name="day_view"),

    # ── Analytics ────────────────────────────────────────────────────
    path("analytics/",      views.analytics,  name="analytics"),

    # ── Trading account management ───────────────────────────────────
    path("accounts/", views.trading_accounts_list, name="trading_accounts_list"),
    path(
        "accounts/<int:account_id>/edit/",
        views.edit_trading_account,
        name="edit_trading_account",
    ),
    path(
        "accounts/<int:account_id>/archive/",
        views.archive_trading_account,
        name="archive_trading_account",
    ),
    path(
        "accounts/<int:account_id>/retire/",
        views.retire_trading_account,
        name="retire_trading_account",
    ),
    path('change-password/', views.change_password, name='change_password'),

    # ── User management (admin only) ─────────────────────────────────
    path("users/",                       views.user_management, name="user_management"),
    path("users/create/",                views.create_user,     name="create_user"),
    path("users/<int:user_id>/edit/",    views.edit_user,       name="edit_user"),
    path("users/<int:user_id>/delete/",  views.delete_user,     name="delete_user"),
]