from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib import messages
from .models import UserProfile


# ─────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return _role_redirect(user)
        else:
            messages.error(request, "Invalid username or password.")

    return render(request, "login.html")


# ─────────────────────────────────────────
# SIGNUP
# ─────────────────────────────────────────
'''
def signup_view(request):
    if request.user.is_authenticated:
        return _role_redirect(request.user)

    if request.method == "POST":
        first_name = request.POST.get("first_name", "").strip()
        last_name  = request.POST.get("last_name", "").strip()
        username   = request.POST.get("username", "").strip()
        email      = request.POST.get("email", "").strip()
        password1  = request.POST.get("password1", "")
        password2  = request.POST.get("password2", "")
        role       = request.POST.get("role", "client")

        # ── Validation ──────────────────────────────────────────────
        if not all([first_name, username, email, password1, password2]):
            messages.error(request, "Please fill in all required fields.")
            return render(request, "signup.html")

        if password1 != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "signup.html")

        if len(password1) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return render(request, "signup.html")

        if User.objects.filter(username=username).exists():
            messages.error(request, "That username is already taken.")
            return render(request, "signup.html")

        if User.objects.filter(email=email).exists():
            messages.error(request, "An account with that email already exists.")
            return render(request, "signup.html")

        if role not in ("admin", "client", "assistant"):
            role = "client"

        # ── Create user ─────────────────────────────────────────────
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password1,
            first_name=first_name,
            last_name=last_name,
        )

        # UserProfile is auto-created by signal; just set the role
        profile = user.profile
        profile.role = role
        profile.save()

        login(request, user)
        messages.success(request, f"Welcome, {first_name}! Your account has been created.")
        return _role_redirect(user)

    return render(request, "signup.html")
'''

# ─────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────

def logout_view(request):
    logout(request)
    return redirect("login")


# ─────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────

def _role_redirect(user):
    """Send user to the right landing page based on their role."""
    try:
        role = user.profile.role
    except UserProfile.DoesNotExist:
        role = "client"

    if role == "admin":
        return redirect("dashboard")   # define in urls.py
    return redirect("dashboard")