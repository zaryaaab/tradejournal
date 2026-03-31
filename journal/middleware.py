# middleware.py
from .models import UserProfile

class UserRoleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Code executed for each request before the view
        response = self.get_response(request)
        # Code executed for each request after the view
        return response

    def process_template_response(self, request, response):
        """
        This method is called for template responses.
        It adds user role information to the template context.
        """
        # Only add to authenticated users
        if request.user.is_authenticated and hasattr(response, 'context_data'):
            try:
                # Try to get the user's role from profile
                role = request.user.profile.role
            except (UserProfile.DoesNotExist, AttributeError):
                # Default to client if profile doesn't exist
                role = "client"
            
            # Add role and helper booleans to template context
            response.context_data['user_role'] = role
            response.context_data['is_admin'] = role == 'admin'
            response.context_data['is_assistant'] = role == 'assistant'
            response.context_data['is_client'] = role == 'client'
        
        return response