import contextvars
from django.utils.deprecation import MiddlewareMixin

_request_context = contextvars.ContextVar('audit_request_context', default=None)

class AuditContextMiddleware(MiddlewareMixin):
    """
    Middleware that captures the current user and remote IP address in a thread-safe context
    so log_activity can automatically attribute actions without requiring explicit request passing.
    """
    def process_request(self, request):
        ip = ''
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')

        user = getattr(request, 'user', None)
        _request_context.set({
            'user': user if user and user.is_authenticated else None,
            'ip': ip,
        })

    def process_response(self, request, response):
        _request_context.set(None)
        return response

def get_current_audit_context():
    return _request_context.get() or {}

def log_activity(action, target_model, description, target_id='', target_repr='', changes=None, actor=None, ip_address=None):
    """
    Unified helper to record an entry in AuditLog.
    Safe against exceptions so audit logging failures never break primary user actions.
    """
    from .models import AuditLog

    ctx = get_current_audit_context()
    user = actor or ctx.get('user')
    ip = ip_address or ctx.get('ip')

    username = ''
    role = ''
    if user and getattr(user, 'is_authenticated', False):
        username = user.username
        profile = getattr(user, 'profile', None)
        role = profile.get_role_display() if profile else ('Superuser' if user.is_superuser else 'Staff')
    else:
        username = 'SYSTEM'
        role = 'System Automation'

    try:
        return AuditLog.objects.create(
            actor=user if getattr(user, 'is_authenticated', False) else None,
            actor_username=username,
            actor_role=role,
            action=action,
            target_model=str(target_model),
            target_id=str(target_id)[:60],
            target_repr=str(target_repr)[:255],
            description=description,
            changes=changes,
            ip_address=ip or None,
        )
    except Exception as e:
        import logging
        logging.getLogger('attendance.audit').error(f"Failed to record audit log: {e}")
        return None
