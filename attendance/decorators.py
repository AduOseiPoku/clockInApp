from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse
from django.conf import settings

def login_required_ajax(view_func):
    """
    Ensures user is authenticated. Returns 401/403 JsonResponse if AJAX,
    otherwise redirects to LOGIN_URL.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                return JsonResponse({'success': False, 'error': 'Authentication required. Please log in.'}, status=401)
            messages.warning(request, "Please sign in to access that page.")
            return redirect(settings.LOGIN_URL)
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def principal_required(view_func):
    """
    Restricts view access to the Principal / School Administrator (or superuser).
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, "Please sign in to access that page.")
            return redirect(settings.LOGIN_URL)
        
        profile = getattr(request.user, 'profile', None)
        if not (profile and profile.is_principal):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Principal privileges required.'}, status=403)
            messages.error(request, "Access denied: Principal administrator privileges required.")
            return redirect('dashboard')
            
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def finance_required(view_func):
    """
    Restricts view access to staff with financial authority (Principal, Bursar/Accountant).
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.warning(request, "Please sign in to access that page.")
            return redirect(settings.LOGIN_URL)
        
        profile = getattr(request.user, 'profile', None)
        if not (profile and profile.can_view_revenue):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Financial authority required.'}, status=403)
            messages.error(request, "Access denied: You do not have permission to access financial records.")
            return redirect('dashboard')
            
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def payment_recording_required(view_func):
    """
    Restricts view access to staff permitted to record payments (Teachers and Principal).
    Explicitly excludes Bursar/Accountant, whose role is auditing and revenue review.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                return JsonResponse({'success': False, 'error': 'Authentication required.'}, status=401)
            messages.warning(request, "Please sign in to record payments.")
            return redirect(settings.LOGIN_URL)
        
        profile = getattr(request.user, 'profile', None)
        if not (profile and profile.can_record_payments):
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
                return JsonResponse({
                    'success': False,
                    'error': 'Payment recording disabled: Fee recording permission required.'
                }, status=403)
            if profile and profile.role == profile.ROLE_TEACHER:
                messages.warning(request, "You are not authorized to collect or record fee payments. Please contact the Principal if you require fee collection access.")
            else:
                messages.warning(request, "The Bursar account is designated for revenue review and auditing. Direct payment recording is handled by authorized teachers.")
            return redirect('payment_list')
            
        return view_func(request, *args, **kwargs)
    return _wrapped_view
