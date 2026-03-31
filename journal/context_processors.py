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