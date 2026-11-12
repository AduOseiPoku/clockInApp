import datetime
from functools import lru_cache
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.db.models import Sum, Q
from decimal import Decimal


def count_school_days_between(start_date, end_date):
    """
    Counts school days (Monday to Friday) strictly in range [start_date, end_date).
    Does not include end_date itself.
    """
    if not start_date or not end_date or start_date >= end_date:
        return 0
    days = 0
    cur = start_date
    while cur < end_date:
        if cur.weekday() < 5:
            days += 1
        cur += datetime.timedelta(days=1)
    return days


class Bus(models.Model):
    """
    Dynamic Bus/Route Model.
    Each bus has its own configurable fee rate set by the administrator.
    Example: 'Tema Bus' (GH₵80.00), 'Madina Bus' (GH₵50.00).
    """
    name = models.CharField(max_length=100, unique=True, help_text="e.g. 'Tema Bus', 'Madina Bus', 'Spintex Bus'")
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Fee charged for this bus route")
    route_description = models.CharField(max_length=255, blank=True, help_text="Major stops or pickup zones")
    driver_name = models.CharField(max_length=100, blank=True)
    driver_phone = models.CharField(max_length=30, blank=True)
    plate_number = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bus'
        verbose_name_plural = 'Buses'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{self.fee:.2f})"

    @property
    def student_count(self):
        return self.students.filter(is_active=True).count()

    @property
    def total_expected_revenue(self):
        return Decimal(self.student_count) * self.fee


class SchoolClass(models.Model):
    """
    School Class model managed by school administrators.
    e.g. 'Class 1A', 'Class 2B', 'Nursery 1', 'Grade 3'.
    """
    name = models.CharField(max_length=50, unique=True, help_text="e.g. 'Class 1A', 'Class 2B', 'Nursery 1'")
    grade_level = models.CharField(max_length=50, blank=True, help_text="Optional level group (e.g. Primary, JHS, Kindergarten)")
    is_active = models.BooleanField(default=True, help_text="Active classes appear in fee recording and attendance")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Class'
        verbose_name_plural = 'Classes'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def student_count(self):
        return self.students.filter(is_active=True).count()


class SchoolSetting(models.Model):
    """
    Key-value dynamic system configuration for school settings
    (e.g., DEFAULT_CANTEEN_FEE, CURRENT_ACADEMIC_PERIOD, SCHOOL_NAME, CURRENCY_SYMBOL).
    Enables administrators to adjust billing rates and school information directly from the web UI.
    """
    key = models.CharField(max_length=60, unique=True, help_text="Configuration key name")
    value = models.CharField(max_length=255, help_text="Configuration value")
    description = models.CharField(max_length=255, blank=True, help_text="Short explanation of what this controls")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'School Setting'
        verbose_name_plural = 'School Settings'
        ordering = ['key']

    def __str__(self):
        return f"{self.key} = {self.value}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.__class__.get_setting.cache_clear()
        self.__class__.get_all_settings_dict.cache_clear()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        self.__class__.get_setting.cache_clear()
        self.__class__.get_all_settings_dict.cache_clear()

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_settings_dict(cls):
        try:
            return {s.key: s.value for s in cls.objects.all()}
        except Exception:
            return {}

    @classmethod
    @lru_cache(maxsize=64)
    def get_setting(cls, key, default=None):
        try:
            item = cls.objects.filter(key=key).first()
            if item and item.value is not None:
                return item.value
        except Exception:
            pass
        return default

    @classmethod
    def set_setting(cls, key, value, description=""):
        cls.objects.update_or_create(
            key=key,
            defaults={'value': str(value), 'description': description}
        )
        cls.get_setting.cache_clear()


def get_default_canteen_fee():
    """Returns active default daily canteen fee (Decimal)."""
    val = SchoolSetting.get_setting('DEFAULT_CANTEEN_FEE')
    if val is not None:
        try:
            return Decimal(str(val))
        except Exception:
            pass
    return getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))


def get_active_academic_period():
    """
    Returns the active academic term (e.g., 'Term 1 - 2026').
    Checks SchoolSetting in the database first, then falls back to settings.py.
    """
    val = SchoolSetting.get_setting('CURRENT_ACADEMIC_PERIOD')
    if val:
        return str(val).strip()
    return getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')


def get_active_currency_symbol():
    """
    Returns the active school currency symbol (e.g., 'GH₵', '$').
    Checks SchoolSetting in the database first, then falls back to settings.py.
    """
    val = SchoolSetting.get_setting('CURRENCY_SYMBOL')
    if val:
        return str(val).strip()
    return getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')


def get_term_date_range():
    """
    Returns (start_date, end_date) as datetime.date objects for the active academic term,
    or (None, None) if not configured.
    """
    start_val = SchoolSetting.get_setting('TERM_START_DATE')
    end_val = SchoolSetting.get_setting('TERM_END_DATE')
    start_date = None
    end_date = None
    if start_val:
        try:
            start_date = datetime.date.fromisoformat(str(start_val).strip())
        except (ValueError, TypeError):
            pass
    if end_val:
        try:
            end_date = datetime.date.fromisoformat(str(end_val).strip())
        except (ValueError, TypeError):
            pass
    return start_date, end_date


def calculate_school_weekdays(start_date, end_date):
    """
    Returns the total count of instructional weekdays (Monday through Friday)
    between start_date and end_date inclusive.
    """
    if not start_date or not end_date or start_date > end_date:
        return 0
    cur = start_date
    weekdays = 0
    while cur <= end_date:
        if cur.weekday() < 5:  # Monday=0 ... Friday=4
            weekdays += 1
        cur += datetime.timedelta(days=1)
    return weekdays


def get_current_term_metrics():
    """
    Computes summary metrics for the active academic period timeframe:
    - start_date: datetime.date or None
    - end_date: datetime.date or None
    - total_days: integer count of instructional weekdays in the term
    - elapsed_days: weekdays passed so far
    - remaining_days: weekdays remaining until term end
    """
    start_date, end_date = get_term_date_range()
    today = timezone.localdate()
    total_days = calculate_school_weekdays(start_date, end_date) if (start_date and end_date) else 0

    if start_date and end_date:
        if today < start_date:
            elapsed_days = 0
            remaining_days = total_days
        elif today > end_date:
            elapsed_days = total_days
            remaining_days = 0
        else:
            elapsed_days = calculate_school_weekdays(start_date, today)
            remaining_days = calculate_school_weekdays(today, end_date)
    else:
        elapsed_days = 0
        remaining_days = 0

    return {
        'start_date': start_date,
        'end_date': end_date,
        'total_days': total_days,
        'elapsed_days': elapsed_days,
        'remaining_days': remaining_days,
    }


class Student(models.Model):
    """
    Student model.
    Enrolled with first name, last name, and class (no student ID required).
    Assigns each student to their designated bus or leaves unassigned (for walkers).
    """
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    student_class = models.CharField(max_length=50, help_text="e.g. 'Class 1A', 'Class 2B', 'Nursery 1'")
    school_class = models.ForeignKey(SchoolClass, on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    bus = models.ForeignKey(Bus, on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    canteen_enrolled = models.BooleanField(default=True, help_text="Whether student takes school lunch/canteen")
    custom_canteen_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Override standard canteen fee")
    bus_credit_wallet = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Available prepaid bus credit wallet balance")
    canteen_credit_wallet = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Available prepaid canteen credit wallet balance")
    is_active = models.BooleanField(default=True)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='registered_students',
        help_text="Staff member or administrator who enrolled this student"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.student_class:
            cls_obj = SchoolClass.objects.filter(name=self.student_class).first()
            if not cls_obj:
                cls_obj = SchoolClass.objects.create(name=self.student_class)
            self.school_class = cls_obj
        elif self.school_class:
            self.student_class = self.school_class.name
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'Student'
        verbose_name_plural = 'Students'
        ordering = ['student_class', 'first_name', 'last_name']
        indexes = [
            models.Index(fields=['student_class', 'is_active'], name='std_class_active_idx'),
            models.Index(fields=['bus', 'is_active'], name='std_bus_active_idx'),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.student_class})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self):
        f = self.first_name[0].upper() if self.first_name else ''
        l = self.last_name[0].upper() if self.last_name else ''
        return f"{f}{l}" or "ST"

    @property
    def bus_fee_required(self):
        if self.bus:
            return self.bus.fee
        return Decimal('0.00')

    @property
    def canteen_fee_required(self):
        if not self.canteen_enrolled:
            return Decimal('0.00')
        if self.custom_canteen_fee is not None:
            return self.custom_canteen_fee
        return get_default_canteen_fee()

    def get_daily_bus_paid(self, target_date=None):
        """Total bus payments recorded on target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        result = self.payments.filter(fee_type='BUS', payment_date=target_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_daily_canteen_paid(self, target_date=None):
        """Total canteen payments recorded on target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        result = self.payments.filter(fee_type='CANTEEN', payment_date=target_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_deducted_amount_for_date(self, fee_type, target_date=None):
        """Net prepaid wallet credit explicitly deducted for target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        qs = self.credit_transactions.filter(fee_type=fee_type, date=target_date)
        agg = qs.aggregate(
            ded=Sum('amount', filter=Q(transaction_type='DEDUCTION')),
            rev=Sum('amount', filter=Q(transaction_type='REVERSAL')),
        )
        net = (agg['ded'] or Decimal('0.00')) - (agg['rev'] or Decimal('0.00'))
        return max(Decimal('0.00'), net)

    def get_wallet_balance_as_of(self, fee_type, target_date=None):
        """Available prepaid wallet balance as of target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        txs = self.credit_transactions.filter(fee_type=fee_type, date__lte=target_date)
        if not txs.exists():
            live_wallet = self.bus_credit_wallet if fee_type == 'BUS' else self.canteen_credit_wallet
            return max(Decimal('0.00'), live_wallet or Decimal('0.00'))
        agg = txs.aggregate(
            top=Sum('amount', filter=Q(transaction_type='TOP_UP')),
            rev=Sum('amount', filter=Q(transaction_type='REVERSAL')),
            ded=Sum('amount', filter=Q(transaction_type='DEDUCTION')),
        )
        bal = (agg['top'] or Decimal('0.00')) + (agg['rev'] or Decimal('0.00')) - (agg['ded'] or Decimal('0.00'))
        return max(Decimal('0.00'), bal)

    def get_daily_bus_settled(self, target_date=None):
        """Total bus fare settled for target_date (direct payments + explicit wallet deductions)."""
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_daily_bus_paid(target_date) + self.get_deducted_amount_for_date('BUS', target_date)

    def get_daily_canteen_settled(self, target_date=None):
        """Total canteen fare settled for target_date (direct payments + explicit wallet deductions)."""
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_daily_canteen_paid(target_date) + self.get_deducted_amount_for_date('CANTEEN', target_date)

    def is_bus_settled_for_date(self, target_date=None):
        """True only when target_date's bus fee has been paid directly or explicitly deducted from wallet."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_daily_bus_settled(target_date) >= self.bus_fee_required

    def is_canteen_settled_for_date(self, target_date=None):
        """True only when target_date's canteen fee has been paid directly or explicitly deducted from wallet."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_daily_canteen_settled(target_date) >= self.canteen_fee_required

    def get_bus_credit_balance(self, target_date=None):
        """Available prepaid bus credit wallet balance as of target_date."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_wallet_balance_as_of('BUS', target_date)

    def get_canteen_credit_balance(self, target_date=None):
        """Available prepaid canteen credit wallet balance as of target_date."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        return self.get_wallet_balance_as_of('CANTEEN', target_date)

    def is_bus_paid_for_date(self, target_date=None):
        """True if bus fee for target_date is already settled OR fully covered by available wallet credit."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_bus_settled(target_date)
        if settled >= self.bus_fee_required:
            return True
        return (settled + self.get_bus_credit_balance(target_date)) >= self.bus_fee_required

    def is_canteen_paid_for_date(self, target_date=None):
        """True if canteen fee for target_date is already settled OR fully covered by available wallet credit."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_canteen_settled(target_date)
        if settled >= self.canteen_fee_required:
            return True
        return (settled + self.get_canteen_credit_balance(target_date)) >= self.canteen_fee_required

    def get_daily_bus_balance(self, target_date=None):
        """Cash top-up balance needed for target_date after direct payments, deductions, and wallet credit."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_bus_settled(target_date)
        wallet = self.get_bus_credit_balance(target_date)
        return max(Decimal('0.00'), self.bus_fee_required - settled - wallet)

    def get_daily_canteen_balance(self, target_date=None):
        """Cash top-up balance needed for target_date after direct payments, deductions, and wallet credit."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_canteen_settled(target_date)
        wallet = self.get_canteen_credit_balance(target_date)
        return max(Decimal('0.00'), self.canteen_fee_required - settled - wallet)

    def get_bus_credit_days(self, target_date=None):
        """Full school days of prepaid bus credit remaining after covering target_date."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return 0
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_bus_settled(target_date)
        owed_today = max(Decimal('0.00'), self.bus_fee_required - settled)
        wallet = self.get_bus_credit_balance(target_date)
        if wallet >= owed_today:
            remaining_after_today = wallet - owed_today
            return int(remaining_after_today // self.bus_fee_required)
        return 0

    def get_canteen_credit_days(self, target_date=None):
        """Full school days of prepaid canteen credit remaining after covering target_date."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return 0
        if target_date is None:
            target_date = timezone.localdate()
        settled = self.get_daily_canteen_settled(target_date)
        owed_today = max(Decimal('0.00'), self.canteen_fee_required - settled)
        wallet = self.get_canteen_credit_balance(target_date)
        if wallet >= owed_today:
            remaining_after_today = wallet - owed_today
            return int(remaining_after_today // self.canteen_fee_required)
        return 0

    def get_prepaid_status(self, target_date=None):
        """
        Summary of prepaid wallet credit, daily settlement state, and partial remainder top-up calculations.
        """
        if target_date is None:
            target_date = timezone.localdate()
        bus_credit = self.get_bus_credit_balance(target_date)
        bus_days = self.get_bus_credit_days(target_date)
        can_credit = self.get_canteen_credit_balance(target_date)
        can_days = self.get_canteen_credit_days(target_date)

        bus_settled_amt = self.get_daily_bus_settled(target_date)
        can_settled_amt = self.get_daily_canteen_settled(target_date)
        bus_owed_today = max(Decimal('0.00'), self.bus_fee_required - bus_settled_amt) if self.bus else Decimal('0.00')
        can_owed_today = max(Decimal('0.00'), self.canteen_fee_required - can_settled_amt) if self.canteen_enrolled else Decimal('0.00')

        bus_active = bool(self.bus and self.bus_fee_required > Decimal('0.00') and (bus_credit >= bus_owed_today and (bus_credit > Decimal('0.00') or self.get_deducted_amount_for_date('BUS', target_date) > Decimal('0.00'))))
        can_active = bool(self.canteen_enrolled and self.canteen_fee_required > Decimal('0.00') and (can_credit >= can_owed_today and (can_credit > Decimal('0.00') or self.get_deducted_amount_for_date('CANTEEN', target_date) > Decimal('0.00'))))

        bus_has_partial = bool(
            self.bus and
            bus_owed_today > Decimal('0.00') and
            Decimal('0.00') < bus_credit < bus_owed_today
        )
        bus_remainder_due = max(Decimal('0.00'), bus_owed_today - bus_credit) if bus_has_partial else Decimal('0.00')

        can_has_partial = bool(
            self.canteen_enrolled and
            can_owed_today > Decimal('0.00') and
            Decimal('0.00') < can_credit < can_owed_today
        )
        can_remainder_due = max(Decimal('0.00'), can_owed_today - can_credit) if can_has_partial else Decimal('0.00')

        has_credit = bus_active or can_active or bus_has_partial or can_has_partial
        is_expiring = (bus_active and bus_days <= 1) or (can_active and can_days <= 1) or bus_has_partial or can_has_partial

        return {
            'has_credit': has_credit,
            'bus_has_credit': bus_active,
            'bus_credit_balance': bus_credit,
            'bus_credit_days': bus_days,
            'bus_has_partial': bus_has_partial,
            'bus_remainder_due': bus_remainder_due,
            'canteen_has_credit': can_active,
            'canteen_credit_balance': can_credit,
            'canteen_credit_days': can_days,
            'canteen_has_partial': can_has_partial,
            'canteen_remainder_due': can_remainder_due,
            'is_expiring_soon': is_expiring,
        }

    def apply_credit_for_date(self, fee_type, target_date=None, actor=None, clock_in=None, max_amount=None):
        """
        Atomically deduct available wallet credit for fee_type on target_date.
        Returns Decimal amount deducted.
        """
        if target_date is None:
            target_date = timezone.localdate()
        with transaction.atomic():
            locked = Student.objects.select_for_update().get(pk=self.pk)
            daily_fee = locked.bus_fee_required if fee_type == 'BUS' else locked.canteen_fee_required
            if daily_fee <= Decimal('0.00'):
                return Decimal('0.00')
            settled = locked.get_daily_bus_settled(target_date) if fee_type == 'BUS' else locked.get_daily_canteen_settled(target_date)
            owed = max(Decimal('0.00'), daily_fee - settled)
            if owed <= Decimal('0.00'):
                return Decimal('0.00')
            wallet_field = 'bus_credit_wallet' if fee_type == 'BUS' else 'canteen_credit_wallet'
            wallet = max(Decimal('0.00'), getattr(locked, wallet_field) or Decimal('0.00'))
            if wallet <= Decimal('0.00'):
                return Decimal('0.00')
            deduct_amt = min(wallet, owed)
            if max_amount is not None:
                deduct_amt = min(deduct_amt, max_amount)
            if deduct_amt <= Decimal('0.00'):
                return Decimal('0.00')
            new_wallet = wallet - deduct_amt
            setattr(locked, wallet_field, new_wallet)
            locked.save(update_fields=[wallet_field])
            setattr(self, wallet_field, new_wallet)
            CreditTransaction.objects.create(
                student=locked,
                fee_type=fee_type,
                transaction_type=CreditTransaction.TYPE_DEDUCTION,
                amount=deduct_amt,
                balance_after=new_wallet,
                date=target_date,
                clock_in=clock_in,
                recorded_by=actor,
                notes=f"Wallet credit applied for {target_date}",
            )
            return deduct_amt

    def reverse_clockin_credit_deductions(self, target_date=None, clock_in=None, actor=None):
        """
        Atomically refund wallet deductions made on target_date (linked to clock_in if provided).
        Returns dict of refunded amounts per fee_type.
        """
        if target_date is None:
            target_date = timezone.localdate()
        refunded = {'BUS': Decimal('0.00'), 'CANTEEN': Decimal('0.00')}
        with transaction.atomic():
            locked = Student.objects.select_for_update().get(pk=self.pk)
            for ft in ('BUS', 'CANTEEN'):
                qs = locked.credit_transactions.filter(fee_type=ft, date=target_date)
                if clock_in is not None:
                    ded_sum = qs.filter(
                        Q(transaction_type=CreditTransaction.TYPE_DEDUCTION) & (Q(clock_in=clock_in) | Q(payment__isnull=True))
                    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
                else:
                    ded_sum = qs.filter(
                        transaction_type=CreditTransaction.TYPE_DEDUCTION,
                        payment__isnull=True,
                    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
                rev_sum = qs.filter(transaction_type=CreditTransaction.TYPE_REVERSAL).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
                net_to_refund = max(Decimal('0.00'), ded_sum - rev_sum)
                if net_to_refund > Decimal('0.00'):
                    wallet_field = 'bus_credit_wallet' if ft == 'BUS' else 'canteen_credit_wallet'
                    cur_wallet = max(Decimal('0.00'), getattr(locked, wallet_field) or Decimal('0.00'))
                    new_wallet = cur_wallet + net_to_refund
                    setattr(locked, wallet_field, new_wallet)
                    locked.save(update_fields=[wallet_field])
                    setattr(self, wallet_field, new_wallet)
                    CreditTransaction.objects.create(
                        student=locked,
                        fee_type=ft,
                        transaction_type=CreditTransaction.TYPE_REVERSAL,
                        amount=net_to_refund,
                        balance_after=new_wallet,
                        date=target_date,
                        clock_in=None,
                        recorded_by=actor,
                        notes=f"Reversed clock-in wallet deduction for {target_date}",
                    )
                    refunded[ft] = net_to_refund
        return refunded

    def get_bus_paid_amount(self, period_or_date=None):
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_bus_paid(period_or_date)
        if period_or_date is None:
            return self.get_daily_bus_paid()
        result = self.payments.filter(fee_type='BUS', period=period_or_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_canteen_paid_amount(self, period_or_date=None):
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_canteen_paid(period_or_date)
        if period_or_date is None:
            return self.get_daily_canteen_paid()
        result = self.payments.filter(fee_type='CANTEEN', period=period_or_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def is_bus_paid(self, period_or_date=None):
        if not self.bus:
            return True  # Walkers have no bus fee
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.is_bus_paid_for_date(period_or_date)
        if period_or_date is None:
            return self.is_bus_paid_for_date()
        return self.is_bus_paid_for_date() or (self.get_bus_paid_amount(period_or_date) >= self.bus_fee_required)

    def is_canteen_paid(self, period_or_date=None):
        if not self.canteen_enrolled:
            return True  # Opted out of canteen
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.is_canteen_paid_for_date(period_or_date)
        if period_or_date is None:
            return self.is_canteen_paid_for_date()
        return self.is_canteen_paid_for_date() or (self.get_canteen_paid_amount(period_or_date) >= self.canteen_fee_required)

    def get_bus_balance(self, period_or_date=None):
        if not self.bus:
            return Decimal('0.00')
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_bus_balance(period_or_date)
        if period_or_date is None:
            return self.get_daily_bus_balance()
        if self.is_bus_paid(period_or_date):
            return Decimal('0.00')
        balance = self.bus_fee_required - self.get_bus_paid_amount(period_or_date)
        return max(Decimal('0.00'), balance)

    def get_canteen_balance(self, period_or_date=None):
        if not self.canteen_enrolled:
            return Decimal('0.00')
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_canteen_balance(period_or_date)
        if period_or_date is None:
            return self.get_daily_canteen_balance()
        if self.is_canteen_paid(period_or_date):
            return Decimal('0.00')
        balance = self.canteen_fee_required - self.get_canteen_paid_amount(period_or_date)
        return max(Decimal('0.00'), balance)

    def get_today_clock_in(self, target_date=None):
        if target_date is None:
            target_date = timezone.localdate()
        return self.clock_in_records.filter(date=target_date).first()


class FeePayment(models.Model):
    """
    Record of bus or canteen fee payment made for a student.
    """
    FEE_TYPES = [
        ('BUS', 'Bus Fee'),
        ('CANTEEN', 'Canteen Fee'),
    ]

    PAYMENT_METHODS = [
        ('CASH', 'Cash'),
        ('MOBILE_MONEY', 'Mobile Money'),
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('CHEQUE', 'Cheque'),
        ('CARD', 'Card'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='payments')
    bus = models.ForeignKey('Bus', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments', help_text="Specific bus route this fee payment was collected on")
    is_temporary_bus = models.BooleanField(default=False, help_text="True if recorded for a one-off/guest bus ride different from student regular route")
    is_advance = models.BooleanField(default=False, help_text="True if recorded as advance payment covering future days")
    days_covered = models.PositiveIntegerField(default=1, help_text="Number of school days this payment is intended to cover")
    fee_type = models.CharField(max_length=20, choices=FEE_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate, db_index=True)
    period = models.CharField(max_length=50, default='Term 1 - 2026', help_text="e.g. 'Term 1 - 2026'")
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHODS, default='CASH')
    receipt_number = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Fee Payment'
        verbose_name_plural = 'Fee Payments'
        ordering = ['-payment_date', '-created_at']
        indexes = [
            models.Index(fields=['fee_type', 'payment_date'], name='feepay_type_date_idx'),
            models.Index(fields=['recorded_by', 'payment_date'], name='feepay_rec_date_idx'),
            models.Index(fields=['payment_method', 'payment_date'], name='feepay_method_date_idx'),
            models.Index(fields=['payment_date', 'recorded_by'], name='feepay_date_rec_idx'),
            models.Index(fields=['student', 'fee_type', 'payment_date'], name='feepay_std_type_date_idx'),
        ]

    def __str__(self):
        return f"{self.student.full_name} - {self.get_fee_type_display()} - {getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{self.amount:.2f}"


class ClockInRecord(models.Model):
    """
    Daily Student Clock-In Record.
    Stamps student arrival time for morning attendance.
    Enforces uniqueness per student per day.
    """
    STATUS_CHOICES = [
        ('PRESENT', 'Present'),
        ('LATE', 'Late'),
        ('EXCUSED', 'Excused'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='clock_in_records')
    date = models.DateField(default=timezone.localdate, db_index=True)
    clock_in_time = models.TimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PRESENT')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Clock-In Record'
        verbose_name_plural = 'Clock-In Records'
        unique_together = ('student', 'date')
        ordering = ['-date', '-clock_in_time']
        indexes = [
            models.Index(fields=['date', 'status'], name='clockin_date_status_idx'),
        ]

    def __str__(self):
        return f"{self.student.full_name} - {self.date} at {self.clock_in_time.strftime('%I:%M %p')}"


class CreditTransaction(models.Model):
    """
    Immutable wallet transaction ledger tracking bulk payment top-ups,
    explicit daily fee deductions, and reversals for a student.
    """
    TYPE_TOP_UP = 'TOP_UP'
    TYPE_DEDUCTION = 'DEDUCTION'
    TYPE_REVERSAL = 'REVERSAL'
    TRANSACTION_TYPES = [
        (TYPE_TOP_UP, 'Bulk Payment / Top-Up'),
        (TYPE_DEDUCTION, 'Daily Fare Deduction'),
        (TYPE_REVERSAL, 'Deduction Reversal / Refund'),
    ]
    FEE_TYPES = [
        ('BUS', 'Bus Fee'),
        ('CANTEEN', 'Canteen Fee'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='credit_transactions')
    fee_type = models.CharField(max_length=20, choices=FEE_TYPES, db_index=True)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    date = models.DateField(default=timezone.localdate, db_index=True)
    payment = models.ForeignKey(FeePayment, on_delete=models.SET_NULL, null=True, blank=True, related_name='credit_transactions')
    clock_in = models.ForeignKey(ClockInRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name='credit_transactions')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Credit Transaction'
        verbose_name_plural = 'Credit Transactions'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['student', 'fee_type', 'date'], name='credtx_std_type_date_idx'),
            models.Index(fields=['date', 'transaction_type'], name='credtx_date_type_idx'),
        ]

    def __str__(self):
        return f"{self.student.full_name} [{self.fee_type}] {self.transaction_type} {self.amount:.2f} -> Bal: {self.balance_after:.2f}"


def apply_payment_to_wallet(payment):
    """
    Sync a newly created FeePayment with the student's prepaid credit wallet:
    - Excess above today's remaining fee is credited into the wallet (TOP_UP).
    - If payment is a partial cash remainder top-up (< daily_fee) while wallet credit > 0,
      applies wallet credit toward completing today's fee (DEDUCTION).
    """
    if getattr(payment, '_skip_wallet_sync', False) or payment.amount <= Decimal('0.00'):
        return

    with transaction.atomic():
        student = Student.objects.select_for_update().get(pk=payment.student_id)
        if payment.fee_type == 'BUS':
            if payment.is_temporary_bus:
                return
            daily_fee = student.bus_fee_required
            if daily_fee <= Decimal('0.00') and payment.bus_id:
                daily_fee = payment.bus.fee
            wallet_field = 'bus_credit_wallet'
        else:
            daily_fee = student.canteen_fee_required
            wallet_field = 'canteen_credit_wallet'

        daily_fee = max(Decimal('0.00'), daily_fee or Decimal('0.00'))

        other_direct = (
            FeePayment.objects.filter(
                student=student,
                fee_type=payment.fee_type,
                payment_date=payment.payment_date,
            )
            .exclude(pk=payment.pk)
            .aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        )
        deducted = student.get_deducted_amount_for_date(payment.fee_type, payment.payment_date)
        owed_before = max(Decimal('0.00'), daily_fee - other_direct - deducted)
        current_wallet = max(Decimal('0.00'), getattr(student, wallet_field) or Decimal('0.00'))

        if payment.amount > owed_before:
            excess = payment.amount - owed_before
            new_wallet = current_wallet + excess
            setattr(student, wallet_field, new_wallet)
            student.save(update_fields=[wallet_field])
            if getattr(payment, 'student', None) is not None and payment.student_id == student.id:
                setattr(payment.student, wallet_field, new_wallet)
            CreditTransaction.objects.create(
                student=student,
                fee_type=payment.fee_type,
                transaction_type=CreditTransaction.TYPE_TOP_UP,
                amount=excess,
                balance_after=new_wallet,
                date=payment.payment_date,
                payment=payment,
                recorded_by=payment.recorded_by,
                notes=f"Bulk/advance credit from payment {payment.receipt_number or payment.id}",
            )
        elif payment.amount < owed_before and current_wallet > Decimal('0.00'):
            owed_after_cash = owed_before - payment.amount
            use_wallet = min(current_wallet, owed_after_cash)
            if use_wallet > Decimal('0.00'):
                new_wallet = current_wallet - use_wallet
                setattr(student, wallet_field, new_wallet)
                student.save(update_fields=[wallet_field])
                if getattr(payment, 'student', None) is not None and payment.student_id == student.id:
                    setattr(payment.student, wallet_field, new_wallet)
                CreditTransaction.objects.create(
                    student=student,
                    fee_type=payment.fee_type,
                    transaction_type=CreditTransaction.TYPE_DEDUCTION,
                    amount=use_wallet,
                    balance_after=new_wallet,
                    date=payment.payment_date,
                    payment=payment,
                    recorded_by=payment.recorded_by,
                    notes=f"Applied wallet credit with remainder payment {payment.receipt_number or payment.id}",
                )


def reverse_payment_wallet_effects(payment):
    """Reverse wallet TOP_UP or DEDUCTION entries associated with a deleted FeePayment."""
    with transaction.atomic():
        student = Student.objects.select_for_update().filter(pk=payment.student_id).first()
        if not student:
            return
        wallet_field = 'bus_credit_wallet' if payment.fee_type == 'BUS' else 'canteen_credit_wallet'
        current_wallet = max(Decimal('0.00'), getattr(student, wallet_field) or Decimal('0.00'))
        txs = list(CreditTransaction.objects.filter(payment=payment))
        delta = Decimal('0.00')
        for tx in txs:
            if tx.transaction_type == CreditTransaction.TYPE_TOP_UP:
                delta -= tx.amount
            elif tx.transaction_type == CreditTransaction.TYPE_DEDUCTION:
                delta += tx.amount
        if delta != Decimal('0.00'):
            new_wallet = max(Decimal('0.00'), current_wallet + delta)
            setattr(student, wallet_field, new_wallet)
            student.save(update_fields=[wallet_field])
        CreditTransaction.objects.filter(payment=payment).delete()


class UserProfile(models.Model):
    """
    User Profile extending Django User with specific school roles:
    ADMIN (Full Control), TEACHER (Clock-in & Roster View), ACCOUNTANT (Payments & Billing)
    """
    ROLE_ADMIN = 'ADMIN'
    ROLE_TEACHER = 'TEACHER'
    ROLE_ACCOUNTANT = 'ACCOUNTANT'

    ROLE_CHOICES = [
        (ROLE_ADMIN, 'School Administrator'),
        (ROLE_TEACHER, 'Teacher'),
        (ROLE_ACCOUNTANT, 'Accountant / Bursar'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_TEACHER)
    phone_number = models.CharField(max_length=30, blank=True)
    assigned_class = models.CharField(max_length=50, blank=True, help_text="Specific class for class teacher (optional)")
    assigned_bus = models.ForeignKey('Bus', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_teachers', help_text="Primary bus route (legacy/default)")
    assigned_buses = models.ManyToManyField('Bus', blank=True, related_name='assigned_multi_teachers', help_text="Specific bus routes/stops assigned to this teacher")
    can_collect_fees = models.BooleanField(
        default=False,
        help_text="Designates whether this teacher is authorized to record and collect fee payments."
    )

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    def refresh_from_db(self, using=None, fields=None):
        super().refresh_from_db(using=using, fields=fields)
        self.__dict__.pop('_cached_assigned_bus_ids', None)
        self.__dict__.pop('_cached_assigned_buses_list', None)
        self.__dict__.pop('_cached_is_bus_fee_teacher', None)

    @property
    def primary_bus(self):
        """Returns the primary assigned bus or first assigned bus from list."""
        if self.assigned_bus:
            return self.assigned_bus
        buses = self.get_assigned_buses_list()
        return buses[0] if buses else None

    def get_assigned_buses_list(self):
        """Returns list of all Bus objects assigned to this teacher."""
        if not hasattr(self, '_cached_assigned_buses_list'):
            buses = list(self.assigned_buses.all())
            if not buses and self.assigned_bus:
                buses = [self.assigned_bus]
            self._cached_assigned_buses_list = buses
        return self._cached_assigned_buses_list

    def get_assigned_bus_ids(self):
        """Returns set of IDs for all buses assigned to this teacher."""
        if not hasattr(self, '_cached_assigned_bus_ids'):
            buses = self.get_assigned_buses_list()
            ids = {b.id for b in buses}
            if self.assigned_bus_id:
                ids.add(self.assigned_bus_id)
            self._cached_assigned_bus_ids = ids
        return self._cached_assigned_bus_ids

    @property
    def is_school_admin(self):
        return self.role == self.ROLE_ADMIN or self.user.is_superuser

    @property
    def is_principal(self):
        return self.is_school_admin

    @property
    def is_accountant(self):
        return self.role == self.ROLE_ACCOUNTANT or self.is_school_admin

    @property
    def is_teacher(self):
        return self.role == self.ROLE_TEACHER or self.is_school_admin

    @property
    def can_view_my_collections(self):
        """Only teachers authorized to collect fees have an individual remittance page."""
        return bool(self.role == self.ROLE_TEACHER and self.can_collect_fees)

    @property
    def is_fee_teacher(self):
        return (self.role == self.ROLE_TEACHER and self.can_collect_fees) or self.is_school_admin

    @property
    def is_bus_fee_teacher(self):
        if not hasattr(self, '_cached_is_bus_fee_teacher'):
            self._cached_is_bus_fee_teacher = bool(
                self.assigned_bus_id or self.get_assigned_bus_ids()
            )
        return self._cached_is_bus_fee_teacher

    @property
    def fee_teacher_title(self):
        buses = self.get_assigned_buses_list()
        if len(buses) == 1:
            return f"Fee Teacher ({buses[0].name})"
        elif len(buses) > 1:
            return f"Fee Teacher ({len(buses)} Buses)"
        elif self.assigned_class:
            return f"Class Teacher ({self.assigned_class})"
        elif self.can_collect_fees:
            return "Fee Collector (Floating)"
        return "Teacher"

    @property
    def can_view_revenue(self):
        return self.role in [self.ROLE_ADMIN, self.ROLE_ACCOUNTANT] or self.user.is_superuser

    @property
    def can_record_payments(self):
        if self.is_school_admin:
            return True
        if self.role == self.ROLE_TEACHER:
            return self.can_collect_fees
        return False

    @property
    def can_register_students(self):
        """
        Check if user is authorized to enroll/register new students.
        Principals always can; teachers can if SchoolSetting ALLOW_TEACHER_STUDENT_REGISTRATION is True.
        """
        if self.is_school_admin:
            return True
        if self.role == self.ROLE_TEACHER and self.user.is_active:
            val = SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION')
            return val == 'True'
        return False

    @property
    def can_manage_settings(self):
        return self.is_school_admin

    @property
    def can_manage_buses(self):
        return self.is_school_admin

    @property
    def can_delete_payments(self):
        return self.is_school_admin


class NotificationLog(models.Model):
    """
    Log of parent notifications (arrival SMS/WhatsApp alerts and fee reminders).
    """
    TYPE_CLOCK_IN = 'CLOCK_IN'
    TYPE_FEE_REMINDER = 'FEE_REMINDER'

    TYPE_CHOICES = [
        (TYPE_CLOCK_IN, 'Arrival / Clock-In SMS'),
        (TYPE_FEE_REMINDER, 'Fee Arrears Reminder'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notifications')
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    recipient = models.CharField(max_length=100, default='Parent / Guardian')
    phone_number = models.CharField(max_length=30, blank=True)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='SENT')

    class Meta:
        verbose_name = 'Notification Log'
        verbose_name_plural = 'Notification Logs'
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.get_notification_type_display()} for {self.student.full_name} ({self.sent_at.strftime('%Y-%m-%d %H:%M')})"


class TeacherRemittance(models.Model):
    """
    Financial record of physical cash collected from students (Bus / Canteen fees)
    handed over by a teacher and received by the Bursar / Accountant.
    """
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='remittances_handed_over',
        help_text="Teacher who collected the physical cash"
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='remittances_received',
        help_text="Bursar or Administrator who received and verified the cash"
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Actual physical cash received and verified"
    )
    expected_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Total recorded unremitted cash at the moment of handover"
    )
    variance = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text="Difference between received amount and expected amount (amount - expected_amount)"
    )
    date = models.DateField(
        default=timezone.localdate,
        db_index=True,
        help_text="Date of cash handover"
    )
    reference_code = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Unique handover voucher code, e.g. REM-20261101-ABCD"
    )
    notes = models.TextField(
        blank=True,
        default='',
        help_text="Optional remarks regarding this settlement"
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Teacher Cash Handover'
        verbose_name_plural = 'Teacher Cash Handovers'

    def __str__(self):
        t_name = self.teacher.get_full_name() or self.teacher.username
        return f"{self.reference_code}: {t_name} -> {self.amount} ({self.date})"


class AuditLog(models.Model):
    """
    Centralized activity and security audit trail for the school.
    Tracks all administrative, financial, attendance, and authentication events.
    """
    ACTION_CHOICES = [
        ('LOGIN', 'User Sign In'),
        ('LOGOUT', 'User Sign Out'),
        ('FAILED_LOGIN', 'Failed Login Attempt'),
        ('PASSWORD_CHANGE', 'Password Changed'),
        ('PAYMENT_RECORDED', 'Fee Payment Recorded'),
        ('PAYMENT_DELETED', 'Fee Payment Deleted'),
        ('REMITTANCE_SETTLED', 'Cash Handover Settled'),
        ('STUDENT_CREATED', 'Student Enrolled'),
        ('STUDENT_UPDATED', 'Student Profile Updated'),
        ('STUDENT_DELETED', 'Student Removed'),
        ('LUNCH_TOGGLED', 'Lunch Enrollment Changed'),
        ('TEACHER_CREATED', 'Teacher Account Created'),
        ('TEACHER_UPDATED', 'Teacher Profile Updated'),
        ('TEACHER_STATUS', 'Teacher Account Status Toggled'),
        ('PERMISSION_CHANGE', 'Staff Permission Adjusted'),
        ('SETTINGS_UPDATED', 'School Settings Changed'),
        ('CLOCK_IN', 'Student Attendance Clock-In'),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_actions'
    )
    actor_username = models.CharField(max_length=150, blank=True)
    actor_role = models.CharField(max_length=50, blank=True)
    action = models.CharField(max_length=30, choices=ACTION_CHOICES, db_index=True)
    target_model = models.CharField(max_length=60, db_index=True)
    target_id = models.CharField(max_length=60, blank=True)
    target_repr = models.CharField(max_length=255, blank=True, help_text="Human-readable label of target item")
    description = models.TextField(help_text="Detailed description of what occurred")
    changes = models.JSONField(null=True, blank=True, help_text="Before and after diff")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['action', 'timestamp'], name='idx_audit_action_time'),
            models.Index(fields=['actor', 'timestamp'], name='idx_audit_actor_time'),
            models.Index(fields=['target_model', 'target_id'], name='idx_audit_target'),
        ]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.actor_username}: {self.get_action_display()} -> {self.target_repr}"


from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

@receiver(post_save, sender=get_user_model())
def create_or_save_user_profile(sender, instance, created, **kwargs):
    if created:
        role = UserProfile.ROLE_ADMIN if instance.is_superuser else UserProfile.ROLE_TEACHER
        UserProfile.objects.get_or_create(user=instance, defaults={'role': role})
    else:
        profile = getattr(instance, 'profile', None)
        if profile:
            if instance.is_superuser and profile.role != UserProfile.ROLE_ADMIN:
                profile.role = UserProfile.ROLE_ADMIN
            profile.save()


