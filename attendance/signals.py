from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from .audit import log_activity
from .models import FeePayment, apply_payment_to_wallet, reverse_payment_wallet_effects

@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    ip = ''
    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')
    
    role = getattr(user.profile, 'role', 'Staff') if hasattr(user, 'profile') else 'Staff'
    log_activity(
        action='LOGIN',
        target_model='User',
        target_id=str(user.id),
        target_repr=user.username,
        description=f"User @{user.username} ({role}) signed in successfully.",
        actor=user,
        ip_address=ip
    )

@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    if user and user.is_authenticated:
        log_activity(
            action='LOGOUT',
            target_model='User',
            target_id=str(user.id),
            target_repr=user.username,
            description=f"User @{user.username} signed out.",
            actor=user
        )

@receiver(user_login_failed)
def log_user_login_failed(sender, credentials, request, **kwargs):
    attempted_username = credentials.get('username', 'unknown')
    ip = ''
    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')

    log_activity(
        action='FAILED_LOGIN',
        target_model='User',
        target_repr=attempted_username,
        description=f"Failed sign-in attempt for username '@{attempted_username}'.",
        ip_address=ip
    )


@receiver(post_save, sender=FeePayment)
def sync_wallet_on_payment_save(sender, instance, created, **kwargs):
    """Apply newly created cash/MoMo payments to the student's explicit wallet."""
    if created:
        apply_payment_to_wallet(instance)


@receiver(pre_delete, sender=FeePayment)
def reverse_wallet_on_payment_delete(sender, instance, **kwargs):
    """Reverse any wallet top-up or deduction effects before a FeePayment is deleted."""
    reverse_payment_wallet_effects(instance)
