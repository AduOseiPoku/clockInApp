from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from .models import SchoolSetting, get_default_canteen_fee, get_overdue_debtors_summary, OVERDUE_DEBT_DAYS_THRESHOLD


def school_settings(request):
    """
    Context processor to inject dynamic school settings, today's date,
    and overdue debtor alert summary (for Admin/Bursar) into all templates.
    """
    all_settings = SchoolSetting.get_all_settings_dict()
    school_name = all_settings.get('SCHOOL_NAME') or getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School')
    currency_symbol = all_settings.get('CURRENCY_SYMBOL') or getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')
    period = all_settings.get('CURRENT_ACADEMIC_PERIOD') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    term_start = all_settings.get('TERM_START_DATE')
    term_end = all_settings.get('TERM_END_DATE')

    raw_canteen = all_settings.get('DEFAULT_CANTEEN_FEE')
    try:
        default_canteen = Decimal(str(raw_canteen)) if raw_canteen is not None else get_default_canteen_fee()
    except Exception:
        default_canteen = get_default_canteen_fee()

    overdue_summary = {
        'total_debtors_count': 0,
        'total_debt_amount': Decimal('0.00'),
        'overdue_debtors_count': 0,
        'overdue_debt_amount': Decimal('0.00'),
    }
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        profile = getattr(user, 'profile', None)
        if user.is_superuser or (profile and profile.can_view_revenue):
            try:
                overdue_summary = get_overdue_debtors_summary()
            except Exception:
                pass

    return {
        'SCHOOL_NAME': school_name,
        'CURRENCY_SYMBOL': currency_symbol,
        'CURRENT_ACADEMIC_PERIOD': period,
        'DEFAULT_CANTEEN_FEE': default_canteen,
        'TERM_START_DATE': term_start,
        'TERM_END_DATE': term_end,
        'TODAY': timezone.localdate(),
        'OVERDUE_DEBT_DAYS_THRESHOLD': OVERDUE_DEBT_DAYS_THRESHOLD,
        'DEBT_ALERT_SUMMARY': overdue_summary,
    }
