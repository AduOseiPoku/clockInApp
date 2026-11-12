import csv
import json
from decimal import Decimal
from datetime import datetime, timedelta
import calendar
from collections import defaultdict
from io import TextIOWrapper
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import transaction, IntegrityError
import uuid
from django.db.models import Q, Sum, Count
from django.conf import settings
from django.urls import reverse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

from django.contrib.auth import get_user_model

User = get_user_model()
from .models import (
    Bus, Student, FeePayment, ClockInRecord, CreditTransaction, UserProfile, NotificationLog,
    SchoolClass, SchoolSetting, get_default_canteen_fee, TeacherRemittance,
    get_term_date_range, get_current_term_metrics,
    get_active_academic_period, get_active_currency_symbol, AuditLog
)
from .audit import log_activity
from .forms import (
    BusForm, StudentForm, FeePaymentForm, SchoolSettingsForm,
    TeacherCreationForm, TeacherUpdateForm, TeacherPasswordResetForm,
    SchoolClassForm, TeacherSelfPasswordChangeForm
)
from .decorators import principal_required, finance_required, payment_recording_required, login_required_ajax

def _get_credit_ledger_maps(target_date, student_ids=None):
    """
    Pre-fetch explicit CreditTransaction aggregates up to target_date in one query
    to prevent N+1 queries in dashboard, stats, bus analytics, and bursar views.
    """
    qs = CreditTransaction.objects.filter(date__lte=target_date)
    if student_ids is not None:
        qs = qs.filter(student_id__in=student_ids)

    rows = qs.values('student_id', 'fee_type').annotate(
        bal_top=Sum('amount', filter=Q(transaction_type=CreditTransaction.TYPE_TOP_UP)),
        bal_rev=Sum('amount', filter=Q(transaction_type=CreditTransaction.TYPE_REVERSAL)),
        bal_ded=Sum('amount', filter=Q(transaction_type=CreditTransaction.TYPE_DEDUCTION)),
        day_ded=Sum('amount', filter=Q(date=target_date, transaction_type=CreditTransaction.TYPE_DEDUCTION)),
        day_rev=Sum('amount', filter=Q(date=target_date, transaction_type=CreditTransaction.TYPE_REVERSAL)),
        day_top=Sum('amount', filter=Q(date=target_date, transaction_type=CreditTransaction.TYPE_TOP_UP)),
    )

    wallet_as_of = {}
    deducted_on_date = {}
    topup_on_date = {}
    has_ledger = set()

    for r in rows:
        key = (r['student_id'], r['fee_type'])
        has_ledger.add(key)
        bal = (r['bal_top'] or Decimal('0.00')) + (r['bal_rev'] or Decimal('0.00')) - (r['bal_ded'] or Decimal('0.00'))
        wallet_as_of[key] = max(Decimal('0.00'), bal)
        net_ded = (r['day_ded'] or Decimal('0.00')) - (r['day_rev'] or Decimal('0.00'))
        deducted_on_date[key] = max(Decimal('0.00'), net_ded)
        topup_on_date[key] = max(Decimal('0.00'), r['day_top'] or Decimal('0.00'))

    return {
        'wallet_as_of': wallet_as_of,
        'deducted_on_date': deducted_on_date,
        'topup_on_date': topup_on_date,
        'has_ledger': has_ledger,
    }


def _wallet_for_student(student, fee_type, ledger):
    """Return wallet balance as of target_date using ledger or direct wallet fallback."""
    key = (student.id, fee_type)
    if key in ledger['has_ledger']:
        return ledger['wallet_as_of'].get(key, Decimal('0.00'))
    raw = student.bus_credit_wallet if fee_type == 'BUS' else student.canteen_credit_wallet
    return max(Decimal('0.00'), raw or Decimal('0.00'))


def _normalize_payment_method(raw_method):
    """Normalize UI payment method strings (e.g. 'MOMO') into canonical FeePayment.PAYMENT_METHODS codes."""
    code = str(raw_method or 'CASH').strip().upper()
    if code == 'MOMO':
        code = 'MOBILE_MONEY'
    valid_codes = {choice[0] for choice in FeePayment.PAYMENT_METHODS}
    return code if code in valid_codes else 'CASH'


def _get_teacher_stat_scope(user_profile):
    """Return (bus_ids, student_class) scope tuple for restricted teachers, or (None, None) for full school."""
    if not user_profile or user_profile.is_school_admin or user_profile.role != UserProfile.ROLE_TEACHER:
        return None, None
    if user_profile.assigned_class:
        return None, user_profile.assigned_class
    assigned_bus_ids = user_profile.get_assigned_bus_ids()
    if assigned_bus_ids:
        return assigned_bus_ids, None
    return None, None


def _deduct_wallet_credits_for_clockin(student, target_date, clock_in, actor):
    """Deduct available daily Bus and Canteen wallet credits upon clock-in."""
    bus_ded = Decimal('0.00')
    can_ded = Decimal('0.00')
    if student.bus_id and student.bus_fee_required > Decimal('0.00'):
        bus_ded = student.apply_credit_for_date('BUS', target_date=target_date, actor=actor, clock_in=clock_in)
    if student.canteen_enrolled and student.canteen_fee_required > Decimal('0.00'):
        can_ded = student.apply_credit_for_date('CANTEEN', target_date=target_date, actor=actor, clock_in=clock_in)
    return {'BUS': bus_ded, 'CANTEEN': can_ded, 'total': bus_ded + can_ded}


def _undo_clockin_and_refund_wallet(student, target_date, clock_in, actor):
    """Refund wallet deductions tied to clock_in/target_date and delete the ClockInRecord if present."""
    refunded = student.reverse_clockin_credit_deductions(
        target_date=target_date,
        clock_in=clock_in,
        actor=actor,
    )
    clock_in_removed = False
    if clock_in:
        clock_in.delete()
        clock_in_removed = True
    return refunded, clock_in_removed


def get_stats_data(target_date=None, period=None, precomputed=None, bus_ids=None, student_class=None):
    """
    Calculate aggregated stats for daily dashboard counters.
    Reconciles daily attendance, daily bus fare collections, and daily canteen revenue.
    Accepts optional precomputed dictionary of payments/attendance to eliminate duplicate queries,
    and optional bus_ids / student_class filters for scoped teacher dashboards.
    """
    if target_date is None:
        target_date = timezone.localdate()

    students = Student.objects.filter(is_active=True).select_related('bus')
    clocked_in_records = ClockInRecord.objects.filter(date=target_date, student__is_active=True)
    if student_class:
        students = students.filter(student_class=student_class)
        clocked_in_records = clocked_in_records.filter(student__student_class=student_class)
    if bus_ids:
        students = students.filter(bus_id__in=bus_ids)
        clocked_in_records = clocked_in_records.filter(student__bus_id__in=bus_ids)

    total_students = students.count()
    clocked_in_count = clocked_in_records.count()
    clocked_in_pct = round((clocked_in_count / total_students * 100), 1) if total_students > 0 else 0
    
    bus_students = students.exclude(bus=None)
    bus_students_count = bus_students.count()

    pre = precomputed or {}

    credit_ledger = pre.get('credit_ledger')
    if credit_ledger is None:
        credit_ledger = _get_credit_ledger_maps(target_date)

    # Pre-fetch direct payments on target_date
    today_bus_payments = pre.get('today_bus_payments')
    if today_bus_payments is None:
        today_bus_payments = dict(
            FeePayment.objects.filter(fee_type='BUS', payment_date=target_date)
            .values('student_id')
            .annotate(total=Sum('amount'))
            .values_list('student_id', 'total')
        )

    today_canteen_payments = pre.get('today_canteen_payments')
    if today_canteen_payments is None:
        today_canteen_payments = dict(
            FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date)
            .values('student_id')
            .annotate(total=Sum('amount'))
            .values_list('student_id', 'total')
        )

    # Actual daily collections on target_date
    daily_bus_collected = FeePayment.objects.filter(fee_type='BUS', payment_date=target_date).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    daily_canteen_collected = FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    daily_total_collected = daily_bus_collected + daily_canteen_collected

    # Daily expected fees for riders & canteen
    bus_paid_count = 0
    bus_unpaid_count = 0
    total_bus_expected = Decimal('0.00')

    for s in bus_students:
        req = s.bus.fee
        total_bus_expected += req
        direct_bus = today_bus_payments.get(s.id, Decimal('0.00'))
        deducted_bus = credit_ledger['deducted_on_date'].get((s.id, 'BUS'), Decimal('0.00'))
        wallet_bus = _wallet_for_student(s, 'BUS', credit_ledger)
        settled_bus = direct_bus + deducted_bus

        if req <= Decimal('0.00') or settled_bus >= req or (settled_bus + wallet_bus) >= req:
            bus_paid_count += 1
        else:
            bus_unpaid_count += 1

    bus_paid_pct = round((bus_paid_count / bus_students_count * 100), 1) if bus_students_count > 0 else 0

    canteen_students = students.filter(canteen_enrolled=True)
    canteen_students_count = canteen_students.count()
    default_canteen_fee = get_default_canteen_fee()

    canteen_paid_count = 0
    canteen_unpaid_count = 0
    total_canteen_expected = Decimal('0.00')

    for s in canteen_students:
        req = s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee
        total_canteen_expected += req
        direct_can = today_canteen_payments.get(s.id, Decimal('0.00'))
        deducted_can = credit_ledger['deducted_on_date'].get((s.id, 'CANTEEN'), Decimal('0.00'))
        wallet_can = _wallet_for_student(s, 'CANTEEN', credit_ledger)
        settled_can = direct_can + deducted_can

        if req <= Decimal('0.00') or settled_can >= req or (settled_can + wallet_can) >= req:
            canteen_paid_count += 1
        else:
            canteen_unpaid_count += 1

    canteen_paid_pct = round((canteen_paid_count / canteen_students_count * 100), 1) if canteen_students_count > 0 else 0
    daily_total_expected = total_bus_expected + total_canteen_expected

    return {
        'total_students': total_students,
        'clocked_in_count': clocked_in_count,
        'not_clocked_in_count': total_students - clocked_in_count,
        'clocked_in_pct': clocked_in_pct,
        'bus_students_count': bus_students_count,
        'bus_paid_count': bus_paid_count,
        'bus_unpaid_count': bus_unpaid_count,
        'bus_paid_pct': bus_paid_pct,
        'canteen_students_count': canteen_students_count,
        'canteen_paid_count': canteen_paid_count,
        'canteen_unpaid_count': canteen_unpaid_count,
        'canteen_paid_pct': canteen_paid_pct,
        'total_bus_expected': total_bus_expected,
        'total_bus_collected': daily_bus_collected,
        'daily_bus_collected': daily_bus_collected,
        'total_canteen_expected': total_canteen_expected,
        'total_canteen_collected': daily_canteen_collected,
        'daily_canteen_collected': daily_canteen_collected,
        'daily_total_collected': daily_total_collected,
        'daily_total_expected': daily_total_expected,
    }


def get_class_fee_summary(target_date=None):
    """
    Computes daily expected fees and collections grouped by class name on target_date.
    Returns:
    {
        class_name: {
            'expected_total': Decimal,
            'bus_expected': Decimal,
            'canteen_expected': Decimal,
            'collected_today': Decimal,
            'balance': Decimal,
            'student_count': int,
        }
    }
    """
    if target_date is None:
        target_date = timezone.localdate()

    default_canteen_fee = get_default_canteen_fee()
    students = Student.objects.filter(is_active=True).select_related('bus')

    summary = {}
    for s in students:
        c_name = (s.student_class or '').strip()
        if not c_name:
            continue
        if c_name not in summary:
            summary[c_name] = {
                'expected_total': Decimal('0.00'),
                'bus_expected': Decimal('0.00'),
                'canteen_expected': Decimal('0.00'),
                'collected_today': Decimal('0.00'),
                'balance': Decimal('0.00'),
                'student_count': 0,
            }

        bus_req = s.bus.fee if s.bus else Decimal('0.00')
        canteen_req = (s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee) if s.canteen_enrolled else Decimal('0.00')

        summary[c_name]['bus_expected'] += bus_req
        summary[c_name]['canteen_expected'] += canteen_req
        summary[c_name]['expected_total'] += (bus_req + canteen_req)
        summary[c_name]['student_count'] += 1

    # Payments recorded today grouped by student class
    today_payments = FeePayment.objects.filter(payment_date=target_date).values('student__student_class').annotate(total=Sum('amount'))
    for item in today_payments:
        c_name = (item['student__student_class'] or '').strip()
        if c_name in summary:
            summary[c_name]['collected_today'] = item['total'] or Decimal('0.00')

    for c_name, data in summary.items():
        data['balance'] = max(Decimal('0.00'), data['expected_total'] - data['collected_today'])

    return summary


def get_collectors_financial_summary(teachers, target_date=None):
    """
    Computes audited daily targets, collections, one-off/guest bus adjustments,
    unpaid roster breakdown, and outstanding balances for a list or queryset
    of teachers on target_date.
    Optimized to run in constant O(1) database queries with zero N+1 lookups.
    """
    if target_date is None:
        target_date = timezone.localdate()

    default_canteen_fee = get_default_canteen_fee()
    teachers_list = list(teachers)
    if not teachers_list:
        return {}

    # Bulk pre-fetch UserProfiles and their M2M assigned_buses to guarantee zero N+1 queries
    teacher_ids = [t.id for t in teachers_list]
    profiles_by_uid = {
        prof.user_id: prof
        for prof in UserProfile.objects.filter(user_id__in=teacher_ids)
        .select_related('assigned_bus')
        .prefetch_related('assigned_buses')
    }

    class_to_teacher_ids = defaultdict(list)
    bus_to_teacher_ids = defaultdict(list)
    teacher_meta = {}

    for t in teachers_list:
        p = profiles_by_uid.get(t.id) or getattr(t, 'profile', None)
        t_class = (p.assigned_class or '').strip() if p else ''
        if p:
            buses_list = list(p.assigned_buses.all())
            if not buses_list and p.assigned_bus:
                buses_list = [p.assigned_bus]
            t_bus_ids = {b.id for b in buses_list}
            if p.assigned_bus_id:
                t_bus_ids.add(p.assigned_bus_id)
        else:
            buses_list = []
            t_bus_ids = set()

        bus_name_str = ", ".join(b.name for b in buses_list) if buses_list else (p.assigned_bus.name if p and p.assigned_bus else '')
        primary_bus_id = p.assigned_bus_id if (p and p.assigned_bus_id) else (list(t_bus_ids)[0] if t_bus_ids else None)
        teacher_meta[t.id] = {
            'teacher': t,
            'assigned_class': t_class,
            'assigned_bus_id': primary_bus_id,
            'assigned_bus_name': bus_name_str,
            'assigned_buses_list': buses_list,
            'is_bus_fee_teacher': bool(t_bus_ids),
            'baseline_student_ids': set(),
            'class_expected': Decimal('0.00'),
            'bus_expected': Decimal('0.00'),
            'canteen_expected': Decimal('0.00'),
            'baseline_target': Decimal('0.00'),
            'advance_prepayments': Decimal('0.00'),
            'prepaid_deductions': Decimal('0.00'),
            'prepaid_students_count': 0,
            'one_off_additions': Decimal('0.00'),
            'one_off_additions_count': 0,
            'transfers_out': Decimal('0.00'),
            'adjusted_target': Decimal('0.00'),
            'today_collected': Decimal('0.00'),
            'bus_collected': Decimal('0.00'),
            'canteen_collected': Decimal('0.00'),
            'today_count': 0,
            'balance': Decimal('0.00'),
            'present_balance': Decimal('0.00'),
            'absent_unpaid_count': 0,
            'absent_unpaid_amount': Decimal('0.00'),
            'clockin_started_today': False,
            'unpaid_students': [],
            'unpaid_count': 0,
            'student_count': 0,
        }
        if t_class:
            class_to_teacher_ids[t_class].append(t.id)
        for b_id in t_bus_ids:
            bus_to_teacher_ids[b_id].append(t.id)

    # 1. Fetch active students, clock-in records, daily payments & credit ledger in O(1) queries
    active_students = Student.objects.filter(is_active=True).select_related('bus')
    clocked_in_student_ids = set(
        ClockInRecord.objects.filter(date=target_date, status__in=['PRESENT', 'LATE'])
        .values_list('student_id', flat=True)
    )
    clockin_started_today = bool(clocked_in_student_ids)

    student_to_teacher_ids = defaultdict(set)
    student_daily_fee_map = {}
    today_bus_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    today_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )

    # Pre-fetch explicit credit ledger aggregates to avoid N+1 queries in the student loop
    credit_ledger = _get_credit_ledger_maps(target_date)
    topup_by_payment = dict(
        CreditTransaction.objects.filter(
            date=target_date,
            transaction_type=CreditTransaction.TYPE_TOP_UP,
            payment__isnull=False,
        )
        .values('payment_id')
        .annotate(total=Sum('amount'))
        .values_list('payment_id', 'total')
    )

    for s in active_students:
        c_name = (s.student_class or '').strip()
        bus_fee = s.bus.fee if s.bus else Decimal('0.00')
        canteen_fee = (s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee) if s.canteen_enrolled else Decimal('0.00')
        student_daily_fee_map[(s.id, 'BUS')] = bus_fee
        student_daily_fee_map[(s.id, 'CANTEEN')] = canteen_fee

        # Check prepaid credit applied today for bus and canteen (both explicit deductions and prior wallet credit)
        bus_prepaid_applied = Decimal('0.00')
        today_bus_direct = today_bus_payments.get(s.id, Decimal('0.00'))
        if s.bus_id and bus_fee > Decimal('0.00'):
            ded_bus = credit_ledger['deducted_on_date'].get((s.id, 'BUS'), Decimal('0.00'))
            wallet_bus = _wallet_for_student(s, 'BUS', credit_ledger)
            today_bus_topup = credit_ledger['topup_on_date'].get((s.id, 'BUS'), Decimal('0.00'))
            cash_for_today_bus = max(Decimal('0.00'), today_bus_direct - today_bus_topup)
            prior_avail_bus = max(Decimal('0.00'), wallet_bus - today_bus_topup)
            bus_prepaid_applied = min(
                max(Decimal('0.00'), bus_fee - cash_for_today_bus),
                ded_bus + prior_avail_bus,
            )

        canteen_prepaid_applied = Decimal('0.00')
        today_can_direct = today_canteen_payments.get(s.id, Decimal('0.00'))
        if s.canteen_enrolled and canteen_fee > Decimal('0.00'):
            ded_can = credit_ledger['deducted_on_date'].get((s.id, 'CANTEEN'), Decimal('0.00'))
            wallet_can = _wallet_for_student(s, 'CANTEEN', credit_ledger)
            today_can_topup = credit_ledger['topup_on_date'].get((s.id, 'CANTEEN'), Decimal('0.00'))
            cash_for_today_can = max(Decimal('0.00'), today_can_direct - today_can_topup)
            prior_avail_can = max(Decimal('0.00'), wallet_can - today_can_topup)
            canteen_prepaid_applied = min(
                max(Decimal('0.00'), canteen_fee - cash_for_today_can),
                ded_can + prior_avail_can,
            )

        bus_due = max(Decimal('0.00'), bus_fee - today_bus_direct - bus_prepaid_applied) if s.bus_id else Decimal('0.00')
        can_due = max(Decimal('0.00'), canteen_fee - today_can_direct - canteen_prepaid_applied) if s.canteen_enrolled else Decimal('0.00')
        total_due = bus_due + can_due
        is_present = s.id in clocked_in_student_ids

        responsible_tids = []
        if s.bus_id and s.bus_id in bus_to_teacher_ids:
            # Student is on a bus with an assigned Fee Teacher
            responsible_tids = bus_to_teacher_ids[s.bus_id]
        elif s.bus_id and c_name in class_to_teacher_ids:
            # Bus has no assigned Fee Teacher; student's classroom teacher collects both
            responsible_tids = class_to_teacher_ids[c_name]
        elif not s.bus_id and c_name in class_to_teacher_ids:
            # Walker student (no bus); classroom teacher collects canteen fee (if enrolled)
            responsible_tids = class_to_teacher_ids[c_name]

        for tid in responsible_tids:
            meta = teacher_meta[tid]
            meta['baseline_student_ids'].add(s.id)
            student_to_teacher_ids[s.id].add(tid)
            if s.bus_id:
                meta['bus_expected'] += bus_fee
            if s.canteen_enrolled:
                meta['canteen_expected'] += canteen_fee
            meta['student_count'] += 1

            if bus_prepaid_applied > Decimal('0.00'):
                meta['prepaid_deductions'] += bus_prepaid_applied
                meta['prepaid_students_count'] += 1
            if canteen_prepaid_applied > Decimal('0.00'):
                meta['prepaid_deductions'] += canteen_prepaid_applied
                if bus_prepaid_applied == Decimal('0.00'):
                    meta['prepaid_students_count'] += 1

            if total_due > Decimal('0.00'):
                meta['unpaid_students'].append({
                    'student_id': s.id,
                    'full_name': s.full_name,
                    'initials': s.initials,
                    'student_class': c_name,
                    'bus_name': s.bus.name if s.bus else 'Walker',
                    'bus_due': bus_due,
                    'canteen_due': can_due,
                    'total_due': total_due,
                    'is_present': is_present,
                })
                if clockin_started_today and not is_present:
                    meta['absent_unpaid_count'] += 1
                    meta['absent_unpaid_amount'] += total_due

    for tid, meta in teacher_meta.items():
        meta['baseline_target'] = meta['bus_expected'] + meta['canteen_expected']
        meta['class_expected'] = meta['baseline_target']  # For backward compatibility

    # 2. Fetch all payments on target_date (include student__bus to avoid any lazy lookup)
    today_payments = FeePayment.objects.filter(payment_date=target_date).select_related('student', 'student__bus', 'bus')
    transferred_per_student_fee = defaultdict(Decimal)

    for p in today_payments:
        collector_id = p.recorded_by_id
        is_guest = p.is_temporary_bus

        # If recorded by one of our tracked teachers
        if collector_id in teacher_meta:
            meta = teacher_meta[collector_id]
            meta['today_collected'] += p.amount
            meta['today_count'] += 1
            daily_fee = Decimal('0.00')
            if p.fee_type == 'BUS':
                meta['bus_collected'] += p.amount
                daily_fee = p.bus.fee if p.bus else (p.student.bus.fee if p.student.bus else Decimal('0.00'))
            elif p.fee_type == 'CANTEEN':
                meta['canteen_collected'] += p.amount
                daily_fee = p.student.custom_canteen_fee if p.student.custom_canteen_fee is not None else default_canteen_fee

            # Advance prepayments: excess amount collected today for future days
            if p.id in topup_by_payment:
                excess = topup_by_payment[p.id]
            elif p.is_advance or p.amount > daily_fee:
                excess = max(Decimal('0.00'), p.amount - daily_fee)
            else:
                excess = Decimal('0.00')
            if excess > Decimal('0.00'):
                meta['advance_prepayments'] += excess

            # Check if this was a guest / one-off bus ride or outside baseline roster
            if (is_guest and p.fee_type == 'BUS') or (p.student_id not in meta['baseline_student_ids']):
                meta['one_off_additions'] += p.amount
                meta['one_off_additions_count'] += 1

        # Adjust baseline teacher down when student paid someone else (Bursar or another teacher)
        # Capped at 1 day's fee for that student+fee_type so multi-day advances don't over-deduct
        for baseline_tid in student_to_teacher_ids.get(p.student_id, ()):
            if baseline_tid != collector_id and p.fee_type in ('BUS', 'CANTEEN'):
                fee_cap = student_daily_fee_map.get((p.student_id, p.fee_type), Decimal('0.00'))
                already_transferred = transferred_per_student_fee[(baseline_tid, p.student_id, p.fee_type)]
                remaining_cap = max(Decimal('0.00'), fee_cap - already_transferred)
                deduction = min(p.amount, remaining_cap)
                if deduction > Decimal('0.00'):
                    teacher_meta[baseline_tid]['transfers_out'] += deduction
                    transferred_per_student_fee[(baseline_tid, p.student_id, p.fee_type)] += deduction

    # 3. Finalize adjusted target, balances, and unpaid roster ordering
    for tid, meta in teacher_meta.items():
        adj = meta['baseline_target'] + meta['advance_prepayments'] - meta['prepaid_deductions'] + meta['one_off_additions'] - meta['transfers_out']
        meta['adjusted_target'] = max(Decimal('0.00'), adj)
        meta['balance'] = max(Decimal('0.00'), meta['adjusted_target'] - meta['today_collected'])
        meta['clockin_started_today'] = clockin_started_today
        if clockin_started_today:
            meta['present_balance'] = max(Decimal('0.00'), meta['balance'] - meta['absent_unpaid_amount'])
        else:
            meta['present_balance'] = meta['balance']
        meta['unpaid_students'].sort(key=lambda x: (not x['is_present'], x['student_class'], x['full_name']))
        meta['unpaid_count'] = len(meta['unpaid_students'])

    return teacher_meta


@login_required
@ensure_csrf_cookie
def dashboard_view(request):
    """
    Teacher Clock-In & Fee Verification Dashboard.
    Provides instant visibility of dynamic bus fees, canteen fees, and 1-click clock-in.
    """
    # Accountants have read-only financial access and do not use morning attendance clock-in
    user_profile = getattr(request.user, 'profile', None)
    if user_profile and user_profile.role == UserProfile.ROLE_ACCOUNTANT:
        return redirect('payment_list')

    # Parse target date
    date_str = request.GET.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    period = request.GET.get('period') or get_active_academic_period()
    class_filter = request.GET.get('class', '')
    bus_filter = request.GET.get('bus', '')
    fee_filter = request.GET.get('fee_status', '')
    clockin_filter = request.GET.get('clockin_status', '')
    search_query = request.GET.get('q', '').strip()

    # Auto-default or redirect to teacher's assigned domain
    user_profile = getattr(request.user, 'profile', None)
    is_attendance_only_teacher = bool(
        user_profile and user_profile.role == UserProfile.ROLE_TEACHER and not user_profile.can_collect_fees
    )
    teacher_assigned_buses = user_profile.get_assigned_buses_list() if user_profile else []
    teacher_assigned_bus_ids = user_profile.get_assigned_bus_ids() if user_profile else set()

    if user_profile and user_profile.role == UserProfile.ROLE_TEACHER:
        # Fee Teachers dedicated to a single bus route without an assigned classroom land on their bus roster
        if len(teacher_assigned_bus_ids) == 1 and not user_profile.assigned_class and not request.GET:
            return redirect('bus_analytics', pk=list(teacher_assigned_bus_ids)[0])
        # Automatically scope bus_filter to assigned bus routes for Bus Fee Teachers
        if teacher_assigned_bus_ids and not bus_filter and not request.GET.get('bus'):
            if len(teacher_assigned_bus_ids) > 1:
                bus_filter = 'my_buses'
            else:
                bus_filter = str(list(teacher_assigned_bus_ids)[0])
        if user_profile.assigned_class:
            class_filter = user_profile.assigned_class

    # Base query of active students
    students_qs = Student.objects.filter(is_active=True).select_related('bus').order_by('student_class', 'first_name', 'last_name')

    if search_query:
        students_qs = students_qs.filter(
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(student_class__icontains=search_query)
        )

    if class_filter:
        students_qs = students_qs.filter(student_class=class_filter)

    # Teacher Bus Roster Strict Isolation:
    is_class_teacher_scoped = bool(
        not user_profile.is_school_admin and user_profile.role == UserProfile.ROLE_TEACHER and user_profile.assigned_class
    )
    is_bus_teacher_scoped = bool(
        user_profile and user_profile.role == UserProfile.ROLE_TEACHER
        and teacher_assigned_bus_ids and not user_profile.is_school_admin
        and not user_profile.assigned_class
    )

    if is_class_teacher_scoped:
        # A classroom teacher is strictly locked to their assigned class
        class_filter = user_profile.assigned_class
        students_qs = students_qs.filter(student_class=class_filter)
        if bus_filter:
            if bus_filter == 'none':
                students_qs = students_qs.filter(bus=None)
            elif bus_filter == 'my_buses' and teacher_assigned_bus_ids:
                students_qs = students_qs.filter(bus_id__in=teacher_assigned_bus_ids)
            else:
                students_qs = students_qs.filter(bus_id=bus_filter)
    elif is_bus_teacher_scoped:
        # Bus duty teachers can ONLY ever see students on their assigned buses
        students_qs = students_qs.filter(bus_id__in=teacher_assigned_bus_ids)
        if bus_filter and bus_filter != 'my_buses':
            try:
                b_id = int(bus_filter)
                if b_id in teacher_assigned_bus_ids:
                    students_qs = students_qs.filter(bus_id=b_id)
                else:
                    bus_filter = 'my_buses'
            except (ValueError, TypeError):
                bus_filter = 'my_buses'
        else:
            bus_filter = 'my_buses' if len(teacher_assigned_bus_ids) > 1 else str(list(teacher_assigned_bus_ids)[0])
    else:
        if bus_filter:
            if bus_filter == 'none':
                students_qs = students_qs.filter(bus=None)
            elif bus_filter == 'my_buses' and teacher_assigned_bus_ids:
                students_qs = students_qs.filter(bus_id__in=teacher_assigned_bus_ids)
            else:
                students_qs = students_qs.filter(bus_id=bus_filter)
        elif teacher_assigned_bus_ids and not user_profile.assigned_class and not class_filter:
            students_qs = students_qs.filter(bus_id__in=teacher_assigned_bus_ids)
            bus_filter = 'my_buses' if len(teacher_assigned_bus_ids) > 1 else str(list(teacher_assigned_bus_ids)[0])

    # Pre-fetch today's clock-in records
    clock_ins = ClockInRecord.objects.filter(date=target_date)
    clock_in_map = {c.student_id: c for c in clock_ins}

    # Pre-fetch explicit credit ledger aggregates up to target_date
    credit_ledger = _get_credit_ledger_maps(target_date)

    # Pre-fetch direct daily payments on target_date
    today_bus_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    today_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )

    default_canteen_fee = get_default_canteen_fee()

    # Assemble student cards/rows with computed daily fee statuses
    student_items = []
    for s in students_qs:
        clock_in = clock_in_map.get(s.id)
        is_clocked_in = clock_in is not None

        # Daily Bus fee calculation
        if s.bus:
            bus_req = s.bus.fee
            direct_bus = today_bus_payments.get(s.id, Decimal('0.00'))
            ded_bus = credit_ledger['deducted_on_date'].get((s.id, 'BUS'), Decimal('0.00'))
            wallet_bus = _wallet_for_student(s, 'BUS', credit_ledger)
            settled_bus = direct_bus + ded_bus

            if bus_req <= Decimal('0.00') or settled_bus >= bus_req:
                is_bus_paid = True
                bus_balance = Decimal('0.00')
                bus_paid = direct_bus if direct_bus > Decimal('0.00') else min(bus_req, settled_bus)
                bus_credit_days = int(wallet_bus // bus_req) if bus_req > Decimal('0.00') else 0
            elif (settled_bus + wallet_bus) >= bus_req:
                is_bus_paid = True
                bus_balance = Decimal('0.00')
                bus_paid = bus_req
                rem = wallet_bus - max(Decimal('0.00'), bus_req - settled_bus)
                bus_credit_days = int(rem // bus_req) if bus_req > Decimal('0.00') else 0
            else:
                is_bus_paid = False
                covered = settled_bus + wallet_bus
                bus_balance = max(Decimal('0.00'), bus_req - covered)
                bus_paid = covered
                bus_credit_days = 0
        else:
            bus_req = Decimal('0.00')
            bus_paid = Decimal('0.00')
            bus_balance = Decimal('0.00')
            is_bus_paid = True
            bus_credit_days = 0

        # Daily Canteen fee calculation
        if s.canteen_enrolled:
            canteen_req = s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee
            direct_can = today_canteen_payments.get(s.id, Decimal('0.00'))
            ded_can = credit_ledger['deducted_on_date'].get((s.id, 'CANTEEN'), Decimal('0.00'))
            wallet_can = _wallet_for_student(s, 'CANTEEN', credit_ledger)
            settled_can = direct_can + ded_can

            if canteen_req <= Decimal('0.00') or settled_can >= canteen_req:
                is_canteen_paid = True
                canteen_balance = Decimal('0.00')
                canteen_paid = direct_can if direct_can > Decimal('0.00') else min(canteen_req, settled_can)
                canteen_credit_days = int(wallet_can // canteen_req) if canteen_req > Decimal('0.00') else 0
            elif (settled_can + wallet_can) >= canteen_req:
                is_canteen_paid = True
                canteen_balance = Decimal('0.00')
                canteen_paid = canteen_req
                rem = wallet_can - max(Decimal('0.00'), canteen_req - settled_can)
                canteen_credit_days = int(rem // canteen_req) if canteen_req > Decimal('0.00') else 0
            else:
                is_canteen_paid = False
                covered = settled_can + wallet_can
                canteen_balance = max(Decimal('0.00'), canteen_req - covered)
                canteen_paid = covered
                canteen_credit_days = 0
        else:
            canteen_req = Decimal('0.00')
            canteen_paid = Decimal('0.00')
            canteen_balance = Decimal('0.00')
            is_canteen_paid = True
            canteen_credit_days = 0

        # Filter by clock-in status
        if clockin_filter == 'clocked_in' and not is_clocked_in:
            continue
        if clockin_filter == 'not_clocked_in' and is_clocked_in:
            continue

        # Filter by fee status
        if fee_filter == 'unpaid_bus' and (not s.bus or is_bus_paid):
            continue
        if fee_filter == 'unpaid_canteen' and (not s.canteen_enrolled or is_canteen_paid):
            continue
        if fee_filter == 'any_unpaid' and is_bus_paid and is_canteen_paid:
            continue
        if fee_filter == 'fully_paid' and (not is_bus_paid or not is_canteen_paid):
            continue

        student_items.append({
            'student': s,
            'clock_in': clock_in,
            'is_clocked_in': is_clocked_in,
            'bus_req': bus_req,
            'bus_paid': bus_paid,
            'bus_balance': bus_balance,
            'is_bus_paid': is_bus_paid,
            'bus_credit_days': bus_credit_days,
            'canteen_req': canteen_req,
            'canteen_paid': canteen_paid,
            'canteen_balance': canteen_balance,
            'is_canteen_paid': is_canteen_paid,
            'canteen_credit_days': canteen_credit_days,
        })

    # Available classes & buses for filter dropdowns (scoped for classroom / bus teachers)
    if is_bus_teacher_scoped:
        buses = user_profile.get_assigned_buses_list()
        available_classes = students_qs.values_list('student_class', flat=True).distinct().order_by('student_class')
    elif is_class_teacher_scoped:
        available_classes = [user_profile.assigned_class]
        buses = teacher_assigned_buses if teacher_assigned_buses else Bus.objects.filter(is_active=True).order_by('name')
    else:
        available_classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
        buses = Bus.objects.filter(is_active=True).order_by('name')

    # Aggregated stats (scoped for classroom / bus duty teachers)
    scoped_bus_ids, scoped_class = _get_teacher_stat_scope(user_profile)
    stats = get_stats_data(
        target_date,
        period,
        precomputed={
            'credit_ledger': credit_ledger,
            'today_bus_payments': today_bus_payments,
            'today_canteen_payments': today_canteen_payments,
        },
        bus_ids=scoped_bus_ids,
        student_class=scoped_class,
    )

    # Dedicated attendance stats for classroom teachers
    teacher_class_stats = None
    if user_profile and user_profile.assigned_class:
        c_name = user_profile.assigned_class
        enrolled_c = Student.objects.filter(is_active=True, student_class=c_name).count()
        present_c = ClockInRecord.objects.filter(
            student__is_active=True, student__student_class=c_name, date=target_date
        ).count()
        absent_c = max(0, enrolled_c - present_c)
        pct_c = round((present_c / enrolled_c * 100), 1) if enrolled_c > 0 else 0.0
        teacher_class_stats = {
            'class_name': c_name,
            'enrolled': enrolled_c,
            'present': present_c,
            'absent': absent_c,
            'pct': pct_c,
        }

    # Teacher cash in hand summary for users with financial authority
    teacher_intake_today = Decimal('0.00')
    collectors_active_count = 0
    if user_profile and user_profile.can_view_revenue:
        teacher_intake_today = FeePayment.objects.filter(
            payment_date=target_date,
            recorded_by__profile__role=UserProfile.ROLE_TEACHER
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        collectors_active_count = FeePayment.objects.filter(
            payment_date=target_date,
            recorded_by__profile__role=UserProfile.ROLE_TEACHER
        ).values('recorded_by').distinct().count()

    # Smart contextual pagination:
    # High page size (80) when scoped to a class or bus so whole rosters fit on 1 page for roll call.
    # Responsive page size (40) when viewing all classes/buses across the whole school.
    is_single_group = bool(class_filter or (bus_filter and bus_filter != 'my_buses') or is_class_teacher_scoped)
    page_size = 80 if is_single_group else 40

    paginator = Paginator(student_items, page_size)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    query_params = request.GET.copy()
    if 'page' in query_params:
        query_params.pop('page')
    extra_querystring = query_params.urlencode()

    context = {
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'period': period,
        'student_items': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'is_paginated': page_obj.has_other_pages(),
        'extra_querystring': extra_querystring,
        'total_filtered': len(student_items),
        'available_classes': available_classes,
        'buses': buses,
        'teacher_assigned_buses': teacher_assigned_buses,
        'class_filter': class_filter,
        'bus_filter': bus_filter,
        'fee_filter': fee_filter,
        'clockin_filter': clockin_filter,
        'search_query': search_query,
        'stats': stats,
        'teacher_class_stats': teacher_class_stats,
        'is_attendance_only_teacher': is_attendance_only_teacher,
        'teacher_intake_today': teacher_intake_today,
        'collectors_active_count': collectors_active_count,
        'currency': get_active_currency_symbol(),
        'is_bus_teacher_scoped': is_bus_teacher_scoped,
        'is_class_teacher_scoped': is_class_teacher_scoped,
    }
    return render(request, 'attendance/dashboard.html', context)


@login_required_ajax
@require_POST
def toggle_clock_in_api(request):
    """
    AJAX API endpoint for 1-click Clock-In and Undo Clock-In.
    Returns JSON response for smooth micro-animations and instantaneous UI update.
    """
    user_profile = getattr(request.user, 'profile', None)
    if user_profile and user_profile.role == UserProfile.ROLE_ACCOUNTANT:
        return JsonResponse({
            'success': False,
            'error': 'Accountant account has read-only financial access. Attendance clock-ins can only be recorded by instructional staff or administrators.'
        }, status=403)

    try:
        data = json.loads(request.body) if request.body else request.POST
        student_id = data.get('student_id')
        date_str = data.get('date')
        action = data.get('action', 'toggle')  # 'clock_in', 'undo', or 'toggle'

        student = get_object_or_404(Student, id=student_id, is_active=True)

        if date_str:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            target_date = timezone.localdate()

        existing = ClockInRecord.objects.filter(student=student, date=target_date).first()

        currency = get_active_currency_symbol()

        if action == 'undo' or (action == 'toggle' and existing):
            with transaction.atomic():
                student = get_object_or_404(Student.objects.select_for_update(), id=student_id, is_active=True)
                existing = ClockInRecord.objects.filter(student=student, date=target_date).first()
                refunded, clock_in_removed = _undo_clockin_and_refund_wallet(
                    student, target_date, existing, request.user
                )
            student.refresh_from_db()
            clocked_in = False
            time_str = ""
            status_str = ""
            total_refunded = refunded['BUS'] + refunded['CANTEEN']
            if total_refunded > Decimal('0.00'):
                message = f"Clock-in undone for {student.full_name} & refunded {currency}{total_refunded:.2f} back to wallet."
            elif clock_in_removed:
                message = f"Clock-in undone for {student.full_name}."
            else:
                message = "Not clocked in."
        else:
            now_time = timezone.localtime().time() if hasattr(timezone, 'localtime') else timezone.now().time()
            with transaction.atomic():
                student = get_object_or_404(Student.objects.select_for_update(), id=student_id, is_active=True)
                try:
                    record, created = ClockInRecord.objects.get_or_create(
                        student=student,
                        date=target_date,
                        defaults={
                            'clock_in_time': now_time,
                            'status': 'PRESENT',
                            'recorded_by': request.user if request.user.is_authenticated else None,
                        }
                    )
                except IntegrityError:
                    record = ClockInRecord.objects.get(student=student, date=target_date)
                deducted = _deduct_wallet_credits_for_clockin(student, target_date, record, request.user)
            student.refresh_from_db()
            clocked_in = True
            time_str = record.clock_in_time.strftime('%I:%M %p')
            status_str = record.get_status_display()
            if deducted['total'] > Decimal('0.00'):
                message = f"✓ Clocked in {student.full_name} at {time_str} (Deducted {currency}{deducted['total']:.2f} from wallet)."
            else:
                message = f"✓ Clocked in {student.full_name} at {time_str}."

        # Scoped live stats (matches initial dashboard cards and reflects any wallet settlement)
        scoped_bus_ids, scoped_class = _get_teacher_stat_scope(user_profile)
        stats = get_stats_data(target_date, bus_ids=scoped_bus_ids, student_class=scoped_class)
        # Ensure JSON-serializable numeric types for Decimal totals
        stats = {
            **stats,
            'total_bus_expected': float(stats['total_bus_expected']),
            'total_bus_collected': float(stats['total_bus_collected']),
            'daily_bus_collected': float(stats['daily_bus_collected']),
            'total_canteen_expected': float(stats['total_canteen_expected']),
            'total_canteen_collected': float(stats['total_canteen_collected']),
            'daily_canteen_collected': float(stats['daily_canteen_collected']),
            'daily_total_collected': float(stats['daily_total_collected']),
            'daily_total_expected': float(stats['daily_total_expected']),
        }
        if user_profile and user_profile.assigned_class:
            c_name = user_profile.assigned_class
            c_enrolled = Student.objects.filter(is_active=True, student_class=c_name).count()
            c_present = ClockInRecord.objects.filter(
                student__is_active=True, student__student_class=c_name, date=target_date
            ).count()
            c_absent = max(0, c_enrolled - c_present)
            c_pct = round((c_present / c_enrolled * 100), 1) if c_enrolled > 0 else 0.0
            stats['class_enrolled'] = c_enrolled
            stats['class_present'] = c_present
            stats['class_absent'] = c_absent
            stats['class_pct'] = c_pct

        payment_summary = get_student_payment_summary(student, target_date)
        if clocked_in and payment_summary.get('has_bulk_payment'):
            rem = payment_summary.get('total_credit_remaining', 0.0)
            message = f"✓ Clocked in {student.full_name} at {time_str} • Remaining credit: {currency}{rem:.2f}"
            if payment_summary.get('total_cash_needed', 0.0) > 0:
                needed = payment_summary['total_cash_needed']
                message = f"✓ Clocked in {student.full_name} at {time_str} • {currency}{needed:.2f} balance still due"

        return JsonResponse({
            'success': True,
            'student_id': student.id,
            'student_name': student.full_name,
            'is_clocked_in': clocked_in,
            'clock_in_time': time_str,
            'status': status_str,
            'message': message,
            'stats': stats,
            'payment_summary': payment_summary,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@payment_recording_required
@require_POST
def quick_payment_api(request):
    """
    AJAX endpoint to record a payment quickly from the modal on the dashboard.
    Supports daily payments, multi-day advance top-ups, and single or dual fee recording.
    """
    try:
        data = json.loads(request.body) if request.body else request.POST
        student_id = data.get('student_id')
        fee_type = data.get('fee_type', 'BUS')
        payment_method = data.get('payment_method', 'CASH')
        receipt_number = data.get('receipt_number', '').strip()
        period = data.get('period') or get_active_academic_period()
        notes = data.get('notes', '').strip()

        # Parse target payment date
        date_str = data.get('payment_date') or data.get('date')
        if date_str:
            try:
                payment_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                payment_date = timezone.localdate()
        else:
            payment_date = timezone.localdate()

        student = get_object_or_404(Student, id=student_id)
        recorded_by = request.user if request.user.is_authenticated else None
        currency = get_active_currency_symbol()

        created_payments = []
        shared_receipt = receipt_number or f"REC-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        with transaction.atomic():
            if fee_type == 'BOTH':
                bus_amount = Decimal(str(data.get('bus_amount') or 0))
                canteen_amount = Decimal(str(data.get('canteen_amount') or 0))

                if bus_amount > Decimal('0.00'):
                    is_bus_adv = bool(student.bus and student.bus_fee_required > 0 and bus_amount > student.bus_fee_required)
                    bus_days_cov = int(bus_amount // student.bus_fee_required) if is_bus_adv else 1
                    p_bus = FeePayment.objects.create(
                        student=student,
                        bus=student.bus,
                        fee_type='BUS',
                        amount=bus_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
                        is_advance=is_bus_adv,
                        days_covered=bus_days_cov,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_bus)

                if canteen_amount > Decimal('0.00'):
                    is_can_adv = bool(student.canteen_enrolled and student.canteen_fee_required > 0 and canteen_amount > student.canteen_fee_required)
                    can_days_cov = int(canteen_amount // student.canteen_fee_required) if is_can_adv else 1
                    p_can = FeePayment.objects.create(
                        student=student,
                        fee_type='CANTEEN',
                        amount=canteen_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
                        is_advance=is_can_adv,
                        days_covered=can_days_cov,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_can)
            else:
                amount = Decimal(str(data.get('amount', 0)))
                if amount > Decimal('0.00'):
                    daily_req = student.bus_fee_required if fee_type == 'BUS' else student.canteen_fee_required
                    is_adv = bool(daily_req > 0 and amount > daily_req)
                    days_cov = int(amount // daily_req) if is_adv else 1
                    p = FeePayment.objects.create(
                        student=student,
                        bus=student.bus if fee_type == 'BUS' else None,
                        fee_type=fee_type,
                        amount=amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
                        is_advance=is_adv,
                        days_covered=days_cov,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p)

        total_amount = sum(p.amount for p in created_payments)

        for p in created_payments:
            log_activity(
                action='PAYMENT_RECORDED',
                target_model='FeePayment',
                target_id=str(p.id),
                target_repr=f"Receipt #{p.receipt_number} ({student.full_name})",
                description=f"Quick modal: Recorded {p.get_fee_type_display()} payment of {currency}{p.amount:.2f} via {p.get_payment_method_display()} for student {student.full_name}.",
                changes={
                    'amount': str(p.amount),
                    'fee_type': p.fee_type,
                    'payment_method': p.payment_method,
                    'receipt_number': p.receipt_number,
                    'payment_date': str(p.payment_date),
                    'student_id': student.id,
                    'student_name': student.full_name,
                },
                actor=request.user,
            )

        # Re-compute student statuses for payment_date
        bus_req = student.bus_fee_required
        bus_paid = student.get_daily_bus_paid(payment_date)
        is_bus_paid = student.is_bus_paid_for_date(payment_date)
        bus_bal = student.get_daily_bus_balance(payment_date)
        bus_credit_days = student.get_bus_credit_days(payment_date)

        canteen_req = student.canteen_fee_required
        canteen_paid = student.get_daily_canteen_paid(payment_date)
        is_canteen_paid = student.is_canteen_paid_for_date(payment_date)
        canteen_bal = student.get_daily_canteen_balance(payment_date)
        canteen_credit_days = student.get_canteen_credit_days(payment_date)

        user_profile = getattr(request.user, 'profile', None) if request.user.is_authenticated else None
        scoped_bus_ids, scoped_class = _get_teacher_stat_scope(user_profile)
        stats = get_stats_data(payment_date, period, bus_ids=scoped_bus_ids, student_class=scoped_class)

        return JsonResponse({
            'success': True,
            'message': f"Payment of {currency}{total_amount:.2f} recorded for {student.full_name}!",
            'payment_id': created_payments[0].id if created_payments else None,
            'receipt_number': created_payments[0].receipt_number if created_payments else '',
            'student_id': student.id,
            'bus_status': {
                'required': float(bus_req),
                'paid': float(bus_paid),
                'balance': float(bus_bal),
                'is_paid': is_bus_paid,
                'credit_days': bus_credit_days,
            },
            'canteen_status': {
                'required': float(canteen_req),
                'paid': float(canteen_paid),
                'balance': float(canteen_bal),
                'is_paid': is_canteen_paid,
                'credit_days': canteen_credit_days,
            },
            'stats': stats,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@payment_recording_required
@require_POST
def collect_both_fees_api(request):
    """
    Fast mobile 1-tap collection for Fee Teachers on bus routes:
    Atomically records BOTH Bus Fare and Canteen Fee (or a single designated fee) for a student.
    """
    try:
        data = json.loads(request.body) if (request.body and request.content_type == 'application/json') else request.POST
    except Exception:
        data = request.POST

    student_id = data.get('student_id')
    if not student_id:
        return JsonResponse({'success': False, 'error': 'Student ID is required.'}, status=400)

    student = get_object_or_404(Student, pk=student_id)
    target_date_str = (data.get('date') or data.get('payment_date') or '').strip()
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    days = int(data.get('days') or 1)
    is_adv = days > 1
    method = _normalize_payment_method(data.get('method') or data.get('payment_method'))
    fee_type_req = str(data.get('fee_type', 'BOTH')).strip().upper()
    period = data.get('period') or get_active_academic_period()
    currency = get_active_currency_symbol()
    default_canteen_fee = get_default_canteen_fee()

    user_profile = getattr(request.user, 'profile', None)
    user_assigned_buses_ids = user_profile.get_assigned_bus_ids() if user_profile else set()
    if student.bus and student.bus.id in user_assigned_buses_ids:
        bus_for_payment = student.bus
        is_temp = False
    elif user_profile and user_profile.assigned_bus:
        bus_for_payment = user_profile.assigned_bus
        is_temp = bool(student.bus and student.bus != user_profile.assigned_bus)
    else:
        bus_for_payment = student.bus
        is_temp = False

    created_payments = []
    messages_list = []

    custom_bus_amount = data.get('bus_amount')
    custom_canteen_amount = data.get('canteen_amount')
    general_amount = data.get('amount')

    if general_amount is not None and str(general_amount).strip() != '':
        try:
            gen_amt = Decimal(str(general_amount))
            if fee_type_req == 'BUS' and (custom_bus_amount is None or str(custom_bus_amount).strip() == ''):
                custom_bus_amount = gen_amt
            elif fee_type_req == 'CANTEEN' and (custom_canteen_amount is None or str(custom_canteen_amount).strip() == ''):
                custom_canteen_amount = gen_amt
            elif fee_type_req in ('BOTH', '') and (custom_bus_amount is None or str(custom_bus_amount).strip() == '') and (custom_canteen_amount is None or str(custom_canteen_amount).strip() == ''):
                assigned_bus_fee = (bus_for_payment.fee if bus_for_payment else (student.bus.fee if student.bus else Decimal('0.00')))
                daily_canteen = (student.custom_canteen_fee if student.custom_canteen_fee is not None else default_canteen_fee) if student.canteen_enrolled else Decimal('0.00')
                combined_daily = assigned_bus_fee + daily_canteen
                if combined_daily > Decimal('0.00') and daily_canteen > Decimal('0.00'):
                    ratio = assigned_bus_fee / combined_daily
                    custom_bus_amount = round(gen_amt * ratio, 2)
                    custom_canteen_amount = gen_amt - custom_bus_amount
                else:
                    custom_bus_amount = gen_amt
        except Exception:
            pass

    with transaction.atomic():
        # 1. Bus payment
        bus_created = False
        bus_amount = Decimal('0.00')
        if fee_type_req in ('BOTH', 'BUS'):
            assigned_bus = bus_for_payment or student.bus
            if assigned_bus:
                existing_bus = FeePayment.objects.filter(
                    student=student, fee_type='BUS', payment_date=target_date
                ).first()
                if not existing_bus and not student.is_bus_paid_for_date(target_date):
                    bus_days = days
                    bus_is_adv = is_adv
                    if custom_bus_amount is not None and str(custom_bus_amount).strip() != '':
                        bus_amount = Decimal(str(custom_bus_amount))
                        if assigned_bus.fee > Decimal('0.00') and bus_amount > assigned_bus.fee:
                            bus_is_adv = True
                            bus_days = int(bus_amount // assigned_bus.fee)
                            bus_note = f"Fee Teacher multi-pay collection: {currency}{bus_amount:.2f} (~{bus_days} days credit)"
                        else:
                            bus_is_adv = False
                            bus_days = 1
                            bus_note = "Fee Teacher bus collection"
                    elif days == 1:
                        bus_credit = student.get_bus_credit_balance(target_date)
                        owed_bus = max(Decimal('0.00'), assigned_bus.fee - student.get_daily_bus_settled(target_date))
                        if Decimal('0.00') < bus_credit < owed_bus:
                            bus_amount = max(Decimal('0.00'), owed_bus - bus_credit)
                            bus_note = f"Fee Teacher remainder top-up collection (Credit applied: {currency}{bus_credit:.2f})"
                        else:
                            bus_amount = assigned_bus.fee
                            bus_note = "Fee Teacher 1-tap bus collection"
                    else:
                        bus_amount = assigned_bus.fee * Decimal(str(days))
                        bus_note = f"Fee Teacher advance {days} days bus collection"

                    if bus_amount > Decimal('0.00'):
                        p_bus = FeePayment.objects.create(
                            student=student,
                            amount=bus_amount,
                            fee_type='BUS',
                            payment_method=method,
                            payment_date=target_date,
                            period=period,
                            recorded_by=request.user,
                            bus=assigned_bus,
                            is_temporary_bus=is_temp,
                            is_advance=bus_is_adv,
                            days_covered=bus_days,
                            notes=bus_note
                        )
                        created_payments.append(p_bus)
                        bus_created = True
                        messages_list.append(f"Bus fare {currency}{bus_amount:.2f}")

        # 2. Canteen payment
        canteen_created = False
        canteen_amount = Decimal('0.00')
        if fee_type_req in ('BOTH', 'CANTEEN') and student.canteen_enrolled:
            daily_canteen = student.custom_canteen_fee if student.custom_canteen_fee is not None else default_canteen_fee
            existing_canteen = FeePayment.objects.filter(
                student=student, fee_type='CANTEEN', payment_date=target_date
            ).first()
            if not existing_canteen and not student.is_canteen_paid_for_date(target_date):
                can_days = days
                can_is_adv = is_adv
                if custom_canteen_amount is not None and str(custom_canteen_amount).strip() != '':
                    canteen_amount = Decimal(str(custom_canteen_amount))
                    if daily_canteen > Decimal('0.00') and canteen_amount > daily_canteen:
                        can_is_adv = True
                        can_days = int(canteen_amount // daily_canteen)
                        canteen_note = f"Fee Teacher multi-pay lunch collection: {currency}{canteen_amount:.2f} (~{can_days} days credit)"
                    else:
                        can_is_adv = False
                        can_days = 1
                        canteen_note = "Fee Teacher lunch collection"
                elif days == 1:
                    can_credit = student.get_canteen_credit_balance(target_date)
                    owed_can = max(Decimal('0.00'), daily_canteen - student.get_daily_canteen_settled(target_date))
                    if Decimal('0.00') < can_credit < owed_can:
                        canteen_amount = max(Decimal('0.00'), owed_can - can_credit)
                        canteen_note = f"Fee Teacher remainder lunch top-up (Credit applied: {currency}{can_credit:.2f})"
                    else:
                        canteen_amount = daily_canteen
                        canteen_note = "Fee Teacher 1-tap lunch collection"
                else:
                    canteen_amount = daily_canteen * Decimal(str(days))
                    canteen_note = f"Fee Teacher advance {days} days lunch collection"

                if canteen_amount > Decimal('0.00'):
                    p_canteen = FeePayment.objects.create(
                        student=student,
                        amount=canteen_amount,
                        fee_type='CANTEEN',
                        payment_method=method,
                        payment_date=target_date,
                        period=period,
                        recorded_by=request.user,
                        is_advance=can_is_adv,
                        days_covered=can_days,
                        notes=canteen_note
                    )
                    created_payments.append(p_canteen)
                    canteen_created = True
                    messages_list.append(f"Lunch fee {currency}{canteen_amount:.2f}")

    for p in created_payments:
        log_activity(
            action='PAYMENT_RECORDED',
            target_model='FeePayment',
            target_id=str(p.id),
            target_repr=f"Receipt #{p.receipt_number} ({student.full_name})",
            description=f"1-Tap route collection: Recorded {p.get_fee_type_display()} payment of {currency}{p.amount:.2f} via {p.get_payment_method_display()} for student {student.full_name}.",
            changes={
                'amount': str(p.amount),
                'fee_type': p.fee_type,
                'payment_method': p.payment_method,
                'receipt_number': p.receipt_number,
                'payment_date': str(p.payment_date),
                'student_id': student.id,
                'student_name': student.full_name,
            },
            actor=request.user,
        )

    total_amount = (bus_amount if bus_created else Decimal('0.00')) + (canteen_amount if canteen_created else Decimal('0.00'))
    msg = f"✓ Collected {currency}{total_amount:.2f} ({', '.join(messages_list)})" if messages_list else "Fees already recorded for today."

    return JsonResponse({
        'success': True,
        'student_id': student.id,
        'student_name': student.full_name,
        'bus_collected': bus_created,
        'bus_amount': float(bus_amount) if bus_created else 0.0,
        'canteen_collected': canteen_created,
        'canteen_amount': float(canteen_amount) if canteen_created else 0.0,
        'total_amount': float(total_amount),
        'message': msg
    })


# ==========================================
# BUS MANAGEMENT VIEWS
# ==========================================
@finance_required
@login_required
def bus_list_view(request):
    """Bus Management: list buses with dynamic fees and assigned student stats. Scoped for teachers."""
    user_profile = getattr(request.user, 'profile', None)
    if user_profile and user_profile.role == UserProfile.ROLE_TEACHER and not user_profile.is_school_admin:
        assigned_ids = user_profile.get_assigned_bus_ids()
        buses = Bus.objects.filter(id__in=assigned_ids).order_by('name')
    else:
        buses = Bus.objects.all().order_by('name')
    period = get_active_academic_period()
    
    # Calculate revenue metrics per bus via bulk aggregates to avoid N+1 queries
    bus_data = []
    total_fleet_expected = Decimal('0.00')
    total_fleet_collected = Decimal('0.00')

    buses_list = list(buses)
    bus_ids = [b.id for b in buses_list]
    student_counts = dict(
        Student.objects.filter(is_active=True, bus_id__in=bus_ids)
        .values('bus_id')
        .annotate(c=Count('id'))
        .values_list('bus_id', 'c')
    )
    bus_collections = dict(
        FeePayment.objects.filter(
            fee_type='BUS',
            student__bus_id__in=bus_ids,
            period=period
        )
        .values('student__bus_id')
        .annotate(total=Sum('amount'))
        .values_list('student__bus_id', 'total')
    )

    for b in buses_list:
        student_count = student_counts.get(b.id, 0)
        expected = Decimal(student_count) * b.fee
        total_fleet_expected += expected
        
        collected = bus_collections.get(b.id, Decimal('0.00'))
        total_fleet_collected += collected

        bus_data.append({
            'bus': b,
            'student_count': student_count,
            'expected_revenue': expected,
            'collected_revenue': collected,
            'collection_pct': round((collected / expected * 100), 1) if expected > 0 else 0,
        })

    context = {
        'bus_data': bus_data,
        'total_buses': len(buses_list),
        'total_fleet_expected': total_fleet_expected,
        'total_fleet_collected': total_fleet_collected,
        'period': period,
    }
    return render(request, 'attendance/buses.html', context)


@login_required
def bus_analytics_view(request, pk):
    """
    Bus Route Payment Analytics (Daily Mode):
    Displays real-time breakdown of Paid vs. Unpaid registered riders
    for a specific bus route, organized class-by-class.
    Accessible by Principal, Bursar/Accountant, and assigned Fee Teachers.
    """
    bus = get_object_or_404(Bus, pk=pk)
    profile = getattr(request.user, 'profile', None)
    is_assigned_to_this_bus = bool(profile and bus.id in profile.get_assigned_bus_ids())
    is_admin_or_bursar = bool(request.user.is_superuser or (profile and (profile.is_school_admin or profile.is_accountant)))

    if not (is_admin_or_bursar or is_assigned_to_this_bus):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'You can only view rosters for your assigned bus routes.'}, status=403)
        messages.error(request, "Access denied: You do not have permission to access financial records.")
        return redirect('dashboard')
    
    # 1. Parse date filter (defaults to today)
    date_str = request.GET.get('date', '').strip()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    period = get_active_academic_period()
    currency = get_active_currency_symbol()

    # 2. Fetch all active students assigned to this bus route (scoped if teacher is a classroom teacher)
    students = bus.students.filter(is_active=True).select_related('bus', 'school_class').order_by('student_class', 'first_name', 'last_name')
    if not is_admin_or_bursar and profile and profile.assigned_class:
        students = students.filter(student_class=profile.assigned_class)
    total_riders = students.count()
    daily_fee = bus.fee

    # 3. Pre-fetch payments and explicit credit ledger
    student_ids = list(students.values_list('id', flat=True))
    credit_ledger = _get_credit_ledger_maps(target_date, student_ids=student_ids)

    today_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date, student_id__in=student_ids)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )

    today_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date, student_id__in=student_ids)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    default_canteen_fee = get_default_canteen_fee()

    # 4. Process each student & group by class
    class_groups = defaultdict(list)

    total_paid_count = 0
    total_unpaid_count = 0
    total_expected = Decimal('0.00')
    total_collected = Decimal('0.00')

    for s in students:
        total_expected += daily_fee
        direct_paid = today_payments.get(s.id, Decimal('0.00'))
        ded_bus = credit_ledger['deducted_on_date'].get((s.id, 'BUS'), Decimal('0.00'))
        wallet_bus = _wallet_for_student(s, 'BUS', credit_ledger)
        settled_bus = direct_paid + ded_bus
        owed_bus = max(Decimal('0.00'), daily_fee - settled_bus)
        credit = wallet_bus

        is_paid = (daily_fee <= Decimal('0.00')) or (settled_bus >= daily_fee) or ((settled_bus + wallet_bus) >= daily_fee)
        has_partial_credit = (not is_paid) and (owed_bus > Decimal('0.00')) and (Decimal('0.00') < wallet_bus < owed_bus)
        credit_available = wallet_bus if has_partial_credit else Decimal('0.00')
        bus_credit_days = int((wallet_bus - owed_bus) // daily_fee) if (is_paid and wallet_bus >= owed_bus and daily_fee > Decimal('0.00')) else 0

        if is_paid:
            total_paid_count += 1
            paid_amount = direct_paid if direct_paid > Decimal('0.00') else daily_fee
            total_collected += paid_amount
            balance = Decimal('0.00')
            remainder_due = Decimal('0.00')
        else:
            total_unpaid_count += 1
            covered = settled_bus + wallet_bus
            total_collected += direct_paid
            balance = max(Decimal('0.00'), daily_fee - covered)
            paid_amount = direct_paid
            remainder_due = balance

        # Canteen calculations
        canteen_fee = (s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee) if s.canteen_enrolled else Decimal('0.00')
        direct_canteen = today_canteen_payments.get(s.id, Decimal('0.00'))
        ded_canteen = credit_ledger['deducted_on_date'].get((s.id, 'CANTEEN'), Decimal('0.00'))
        wallet_canteen = _wallet_for_student(s, 'CANTEEN', credit_ledger)
        settled_canteen = direct_canteen + ded_canteen
        owed_canteen = max(Decimal('0.00'), canteen_fee - settled_canteen)
        canteen_credit = wallet_canteen

        canteen_is_paid = (not s.canteen_enrolled) or (canteen_fee <= Decimal('0.00')) or (settled_canteen >= canteen_fee) or ((settled_canteen + wallet_canteen) >= canteen_fee)
        canteen_has_partial = (not canteen_is_paid) and s.canteen_enrolled and (owed_canteen > Decimal('0.00')) and (Decimal('0.00') < wallet_canteen < owed_canteen)
        canteen_credit_available = wallet_canteen if canteen_has_partial else Decimal('0.00')
        canteen_credit_days = int((wallet_canteen - owed_canteen) // canteen_fee) if (canteen_is_paid and s.canteen_enrolled and wallet_canteen >= owed_canteen and canteen_fee > Decimal('0.00')) else 0

        if canteen_is_paid:
            canteen_amount_paid = direct_canteen if direct_canteen > Decimal('0.00') else (canteen_fee if s.canteen_enrolled else Decimal('0.00'))
            canteen_balance = Decimal('0.00')
            canteen_remainder_due = Decimal('0.00')
        else:
            covered_canteen = settled_canteen + wallet_canteen
            canteen_amount_paid = direct_canteen
            canteen_balance = max(Decimal('0.00'), canteen_fee - covered_canteen)
            canteen_remainder_due = canteen_balance

        both_is_paid = is_paid and canteen_is_paid
        combined_due = remainder_due + canteen_remainder_due

        student_info = {
            'student': s,
            'is_paid': is_paid,
            'amount_paid': paid_amount,
            'balance': balance,
            'bus_fee': daily_fee,
            'credit': credit,
            'has_partial_credit': has_partial_credit,
            'credit_available': credit_available,
            'remainder_due': remainder_due,
            'bus_credit_days': bus_credit_days,
            'canteen_enrolled': s.canteen_enrolled,
            'canteen_fee': canteen_fee,
            'canteen_is_paid': canteen_is_paid,
            'canteen_amount_paid': canteen_amount_paid,
            'canteen_balance': canteen_balance,
            'canteen_credit': canteen_credit,
            'canteen_has_partial': canteen_has_partial,
            'canteen_credit_available': canteen_credit_available,
            'canteen_remainder_due': canteen_remainder_due,
            'canteen_credit_days': canteen_credit_days,
            'both_is_paid': both_is_paid,
            'combined_due': combined_due,
            'parent_phone': getattr(s, 'parent_phone', ''),
        }
        class_name = s.student_class or 'Unassigned Class'
        class_groups[class_name].append(student_info)

    # 4b. Fetch guest / one-off riders recorded for this bus on target_date from students NOT enrolled in this bus
    guest_payments = FeePayment.objects.filter(
        fee_type='BUS',
        bus=bus,
        payment_date=target_date,
        is_temporary_bus=True
    ).select_related('student__bus').exclude(student_id__in=student_ids)

    guest_riders = []
    for gp in guest_payments:
        s = gp.student
        guest_riders.append({
            'student': s,
            'is_paid': True,
            'amount_paid': gp.amount,
            'balance': Decimal('0.00'),
            'parent_phone': getattr(s, 'parent_phone', ''),
            'is_guest': True,
            'regular_bus_name': s.bus.name if s.bus else 'Walker',
        })
        total_paid_count += 1
        total_expected += gp.amount
        total_collected += gp.amount

    # 5. Build class breakdown list with per-class statistics
    class_breakdown = []
    for class_name in sorted(class_groups.keys()):
        c_students = class_groups[class_name]
        c_riders = len(c_students)
        c_paid = sum(1 for item in c_students if item['is_paid'])
        c_unpaid = c_riders - c_paid
        c_pct = round((c_paid / c_riders * 100), 1) if c_riders > 0 else 0
        c_expected = Decimal(c_riders) * daily_fee
        c_collected = sum(item['amount_paid'] for item in c_students)
        c_outstanding = max(Decimal('0.00'), c_expected - c_collected)

        class_breakdown.append({
            'class_name': class_name,
            'riders_count': c_riders,
            'paid_count': c_paid,
            'unpaid_count': c_unpaid,
            'compliance_pct': c_pct,
            'expected_amount': c_expected,
            'collected_amount': c_collected,
            'outstanding_amount': c_outstanding,
            'students': c_students,
        })

    paid_pct = round((total_paid_count / total_riders * 100), 1) if total_riders > 0 else 0
    unpaid_pct = round((total_unpaid_count / total_riders * 100), 1) if total_riders > 0 else 0
    total_outstanding = max(Decimal('0.00'), total_expected - total_collected)

    bus_unpaid_count = total_unpaid_count
    canteen_unpaid_count = 0
    fully_settled_count = 0
    for c in class_breakdown:
        for item in c['students']:
            if item['canteen_enrolled'] and not item['canteen_is_paid']:
                canteen_unpaid_count += 1
            if item['both_is_paid']:
                fully_settled_count += 1

    summary = {
        'total_riders': total_riders,
        'paid_count': total_paid_count,
        'unpaid_count': total_unpaid_count,
        'bus_unpaid_count': bus_unpaid_count,
        'canteen_unpaid_count': canteen_unpaid_count,
        'fully_settled_count': fully_settled_count,
        'paid_pct': paid_pct,
        'unpaid_pct': unpaid_pct,
        'total_expected': total_expected,
        'total_collected': total_collected,
        'total_outstanding': total_outstanding,
    }

    # Flattened unpaid list across all classrooms for instant route-wide access
    all_unpaid_students = []
    for c in class_breakdown:
        for item in c['students']:
            if not item['is_paid']:
                all_unpaid_students.append({
                    **item,
                    'class_name': c['class_name'],
                })

    # Fetch fee teachers assigned to this bus route
    fee_teachers = User.objects.filter(
        profile__role=UserProfile.ROLE_TEACHER,
        profile__assigned_bus=bus,
        profile__can_collect_fees=True,
        is_active=True
    ).select_related('profile')

    context = {
        'bus': bus,
        'summary': summary,
        'class_breakdown': class_breakdown,
        'all_unpaid_students': all_unpaid_students,
        'guest_riders': guest_riders,
        'guest_riders_count': len(guest_riders),
        'fee_teachers': fee_teachers,
        'target_date': target_date.strftime('%Y-%m-%d'),
        'currency': currency,
        'period': period,
    }
    return render(request, 'attendance/bus_analytics.html', context)


@principal_required
def bus_create_view(request):
    """Add a new Bus route with dynamic fee."""
    if request.method == 'POST':
        form = BusForm(request.POST)
        if form.is_valid():
            bus = form.save()
            messages.success(request, f"Bus '{bus.name}' created with route fee {get_active_currency_symbol()}{bus.fee:.2f}!")
            return redirect('bus_list')
    else:
        form = BusForm()
    return render(request, 'attendance/bus_form.html', {'form': form, 'title': 'Add New Bus Route'})


@principal_required
def bus_update_view(request, pk):
    """Edit Bus details or adjust fee amount."""
    bus = get_object_or_404(Bus, pk=pk)
    if request.method == 'POST':
        form = BusForm(request.POST, instance=bus)
        if form.is_valid():
            bus = form.save()
            messages.success(request, f"Bus '{bus.name}' updated successfully!")
            return redirect('bus_list')
    else:
        form = BusForm(instance=bus)
    return render(request, 'attendance/bus_form.html', {'form': form, 'bus': bus, 'title': f"Edit {bus.name}"})


@principal_required
def bus_delete_view(request, pk):
    """Delete a bus route."""
    bus = get_object_or_404(Bus, pk=pk)
    if request.method == 'POST':
        name = bus.name
        bus.delete()
        messages.success(request, f"Bus '{name}' deleted.")
        return redirect('bus_list')
    return render(request, 'attendance/confirm_delete.html', {
        'object': bus,
        'object_name': f"Bus '{bus.name}'",
        'cancel_url': 'bus_list',
    })


# ==========================================
# CLASSROOM MANAGEMENT (PRINCIPAL ONLY)
# ==========================================
@principal_required
def class_list_view(request):
    """View and manage uniform school classes created by the Principal."""
    classes = SchoolClass.objects.annotate(
        active_student_count=Count('students', filter=Q(students__is_active=True))
    ).order_by('name')
    total_classes = classes.count()
    active_classes = classes.filter(is_active=True).count()
    total_enrolled = Student.objects.filter(is_active=True).count()

    return render(request, 'attendance/classes.html', {
        'classes': classes,
        'total_classes': total_classes,
        'active_classes': active_classes,
        'total_enrolled': total_enrolled,
        'title': 'School Classes',
    })


@principal_required
def class_create_view(request):
    """Add a new school class for roster and enrollment uniformity."""
    if request.method == 'POST':
        form = SchoolClassForm(request.POST)
        if form.is_valid():
            sc = form.save()
            messages.success(request, f"✓ Class '{sc.name}' created successfully!")
            return redirect('class_list')
    else:
        form = SchoolClassForm()
    return render(request, 'attendance/class_form.html', {
        'form': form,
        'title': 'Add New Class',
        'is_edit': False,
    })


@principal_required
def class_update_view(request, pk):
    """Update an existing class name, grade level, or active status."""
    sc = get_object_or_404(SchoolClass, pk=pk)
    old_name = sc.name
    if request.method == 'POST':
        form = SchoolClassForm(request.POST, instance=sc)
        if form.is_valid():
            updated_sc = form.save()
            if old_name != updated_sc.name:
                Student.objects.filter(student_class=old_name).update(student_class=updated_sc.name)
            messages.success(request, f"✓ Class '{updated_sc.name}' updated successfully!")
            return redirect('class_list')
    else:
        form = SchoolClassForm(instance=sc)
    return render(request, 'attendance/class_form.html', {
        'form': form,
        'school_class': sc,
        'title': f"Edit Class: {sc.name}",
        'is_edit': True,
    })


@principal_required
def class_delete_view(request, pk):
    """Delete a class if safe or confirm with administrator."""
    sc = get_object_or_404(SchoolClass, pk=pk)
    student_count = sc.students.filter(is_active=True).count()
    if request.method == 'POST':
        name = sc.name
        sc.delete()
        messages.success(request, f"Class '{name}' deleted.")
        return redirect('class_list')
    return render(request, 'attendance/confirm_delete.html', {
        'object': sc,
        'object_name': f"Class '{sc.name}' ({student_count} active students)",
        'cancel_url': 'class_list',
    })


# ==========================================
# STUDENT MANAGEMENT VIEWS
# ==========================================
@login_required
@ensure_csrf_cookie
def student_list_view(request):
    """Student roster listing with bus assignments and canteen status."""
    class_filter = request.GET.get('class', '')
    bus_filter = request.GET.get('bus', '')
    search = request.GET.get('q', '').strip()

    user_profile = getattr(request.user, 'profile', None)
    teacher_assigned_buses = user_profile.get_assigned_buses_list() if user_profile else []
    teacher_assigned_bus_ids = user_profile.get_assigned_bus_ids() if user_profile else set()

    is_admin = bool(
        user_profile and (user_profile.is_school_admin or user_profile.role == UserProfile.ROLE_ACCOUNTANT)
    )
    is_class_teacher_scoped = bool(
        not is_admin and user_profile and user_profile.role == UserProfile.ROLE_TEACHER and user_profile.assigned_class
    )
    is_bus_teacher_scoped = bool(
        not is_admin and user_profile and user_profile.role == UserProfile.ROLE_TEACHER
        and teacher_assigned_bus_ids and not user_profile.assigned_class
    )

    students = Student.objects.filter(is_active=True).select_related('bus', 'school_class', 'registered_by').order_by('student_class', 'first_name', 'last_name')

    # Apply strict role scoping
    if is_class_teacher_scoped:
        # A class teacher is strictly locked to their assigned class - no bus filtering
        class_filter = user_profile.assigned_class
        students = students.filter(student_class=class_filter)
        classes_names = [user_profile.assigned_class]
        bus_filter = ''
        buses = []

    elif is_bus_teacher_scoped:
        # A bus teacher is strictly locked to their assigned buses
        students = students.filter(bus_id__in=teacher_assigned_bus_ids)
        buses = teacher_assigned_buses

        if not bus_filter:
            if len(teacher_assigned_bus_ids) > 1:
                bus_filter = 'my_buses'
            else:
                bus_filter = str(list(teacher_assigned_bus_ids)[0])

        if bus_filter and bus_filter != 'my_buses':
            try:
                b_id = int(bus_filter)
                if b_id in teacher_assigned_bus_ids:
                    students = students.filter(bus_id=b_id)
                else:
                    bus_filter = 'my_buses'
            except (ValueError, TypeError):
                bus_filter = 'my_buses'

        if class_filter:
            students = students.filter(student_class=class_filter)

        classes_names = list(SchoolClass.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
        if not classes_names:
            classes_names = list(Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class'))

    else:
        # Admins, Accountants, or Unassigned Floating Teachers
        if class_filter:
            students = students.filter(student_class=class_filter)
        if bus_filter:
            if bus_filter == 'none':
                students = students.filter(bus=None)
            elif bus_filter == 'my_buses' and teacher_assigned_bus_ids:
                students = students.filter(bus_id__in=teacher_assigned_bus_ids)
            else:
                students = students.filter(bus_id=bus_filter)

        classes_names = list(SchoolClass.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
        if not classes_names:
            classes_names = list(Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class'))
        buses = Bus.objects.filter(is_active=True).order_by('name')

    if search:
        students = students.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(student_class__icontains=search)
        )

    onboarding_mode_active = (SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION') == 'True')
    can_register = bool(user_profile and user_profile.can_register_students)
    is_attendance_only_teacher = bool(
        user_profile and user_profile.role == UserProfile.ROLE_TEACHER and not user_profile.can_collect_fees
    )

    paginator = Paginator(students, 40)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    target_date = timezone.localdate()
    can_clock_in_students = bool(
        not user_profile or user_profile.role != UserProfile.ROLE_ACCOUNTANT
    )
    page_student_ids = [s.id for s in page_obj]
    clock_ins = ClockInRecord.objects.filter(date=target_date, student_id__in=page_student_ids)
    clock_in_map = {c.student_id: c for c in clock_ins}
    for s in page_obj:
        s.today_clock_in = clock_in_map.get(s.id)
        s.is_clocked_in = bool(s.today_clock_in)

    today_onboarded_count = 0
    my_onboarded_today_count = 0
    if onboarding_mode_active:
        today_onboarded_count = Student.objects.filter(
            is_active=True,
            created_at__date=target_date,
        ).count()
        if request.user.is_authenticated:
            my_onboarded_today_count = Student.objects.filter(
                is_active=True,
                created_at__date=target_date,
                registered_by=request.user,
            ).count()

    context = {
        'students': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'classes': classes_names,
        'buses': buses,
        'teacher_assigned_buses': teacher_assigned_buses,
        'class_filter': class_filter,
        'bus_filter': bus_filter,
        'search': search,
        'total_count': paginator.count,
        'onboarding_mode_active': onboarding_mode_active,
        'today_onboarded_count': today_onboarded_count,
        'my_onboarded_today_count': my_onboarded_today_count,
        'can_register': can_register,
        'can_clock_in_students': can_clock_in_students,
        'clock_in_map': clock_in_map,
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'is_attendance_only_teacher': is_attendance_only_teacher,
        'is_bus_teacher_scoped': is_bus_teacher_scoped,
        'is_class_teacher_scoped': is_class_teacher_scoped,
    }
    return render(request, 'attendance/students.html', context)


@login_required
def student_create_view(request):
    """Enroll a new student. Accessible by Principals, and by Teachers during onboarding mode."""
    user_profile = getattr(request.user, 'profile', None)
    if not (user_profile and user_profile.can_register_students):
        messages.error(request, 'Access denied: Student registration is currently restricted to administrators.')
        return redirect('student_list')

    if request.method == 'POST':
        form = StudentForm(request.POST, user_obj=request.user)
        if form.is_valid():
            student = form.save(commit=False)
            if not student.registered_by and request.user.is_authenticated:
                student.registered_by = request.user
            student.save()
            log_activity(
                action='STUDENT_CREATED',
                target_model='Student',
                target_id=str(student.id),
                target_repr=f"{student.full_name} ({student.student_class})",
                description=f"Enrolled student {student.full_name} in class {student.student_class}" + (f" with bus {student.bus.name}" if student.bus else "") + ".",
                changes={
                    'full_name': student.full_name,
                    'student_class': str(student.student_class),
                    'bus': student.bus.name if student.bus else None,
                    'canteen_enrolled': student.canteen_enrolled,
                },
                actor=request.user,
            )
            messages.success(request, f"✓ Student '{student.full_name}' enrolled in {student.student_class}!")
            if 'save_and_add' in request.POST:
                return redirect(f"{reverse('student_create')}?class={student.student_class}")
            return redirect('student_list')
    else:
        class_param = request.GET.get('class', '')
        initial = {'student_class': class_param} if class_param else {}
        form = StudentForm(user_obj=request.user, initial=initial)
    return render(request, 'attendance/student_form.html', {'form': form, 'title': 'Enroll New Student'})


@principal_required
def toggle_onboarding_registration_view(request):
    """
    1-Click toggle for the Principal to enable or disable student registration for teachers.
    """
    current = SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION') == 'True'
    new_state = not current
    SchoolSetting.set_setting(
        'ALLOW_TEACHER_STUDENT_REGISTRATION',
        'True' if new_state else 'False',
        "Permit teachers to register students during onboarding"
    )
    log_activity(
        action='SETTINGS_UPDATED',
        target_model='SchoolSetting',
        target_id='ALLOW_TEACHER_STUDENT_REGISTRATION',
        target_repr='Student Onboarding Mode',
        description=f"Toggled Student Onboarding Mode to {new_state} (Teachers can enroll students: {new_state}).",
        changes={'ALLOW_TEACHER_STUDENT_REGISTRATION': new_state},
        actor=request.user,
    )
    state_str = "activated (all teachers can now enroll students)" if new_state else "deactivated (restricted to Principal only)"
    messages.success(request, f"🚀 Student Onboarding Mode has been {state_str}.")
    next_url = request.GET.get('next') or request.POST.get('next') or 'student_list'
    return redirect(next_url)


@principal_required
def student_update_view(request, pk):
    """Edit student details or bus/canteen assignment."""
    student = get_object_or_404(Student, pk=pk)
    if request.method == 'POST':
        form = StudentForm(request.POST, instance=student, user_obj=request.user)
        if form.is_valid():
            student = form.save(commit=False)
            if not student.registered_by and request.user.is_authenticated:
                student.registered_by = request.user
            student.save()
            log_activity(
                action='STUDENT_UPDATED',
                target_model='Student',
                target_id=str(student.id),
                target_repr=f"{student.full_name} ({student.student_class})",
                description=f"Updated profile for student {student.full_name} ({student.student_class}).",
                changes={
                    'full_name': student.full_name,
                    'student_class': str(student.student_class),
                    'bus': student.bus.name if student.bus else None,
                    'canteen_enrolled': student.canteen_enrolled,
                },
                actor=request.user,
            )
            messages.success(request, f"Student '{student.full_name}' updated successfully!")
            return redirect('student_list')
    else:
        form = StudentForm(instance=student, user_obj=request.user)
    return render(request, 'attendance/student_form.html', {'form': form, 'student': student, 'title': f"Edit {student.full_name}"})


@principal_required
def toggle_student_lunch_view(request, pk):
    """
    1-Click toggle to include or remove a student from the school lunch program.
    Supports both AJAX (instant client-side update) and standard POST redirect.
    """
    student = get_object_or_404(Student, pk=pk)
    if request.method == 'POST':
        student.canteen_enrolled = not student.canteen_enrolled
        student.save(update_fields=['canteen_enrolled'])

        status_text = "Lunch Included" if student.canteen_enrolled else "No Lunch (Opted Out)"
        msg = f"{student.full_name}: {status_text}."

        log_activity(
            action='LUNCH_TOGGLED',
            target_model='Student',
            target_id=str(student.id),
            target_repr=student.full_name,
            description=f"Changed lunch program enrollment for {student.full_name} to: {status_text}.",
            changes={'canteen_enrolled': student.canteen_enrolled},
            actor=request.user,
        )

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({
                'success': True,
                'student_id': student.id,
                'canteen_enrolled': student.canteen_enrolled,
                'status_text': status_text,
                'message': msg,
            })

        messages.success(request, msg)
    return redirect('student_list')


@principal_required
def student_delete_view(request, pk):
    """Deactivate or remove student."""
    student = get_object_or_404(Student, pk=pk)
    if request.method == 'POST':
        name = student.full_name
        cls_name = str(student.student_class)
        log_activity(
            action='STUDENT_DELETED',
            target_model='Student',
            target_id=str(student.id),
            target_repr=f"{name} ({cls_name})",
            description=f"Permanently removed student {name} ({cls_name}) from school roster.",
            changes={'student_name': name, 'student_class': cls_name},
            actor=request.user,
        )
        student.delete()
        messages.success(request, f"Student '{name}' removed from roster.")
        return redirect('student_list')
    return render(request, 'attendance/confirm_delete.html', {
        'object': student,
        'object_name': f"Student '{student.full_name}' ({student.student_class})",
        'cancel_url': 'student_list',
    })


# ==========================================
# FEE PAYMENTS VIEWS
# ==========================================
@finance_required
def payment_list_view(request):
    """Payments ledger showing history of bus and canteen fee payments."""
    payments = FeePayment.objects.select_related('student', 'student__bus', 'bus', 'recorded_by', 'recorded_by__profile').order_by('-payment_date', '-created_at')

    fee_type = request.GET.get('fee_type', '')
    student_class = request.GET.get('class', '')
    search = request.GET.get('q', '').strip()
    date_preset = request.GET.get('preset', '')
    date_str = request.GET.get('date', '').strip()
    collector = request.GET.get('collector', '').strip()

    today = timezone.localdate()

    if date_preset == 'today':
        payments = payments.filter(payment_date=today)
    elif date_preset == 'yesterday':
        payments = payments.filter(payment_date=today - timedelta(days=1))
    elif date_preset == 'week':
        payments = payments.filter(payment_date__gte=today - timedelta(days=7))
    elif date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            payments = payments.filter(payment_date=filter_date)
        except ValueError:
            pass

    if fee_type:
        payments = payments.filter(fee_type=fee_type)
    if student_class:
        payments = payments.filter(student__student_class=student_class)
    if collector:
        payments = payments.filter(recorded_by_id=collector)
    if search:
        payments = payments.filter(
            Q(student__first_name__icontains=search) |
            Q(student__last_name__icontains=search) |
            Q(receipt_number__icontains=search) |
            Q(recorded_by__first_name__icontains=search) |
            Q(recorded_by__last_name__icontains=search) |
            Q(recorded_by__username__icontains=search)
        )

    classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    total_amount = payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # Today's cashier collections
    today_payments = FeePayment.objects.filter(payment_date=today)
    today_total = today_payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_bus_total = today_payments.filter(fee_type='BUS').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_canteen_total = today_payments.filter(fee_type='CANTEEN').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # Authorized fee collection teachers summary (for Accountant audit overview)
    authorized_teachers = User.objects.filter(
        profile__role=UserProfile.ROLE_TEACHER,
        profile__can_collect_fees=True,
        is_active=True
    ).select_related('profile', 'profile__assigned_bus').prefetch_related('profile__assigned_buses').order_by('first_name', 'username')

    target_audit_date = today
    if date_str:
        try:
            target_audit_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_audit_date = today

    first_day_of_month = target_audit_date.replace(day=1)
    month_name = target_audit_date.strftime('%B %Y')

    teacher_ids = [t.id for t in authorized_teachers]

    # 1. Total collections recorded today per authorized teacher
    today_totals_by_teacher = dict(
        FeePayment.objects.filter(recorded_by_id__in=teacher_ids, payment_date=target_audit_date)
        .values('recorded_by_id')
        .annotate(total=Sum('amount'))
        .values_list('recorded_by_id', 'total')
    )

    # 2. Remittances and cash collections by teacher on audit date
    remittances_today = list(
        TeacherRemittance.objects.filter(date=target_audit_date)
        .select_related('teacher', 'teacher__profile', 'received_by')
        .order_by('-created_at')
    )
    remittance_by_teacher = defaultdict(Decimal)
    for rem in remittances_today:
        remittance_by_teacher[rem.teacher_id] += rem.amount

    cash_payments_today = (
        FeePayment.objects.filter(recorded_by_id__in=teacher_ids, payment_date=target_audit_date, payment_method='CASH')
        .values('recorded_by_id')
        .annotate(total=Sum('amount'))
    )
    cash_by_teacher = {item['recorded_by_id']: (item['total'] or Decimal('0.00')) for item in cash_payments_today}

    authorized_collectors_summary = []
    collectors_total_collected = Decimal('0.00')
    collectors_total_expected = Decimal('0.00')
    collectors_total_balance = Decimal('0.00')
    collectors_settled_count = 0
    collectors_partial_count = 0
    collectors_pending_count = 0

    for t in authorized_teachers:
        c_coll = cash_by_teacher.get(t.id, Decimal('0.00'))
        c_rem = remittance_by_teacher.get(t.id, Decimal('0.00'))
        cash_in_hand = max(Decimal('0.00'), c_coll - c_rem)
        t_today_total = today_totals_by_teacher.get(t.id, Decimal('0.00'))
        collectors_total_collected += t_today_total

        authorized_collectors_summary.append({
            'user': t,
            'today_total': t_today_total,
            'cash_collected': c_coll,
            'cash_remitted': c_rem,
            'cash_in_hand': cash_in_hand,
            'has_cash_in_hand': cash_in_hand > 0,
        })

    # All staff who have recorded payments for filter dropdown
    recorded_by_ids = FeePayment.objects.exclude(recorded_by=None).values_list('recorded_by_id', flat=True).distinct()
    collectors_list = User.objects.filter(id__in=recorded_by_ids).select_related('profile').order_by('first_name', 'username')

    # Drawer balancing metrics
    admin_cash_today = FeePayment.objects.filter(
        payment_date=target_audit_date,
        payment_method='CASH',
        recorded_by__profile__role__in=[UserProfile.ROLE_ADMIN, UserProfile.ROLE_ACCOUNTANT]
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    total_remitted_today = sum((rem.amount for rem in remittances_today), Decimal('0.00'))
    drawer_cash_today = admin_cash_today + total_remitted_today

    teacher_ids_set = set(t.id for t in authorized_teachers)
    total_teacher_cash = sum((cash_by_teacher.get(uid, Decimal('0.00')) for uid in teacher_ids_set), Decimal('0.00'))
    teacher_cash_in_field = max(Decimal('0.00'), total_teacher_cash - total_remitted_today)

    today_momo_total = FeePayment.objects.filter(
        payment_date=target_audit_date,
        payment_method='MOBILE_MONEY'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    today_bank_total = FeePayment.objects.filter(
        payment_date=target_audit_date,
        payment_method__in=['BANK_TRANSFER', 'CHEQUE', 'CARD']
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    paginator = Paginator(payments, 20)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    context = {
        'payments': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'classes': classes,
        'fee_type': fee_type,
        'student_class': student_class,
        'search': search,
        'date_preset': date_preset,
        'date_str': date_str,
        'target_audit_date': target_audit_date,
        'target_audit_date_str': target_audit_date.strftime('%B %d, %Y'),
        'month_name': month_name,
        'selected_collector': collector,
        'collectors_list': collectors_list,
        'authorized_collectors_summary': authorized_collectors_summary,
        'collectors_total_expected': collectors_total_expected,
        'collectors_total_collected': collectors_total_collected,
        'collectors_total_balance': collectors_total_balance,
        'collectors_settled_count': collectors_settled_count,
        'collectors_partial_count': collectors_partial_count,
        'collectors_pending_count': collectors_pending_count,
        'total_amount': total_amount,
        'payment_count': paginator.count,
        'today_total': today_total,
        'today_bus_total': today_bus_total,
        'today_canteen_total': today_canteen_total,
        'drawer_cash_today': drawer_cash_today,
        'teacher_cash_in_field': teacher_cash_in_field,
        'total_remitted_today': total_remitted_today,
        'today_momo_total': today_momo_total,
        'today_bank_total': today_bank_total,
        'remittances_today': remittances_today,
    }
    return render(request, 'attendance/payments.html', context)


@finance_required
def payment_audit_summary_api(request):
    """
    AJAX endpoint returning live bursary drawer balances, unremitted teacher cash,
    and verified remittance settlements. Enables instant UI updates without full reloads.
    """
    date_str = request.GET.get('date', '').strip()
    target_date = timezone.localdate()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()

    authorized_teachers = list(User.objects.filter(
        profile__role=UserProfile.ROLE_TEACHER,
        profile__can_collect_fees=True,
        is_active=True
    ).select_related('profile').order_by('first_name', 'username'))

    teacher_ids = [t.id for t in authorized_teachers]

    today_totals_by_teacher = dict(
        FeePayment.objects.filter(recorded_by_id__in=teacher_ids, payment_date=target_date)
        .values('recorded_by_id')
        .annotate(total=Sum('amount'))
        .values_list('recorded_by_id', 'total')
    )

    remittances_today = list(
        TeacherRemittance.objects.filter(date=target_date)
        .select_related('teacher', 'teacher__profile', 'received_by')
        .order_by('-created_at')
    )
    remittance_by_teacher = defaultdict(Decimal)
    for rem in remittances_today:
        remittance_by_teacher[rem.teacher_id] += rem.amount

    cash_payments_today = (
        FeePayment.objects.filter(recorded_by_id__in=teacher_ids, payment_date=target_date, payment_method='CASH')
        .values('recorded_by_id')
        .annotate(total=Sum('amount'))
    )
    cash_by_teacher = {item['recorded_by_id']: (item['total'] or Decimal('0.00')) for item in cash_payments_today}

    admin_cash_today = FeePayment.objects.filter(
        payment_date=target_date,
        payment_method='CASH',
        recorded_by__profile__role__in=[UserProfile.ROLE_ADMIN, UserProfile.ROLE_ACCOUNTANT]
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    total_remitted_today = sum((rem.amount for rem in remittances_today), Decimal('0.00'))
    drawer_cash_today = admin_cash_today + total_remitted_today

    teacher_ids_set = set(teacher_ids)
    total_teacher_cash = sum((cash_by_teacher.get(uid, Decimal('0.00')) for uid in teacher_ids_set), Decimal('0.00'))
    teacher_cash_in_field = max(Decimal('0.00'), total_teacher_cash - total_remitted_today)

    today_momo_total = FeePayment.objects.filter(
        payment_date=target_date,
        payment_method='MOBILE_MONEY'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    today_bank_total = FeePayment.objects.filter(
        payment_date=target_date,
        payment_method__in=['BANK_TRANSFER', 'CHEQUE', 'CARD']
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    collectors = []
    collectors_total_collected = Decimal('0.00')
    for t in authorized_teachers:
        c_coll = cash_by_teacher.get(t.id, Decimal('0.00'))
        c_rem = remittance_by_teacher.get(t.id, Decimal('0.00'))
        cash_in_hand = max(Decimal('0.00'), c_coll - c_rem)
        t_today_total = today_totals_by_teacher.get(t.id, Decimal('0.00'))
        collectors_total_collected += t_today_total
        collectors.append({
            'id': t.id,
            'name': t.get_full_name() or t.username,
            'username': t.username,
            'initials': f"{(t.first_name or t.username)[:1]}{(t.last_name or '')[:1]}",
            'cash_collected': float(c_coll),
            'cash_remitted': float(c_rem),
            'cash_in_hand': float(cash_in_hand),
            'today_total': float(t_today_total),
        })

    remittances_data = []
    for rem in remittances_today:
        remittances_data.append({
            'id': rem.id,
            'reference_code': rem.reference_code,
            'teacher_name': rem.teacher.get_full_name() or rem.teacher.username,
            'amount': float(rem.amount),
            'received_by': rem.received_by.get_full_name() or rem.received_by.username,
            'time': rem.created_at.strftime('%I:%M %p'),
        })

    return JsonResponse({
        'success': True,
        'date_str': target_date.strftime('%B %d, %Y'),
        'drawer_cash_today': float(drawer_cash_today),
        'teacher_cash_in_field': float(teacher_cash_in_field),
        'today_momo_total': float(today_momo_total),
        'today_bank_total': float(today_bank_total),
        'total_remitted_today': float(total_remitted_today),
        'collectors_total_collected': float(collectors_total_collected),
        'collectors': collectors,
        'remittances': remittances_data,
    })


@finance_required
def teacher_remittance_create_view(request):
    """
    One-sided cash handover settlement endpoint:
    Bursar selects a teacher, enters the physical cash amount received,
    and acknowledges receipt. System records settlement and generates a slip.
    """
    user_profile = getattr(request.user, 'profile', None)
    is_finance = bool(
        request.user.is_superuser or
        (user_profile and (user_profile.is_school_admin or user_profile.is_accountant))
    )
    if not is_finance:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'Finance administrator privileges required.'}, status=403)
        messages.error(request, 'Finance administrator privileges required.')
        return redirect('payment_list')

    if request.method == 'POST':
        teacher_id = request.POST.get('teacher_id')
        amount_raw = request.POST.get('amount', '').strip()
        notes = request.POST.get('notes', '').strip()
        date_str = request.POST.get('date', '').strip()

        if not teacher_id:
            error_msg = 'Please select a teacher.'
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect('payment_list')

        User = get_user_model()
        teacher = get_object_or_404(User, pk=teacher_id)

        try:
            amount = Decimal(amount_raw)
            if amount <= 0:
                raise ValueError
        except Exception:
            error_msg = 'Please enter a valid positive cash amount.'
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect('payment_list')

        target_date = timezone.localdate()
        if date_str:
            try:
                target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            except ValueError:
                pass

        # Calculate expected unremitted cash for this teacher on this date
        cash_collected = FeePayment.objects.filter(
            recorded_by=teacher,
            payment_date=target_date,
            payment_method='CASH'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        already_remitted = TeacherRemittance.objects.filter(
            teacher=teacher,
            date=target_date
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        expected_amount = max(Decimal('0.00'), cash_collected - already_remitted)
        variance = amount - expected_amount

        # Generate unique reference code
        random_suffix = uuid.uuid4().hex[:4].upper()
        ref_code = f"REM-{target_date.strftime('%Y%m%d')}-{random_suffix}"

        remittance = TeacherRemittance.objects.create(
            teacher=teacher,
            received_by=request.user,
            amount=amount,
            expected_amount=expected_amount,
            variance=variance,
            date=target_date,
            reference_code=ref_code,
            notes=notes
        )

        teacher_name = teacher.get_full_name() or teacher.username
        currency = get_active_currency_symbol()

        log_activity(
            action='REMITTANCE_SETTLED',
            target_model='TeacherRemittance',
            target_id=str(remittance.id),
            target_repr=f"Slip {ref_code} ({teacher_name})",
            description=f"Settled physical cash handover of {currency}{amount:,.2f} from {teacher_name}. Slip Ref: {ref_code} (Variance: {currency}{variance:,.2f}).",
            changes={
                'amount': str(amount),
                'expected_amount': str(expected_amount),
                'variance': str(variance),
                'teacher': teacher_name,
                'reference_code': ref_code,
                'date': str(target_date),
            },
            actor=request.user,
        )

        success_msg = f"Successfully received {currency}{amount:,.2f} cash handover from {teacher_name}. Slip Ref: {ref_code}"
        messages.success(request, success_msg)

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'reference_code': ref_code,
                'amount': f"{amount:.2f}",
                'expected_amount': f"{expected_amount:.2f}",
                'variance': f"{variance:.2f}",
                'teacher_name': teacher_name,
                'receipt_url': reverse('teacher_remittance_receipt', kwargs={'pk': remittance.pk}),
                'message': success_msg
            })

        return redirect('teacher_remittance_receipt', pk=remittance.pk)

    return redirect('payment_list')


@login_required
def teacher_remittance_receipt_view(request, pk):
    """
    Printable settlement slip / acknowledgment receipt for a teacher cash handover.
    Accessible by Finance Officers (Bursar/Principal) and the remitting teacher.
    """
    remittance = get_object_or_404(
        TeacherRemittance.objects.select_related('teacher', 'teacher__profile', 'received_by', 'received_by__profile'),
        pk=pk
    )
    user = request.user
    user_profile = getattr(user, 'profile', None)

    is_finance = bool(user.is_superuser or (user_profile and (user_profile.is_school_admin or user_profile.is_accountant)))
    is_remitting_teacher = (user.id == remittance.teacher_id)

    if not (is_finance or is_remitting_teacher):
        messages.error(request, 'Access restricted to finance administrators and the remitting teacher.')
        return redirect('dashboard')

    context = {
        'remittance': remittance,
        'currency': get_active_currency_symbol(),
        'school_name': getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School'),
    }
    return render(request, 'attendance/remittance_receipt.html', context)


@login_required
def my_collections_view(request):
    """
    Convenience view redirecting authorized fee teachers to their own
    collection audit dossier. Administrators are directed to the main financial ledger.
    """
    user_profile = getattr(request.user, 'profile', None)
    if request.user.is_superuser or (user_profile and user_profile.is_school_admin):
        return redirect('payment_list')
    if not (user_profile and user_profile.can_view_my_collections):
        messages.error(request, "Access restricted: You are not authorized to collect fees.")
        return redirect('dashboard')
    return redirect('collector_audit', pk=request.user.pk)


@login_required
def collector_audit_view(request, pk):
    """
    Dedicated financial audit dossier and collection log for an authorized teacher.
    Accessible to Principal, Accountant, and the teacher themselves for their own intake records.
    Displays:
    - Target Expected Today (with Bus/Canteen breakdown & absent student insight)
    - Cash vs Electronic (MoMo/Bank) Collected Today
    - Cash Remitted to Bursar (with Settlement Slips) vs Cash Still in Hand
    - Unpaid Student Roster for Today with 1-click collection shortcut
    - Cumulative intake for current month
    - Paginated & searchable timestamped log of all fees collected by this teacher.
    """
    teacher = get_object_or_404(
        User.objects.select_related('profile', 'profile__assigned_bus').prefetch_related('profile__assigned_buses'),
        pk=pk
    )
    user_profile = getattr(request.user, 'profile', None)
    
    is_finance_admin = bool(request.user.is_superuser or (user_profile and user_profile.can_view_revenue))
    is_authorized_self = bool(
        request.user.id == teacher.id and
        user_profile and
        user_profile.can_view_my_collections
    )

    if not (is_finance_admin or is_authorized_self):
        messages.error(request, "Access denied: You do not have permission to access financial records.")
        return redirect('dashboard')

    is_self_view = (request.user.id == teacher.id and not is_finance_admin)
    today = timezone.localdate()
    
    date_str = request.GET.get('date', '').strip()
    log_period = request.GET.get('period', 'today')  # today, week, month, all
    search_q = request.GET.get('q', '').strip()
    fee_type_filter = request.GET.get('fee_type', '').strip()
    method_filter = request.GET.get('method', '').strip()
    
    target_date = today
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = today

    first_day_of_month = target_date.replace(day=1)
    month_name = target_date.strftime('%B %Y')
    _, last_day = calendar.monthrange(target_date.year, target_date.month)
    last_day_of_month = target_date.replace(day=last_day)

    # 1. Audited daily expectations & unpaid roster in O(1) constant queries
    collectors_meta = get_collectors_financial_summary([teacher], target_date)
    meta = collectors_meta.get(teacher.id, {})

    today_collected = meta.get('today_collected', Decimal('0.00'))
    today_count = meta.get('today_count', 0)
    class_expected = meta.get('adjusted_target', Decimal('0.00'))
    baseline_target = meta.get('baseline_target', Decimal('0.00'))
    class_balance = meta.get('balance', Decimal('0.00'))
    present_balance = meta.get('present_balance', class_balance)
    student_count = meta.get('student_count', 0)
    unpaid_students = meta.get('unpaid_students', [])

    # 2. Payment method breakdown for target_date (1 aggregate query)
    today_method_rows = (
        FeePayment.objects.filter(recorded_by=teacher, payment_date=target_date)
        .values('payment_method')
        .annotate(total=Sum('amount'), count=Count('id'))
    )
    cash_collected_today = Decimal('0.00')
    momo_collected_today = Decimal('0.00')
    bank_collected_today = Decimal('0.00')
    for row in today_method_rows:
        amt = row['total'] or Decimal('0.00')
        m = row['payment_method']
        if m == 'CASH':
            cash_collected_today += amt
        elif m == 'MOBILE_MONEY':
            momo_collected_today += amt
        else:
            bank_collected_today += amt
    electronic_collected_today = momo_collected_today + bank_collected_today

    # 3. Handover / Remittances accepted by Bursar on target_date (1 query with select_related)
    remittances_today = list(
        TeacherRemittance.objects.filter(teacher=teacher, date=target_date)
        .select_related('received_by', 'received_by__profile')
        .order_by('-created_at')
    )
    cash_remitted_today = sum((r.amount for r in remittances_today), Decimal('0.00'))
    cash_in_hand = max(Decimal('0.00'), cash_collected_today - cash_remitted_today)

    # 4. Monthly & All-Time cumulative collections (2 aggregate queries)
    month_data = FeePayment.objects.filter(
        recorded_by=teacher,
        payment_date__gte=first_day_of_month,
        payment_date__lte=last_day_of_month
    ).aggregate(total=Sum('amount'), count=Count('id'))
    month_total = month_data['total'] or Decimal('0.00')
    month_count = month_data['count']

    all_data = FeePayment.objects.filter(recorded_by=teacher).aggregate(total=Sum('amount'), count=Count('id'))
    all_total = all_data['total'] or Decimal('0.00')
    all_count = all_data['count']

    # Determine status & compliance percentage
    if today_collected >= class_expected and class_expected > 0:
        status_label = 'Settled'
        status_badge_class = 'stat-badge-success'
        compliance_pct = 100
    elif today_collected > 0 and class_balance > 0:
        status_label = 'Partial'
        status_badge_class = 'stat-badge-warning'
        compliance_pct = min(100, int((today_collected / class_expected) * 100)) if class_expected > 0 else 100
    elif today_collected == 0 and class_expected > 0:
        status_label = 'Pending Intake'
        status_badge_class = 'stat-badge-danger'
        compliance_pct = 0
    else:
        status_label = 'Settled' if today_collected > 0 else 'No Target'
        status_badge_class = 'stat-badge-success' if today_collected > 0 else 'stat-badge-info'
        compliance_pct = 100 if today_collected > 0 else 0

    # 5. Filtered & Paginated audit log (zero N+1 via select_related)
    audit_logs_qs = (
        FeePayment.objects.filter(recorded_by=teacher)
        .select_related('student', 'student__bus', 'bus')
        .order_by('-payment_date', '-created_at')
    )

    if log_period == 'today':
        audit_logs_qs = audit_logs_qs.filter(payment_date=target_date)
    elif log_period == 'week':
        audit_logs_qs = audit_logs_qs.filter(payment_date__gte=target_date - timedelta(days=7), payment_date__lte=target_date)
    elif log_period == 'month':
        audit_logs_qs = audit_logs_qs.filter(payment_date__gte=first_day_of_month, payment_date__lte=last_day_of_month)
    elif log_period == 'all':
        pass

    if search_q:
        audit_logs_qs = audit_logs_qs.filter(
            Q(student__first_name__icontains=search_q) |
            Q(student__last_name__icontains=search_q) |
            Q(receipt_number__icontains=search_q)
        )
    if fee_type_filter in ('BUS', 'CANTEEN'):
        audit_logs_qs = audit_logs_qs.filter(fee_type=fee_type_filter)
    if method_filter:
        audit_logs_qs = audit_logs_qs.filter(payment_method=method_filter)

    log_data = audit_logs_qs.aggregate(total=Sum('amount'), count=Count('id'))
    filtered_log_total = log_data['total'] or Decimal('0.00')
    audit_logs_count = log_data['count']

    paginator = Paginator(audit_logs_qs, 25)
    page_number = request.GET.get('page', 1)
    try:
        audit_page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        audit_page_obj = paginator.page(1)

    context = {
        'teacher': teacher,
        'profile': getattr(teacher, 'profile', None),
        'is_self_view': is_self_view,
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'target_date_str': target_date.strftime('%B %d, %Y'),
        'month_name': month_name,
        'class_expected': class_expected,
        'baseline_target': baseline_target,
        'today_collected': today_collected,
        'today_count': today_count,
        'cash_collected_today': cash_collected_today,
        'momo_collected_today': momo_collected_today,
        'bank_collected_today': bank_collected_today,
        'electronic_collected_today': electronic_collected_today,
        'remittances_today': remittances_today,
        'cash_remitted_today': cash_remitted_today,
        'cash_in_hand': cash_in_hand,
        'class_balance': class_balance,
        'present_balance': present_balance,
        'compliance_pct': compliance_pct,
        'month_total': month_total,
        'month_count': month_count,
        'all_total': all_total,
        'all_count': all_count,
        'meta': meta,
        'unpaid_students': unpaid_students,
        'status_label': status_label,
        'status_badge_class': status_badge_class,
        'student_count': student_count,
        'log_period': log_period,
        'search_q': search_q,
        'fee_type_filter': fee_type_filter,
        'method_filter': method_filter,
        'audit_logs': audit_page_obj,
        'page_obj': audit_page_obj,
        'is_paginated': audit_page_obj.has_other_pages(),
        'audit_logs_count': audit_logs_count,
        'filtered_log_total': filtered_log_total,
        'currency': get_active_currency_symbol(),
    }
    return render(request, 'attendance/collector_audit.html', context)


def get_student_payment_summary(student, target_date=None):
    """
    Computes real-time daily fee context, explicit prepaid wallet balances,
    today's deduction status, and partial remainder top-up amounts for a student.
    """
    if target_date is None:
        target_date = timezone.localdate()

    bus_fee_dec = student.bus_fee_required if student.bus else Decimal('0.00')
    canteen_fee_dec = student.canteen_fee_required if student.canteen_enrolled else Decimal('0.00')

    bus_wallet_dec = student.get_bus_credit_balance(target_date)
    can_wallet_dec = student.get_canteen_credit_balance(target_date)

    bus_paid_dec = student.get_daily_bus_paid(target_date)
    can_paid_dec = student.get_daily_canteen_paid(target_date)

    bus_deducted_dec = student.get_deducted_amount_for_date('BUS', target_date) if (student.bus and bus_fee_dec > 0) else Decimal('0.00')
    can_deducted_dec = student.get_deducted_amount_for_date('CANTEEN', target_date) if (student.canteen_enrolled and canteen_fee_dec > 0) else Decimal('0.00')

    bus_settled_amt = bus_paid_dec + bus_deducted_dec
    can_settled_amt = can_paid_dec + can_deducted_dec

    bus_owed_today = max(Decimal('0.00'), bus_fee_dec - bus_settled_amt) if (student.bus and bus_fee_dec > 0) else Decimal('0.00')
    can_owed_today = max(Decimal('0.00'), canteen_fee_dec - can_settled_amt) if (student.canteen_enrolled and canteen_fee_dec > 0) else Decimal('0.00')

    bus_to_deduct_dec = min(bus_wallet_dec, bus_owed_today)
    can_to_deduct_dec = min(can_wallet_dec, can_owed_today)

    bus_cash_needed_dec = max(Decimal('0.00'), bus_owed_today - bus_to_deduct_dec)
    can_cash_needed_dec = max(Decimal('0.00'), can_owed_today - can_to_deduct_dec)

    bus_wallet_after_dec = max(Decimal('0.00'), bus_wallet_dec - bus_to_deduct_dec)
    can_wallet_after_dec = max(Decimal('0.00'), can_wallet_dec - can_to_deduct_dec)

    bus_fee = float(bus_fee_dec)
    canteen_fee = float(canteen_fee_dec)
    bus_credit = float(bus_wallet_dec)
    can_credit = float(can_wallet_dec)
    bus_paid = float(bus_paid_dec)
    can_paid = float(can_paid_dec)
    bus_deducted_today = float(bus_deducted_dec)
    canteen_deducted_today = float(can_deducted_dec)

    bus_to_deduct = round(float(bus_to_deduct_dec), 2)
    canteen_to_deduct = round(float(can_to_deduct_dec), 2)
    total_to_deduct = round(bus_to_deduct + canteen_to_deduct, 2)

    bus_cash_needed = round(float(bus_cash_needed_dec), 2)
    canteen_cash_needed = round(float(can_cash_needed_dec), 2)
    total_cash_needed = round(bus_cash_needed + canteen_cash_needed, 2)

    bus_wallet_after = round(float(bus_wallet_after_dec), 2)
    canteen_wallet_after = round(float(can_wallet_after_dec), 2)
    total_wallet_after = round(bus_wallet_after + canteen_wallet_after, 2)

    bus_balance = bus_cash_needed
    canteen_balance = canteen_cash_needed

    # Partial credit / part-payment top-up calculations
    bus_has_partial = bool(
        student.bus
        and bus_owed_today > Decimal('0.00')
        and (
            Decimal('0.00') < bus_wallet_dec < bus_owed_today
            or Decimal('0.00') < bus_settled_amt < bus_fee_dec
        )
    )
    bus_remainder_due = bus_cash_needed if bus_has_partial else 0.0

    can_has_partial = bool(
        student.canteen_enrolled
        and can_owed_today > Decimal('0.00')
        and (
            Decimal('0.00') < can_wallet_dec < can_owed_today
            or Decimal('0.00') < can_settled_amt < canteen_fee_dec
        )
    )
    can_remainder_due = canteen_cash_needed if can_has_partial else 0.0

    # Remaining advance credit days after covering today
    bus_credit_days = int(bus_wallet_after_dec // bus_fee_dec) if (student.bus and bus_fee_dec > 0 and bus_wallet_dec >= bus_owed_today) else 0
    bus_credit_remaining = bus_wallet_after if (student.bus and bus_fee_dec > 0 and bus_wallet_dec >= bus_owed_today) else 0.0

    canteen_credit_days = int(can_wallet_after_dec // canteen_fee_dec) if (student.canteen_enrolled and canteen_fee_dec > 0 and can_wallet_dec >= can_owed_today) else 0
    canteen_credit_remaining = canteen_wallet_after if (student.canteen_enrolled and canteen_fee_dec > 0 and can_wallet_dec >= can_owed_today) else 0.0

    bus_settled_today = bool((not student.bus) or (bus_fee_dec <= Decimal('0.00')) or (bus_owed_today <= Decimal('0.00')))
    canteen_settled_today = bool((not student.canteen_enrolled) or (canteen_fee_dec <= Decimal('0.00')) or (can_owed_today <= Decimal('0.00')))

    is_bus_paid = bool((not student.bus) or (bus_fee <= 0.0) or (bus_balance <= 0.0))
    is_canteen_paid = bool((not student.canteen_enrolled) or (canteen_fee <= 0.0) or (canteen_balance <= 0.0))
    has_applicable_fees = bool((student.bus and bus_fee > 0) or (student.canteen_enrolled and canteen_fee > 0))
    is_fully_paid = bool(has_applicable_fees and is_bus_paid and is_canteen_paid)
    is_fully_settled_today = bool(has_applicable_fees and bus_settled_today and canteen_settled_today)

    has_bus_bulk = bool(student.bus and bus_fee > 0 and (bus_wallet_dec > Decimal('0.00') or bus_deducted_dec > Decimal('0.00')))
    has_canteen_bulk = bool(student.canteen_enrolled and canteen_fee > 0 and (can_wallet_dec > Decimal('0.00') or can_deducted_dec > Decimal('0.00')))
    has_bulk_payment = has_bus_bulk or has_canteen_bulk
    total_wallet_balance = round(bus_credit + can_credit, 2)
    total_credit_remaining = total_wallet_after if is_fully_paid else round(bus_credit_remaining + canteen_credit_remaining, 2)
    has_wallet_action = bool(has_applicable_fees and (total_wallet_balance > 0 or total_to_deduct > 0 or is_fully_settled_today))

    clock_in = ClockInRecord.objects.filter(student=student, date=target_date).first()
    is_clocked_in = clock_in is not None
    clock_in_time = clock_in.clock_in_time.strftime('%I:%M %p') if (clock_in and clock_in.clock_in_time) else ''

    return {
        'bus_fee': bus_fee,
        'bus_paid': bus_paid,
        'bus_deducted_today': bus_deducted_today,
        'bus_settled_today': bus_settled_today,
        'bus_balance': bus_balance,
        'bus_credit': bus_credit,
        'bus_wallet_balance': round(bus_credit, 2),
        'bus_to_deduct': bus_to_deduct,
        'bus_cash_needed': bus_cash_needed,
        'bus_wallet_after': bus_wallet_after,
        'bus_has_partial': bus_has_partial,
        'bus_remainder_due': bus_remainder_due,
        'bus_credit_days': bus_credit_days,
        'bus_credit_remaining': bus_credit_remaining,
        'canteen_fee': canteen_fee,
        'canteen_paid': can_paid,
        'canteen_deducted_today': canteen_deducted_today,
        'canteen_settled_today': canteen_settled_today,
        'canteen_balance': canteen_balance,
        'canteen_credit': can_credit,
        'canteen_wallet_balance': round(can_credit, 2),
        'canteen_to_deduct': canteen_to_deduct,
        'canteen_cash_needed': canteen_cash_needed,
        'canteen_wallet_after': canteen_wallet_after,
        'canteen_has_partial': can_has_partial,
        'canteen_remainder_due': can_remainder_due,
        'canteen_credit_days': canteen_credit_days,
        'canteen_credit_remaining': canteen_credit_remaining,
        'total_wallet_balance': total_wallet_balance,
        'total_to_deduct': total_to_deduct,
        'total_cash_needed': total_cash_needed,
        'total_wallet_after': total_wallet_after,
        'total_credit_remaining': total_credit_remaining,
        'is_bus_paid': is_bus_paid,
        'is_canteen_paid': is_canteen_paid,
        'is_fully_paid': is_fully_paid,
        'is_fully_settled_today': is_fully_settled_today,
        'has_applicable_fees': has_applicable_fees,
        'has_bus_bulk': has_bus_bulk,
        'has_canteen_bulk': has_canteen_bulk,
        'has_bulk_payment': has_bulk_payment,
        'has_wallet_action': has_wallet_action,
        'is_clocked_in': is_clocked_in,
        'clock_in_time': clock_in_time,
    }


@payment_recording_required
def student_payment_details_api(request, student_id):
    """AJAX API endpoint: Returns live payment balances and credit status for a student."""
    student = get_object_or_404(Student, id=student_id, is_active=True)
    date_str = request.GET.get('date')
    target_date = timezone.localdate()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    details = get_student_payment_summary(student, target_date)
    return JsonResponse({'success': True, 'details': details})


@payment_recording_required
@require_POST
def wallet_settle_and_clockin_api(request):
    """
    Explicitly deducts today's fee(s) from the student's prepaid wallet (and collects
    any exact partial remainder top-up in Cash/MoMo if credit < daily fee), records
    today's ClockInRecord, and supports action='undo' to reverse deductions & clock-in.
    """
    try:
        data = json.loads(request.body) if (request.body and request.content_type == 'application/json') else request.POST
    except Exception:
        data = request.POST

    student_id = data.get('student_id')
    if not student_id:
        return JsonResponse({'success': False, 'error': 'Student ID is required.'}, status=400)

    action = str(data.get('action') or 'settle_and_clockin').strip().lower()
    date_str = (data.get('date') or '').strip()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    method = _normalize_payment_method(data.get('method') or data.get('payment_method'))

    raw_collect = data.get('collect_remainder', True)
    if isinstance(raw_collect, str):
        collect_remainder = raw_collect.strip().lower() not in ('0', 'false', 'no')
    else:
        collect_remainder = bool(raw_collect)

    currency = get_active_currency_symbol()
    period = get_active_academic_period()

    if action == 'undo':
        with transaction.atomic():
            student = get_object_or_404(Student.objects.select_for_update(), pk=student_id, is_active=True)
            clock_in = ClockInRecord.objects.filter(student=student, date=target_date).first()
            refunded, clock_in_removed = _undo_clockin_and_refund_wallet(
                student, target_date, clock_in, request.user
            )

        student.refresh_from_db()
        details = get_student_payment_summary(student, target_date)
        total_refunded = refunded['BUS'] + refunded['CANTEEN']
        if total_refunded > Decimal('0.00'):
            msg = f"Undid clock-in for {student.full_name} & refunded {currency}{total_refunded:.2f} back to wallet."
        elif clock_in_removed:
            msg = f"Clock-in undone for {student.full_name}."
        else:
            msg = f"Nothing to undo for {student.full_name}."
        return JsonResponse({
            'success': True,
            'action': 'undo',
            'refunded': float(total_refunded),
            'bus_refunded': float(refunded['BUS']),
            'canteen_refunded': float(refunded['CANTEEN']),
            'message': msg,
            'details': details,
        })

    # Action: settle_and_clockin
    now_time = timezone.localtime().time() if hasattr(timezone, 'localtime') else timezone.now().time()
    receipt_number = f"WLT-{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
    created_payments = []

    try:
        with transaction.atomic():
            student = get_object_or_404(Student.objects.select_for_update(), pk=student_id, is_active=True)
            clock_in, clock_in_created = ClockInRecord.objects.get_or_create(
                student=student,
                date=target_date,
                defaults={
                    'clock_in_time': now_time,
                    'status': 'PRESENT',
                    'recorded_by': request.user,
                },
            )

            bus_deducted = Decimal('0.00')
            bus_cash = Decimal('0.00')
            if student.bus_id and student.bus_fee_required > Decimal('0.00'):
                bus_owed = max(Decimal('0.00'), student.bus_fee_required - student.get_daily_bus_settled(target_date))
                if bus_owed > Decimal('0.00'):
                    bus_deducted = student.apply_credit_for_date(
                        'BUS', target_date=target_date, actor=request.user, clock_in=clock_in
                    )
                    bus_cash = max(Decimal('0.00'), bus_owed - bus_deducted)
                    if bus_cash > Decimal('0.00'):
                        if not collect_remainder:
                            raise ValueError(f"Remaining Bus top-up of {currency}{bus_cash:.2f} is required.")
                        note = (
                            f"Partial credit settlement + top-up (Wallet used: {currency}{bus_deducted:.2f})"
                            if bus_deducted > Decimal('0.00')
                            else f"Bus fee settled at clock-in"
                        )
                        p_bus = FeePayment(
                            student=student,
                            bus=student.bus,
                            is_temporary_bus=False,
                            fee_type='BUS',
                            amount=bus_cash,
                            payment_date=target_date,
                            period=period,
                            payment_method=method,
                            receipt_number=receipt_number,
                            notes=note,
                            recorded_by=request.user,
                        )
                        p_bus._skip_wallet_sync = True
                        p_bus.save()
                        created_payments.append(p_bus)

            canteen_deducted = Decimal('0.00')
            canteen_cash = Decimal('0.00')
            if student.canteen_enrolled and student.canteen_fee_required > Decimal('0.00'):
                can_owed = max(Decimal('0.00'), student.canteen_fee_required - student.get_daily_canteen_settled(target_date))
                if can_owed > Decimal('0.00'):
                    canteen_deducted = student.apply_credit_for_date(
                        'CANTEEN', target_date=target_date, actor=request.user, clock_in=clock_in
                    )
                    canteen_cash = max(Decimal('0.00'), can_owed - canteen_deducted)
                    if canteen_cash > Decimal('0.00'):
                        if not collect_remainder:
                            raise ValueError(f"Remaining Canteen top-up of {currency}{canteen_cash:.2f} is required.")
                        note = (
                            f"Partial credit settlement + top-up (Wallet used: {currency}{canteen_deducted:.2f})"
                            if canteen_deducted > Decimal('0.00')
                            else f"Canteen fee settled at clock-in"
                        )
                        p_can = FeePayment(
                            student=student,
                            fee_type='CANTEEN',
                            amount=canteen_cash,
                            payment_date=target_date,
                            period=period,
                            payment_method=method,
                            receipt_number=receipt_number,
                            notes=note,
                            recorded_by=request.user,
                        )
                        p_can._skip_wallet_sync = True
                        p_can.save()
                        created_payments.append(p_can)
    except ValueError as err:
        return JsonResponse({'success': False, 'error': str(err)}, status=400)

    total_deducted = bus_deducted + canteen_deducted
    total_cash = bus_cash + canteen_cash

    for p in created_payments:
        log_activity(
            action='PAYMENT_RECORDED',
            target_model='FeePayment',
            target_id=str(p.id),
            target_repr=f"Receipt #{p.receipt_number} ({student.full_name})",
            description=f"Collected {currency}{p.amount:.2f} {p.get_fee_type_display()} remainder via {p.get_payment_method_display()} for {student.full_name}.",
            changes={
                'amount': str(p.amount),
                'fee_type': p.fee_type,
                'payment_method': p.payment_method,
                'receipt_number': p.receipt_number,
                'payment_date': str(p.payment_date),
                'student_id': student.id,
            },
            actor=request.user,
        )

    if clock_in_created:
        log_activity(
            action='CLOCK_IN',
            target_model='Student',
            target_id=str(student.id),
            target_repr=student.full_name,
            description=(
                f"Clocked in {student.full_name} for {target_date} "
                f"(Wallet deducted: {currency}{total_deducted:.2f}, Top-up cash: {currency}{total_cash:.2f})."
            ),
            actor=request.user,
        )

    student.refresh_from_db()
    details = get_student_payment_summary(student, target_date)
    parts = []
    if total_deducted > Decimal('0.00'):
        parts.append(f"deducted {currency}{total_deducted:.2f} from wallet")
    if total_cash > Decimal('0.00'):
        parts.append(f"collected {currency}{total_cash:.2f}")
    if parts:
        msg = f"✓ Settled ({' + '.join(parts)}) & clocked in {student.full_name}!"
    else:
        msg = f"✓ Clocked in {student.full_name} at {details['clock_in_time']}."

    return JsonResponse({
        'success': True,
        'action': 'settle_and_clockin',
        'clock_in_time': details['clock_in_time'],
        'bus_deducted': float(bus_deducted),
        'canteen_deducted': float(canteen_deducted),
        'total_deducted': float(total_deducted),
        'cash_collected': float(total_cash),
        'message': msg,
        'details': details,
    })


@payment_recording_required
def payment_create_view(request):
    """Form to record a daily or advance fee payment."""
    student_id = request.GET.get('student_id')
    target_student = None
    initial = {}
    selected_class_id = None
    is_guest_mode_initial = False

    user_profile = getattr(request.user, 'profile', None) if request.user.is_authenticated else None
    assigned_buses_list = user_profile.get_assigned_buses_list() if user_profile else []
    is_bus_fee_teacher = bool(user_profile and user_profile.is_bus_fee_teacher and assigned_buses_list)
    is_admin_user = bool(user_profile and (user_profile.is_school_admin or user_profile.is_accountant))
    is_scoped_fee_teacher = is_bus_fee_teacher and not is_admin_user
    assigned_bus = assigned_buses_list[0] if assigned_buses_list else None

    if student_id:
        target_student = Student.objects.filter(id=student_id, is_active=True).first()
        if target_student:
            initial['student'] = target_student.id
            if is_scoped_fee_teacher and assigned_bus:
                initial['bus'] = assigned_bus.id
                if target_student.bus and target_student.bus != assigned_bus:
                    initial['is_one_off_bus'] = True
                    is_guest_mode_initial = True
            elif target_student.bus:
                initial['bus'] = target_student.bus.id

            if target_student.school_class:
                initial['student_class'] = str(target_student.school_class.id)
                selected_class_id = str(target_student.school_class.id)
            else:
                cls_obj = SchoolClass.objects.filter(name=target_student.student_class).first()
                if cls_obj:
                    initial['student_class'] = str(cls_obj.id)
                    selected_class_id = str(cls_obj.id)
    elif is_scoped_fee_teacher and assigned_bus:
        initial['bus'] = assigned_bus.id

    fee_type_arg = request.GET.get('fee_type')
    if fee_type_arg in ('BUS', 'CANTEEN'):
        initial['fee_type'] = fee_type_arg

    bus_id_arg = request.GET.get('bus_id')
    selected_bus_obj = None
    if bus_id_arg:
        try:
            selected_bus_obj = Bus.objects.filter(id=bus_id_arg, is_active=True).first()
            if selected_bus_obj:
                initial['bus'] = selected_bus_obj.id
                initial['fee_type'] = 'BUS'
        except Exception:
            pass

    is_one_off_arg = request.GET.get('is_one_off')
    if is_one_off_arg:
        initial['is_one_off_bus'] = True
        is_guest_mode_initial = True

    if request.method == 'POST':
        form = FeePaymentForm(request.POST)
        if form.is_valid():
            student = form.cleaned_data['student']
            if is_scoped_fee_teacher and assigned_bus:
                selected_bus = form.cleaned_data.get('bus') or assigned_bus
            else:
                selected_bus = form.cleaned_data.get('bus') or student.bus

            is_diff_bus = bool(selected_bus and student.bus != selected_bus) or bool(form.cleaned_data.get('is_one_off_bus'))

            # School Administrators can assign regular bus; Teachers cannot alter student regular bus on their own!
            if is_admin_user and not form.cleaned_data.get('is_one_off_bus'):
                if selected_bus != student.bus:
                    student.bus = selected_bus
                    student.save(update_fields=['bus'])

            bus_amount = form.cleaned_data.get('bus_amount') or Decimal('0.00')
            canteen_amount = form.cleaned_data.get('canteen_amount') or Decimal('0.00')
            payment_date = form.cleaned_data.get('payment_date') or timezone.localdate()
            period = form.cleaned_data.get('period') or get_active_academic_period()
            payment_method = _normalize_payment_method(form.cleaned_data.get('payment_method'))
            receipt_number = form.cleaned_data.get('receipt_number') or f"REC-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            notes = form.cleaned_data.get('notes') or ''
            recorded_by = request.user if request.user.is_authenticated else None

            if is_diff_bus and assigned_bus:
                orig_name = student.bus.name if student.bus else 'Walker'
                tag = f"[Guest ride on {assigned_bus.name} (Registered: {orig_name})]"
                if tag not in notes:
                    notes = f"{tag} {notes}".strip()

            is_bus_adv = bool(student.bus and student.bus_fee_required > 0 and bus_amount > student.bus_fee_required)
            bus_days_cov = int(bus_amount // student.bus_fee_required) if is_bus_adv else 1

            is_can_adv = bool(student.canteen_enrolled and student.canteen_fee_required > 0 and canteen_amount > student.canteen_fee_required)
            can_days_cov = int(canteen_amount // student.canteen_fee_required) if is_can_adv else 1

            created_payments = []
            with transaction.atomic():
                if bus_amount > Decimal('0.00'):
                    p_bus = FeePayment.objects.create(
                        student=student,
                        bus=selected_bus,
                        is_temporary_bus=is_diff_bus,
                        fee_type='BUS',
                        amount=bus_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=receipt_number,
                        is_advance=is_bus_adv,
                        days_covered=bus_days_cov,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_bus)

                if canteen_amount > Decimal('0.00'):
                    p_canteen = FeePayment.objects.create(
                        student=student,
                        fee_type='CANTEEN',
                        amount=canteen_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=receipt_number,
                        is_advance=is_can_adv,
                        days_covered=can_days_cov,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_canteen)

            currency = get_active_currency_symbol()
            total_amount = sum(p.amount for p in created_payments)

            for p in created_payments:
                log_activity(
                    action='PAYMENT_RECORDED',
                    target_model='FeePayment',
                    target_id=str(p.id),
                    target_repr=f"Receipt #{p.receipt_number} ({student.full_name})",
                    description=f"Recorded {p.get_fee_type_display()} payment of {currency}{p.amount:.2f} via {p.get_payment_method_display()} for student {student.full_name} ({student.student_class}).",
                    changes={
                        'amount': str(p.amount),
                        'fee_type': p.fee_type,
                        'payment_method': p.payment_method,
                        'receipt_number': p.receipt_number,
                        'payment_date': str(p.payment_date),
                        'student_id': student.id,
                        'student_name': student.full_name,
                    },
                    actor=request.user,
                )
            if len(created_payments) == 2:
                breakdown = f" ({currency}{bus_amount:.2f} Bus + {currency}{canteen_amount:.2f} Canteen)"
            elif len(created_payments) == 1:
                breakdown = f" ({created_payments[0].get_fee_type_display()})"
            else:
                breakdown = ""

            adv_tags = []
            if is_bus_adv and bus_amount > Decimal('0.00'):
                adv_tags.append(f"Bus: {bus_days_cov}d bulk")
            if is_can_adv and canteen_amount > Decimal('0.00'):
                adv_tags.append(f"Canteen: {can_days_cov}d bulk")
            adv_note = f" [{', '.join(adv_tags)}]" if adv_tags else ""

            wallet_topup_total = (
                CreditTransaction.objects.filter(
                    payment__in=created_payments,
                    transaction_type=CreditTransaction.TYPE_TOP_UP,
                ).aggregate(t=Sum('amount'))['t']
                or Decimal('0.00')
            )
            wallet_note = ""
            if wallet_topup_total > Decimal('0.00'):
                today_portion = max(Decimal('0.00'), total_amount - wallet_topup_total)
                if today_portion > Decimal('0.00'):
                    wallet_note = f" (Today settled: {currency}{today_portion:.2f}; {currency}{wallet_topup_total:.2f} added to Bulk Wallet)"
                else:
                    wallet_note = f" ({currency}{wallet_topup_total:.2f} added to Bulk Wallet)"

            messages.success(request, f"✓ Payment of {currency}{total_amount:.2f}{breakdown}{adv_note} for {student.full_name} recorded!{wallet_note} Receipt: {receipt_number}")
            if 'save_and_add' in request.POST:
                params = ['focus_search=1']
                if selected_bus_obj:
                    params.append(f"bus_id={selected_bus_obj.id}")
                if form.cleaned_data.get('is_one_off_bus') or is_one_off_arg:
                    params.append('is_one_off=1')
                return redirect(f"{reverse('payment_create')}?{'&'.join(params)}")
            if user_profile and user_profile.can_view_revenue:
                return redirect('payment_list')
            if user_profile and user_profile.can_view_my_collections:
                return redirect('my_collections')
            return redirect('dashboard')
    else:
        form = FeePaymentForm(initial=initial)

    today = timezone.localdate()
    all_students_by_class = {}
    scoped_students_by_class = {}
    all_bus_riders = []

    school_classes = list(SchoolClass.objects.filter(is_active=True).order_by('name'))
    class_name_to_id = {sc.name.strip(): str(sc.id) for sc in school_classes if sc.name}

    students_qs = Student.objects.filter(is_active=True).select_related('bus', 'school_class').order_by('first_name', 'last_name')
    for s in students_qs:
        cls_key = str(s.school_class_id) if s.school_class_id else class_name_to_id.get((s.student_class or '').strip(), "")

        s_dict = {
            'id': s.id,
            'name': s.full_name,
            'class_id': cls_key,
            'class_name': s.school_class.name if s.school_class else (s.student_class or 'Unassigned'),
            'bus_id': s.bus_id or '',
            'bus_name': s.bus.name if s.bus else 'Walker',
            'has_bus': bool(s.bus_id),
            'bus_fee': float(s.bus_fee_required),
            'canteen_enrolled': bool(s.canteen_enrolled),
            'canteen_fee': float(s.canteen_fee_required),
            'bus_wallet': float(s.bus_credit_wallet or Decimal('0.00')),
            'canteen_wallet': float(s.canteen_credit_wallet or Decimal('0.00')),
        }

        # If a specific student was targeted in the URL, preload their financial details immediately
        if target_student and s.id == target_student.id:
            s_dict.update(get_student_payment_summary(s, today))
            s_dict['loaded_date'] = today.strftime('%Y-%m-%d')

        if cls_key:
            if cls_key not in all_students_by_class:
                all_students_by_class[cls_key] = []
            all_students_by_class[cls_key].append(s_dict)

        assigned_bus_ids_set = {b.id for b in assigned_buses_list}
        if is_scoped_fee_teacher and s.bus_id in assigned_bus_ids_set:
            all_bus_riders.append(s_dict)
            if cls_key:
                if cls_key not in scoped_students_by_class:
                    scoped_students_by_class[cls_key] = []
                scoped_students_by_class[cls_key].append(s_dict)

    scoped_classes = []
    if is_scoped_fee_teacher and assigned_bus:
        for sc in school_classes:
            sc_id_str = str(sc.id)
            count = len(scoped_students_by_class.get(sc_id_str, []))
            if count > 0:
                scoped_classes.append({
                    'id': sc_id_str,
                    'name': f'{sc.name} ({count} riders)'
                })

    all_classes = [
        {'id': str(sc.id), 'name': sc.name}
        for sc in school_classes
    ]

    is_class_only_fee_teacher = bool(
        not is_admin_user and not is_bus_fee_teacher
        and user_profile and user_profile.role == UserProfile.ROLE_TEACHER
        and user_profile.assigned_class
    )
    if is_class_only_fee_teacher:
        assigned_cls_id = class_name_to_id.get(user_profile.assigned_class.strip(), '')
        if assigned_cls_id:
            all_classes = [c for c in all_classes if c['id'] == assigned_cls_id]
            all_students_by_class = {assigned_cls_id: all_students_by_class.get(assigned_cls_id, [])}
            if not selected_class_id:
                selected_class_id = assigned_cls_id

    active_classes_list = scoped_classes if is_scoped_fee_teacher else all_classes
    form.fields['student_class'].widget.choices = [
        ('', '-- 1. Select Class First --')
    ] + [(c['id'], c['name']) for c in active_classes_list]

    buses_list = [
        {'id': b.id, 'name': b.name, 'fee': float(b.fee)}
        for b in Bus.objects.filter(is_active=True).order_by('name')
    ]

    context = {
        'form': form,
        'title': 'Record Fee Payment',
        'is_scoped_fee_teacher': is_scoped_fee_teacher,
        'assigned_bus': assigned_bus,
        'scoped_rider_count': len(all_bus_riders),
        'scoped_students_by_class_json': json.dumps(scoped_students_by_class),
        'all_students_by_class_json': json.dumps(all_students_by_class),
        'scoped_classes_json': json.dumps(scoped_classes),
        'all_classes_json': json.dumps(all_classes),
        'students_by_class_json': json.dumps(scoped_students_by_class if is_scoped_fee_teacher else all_students_by_class),
        'buses_json': json.dumps(buses_list),
        'initial_student_id': student_id or '',
        'initial_class_id': selected_class_id or '',
        'is_one_off_mode': bool(request.GET.get('is_one_off')) or is_guest_mode_initial,
        'is_guest_mode_initial': is_guest_mode_initial,
        'locked_bus': selected_bus_obj or (assigned_bus if is_scoped_fee_teacher else None),
        'is_admin_user': is_admin_user,
    }
    return render(request, 'attendance/payment_form.html', context)


@principal_required
def payment_delete_view(request, pk):
    """Delete a payment record."""
    payment = get_object_or_404(FeePayment, pk=pk)
    if request.method == 'POST':
        receipt_no = payment.receipt_number or f"ID-{payment.id}"
        student_name = payment.student.full_name if payment.student else "Unknown"
        amount = str(payment.amount)
        fee_type = payment.get_fee_type_display()
        currency = get_active_currency_symbol()
        log_activity(
            action='PAYMENT_DELETED',
            target_model='FeePayment',
            target_id=str(payment.id),
            target_repr=f"Receipt #{receipt_no} ({student_name})",
            description=f"Deleted {fee_type} payment of {currency}{amount} for student {student_name} (Receipt: {receipt_no}).",
            changes={
                'deleted_receipt_number': receipt_no,
                'deleted_amount': amount,
                'deleted_fee_type': payment.fee_type,
                'student_name': student_name,
                'payment_date': str(payment.payment_date),
            },
            actor=request.user,
        )
        payment.delete()
        messages.success(request, f"Payment receipt {payment.receipt_number} deleted.")
        return redirect('payment_list')
    return render(request, 'attendance/confirm_delete.html', {
        'object': payment,
        'object_name': f"Payment #{payment.receipt_number} ({get_active_currency_symbol()}{payment.amount:.2f}) for {payment.student.full_name}",
        'cancel_url': 'payment_list',
    })


# ==========================================
# REPORTS & RECONCILIATION
# ==========================================
@finance_required
def reports_view(request):
    """Daily Attendance & Fee Reconciliation Report."""
    date_str = request.GET.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    period = request.GET.get('period') or get_active_academic_period()

    # Bulk aggregate student totals by class in 1 query
    class_totals = dict(
        Student.objects.filter(is_active=True)
        .values('student_class')
        .annotate(c=Count('id'))
        .values_list('student_class', 'c')
    )

    # Bulk aggregate attendance by class in 1 query
    class_presents = dict(
        ClockInRecord.objects.filter(date=target_date, student__is_active=True)
        .values('student__student_class')
        .annotate(c=Count('id'))
        .values_list('student__student_class', 'c')
    )

    classes = sorted(class_totals.keys())
    class_reports = []
    total_school_students = 0
    total_school_present = 0

    for cls in classes:
        tot = class_totals.get(cls, 0)
        pres = class_presents.get(cls, 0)
        pct = round((pres / tot * 100), 1) if tot > 0 else 0
        total_school_students += tot
        total_school_present += pres
        class_reports.append({
            'class_name': cls,
            'total': tot,
            'present': pres,
            'absent': max(0, tot - pres),
            'pct': pct,
        })

    # Bulk aggregate bus riders by bus in 1 query
    bus_rider_counts = dict(
        Student.objects.filter(is_active=True, bus__isnull=False)
        .values('bus_id')
        .annotate(c=Count('id'))
        .values_list('bus_id', 'c')
    )

    # Bulk aggregate bus collections on target_date in 1 query
    bus_collections = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date, student__bus__isnull=False)
        .values('student__bus_id')
        .annotate(total=Sum('amount'))
        .values_list('student__bus_id', 'total')
    )

    buses = Bus.objects.filter(is_active=True).order_by('name')
    bus_reports = []
    total_bus_exp = Decimal('0.00')
    total_bus_col = Decimal('0.00')

    for b in buses:
        r_count = bus_rider_counts.get(b.id, 0)
        exp = Decimal(r_count) * b.fee
        total_bus_exp += exp
        col = bus_collections.get(b.id, Decimal('0.00'))
        total_bus_col += col
        bus_reports.append({
            'bus': b,
            'riders_count': r_count,
            'rate': b.fee,
            'expected': exp,
            'collected': col,
            'balance': max(Decimal('0.00'), exp - col),
            'collection_pct': round((col / exp * 100), 1) if exp > 0 else 0,
        })

    # Canteen fee report for target_date
    canteen_enrolled_count = Student.objects.filter(canteen_enrolled=True, is_active=True).count()
    default_canteen_fee = get_default_canteen_fee()
    canteen_expected = Decimal(canteen_enrolled_count) * default_canteen_fee
    canteen_collected = FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    canteen_balance = max(Decimal('0.00'), canteen_expected - canteen_collected)

    grand_total_exp = total_bus_exp + canteen_expected
    grand_total_col = total_bus_col + canteen_collected
    grand_total_bal = max(Decimal('0.00'), grand_total_exp - grand_total_col)

    context = {
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'period': period,
        'class_reports': class_reports,
        'total_school_students': total_school_students,
        'total_school_present': total_school_present,
        'overall_attendance_pct': round((total_school_present / total_school_students * 100), 1) if total_school_students > 0 else 0,
        'bus_reports': bus_reports,
        'total_bus_exp': total_bus_exp,
        'total_bus_col': total_bus_col,
        'total_bus_bal': max(Decimal('0.00'), total_bus_exp - total_bus_col),
        'canteen_enrolled_count': canteen_enrolled_count,
        'canteen_rate': default_canteen_fee,
        'canteen_expected': canteen_expected,
        'canteen_collected': canteen_collected,
        'canteen_balance': canteen_balance,
        'grand_total_exp': grand_total_exp,
        'grand_total_col': grand_total_col,
        'grand_total_bal': grand_total_bal,
        'currency': get_active_currency_symbol(),
    }
    return render(request, 'attendance/reports.html', context)


# ==========================================
# AUTHENTICATION VIEWS
# ==========================================
def login_view(request):
    """Custom Login View with quick demo role buttons."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            role_display = user.profile.get_role_display() if hasattr(user, 'profile') else 'Staff'
            messages.success(request, f"Welcome back, {user.username}! Signed in as {role_display}.")
            next_url = request.GET.get('next')
            if not next_url:
                if hasattr(user, 'profile') and user.profile.role == UserProfile.ROLE_ACCOUNTANT:
                    next_url = 'payment_list'
                else:
                    next_url = 'dashboard'
            return redirect(next_url)
        else:
            messages.error(request, "Invalid username or password.")
    else:
        form = AuthenticationForm()

    return render(request, 'attendance/login.html', {'form': form})


def logout_view(request):
    """Sign out current user."""
    logout(request)
    messages.info(request, "You have been signed out.")
    return redirect('login')


@login_required
def change_password_view(request):
    """
    Allow any authenticated staff member (Teacher, Bursar, Principal)
    to change their own password securely by verifying their current password.
    Preserves active session so the user is not logged out upon update.
    """
    if request.method == 'POST':
        form = TeacherSelfPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            log_activity(
                action='PASSWORD_CHANGE',
                target_model='User',
                target_id=str(user.id),
                target_repr=user.username,
                description=f"User @{user.username} self-updated their account password.",
                actor=user,
            )
            messages.success(request, "✓ Your password has been successfully updated.")
            return redirect('dashboard')
    else:
        form = TeacherSelfPasswordChangeForm(user=request.user)

    return render(request, 'attendance/change_password.html', {
        'form': form,
        'title': 'Change Password',
    })



# ==========================================
# BULK STUDENT IMPORT & EXPORT
# ==========================================
@principal_required
def student_template_download(request):
    """Download a CSV template for bulk student enrollment."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="student_enrollment_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['first_name', 'last_name', 'student_class', 'bus_name', 'canteen_enrolled'])
    writer.writerow(['Kwesi', 'Arthur', 'Class 1A', 'Tema Bus', 'yes'])
    writer.writerow(['Akosua', 'Boateng', 'Class 1A', 'Madina Bus', 'yes'])
    writer.writerow(['Ama', 'Serwaa', 'Class 2B', '', 'yes'])  # Walker
    writer.writerow(['Nana', 'Opoku', 'Class 2B', 'Spintex Bus', 'no'])
    return response


@principal_required
def student_import_view(request):
    """Bulk import students from a CSV file."""
    if request.method == 'POST' and request.FILES.get('csv_file'):
        csv_file = request.FILES['csv_file']
        if not csv_file.name.endswith('.csv'):
            messages.error(request, "Please upload a valid .csv file.")
            return redirect('student_import')

        try:
            file_data = TextIOWrapper(csv_file.file, encoding='utf-8', errors='ignore')
            reader = csv.DictReader(file_data)
            
            created_count = 0
            updated_count = 0
            buses_cache = {b.name.lower(): b for b in Bus.objects.all()}

            for row in reader:
                first_name = row.get('first_name', '').strip()
                last_name = row.get('last_name', '').strip()
                s_class = row.get('student_class', '').strip()
                bus_name = row.get('bus_name', '').strip()
                canteen_str = row.get('canteen_enrolled', 'yes').strip().lower()
                canteen_enrolled = (canteen_str not in ('no', 'false', '0', 'n', 'opted out'))

                if not first_name or not last_name or not s_class:
                    continue

                # Match bus
                bus_obj = None
                if bus_name:
                    bus_obj = buses_cache.get(bus_name.lower())
                    if not bus_obj:
                        # Auto-create bus with default fee if needed
                        bus_obj = Bus.objects.create(name=bus_name, fee=Decimal('60.00'))
                        buses_cache[bus_name.lower()] = bus_obj

                student, created = Student.objects.update_or_create(
                    first_name=first_name,
                    last_name=last_name,
                    student_class=s_class,
                    defaults={
                        'bus': bus_obj,
                        'canteen_enrolled': canteen_enrolled,
                        'is_active': True,
                    }
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

            messages.success(request, f"Import complete! Added {created_count} new students, updated {updated_count} existing records.")
            return redirect('student_list')
        except Exception as e:
            messages.error(request, f"Error processing CSV: {str(e)}")
            return redirect('student_import')

    return render(request, 'attendance/student_import.html')


@principal_required
def export_attendance_csv(request):
    """Export daily attendance roster to CSV."""
    date_str = request.GET.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="attendance_{target_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Student Name', 'Class', 'Bus Route', 'Status', 'Clock-In Time', 'Date'])

    students = Student.objects.filter(is_active=True).select_related('bus').order_by('student_class', 'first_name')
    clock_ins = {c.student_id: c for c in ClockInRecord.objects.filter(date=target_date)}

    for s in students:
        cin = clock_ins.get(s.id)
        status = cin.get_status_display() if cin else 'ABSENT'
        c_time = cin.clock_in_time.strftime('%I:%M %p') if cin else '—'
        bus_name = s.bus.name if s.bus else 'Walker / No Bus'
        writer.writerow([s.full_name, s.student_class, bus_name, status, c_time, str(target_date)])

    return response


@finance_required
def export_debtors_csv(request):
    """Export list of students with outstanding daily bus or canteen fees to CSV."""
    date_str = request.GET.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    period = request.GET.get('period') or get_active_academic_period()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="daily_debtors_{target_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Student Name', 'Class', 'Bus Route', 'Bus Fee Required', 'Bus Fee Status', 'Bus Balance', 'Canteen Fee Required', 'Canteen Status', 'Canteen Balance', 'Total Owed Today', 'Date'])

    today_bus_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    today_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    total_bus_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date__lte=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    total_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date__lte=target_date)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )
    past_attendance_counts = dict(
        ClockInRecord.objects.filter(date__lt=target_date)
        .values('student_id')
        .annotate(c=Count('id'))
        .values_list('student_id', 'c')
    )

    default_canteen_fee = get_default_canteen_fee()
    students = Student.objects.filter(is_active=True).select_related('bus').order_by('student_class', 'first_name')
    currency = get_active_currency_symbol()

    for s in students:
        past_days = past_attendance_counts.get(s.id, 0)

        # Bus fee calculation
        if s.bus:
            bus_req = s.bus.fee
            direct_bus = today_bus_payments.get(s.id, Decimal('0.00'))
            cum_bus = total_bus_payments.get(s.id, Decimal('0.00'))
            bus_credit = max(Decimal('0.00'), cum_bus - Decimal(past_days) * bus_req)
            is_bus_paid = (direct_bus >= bus_req) or (bus_credit >= bus_req)
            covered_bus = max(direct_bus, bus_credit)
            bus_bal = Decimal('0.00') if is_bus_paid else max(Decimal('0.00'), bus_req - covered_bus)
        else:
            bus_req = Decimal('0.00')
            bus_bal = Decimal('0.00')
            is_bus_paid = True

        # Canteen fee calculation
        if s.canteen_enrolled:
            can_req = s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee
            direct_can = today_canteen_payments.get(s.id, Decimal('0.00'))
            cum_can = total_canteen_payments.get(s.id, Decimal('0.00'))
            can_credit = max(Decimal('0.00'), cum_can - Decimal(past_days) * can_req)
            is_can_paid = (direct_can >= can_req) or (can_credit >= can_req)
            covered_can = max(direct_can, can_credit)
            can_bal = Decimal('0.00') if is_can_paid else max(Decimal('0.00'), can_req - covered_can)
        else:
            can_req = Decimal('0.00')
            can_bal = Decimal('0.00')
            is_can_paid = True

        total_owed = bus_bal + can_bal

        if total_owed > Decimal('0.00'):
            bus_name = s.bus.name if s.bus else 'Walker / No Bus'
            bus_status = 'No Bus' if not s.bus else ('Paid' if is_bus_paid else f'Owes {currency}{bus_bal:.2f}')
            can_status = 'Opted Out' if not s.canteen_enrolled else ('Paid' if is_can_paid else f'Owes {currency}{can_bal:.2f}')
            writer.writerow([
                s.full_name,
                s.student_class,
                bus_name,
                f"{currency}{bus_req:.2f}",
                bus_status,
                f"{currency}{bus_bal:.2f}",
                f"{currency}{can_req:.2f}",
                can_status,
                f"{currency}{can_bal:.2f}",
                f"{currency}{total_owed:.2f}",
                str(target_date)
            ])

    return response


@finance_required
def payment_export_csv(request):
    """Export filtered payments ledger to CSV."""
    payments = FeePayment.objects.select_related('student', 'bus', 'recorded_by').order_by('-payment_date', '-created_at')

    fee_type = request.GET.get('fee_type', '')
    student_class = request.GET.get('class', '')
    search = request.GET.get('q', '').strip()
    date_preset = request.GET.get('preset', '')
    date_str = request.GET.get('date', '').strip()
    collector = request.GET.get('collector', '').strip()

    today = timezone.localdate()

    if date_preset == 'today':
        payments = payments.filter(payment_date=today)
    elif date_preset == 'yesterday':
        payments = payments.filter(payment_date=today - timedelta(days=1))
    elif date_preset == 'week':
        payments = payments.filter(payment_date__gte=today - timedelta(days=7))
    elif date_str:
        try:
            filter_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            payments = payments.filter(payment_date=filter_date)
        except ValueError:
            pass

    if fee_type:
        payments = payments.filter(fee_type=fee_type)
    if student_class:
        payments = payments.filter(student__student_class=student_class)
    if collector:
        payments = payments.filter(recorded_by_id=collector)
    if search:
        payments = payments.filter(
            Q(student__first_name__icontains=search) |
            Q(student__last_name__icontains=search) |
            Q(receipt_number__icontains=search) |
            Q(recorded_by__username__icontains=search) |
            Q(recorded_by__first_name__icontains=search) |
            Q(recorded_by__last_name__icontains=search)
        )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    timestamp_str = timezone.now().strftime('%Y%m%d_%H%M')
    response['Content-Disposition'] = f'attachment; filename="payments_ledger_{timestamp_str}.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Payment Date',
        'Receipt Number',
        'Student Name',
        'Class',
        'Fee Type',
        'Bus Route',
        'Advance Days',
        'Payment Method',
        'Amount (GHS)',
        'Received By',
        'Period',
        'Notes',
        'Created At'
    ])

    for p in payments:
        bus_name = p.bus.name if p.bus else (p.student.bus.name if p.student and p.student.bus else 'Walker')
        collector_name = p.recorded_by.get_full_name() if p.recorded_by else (p.recorded_by.username if p.recorded_by else 'System')
        writer.writerow([
            p.payment_date.strftime('%Y-%m-%d'),
            p.receipt_number,
            p.student.full_name if p.student else 'Unknown',
            p.student.student_class if p.student else '',
            p.get_fee_type_display(),
            bus_name,
            p.days_covered,
            p.get_payment_method_display(),
            f"{p.amount:.2f}",
            collector_name,
            p.period,
            p.notes,
            p.created_at.strftime('%Y-%m-%d %H:%M:%S')
        ])

    return response


# ==========================================
# PRINTABLE RECEIPT VOUCHER
# ==========================================
@login_required
def payment_receipt_view(request, pk):
    """Printable / PDF-style receipt voucher for a recorded fee payment."""
    payment = get_object_or_404(FeePayment.objects.select_related('student', 'student__bus', 'recorded_by'), pk=pk)
    student = payment.student
    period = payment.period

    # If this receipt number is shared across multiple items (e.g. Bus + Canteen)
    if payment.receipt_number:
        all_payments = list(FeePayment.objects.filter(
            receipt_number=payment.receipt_number,
            student=student
        ).order_by('fee_type'))
    else:
        all_payments = [payment]

    total_amount_paid = sum(p.amount for p in all_payments)

    bus_balance = student.get_bus_balance(period)
    canteen_balance = student.get_canteen_balance(period)

    items = []
    for p in all_payments:
        if p.fee_type == 'BUS':
            req = student.bus_fee_required
            bal = bus_balance
            desc = f"Bus Transportation ({student.bus.name if student.bus else 'Walker'})"
        else:
            req = student.canteen_fee_required
            bal = canteen_balance
            desc = "School Canteen Meal Plan"
        items.append({
            'payment': p,
            'fee_type': p.fee_type,
            'fee_title': p.get_fee_type_display(),
            'description': desc,
            'amount': p.amount,
            'required': req,
            'balance': bal,
        })

    context = {
        'payment': payment,
        'payments': all_payments,
        'items': items,
        'student': student,
        'period': period,
        'total_amount_paid': total_amount_paid,
        'bus_balance': bus_balance,
        'canteen_balance': canteen_balance,
        'total_balance': bus_balance + canteen_balance,
        'balance': items[0]['balance'] if items else Decimal('0.00'),
        'total_required': items[0]['required'] if items else Decimal('0.00'),
        'currency': get_active_currency_symbol(),
    }
    return render(request, 'attendance/receipt.html', context)


# ==========================================
# NOTIFICATIONS AUDIT LOG
# ==========================================
@principal_required
def notifications_view(request):
    """View parent notification alert history."""
    logs = NotificationLog.objects.select_related('student', 'student__bus').order_by('-sent_at')[:100]
    total_alerts = NotificationLog.objects.count()
    clockin_alerts = NotificationLog.objects.filter(notification_type=NotificationLog.TYPE_CLOCK_IN).count()
    fee_alerts = NotificationLog.objects.filter(notification_type=NotificationLog.TYPE_FEE_REMINDER).count()

    context = {
        'logs': logs,
        'total_alerts': total_alerts,
        'clockin_alerts': clockin_alerts,
        'fee_alerts': fee_alerts,
    }
    return render(request, 'attendance/notifications.html', context)


# ==========================================
# SCHOOL SETTINGS & CANTEEN RATE CONFIGURATION
# ==========================================
@principal_required
def school_settings_view(request):
    """View and update system-wide school billing, academic term timeframe, and default canteen settings."""
    current_canteen = get_default_canteen_fee()
    current_school_name = SchoolSetting.get_setting('SCHOOL_NAME') or getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School')
    current_period = get_active_academic_period()
    current_currency = get_active_currency_symbol()
    current_allow_registration = (SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION') == 'True')
    start_date, end_date = get_term_date_range()

    if request.method == 'POST':
        form = SchoolSettingsForm(request.POST)
        if form.is_valid():
            new_canteen = form.cleaned_data['default_canteen_fee']
            new_school_name = form.cleaned_data['school_name']
            new_period = form.cleaned_data['current_period']
            new_currency = form.cleaned_data['currency_symbol']
            new_allow_reg = form.cleaned_data.get('allow_teacher_student_registration', False)
            new_start_date = form.cleaned_data.get('term_start_date')
            new_end_date = form.cleaned_data.get('term_end_date')

            SchoolSetting.set_setting('DEFAULT_CANTEEN_FEE', new_canteen, "Standard default daily canteen lunch rate")
            SchoolSetting.set_setting('SCHOOL_NAME', new_school_name, "Official school name")
            SchoolSetting.set_setting('CURRENT_ACADEMIC_PERIOD', new_period, "Current active academic term")
            SchoolSetting.set_setting('CURRENCY_SYMBOL', new_currency, "Default currency symbol")
            SchoolSetting.set_setting('ALLOW_TEACHER_STUDENT_REGISTRATION', 'True' if new_allow_reg else 'False', "Permit teachers to register students during onboarding")

            if 'term_start_date' in request.POST:
                if new_start_date:
                    SchoolSetting.set_setting('TERM_START_DATE', new_start_date.isoformat(), "Opening calendar date of academic term")
                else:
                    SchoolSetting.objects.filter(key='TERM_START_DATE').delete()

            if 'term_end_date' in request.POST:
                if new_end_date:
                    SchoolSetting.set_setting('TERM_END_DATE', new_end_date.isoformat(), "Closing calendar date of academic term")
                else:
                    SchoolSetting.objects.filter(key='TERM_END_DATE').delete()

            reg_status_msg = "enabled (teachers can register students)" if new_allow_reg else "disabled (restricted to Principal only)"
            messages.success(request, f"✓ Settings updated! Onboarding registration is {reg_status_msg}.")
            if new_end_date and new_end_date < timezone.localdate():
                messages.info(request, f"ℹ️ Note: Term end date ({new_end_date.strftime('%b %d, %Y')}) is in the past. This timeframe was saved as a concluded period.")
            return redirect('school_settings')
    else:
        form = SchoolSettingsForm(initial={
            'default_canteen_fee': current_canteen,
            'school_name': current_school_name,
            'current_period': current_period,
            'currency_symbol': current_currency,
            'allow_teacher_student_registration': current_allow_registration,
            'term_start_date': start_date,
            'term_end_date': end_date,
        })

    enrolled_canteen_count = Student.objects.filter(canteen_enrolled=True, is_active=True).count()
    daily_canteen_expected = Decimal(enrolled_canteen_count) * current_canteen
    term_metrics = get_current_term_metrics()
    term_projected_canteen_per_student = Decimal(term_metrics['total_days']) * current_canteen
    term_projected_canteen_total = Decimal(term_metrics['total_days']) * daily_canteen_expected

    context = {
        'form': form,
        'title': 'School & Canteen Settings',
        'current_canteen': current_canteen,
        'enrolled_canteen_count': enrolled_canteen_count,
        'daily_canteen_expected': daily_canteen_expected,
        'allow_teacher_registration': current_allow_registration,
        'term_metrics': term_metrics,
        'term_projected_canteen_per_student': term_projected_canteen_per_student,
        'term_projected_canteen_total': term_projected_canteen_total,
    }
    return render(request, 'attendance/settings.html', context)


# ==========================================
# TEACHER ACCOUNT MANAGEMENT & STAFF ROSTER
# ==========================================
@principal_required
def teacher_list_view(request):
    """
    Staff directory to view teacher accounts, classroom assignments, and fee collection authorization.
    Principals have full management rights; Accountants have read-only auditing access.
    """
    User = get_user_model()
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    fee_access = request.GET.get('fee_access', '').strip()

    is_accountant_user = bool(
        request.user.is_authenticated and 
        hasattr(request.user, 'profile') and 
        request.user.profile.is_accountant and 
        not request.user.profile.is_principal
    )

    STAFF_ROLES = [UserProfile.ROLE_TEACHER, UserProfile.ROLE_ADMIN]
    teachers_qs = User.objects.filter(profile__role__in=STAFF_ROLES).select_related('profile', 'profile__assigned_bus').prefetch_related('profile__assigned_buses').order_by('first_name', 'username')

    if is_accountant_user:
        # Accountants MUST only see teachers who are authorized to collect fees
        teachers_qs = teachers_qs.filter(profile__can_collect_fees=True, is_active=True)
    else:
        if status_filter == 'active':
            teachers_qs = teachers_qs.filter(is_active=True)
        elif status_filter == 'inactive':
            teachers_qs = teachers_qs.filter(is_active=False)

        if fee_access == 'authorized':
            teachers_qs = teachers_qs.filter(profile__can_collect_fees=True)
        elif fee_access == 'attendance_only':
            teachers_qs = teachers_qs.filter(profile__can_collect_fees=False)

    if search_query:
        teachers_qs = teachers_qs.filter(
            Q(username__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(profile__assigned_class__icontains=search_query)
        )

    today = timezone.localdate()
    collectors_meta = get_collectors_financial_summary(teachers_qs, today)

    # Attach computed metrics to teacher objects
    teachers_list = list(teachers_qs)
    total_remittance_today = Decimal('0.00')
    total_class_expected = Decimal('0.00')

    for t in teachers_list:
        meta = collectors_meta.get(t.id, {})
        t.today_collected = meta.get('today_collected', Decimal('0.00'))
        t.today_count = meta.get('today_count', 0)
        t.assigned_bus_name = meta.get('assigned_bus_name', '')
        t.assigned_buses_list = meta.get('assigned_buses_list', []) or (t.profile.get_assigned_buses_list() if hasattr(t, 'profile') else [])
        t.is_bus_fee_teacher = meta.get('is_bus_fee_teacher', False)
        t.bus_expected = meta.get('bus_expected', Decimal('0.00'))
        t.canteen_expected = meta.get('canteen_expected', Decimal('0.00'))
        t.bus_collected = meta.get('bus_collected', Decimal('0.00'))
        t.canteen_collected = meta.get('canteen_collected', Decimal('0.00'))
        t.baseline_target = meta.get('baseline_target', Decimal('0.00'))
        t.advance_prepayments = meta.get('advance_prepayments', Decimal('0.00'))
        t.prepaid_deductions = meta.get('prepaid_deductions', Decimal('0.00'))
        t.prepaid_students_count = meta.get('prepaid_students_count', 0)
        t.one_off_additions = meta.get('one_off_additions', Decimal('0.00'))
        t.one_off_additions_count = meta.get('one_off_additions_count', 0)
        t.transfers_out = meta.get('transfers_out', Decimal('0.00'))
        t.adjusted_target = meta.get('adjusted_target', Decimal('0.00'))
        t.class_expected = t.adjusted_target  # for template & test backward compat
        t.class_collected = meta.get('today_collected', Decimal('0.00'))
        t.class_balance = meta.get('balance', Decimal('0.00'))
        t.student_count = meta.get('student_count', 0)

        total_remittance_today += t.today_collected
        total_class_expected += t.adjusted_target

    total_teachers = User.objects.filter(profile__role__in=STAFF_ROLES).count()
    active_teachers = User.objects.filter(profile__role__in=STAFF_ROLES, is_active=True).count()
    authorized_collectors_count = User.objects.filter(profile__role__in=STAFF_ROLES, profile__can_collect_fees=True, is_active=True).count()
    assigned_classes_count = User.objects.filter(profile__role__in=STAFF_ROLES).exclude(profile__assigned_class='').values('profile__assigned_class').distinct().count()
    bus_teachers_count = sum(1 for t in teachers_list if getattr(t, 'assigned_buses_list', None))

    # Check for newly created or reset teacher in session to display credential banner
    created_info = request.session.pop('created_teacher_info', None)
    reset_info = request.session.pop('reset_teacher_info', None)

    context = {
        'teachers': teachers_list,
        'search': search_query,
        'status_filter': status_filter,
        'fee_access': fee_access,
        'total_teachers': total_teachers,
        'active_teachers': active_teachers,
        'authorized_collectors_count': authorized_collectors_count,
        'assigned_classes_count': assigned_classes_count,
        'bus_teachers_count': bus_teachers_count,
        'is_accountant_view': is_accountant_user,
        'total_remittance_today': total_remittance_today,
        'total_class_expected': total_class_expected,
        'created_info': created_info,
        'reset_info': reset_info,
    }
    return render(request, 'attendance/teachers.html', context)


@principal_required
def teacher_create_view(request):
    """
    Allow Principal to create a new teacher account and set an initial password.
    """
    if request.method == 'POST':
        form = TeacherCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            initial_password = form.cleaned_data['password']
            log_activity(
                action='TEACHER_CREATED',
                target_model='User',
                target_id=str(user.id),
                target_repr=f"@{user.username} ({user.get_full_name() or 'Staff'})",
                description=f"Created staff account for @{user.username} ({user.get_full_name() or 'Staff'}).",
                changes={'username': user.username, 'role': user.profile.get_role_display() if hasattr(user, 'profile') else 'Staff'},
                actor=request.user,
            )
            messages.success(
                request,
                f"✓ Teacher account created for {user.get_full_name() or user.username} (@{user.username})."
            )
            request.session['created_teacher_info'] = {
                'username': user.username,
                'full_name': user.get_full_name() or user.username,
                'password': initial_password,
                'assigned_class': user.profile.assigned_class if hasattr(user, 'profile') else '',
            }
            return redirect('teacher_list')
    else:
        form = TeacherCreationForm()

    return render(request, 'attendance/teacher_form.html', {
        'form': form,
        'title': 'Add New Teacher',
        'is_edit': False,
    })


@principal_required
def teacher_update_view(request, pk):
    """
    Update an existing staff member's profile (role/privileges, name, email, phone, assigned class, active status).
    """
    User = get_user_model()
    teacher = get_object_or_404(User.objects.filter(profile__role__in=[UserProfile.ROLE_TEACHER, UserProfile.ROLE_ADMIN]).select_related('profile'), pk=pk)

    if request.method == 'POST':
        form = TeacherUpdateForm(request.POST, user_obj=teacher)
        if form.is_valid():
            if teacher.id == request.user.id and not form.cleaned_data.get('is_active', True):
                messages.error(request, "You cannot deactivate your own account.")
                return render(request, 'attendance/teacher_form.html', {
                    'form': form,
                    'teacher': teacher,
                    'title': f"Edit Staff: {teacher.get_full_name() or teacher.username}",
                    'is_edit': True,
                })
            form.save()
            log_activity(
                action='TEACHER_UPDATED',
                target_model='User',
                target_id=str(teacher.id),
                target_repr=f"@{teacher.username}",
                description=f"Updated profile/permissions for staff member @{teacher.username}.",
                actor=request.user,
            )
            messages.success(request, f"✓ Staff member '{teacher.username}' updated successfully.")
            return redirect('teacher_list')
    else:
        initial_data = {
            'first_name': teacher.first_name,
            'last_name': teacher.last_name,
            'email': teacher.email,
            'is_active': teacher.is_active,
            'phone_number': teacher.profile.phone_number if hasattr(teacher, 'profile') else '',
            'assigned_class': teacher.profile.assigned_class if hasattr(teacher, 'profile') else '',
        }
        form = TeacherUpdateForm(initial=initial_data, user_obj=teacher)

    return render(request, 'attendance/teacher_form.html', {
        'form': form,
        'teacher': teacher,
        'title': f"Edit Staff: {teacher.get_full_name() or teacher.username}",
        'is_edit': True,
    })


@principal_required
def teacher_password_reset_view(request, pk):
    """
    Allow Principal to set a new password for a teacher or staff member.
    """
    User = get_user_model()
    teacher = get_object_or_404(User.objects.filter(profile__role__in=[UserProfile.ROLE_TEACHER, UserProfile.ROLE_ADMIN]), pk=pk)

    if request.method == 'POST':
        form = TeacherPasswordResetForm(request.POST, user_obj=teacher)
        if form.is_valid():
            form.save()
            new_password = form.cleaned_data['new_password']
            log_activity(
                action='PASSWORD_CHANGE',
                target_model='User',
                target_id=str(teacher.id),
                target_repr=teacher.username,
                description=f"Administrative password reset performed for staff member @{teacher.username}.",
                actor=request.user,
            )
            messages.success(
                request,
                f"✓ Password reset for '{teacher.username}'."
            )
            request.session['reset_teacher_info'] = {
                'username': teacher.username,
                'full_name': teacher.get_full_name() or teacher.username,
                'password': new_password,
            }
            return redirect('teacher_list')
    else:
        form = TeacherPasswordResetForm(user_obj=teacher)

    return render(request, 'attendance/teacher_password_reset.html', {
        'form': form,
        'teacher': teacher,
    })


@principal_required
def teacher_toggle_status_view(request, pk):
    """
    1-click toggle to activate or deactivate a teacher or staff account.
    """
    User = get_user_model()
    teacher = get_object_or_404(User.objects.filter(profile__role__in=[UserProfile.ROLE_TEACHER, UserProfile.ROLE_ADMIN]), pk=pk)
    if teacher.id == request.user.id:
        messages.error(request, "You cannot deactivate your own administrative account.")
        return redirect('teacher_list')
    teacher.is_active = not teacher.is_active
    teacher.save()
    status_str = "activated" if teacher.is_active else "deactivated"
    log_activity(
        action='TEACHER_STATUS',
        target_model='User',
        target_id=str(teacher.id),
        target_repr=teacher.username,
        description=f"Staff account @{teacher.username} was {status_str}.",
        changes={'is_active': teacher.is_active},
        actor=request.user,
    )
    messages.info(request, f"Staff account '{teacher.username}' has been {status_str}.")
    return redirect('teacher_list')


@principal_required
def audit_log_view(request):
    """
    Principal Activity & Security Audit Trail.
    Displays paginated system events with search, action filtering, staff filtering, and date presets.
    """
    search_query = request.GET.get('q', '').strip()
    action_filter = request.GET.get('action', '').strip()
    actor_filter = request.GET.get('actor', '').strip()
    date_preset = request.GET.get('preset', '').strip()
    date_str = request.GET.get('date', '').strip()

    logs_qs = AuditLog.objects.select_related('actor', 'actor__profile')

    # Date filter logic
    today = timezone.localdate()
    if date_preset == 'today':
        logs_qs = logs_qs.filter(timestamp__date=today)
    elif date_preset == 'yesterday':
        yesterday = today - timedelta(days=1)
        logs_qs = logs_qs.filter(timestamp__date=yesterday)
    elif date_preset == 'week':
        week_ago = today - timedelta(days=7)
        logs_qs = logs_qs.filter(timestamp__date__gte=week_ago)
    elif date_str:
        try:
            parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            logs_qs = logs_qs.filter(timestamp__date=parsed_date)
        except ValueError:
            pass

    # Action filter
    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)

    # Actor filter
    if actor_filter:
        logs_qs = logs_qs.filter(actor_id=actor_filter)

    # Search query
    if search_query:
        logs_qs = logs_qs.filter(
            Q(actor_username__icontains=search_query) |
            Q(target_repr__icontains=search_query) |
            Q(target_id__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(ip_address__icontains=search_query)
        )

    # Compute stat counters efficiently
    total_logs_count = AuditLog.objects.count()
    today_count = AuditLog.objects.filter(timestamp__date=today).count()
    deletions_count = AuditLog.objects.filter(action__in=['PAYMENT_DELETED', 'STUDENT_DELETED']).count()
    security_count = AuditLog.objects.filter(action__in=['PASSWORD_CHANGE', 'LOGIN', 'FAILED_LOGIN']).count()

    # Pagination
    paginator = Paginator(logs_qs, 30)
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.get_page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    staff_users = User.objects.filter(is_active=True).order_by('username')

    context = {
        'logs': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'total_logs_count': total_logs_count,
        'today_count': today_count,
        'deletions_count': deletions_count,
        'security_count': security_count,
        'action_choices': AuditLog.ACTION_CHOICES,
        'staff_users': staff_users,
        'search': search_query,
        'action_filter': action_filter,
        'actor_filter': actor_filter,
        'date_preset': date_preset,
        'date_str': date_str,
    }
    return render(request, 'attendance/audit_logs.html', context)


@require_GET
def health_check_view(request):
    """
    Health check endpoint for Docker and Dokploy liveness/readiness probes.
    Returns 200 if application and database connection are healthy.
    """
    from django.db import connection
    status = {'status': 'healthy'}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
        status['database'] = 'connected'
        return JsonResponse(status, status=200)
    except Exception as exc:
        status['status'] = 'degraded'
        status['database'] = str(exc)
        return JsonResponse(status, status=503)

