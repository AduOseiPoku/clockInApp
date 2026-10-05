from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from .models import SchoolSetting, get_default_canteen_fee

def school_settings(request):
    """
    Context processor to inject dynamic school settings and today's date into all templates.
    """
    school_name = SchoolSetting.get_setting('SCHOOL_NAME') or getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School')
    currency_symbol = SchoolSetting.get_setting('CURRENCY_SYMBOL') or getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')
    period = SchoolSetting.get_setting('CURRENT_ACADEMIC_PERIOD') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')

    return {
        'SCHOOL_NAME': school_name,
        'CURRENCY_SYMBOL': currency_symbol,
        'CURRENT_ACADEMIC_PERIOD': period,
        'DEFAULT_CANTEEN_FEE': get_default_canteen_fee(),
        'TODAY': timezone.localdate(),
    }
