import csv
import json
from decimal import Decimal
from datetime import datetime
from collections import defaultdict
from io import TextIOWrapper
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.conf import settings

from django.contrib.auth import get_user_model

User = get_user_model()
from .models import Bus, Student, FeePayment, ClockInRecord, UserProfile, NotificationLog, SchoolClass, SchoolSetting, get_default_canteen_fee
from .forms import BusForm, StudentForm, FeePaymentForm, SchoolSettingsForm, TeacherCreationForm, TeacherUpdateForm, TeacherPasswordResetForm, SchoolClassForm
from .decorators import principal_required, finance_required, payment_recording_required, login_required_ajax

def get_stats_data(target_date=None, period=None):
    """
    Calculate aggregated stats for daily dashboard counters.
    Reconciles daily attendance, daily bus fare collections, and daily canteen revenue.
    """
    if target_date is None:
        target_date = timezone.localdate()

    students = Student.objects.filter(is_active=True).select_related('bus')
    total_students = students.count()
    
    clocked_in_records = ClockInRecord.objects.filter(date=target_date, student__is_active=True)
    clocked_in_count = clocked_in_records.count()
    clocked_in_pct = round((clocked_in_count / total_students * 100), 1) if total_students > 0 else 0
    
    bus_students = students.exclude(bus=None)
    bus_students_count = bus_students.count()

    # Pre-fetch past attendance counts before target_date for credit calculations
    past_attendance_counts = dict(
        ClockInRecord.objects.filter(date__lt=target_date)
        .values('student_id')
        .annotate(c=Count('id'))
        .values_list('student_id', 'c')
    )

    # Pre-fetch direct payments on target_date
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

    # Pre-fetch cumulative payments up to target_date
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
        cum_bus = total_bus_payments.get(s.id, Decimal('0.00'))
        past_days = past_attendance_counts.get(s.id, 0)
        past_cost = Decimal(past_days) * req
        credit = max(Decimal('0.00'), cum_bus - past_cost)

        if direct_bus >= req or credit >= req:
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
        cum_can = total_canteen_payments.get(s.id, Decimal('0.00'))
        past_days = past_attendance_counts.get(s.id, 0)
        past_cost = Decimal(past_days) * req
        credit = max(Decimal('0.00'), cum_can - past_cost)

        if direct_can >= req or credit >= req:
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
    and outstanding balances for a list or queryset of teachers on target_date.
    Optimized to run in constant O(1) database queries.
    """
    if target_date is None:
        target_date = timezone.localdate()

    default_canteen_fee = get_default_canteen_fee()
    teachers_list = list(teachers)
    if not teachers_list:
        return {}

    class_to_teacher_ids = defaultdict(list)
    bus_to_teacher_ids = defaultdict(list)
    teacher_meta = {}

    for t in teachers_list:
        p = getattr(t, 'profile', None)
        t_class = (p.assigned_class or '').strip() if p else ''
        t_bus_id = p.assigned_bus_id if p and p.assigned_bus_id else None
        teacher_meta[t.id] = {
            'teacher': t,
            'assigned_class': t_class,
            'assigned_bus_id': t_bus_id,
            'assigned_bus_name': p.assigned_bus.name if p and p.assigned_bus else '',
            'is_bus_fee_teacher': bool(t_bus_id),
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
            'student_count': 0,
        }
        if t_class:
            class_to_teacher_ids[t_class].append(t.id)
        if t_bus_id:
            bus_to_teacher_ids[t_bus_id].append(t.id)

    # 1. Fetch active students to establish baseline expectations & prior prepaid coverage
    active_students = Student.objects.filter(is_active=True).select_related('bus')
    student_to_teacher_ids = defaultdict(set)
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

    for s in active_students:
        c_name = (s.student_class or '').strip()
        bus_fee = s.bus.fee if s.bus else Decimal('0.00')
        canteen_fee = (s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee) if s.canteen_enrolled else Decimal('0.00')

        # Check prepaid credit applied today for bus and canteen (both full advance and partial remainder top-up)
        bus_prepaid_applied = Decimal('0.00')
        if s.bus_id and bus_fee > Decimal('0.00'):
            today_bus_direct = today_bus_payments.get(s.id, Decimal('0.00'))
            current_bus_credit = s.get_bus_credit_balance(target_date)
            prior_bus_credit = max(Decimal('0.00'), current_bus_credit - today_bus_direct)
            if prior_bus_credit > Decimal('0.00'):
                bus_prepaid_applied = min(bus_fee, prior_bus_credit)

        canteen_prepaid_applied = Decimal('0.00')
        if s.canteen_enrolled and canteen_fee > Decimal('0.00'):
            today_can_direct = today_canteen_payments.get(s.id, Decimal('0.00'))
            current_can_credit = s.get_canteen_credit_balance(target_date)
            prior_can_credit = max(Decimal('0.00'), current_can_credit - today_can_direct)
            if prior_can_credit > Decimal('0.00'):
                canteen_prepaid_applied = min(canteen_fee, prior_can_credit)

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

    for tid, meta in teacher_meta.items():
        meta['baseline_target'] = meta['bus_expected'] + meta['canteen_expected']
        meta['class_expected'] = meta['baseline_target']  # For backward compatibility

    # 2. Fetch all payments on target_date
    today_payments = FeePayment.objects.filter(payment_date=target_date).select_related('student', 'bus')
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
            if p.is_advance or p.amount > daily_fee:
                excess = max(Decimal('0.00'), p.amount - daily_fee)
                if excess > Decimal('0.00'):
                    meta['advance_prepayments'] += excess

            # Check if this was a guest / one-off bus ride or outside baseline roster
            if (is_guest and p.fee_type == 'BUS') or (p.student_id not in meta['baseline_student_ids']):
                meta['one_off_additions'] += p.amount
                meta['one_off_additions_count'] += 1

        # Check if this student was in another teacher's baseline, but paid a DIFFERENT teacher on another bus
        for baseline_tid in student_to_teacher_ids.get(p.student_id, ()):
            if baseline_tid != collector_id and p.fee_type == 'BUS':
                # Student paid someone else on another bus route -> adjust baseline teacher's expectation down
                teacher_meta[baseline_tid]['transfers_out'] += p.amount

    # 3. Finalize adjusted target and balances
    for tid, meta in teacher_meta.items():
        adj = meta['baseline_target'] + meta['advance_prepayments'] - meta['prepaid_deductions'] + meta['one_off_additions'] - meta['transfers_out']
        meta['adjusted_target'] = max(Decimal('0.00'), adj)
        meta['balance'] = max(Decimal('0.00'), meta['adjusted_target'] - meta['today_collected'])

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

    period = request.GET.get('period', getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026'))
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
    if user_profile and user_profile.role == UserProfile.ROLE_TEACHER:
        # Fee Teachers dedicated to a bus route without an assigned classroom land on their bus roster
        if user_profile.assigned_bus_id and not user_profile.assigned_class and not request.GET:
            return redirect('bus_analytics', pk=user_profile.assigned_bus_id)
        # Automatically scope bus_filter to assigned bus route for Bus Fee Teachers
        if user_profile.assigned_bus_id and not bus_filter:
            bus_filter = str(user_profile.assigned_bus_id)
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

    if bus_filter:
        if bus_filter == 'none':
            students_qs = students_qs.filter(bus=None)
        else:
            students_qs = students_qs.filter(bus_id=bus_filter)

    # Pre-fetch today's clock-in records
    clock_ins = ClockInRecord.objects.filter(date=target_date)
    clock_in_map = {c.student_id: c for c in clock_ins}

    # Pre-fetch past attendance counts before target_date for advance credit calculation
    past_attendance_counts = dict(
        ClockInRecord.objects.filter(date__lt=target_date)
        .values('student_id')
        .annotate(c=Count('id'))
        .values_list('student_id', 'c')
    )

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

    # Pre-fetch cumulative payments up to target_date
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

    default_canteen_fee = get_default_canteen_fee()

    # Assemble student cards/rows with computed daily fee statuses
    student_items = []
    for s in students_qs:
        clock_in = clock_in_map.get(s.id)
        is_clocked_in = clock_in is not None
        past_days = past_attendance_counts.get(s.id, 0)

        # Daily Bus fee calculation
        if s.bus:
            bus_req = s.bus.fee
            direct_bus = today_bus_payments.get(s.id, Decimal('0.00'))
            cum_bus = total_bus_payments.get(s.id, Decimal('0.00'))
            past_cost = Decimal(past_days) * bus_req
            bus_credit = max(Decimal('0.00'), cum_bus - past_cost)

            if direct_bus >= bus_req:
                is_bus_paid = True
                bus_balance = Decimal('0.00')
                bus_paid = direct_bus
                rem = bus_credit - bus_req if bus_credit >= bus_req else Decimal('0.00')
                bus_credit_days = int(rem // bus_req) if bus_req > 0 else 0
            elif bus_credit >= bus_req:
                is_bus_paid = True
                bus_balance = Decimal('0.00')
                bus_paid = bus_req
                rem = bus_credit - bus_req
                bus_credit_days = int(rem // bus_req) if bus_req > 0 else 0
            else:
                is_bus_paid = False
                covered = max(direct_bus, bus_credit)
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
            cum_can = total_canteen_payments.get(s.id, Decimal('0.00'))
            past_cost = Decimal(past_days) * canteen_req
            can_credit = max(Decimal('0.00'), cum_can - past_cost)

            if direct_can >= canteen_req:
                is_canteen_paid = True
                canteen_balance = Decimal('0.00')
                canteen_paid = direct_can
                rem = can_credit - canteen_req if can_credit >= canteen_req else Decimal('0.00')
                canteen_credit_days = int(rem // canteen_req) if canteen_req > 0 else 0
            elif can_credit >= canteen_req:
                is_canteen_paid = True
                canteen_balance = Decimal('0.00')
                canteen_paid = canteen_req
                rem = can_credit - canteen_req
                canteen_credit_days = int(rem // canteen_req) if canteen_req > 0 else 0
            else:
                is_canteen_paid = False
                covered = max(direct_can, can_credit)
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

    # Available classes & buses for filter dropdowns
    available_classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    buses = Bus.objects.filter(is_active=True).order_by('name')

    # Aggregated stats
    stats = get_stats_data(target_date, period)

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

    context = {
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'period': period,
        'student_items': student_items,
        'total_filtered': len(student_items),
        'available_classes': available_classes,
        'buses': buses,
        'class_filter': class_filter,
        'bus_filter': bus_filter,
        'fee_filter': fee_filter,
        'clockin_filter': clockin_filter,
        'search_query': search_query,
        'stats': stats,
        'teacher_class_stats': teacher_class_stats,
        'is_attendance_only_teacher': is_attendance_only_teacher,
        'currency': getattr(settings, 'CURRENCY_SYMBOL', 'GH₵'),
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

        if action == 'undo' or (action == 'toggle' and existing):
            if existing:
                existing.delete()
                clocked_in = False
                time_str = ""
                status_str = ""
                message = f"Clock-in undone for {student.full_name}."
            else:
                clocked_in = False
                time_str = ""
                status_str = ""
                message = "Not clocked in."
        else:
            now_time = timezone.localtime().time() if hasattr(timezone, 'localtime') else timezone.now().time()
            record, created = ClockInRecord.objects.get_or_create(
                student=student,
                date=target_date,
                defaults={
                    'clock_in_time': now_time,
                    'status': 'PRESENT',
                    'recorded_by': request.user if request.user.is_authenticated else None,
                }
            )
            clocked_in = True
            time_str = record.clock_in_time.strftime('%I:%M %p')
            status_str = record.get_status_display()
            message = f"✓ Clocked in {student.full_name} at {time_str}."

        # Fetch updated quick counters
        period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        stats = get_stats_data(target_date, period)
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

        return JsonResponse({
            'success': True,
            'student_id': student.id,
            'student_name': student.full_name,
            'is_clocked_in': clocked_in,
            'clock_in_time': time_str,
            'status': status_str,
            'message': message,
            'stats': stats,
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
        period = data.get('period', getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026'))
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
        currency = getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')

        created_payments = []
        shared_receipt = receipt_number or f"REC-{timezone.now().strftime('%Y%m%d%H%M%S')}"

        with transaction.atomic():
            if fee_type == 'BOTH':
                bus_amount = Decimal(str(data.get('bus_amount') or 0))
                canteen_amount = Decimal(str(data.get('canteen_amount') or 0))

                if bus_amount > Decimal('0.00'):
                    is_bus_adv = bool(student.bus and student.bus_fee_required > 0 and bus_amount > student.bus_fee_required)
                    bus_days_cov = int(bus_amount // student.bus_fee_required) if is_bus_adv else 1
                    p_bus = FeePayment.objects.create(
                        student=student,
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

        stats = get_stats_data(payment_date, period)

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
    method = data.get('method') or data.get('payment_method') or 'CASH'
    fee_type_req = str(data.get('fee_type', 'BOTH')).strip().upper()
    period = data.get('period') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    currency = getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')
    default_canteen_fee = get_default_canteen_fee()

    user_profile = getattr(request.user, 'profile', None)
    bus_for_payment = user_profile.assigned_bus if (user_profile and user_profile.assigned_bus) else student.bus
    is_temp = bool(student.bus and user_profile and user_profile.assigned_bus and student.bus != user_profile.assigned_bus)

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
                        if Decimal('0.00') < bus_credit < assigned_bus.fee:
                            bus_amount = max(Decimal('0.00'), assigned_bus.fee - bus_credit)
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
                    if Decimal('0.00') < can_credit < daily_canteen:
                        canteen_amount = max(Decimal('0.00'), daily_canteen - can_credit)
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
def bus_list_view(request):
    """Admin Bus Management: list all buses with dynamic fees and assigned student stats."""
    buses = Bus.objects.all().order_by('name')
    period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    
    # Calculate revenue metrics per bus
    bus_data = []
    total_fleet_expected = Decimal('0.00')
    total_fleet_collected = Decimal('0.00')

    for b in buses:
        student_count = b.students.filter(is_active=True).count()
        expected = Decimal(student_count) * b.fee
        total_fleet_expected += expected
        
        collected = FeePayment.objects.filter(
            fee_type='BUS',
            student__bus=b,
            period=period
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
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
        'total_buses': buses.count(),
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
    is_assigned_fee_teacher = bool(profile and profile.can_record_payments and profile.assigned_bus_id == bus.id)
    if not (request.user.is_superuser or (profile and (profile.can_view_revenue or is_assigned_fee_teacher))):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'Financial authority required.'}, status=403)
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

    period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    currency = getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')

    # 2. Fetch all active students assigned to this bus route
    students = bus.students.filter(is_active=True).order_by('student_class', 'first_name', 'last_name')
    total_riders = students.count()
    daily_fee = bus.fee

    # 3. Pre-fetch payments and attendance
    student_ids = list(students.values_list('id', flat=True))

    past_attendance_counts = dict(
        ClockInRecord.objects.filter(date__lt=target_date, student_id__in=student_ids)
        .values('student_id')
        .annotate(c=Count('id'))
        .values_list('student_id', 'c')
    )

    today_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date=target_date, student_id__in=student_ids)
        .values('student_id')
        .annotate(total=Sum('amount'))
        .values_list('student_id', 'total')
    )

    cumulative_payments = dict(
        FeePayment.objects.filter(fee_type='BUS', payment_date__lte=target_date, student_id__in=student_ids)
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

    cumulative_canteen_payments = dict(
        FeePayment.objects.filter(fee_type='CANTEEN', payment_date__lte=target_date, student_id__in=student_ids)
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
        cum_paid = cumulative_payments.get(s.id, Decimal('0.00'))
        past_days = past_attendance_counts.get(s.id, 0)
        past_cost = Decimal(past_days) * daily_fee
        credit = max(Decimal('0.00'), cum_paid - past_cost)

        is_paid = (direct_paid >= daily_fee) or (credit >= daily_fee)
        has_partial_credit = (not is_paid) and (Decimal('0.00') < credit < daily_fee)
        credit_available = credit if has_partial_credit else Decimal('0.00')
        bus_credit_days = int((credit - daily_fee) // daily_fee) if (is_paid and credit >= daily_fee and daily_fee > 0) else 0

        if is_paid:
            total_paid_count += 1
            paid_amount = direct_paid if direct_paid > 0 else daily_fee
            total_collected += paid_amount
            balance = Decimal('0.00')
            remainder_due = Decimal('0.00')
        else:
            total_unpaid_count += 1
            covered = max(direct_paid, credit)
            total_collected += direct_paid
            balance = max(Decimal('0.00'), daily_fee - covered)
            paid_amount = direct_paid
            remainder_due = balance

        # Canteen calculations
        canteen_fee = (s.custom_canteen_fee if s.custom_canteen_fee is not None else default_canteen_fee) if s.canteen_enrolled else Decimal('0.00')
        direct_canteen = today_canteen_payments.get(s.id, Decimal('0.00'))
        cum_canteen = cumulative_canteen_payments.get(s.id, Decimal('0.00'))
        canteen_cost = Decimal(past_days) * canteen_fee
        canteen_credit = max(Decimal('0.00'), cum_canteen - canteen_cost)

        canteen_is_paid = not s.canteen_enrolled or (direct_canteen >= canteen_fee) or (canteen_credit >= canteen_fee)
        canteen_has_partial = (not canteen_is_paid) and s.canteen_enrolled and (Decimal('0.00') < canteen_credit < canteen_fee)
        canteen_credit_available = canteen_credit if canteen_has_partial else Decimal('0.00')
        canteen_credit_days = int((canteen_credit - canteen_fee) // canteen_fee) if (canteen_is_paid and s.canteen_enrolled and canteen_credit >= canteen_fee and canteen_fee > 0) else 0

        if canteen_is_paid:
            canteen_amount_paid = direct_canteen if direct_canteen > 0 else (canteen_fee if s.canteen_enrolled else Decimal('0.00'))
            canteen_balance = Decimal('0.00')
            canteen_remainder_due = Decimal('0.00')
        else:
            covered_canteen = max(direct_canteen, canteen_credit)
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
    ).select_related('student').exclude(student_id__in=student_ids)

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

    summary = {
        'total_riders': total_riders,
        'paid_count': total_paid_count,
        'unpaid_count': total_unpaid_count,
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
            messages.success(request, f"Bus '{bus.name}' created with route fee {getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{bus.fee:.2f}!")
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
    classes = SchoolClass.objects.all().order_by('name')
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
def student_list_view(request):
    """Student roster listing with bus assignments and canteen status."""
    class_filter = request.GET.get('class', '')
    bus_filter = request.GET.get('bus', '')
    search = request.GET.get('q', '').strip()

    user_profile = getattr(request.user, 'profile', None)
    if user_profile and user_profile.role == UserProfile.ROLE_TEACHER:
        if user_profile.assigned_bus_id and not bus_filter and not request.GET.get('bus'):
            bus_filter = str(user_profile.assigned_bus_id)
        if user_profile.assigned_class and not class_filter and not request.GET.get('class'):
            class_filter = user_profile.assigned_class

    students = Student.objects.filter(is_active=True).select_related('bus', 'school_class').order_by('student_class', 'first_name', 'last_name')

    if class_filter:
        students = students.filter(student_class=class_filter)
    if bus_filter:
        if bus_filter == 'none':
            students = students.filter(bus=None)
        else:
            students = students.filter(bus_id=bus_filter)
    if search:
        students = students.filter(
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search) |
            Q(student_class__icontains=search)
        )

    classes = SchoolClass.objects.filter(is_active=True).order_by('name')
    if not classes.exists():
        classes_names = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    else:
        classes_names = classes.values_list('name', flat=True)

    buses = Bus.objects.filter(is_active=True).order_by('name')

    onboarding_mode_active = (SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION') == 'True')
    can_register = bool(user_profile and user_profile.can_register_students)
    is_attendance_only_teacher = bool(
        user_profile and user_profile.role == UserProfile.ROLE_TEACHER and not user_profile.can_collect_fees
    )

    target_date = timezone.localdate()
    clock_ins = ClockInRecord.objects.filter(date=target_date)
    clock_in_map = {c.student_id: c for c in clock_ins}

    context = {
        'students': students,
        'classes': classes_names,
        'buses': buses,
        'class_filter': class_filter,
        'bus_filter': bus_filter,
        'search': search,
        'total_count': students.count(),
        'onboarding_mode_active': onboarding_mode_active,
        'can_register': can_register,
        'clock_in_map': clock_in_map,
        'target_date': target_date,
        'date_str': target_date.strftime('%Y-%m-%d'),
        'is_attendance_only_teacher': is_attendance_only_teacher,
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
            student = form.save()
            messages.success(request, f"✓ Student '{student.full_name}' enrolled in {student.student_class}!")
            return redirect('student_list')
    else:
        form = StudentForm(user_obj=request.user)
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
    state_str = "activated (all teachers can now enroll students)" if new_state else "deactivated (restricted to Principal only)"
    messages.success(request, f"🚀 Student Onboarding Mode has been {state_str}.")
    next_url = request.GET.get('next') or request.POST.get('next') or 'student_list'
    return redirect(next_url)


@principal_required
def student_update_view(request, pk):
    """Edit student details or bus/canteen assignment."""
    student = get_object_or_404(Student, pk=pk)
    if request.method == 'POST':
        form = StudentForm(request.POST, instance=student)
        if form.is_valid():
            student = form.save()
            messages.success(request, f"Student '{student.full_name}' updated successfully!")
            return redirect('student_list')
    else:
        form = StudentForm(instance=student)
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
    payments = FeePayment.objects.select_related('student', 'student__bus', 'recorded_by', 'recorded_by__profile').order_by('-payment_date', '-created_at')

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
        payments = payments.filter(payment_date=today - datetime.timedelta(days=1))
    elif date_preset == 'week':
        payments = payments.filter(payment_date__gte=today - datetime.timedelta(days=7))
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
    ).select_related('profile', 'profile__assigned_bus').order_by('first_name', 'username')

    collectors_meta = get_collectors_financial_summary(authorized_teachers, today)
    authorized_collectors_summary = []
    for t in authorized_teachers:
        meta = collectors_meta.get(t.id, {})
        t_all_total = FeePayment.objects.filter(recorded_by=t).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        t_class = meta.get('assigned_class') or ''
        t_bus_name = meta.get('assigned_bus_name') or ''
        class_expected = meta.get('adjusted_target', Decimal('0.00'))
        class_collected = meta.get('today_collected', Decimal('0.00'))
        class_balance = meta.get('balance', Decimal('0.00'))
        student_count = meta.get('student_count', 0)

        authorized_collectors_summary.append({
            'user': t,
            'today_total': meta.get('today_collected', Decimal('0.00')),
            'today_count': meta.get('today_count', 0),
            'all_total': t_all_total,
            'assigned_class': t_class or 'Floating',
            'assigned_bus_name': t_bus_name,
            'is_bus_fee_teacher': meta.get('is_bus_fee_teacher', False),
            'bus_expected': meta.get('bus_expected', Decimal('0.00')),
            'canteen_expected': meta.get('canteen_expected', Decimal('0.00')),
            'bus_collected': meta.get('bus_collected', Decimal('0.00')),
            'canteen_collected': meta.get('canteen_collected', Decimal('0.00')),
            'baseline_target': meta.get('baseline_target', Decimal('0.00')),
            'advance_prepayments': meta.get('advance_prepayments', Decimal('0.00')),
            'prepaid_deductions': meta.get('prepaid_deductions', Decimal('0.00')),
            'prepaid_students_count': meta.get('prepaid_students_count', 0),
            'one_off_additions': meta.get('one_off_additions', Decimal('0.00')),
            'one_off_additions_count': meta.get('one_off_additions_count', 0),
            'transfers_out': meta.get('transfers_out', Decimal('0.00')),
            'class_expected': class_expected,
            'class_collected': class_collected,
            'class_balance': class_balance,
            'student_count': student_count,
        })

    # All staff who have recorded payments for filter dropdown
    recorded_by_ids = FeePayment.objects.exclude(recorded_by=None).values_list('recorded_by_id', flat=True).distinct()
    collectors_list = User.objects.filter(id__in=recorded_by_ids).select_related('profile').order_by('first_name', 'username')

    context = {
        'payments': payments,
        'classes': classes,
        'fee_type': fee_type,
        'student_class': student_class,
        'search': search,
        'date_preset': date_preset,
        'date_str': date_str,
        'selected_collector': collector,
        'collectors_list': collectors_list,
        'authorized_collectors_summary': authorized_collectors_summary,
        'total_amount': total_amount,
        'payment_count': payments.count(),
        'today_total': today_total,
        'today_bus_total': today_bus_total,
        'today_canteen_total': today_canteen_total,
    }
    return render(request, 'attendance/payments.html', context)


@payment_recording_required
def payment_create_view(request):
    """Form to record a daily or advance fee payment."""
    student_id = request.GET.get('student_id')
    initial = {}
    selected_class_id = None
    is_guest_mode_initial = False

    user_profile = getattr(request.user, 'profile', None) if request.user.is_authenticated else None
    is_bus_fee_teacher = bool(user_profile and user_profile.is_bus_fee_teacher and user_profile.assigned_bus)
    is_admin_user = bool(user_profile and (user_profile.is_school_admin or user_profile.is_accountant))
    is_scoped_fee_teacher = is_bus_fee_teacher and not is_admin_user
    assigned_bus = user_profile.assigned_bus if is_bus_fee_teacher else None

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
            period = form.cleaned_data.get('period') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
            payment_method = form.cleaned_data.get('payment_method') or 'CASH'
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

            currency = getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')
            total_amount = sum(p.amount for p in created_payments)
            if len(created_payments) == 2:
                breakdown = f" ({currency}{bus_amount:.2f} Bus + {currency}{canteen_amount:.2f} Canteen)"
            else:
                breakdown = f" ({created_payments[0].get_fee_type_display()})"
            messages.success(request, f"Payment of {currency}{total_amount:.2f}{breakdown} for {student.full_name} recorded! Receipt: {receipt_number}")
            return redirect('payment_list')
    else:
        form = FeePaymentForm(initial=initial)

    today = timezone.localdate()
    all_students_by_class = {}
    scoped_students_by_class = {}
    all_bus_riders = []

    students_qs = Student.objects.filter(is_active=True).select_related('bus', 'school_class').order_by('first_name', 'last_name')
    for s in students_qs:
        cls_key = str(s.school_class.id) if s.school_class else ""
        if not cls_key:
            cls_obj = SchoolClass.objects.filter(name=s.student_class).first()
            if cls_obj:
                cls_key = str(cls_obj.id)

        bus_credit = s.get_bus_credit_balance(today)
        bus_has_partial = bool(s.bus and not s.is_bus_paid_for_date(today) and Decimal('0.00') < bus_credit < s.bus_fee_required)
        bus_remainder_due = max(Decimal('0.00'), s.bus_fee_required - bus_credit - s.get_daily_bus_paid(today)) if bus_has_partial else Decimal('0.00')
        can_credit = s.get_canteen_credit_balance(today)
        can_has_partial = bool(s.canteen_enrolled and not s.is_canteen_paid_for_date(today) and Decimal('0.00') < can_credit < s.canteen_fee_required)
        can_remainder_due = max(Decimal('0.00'), s.canteen_fee_required - can_credit - s.get_daily_canteen_paid(today)) if can_has_partial else Decimal('0.00')

        s_dict = {
            'id': s.id,
            'name': s.full_name,
            'class_name': s.school_class.name if s.school_class else (s.student_class or 'Unassigned'),
            'bus_id': s.bus.id if s.bus else '',
            'bus_name': s.bus.name if s.bus else 'Walker',
            'has_bus': bool(s.bus),
            'bus_fee': float(s.bus_fee_required),
            'bus_paid': float(s.get_daily_bus_paid(today)),
            'bus_balance': float(s.get_daily_bus_balance(today)),
            'bus_credit': float(bus_credit),
            'bus_has_partial': bus_has_partial,
            'bus_remainder_due': float(bus_remainder_due),
            'bus_credit_days': s.get_bus_credit_days(today),
            'canteen_enrolled': bool(s.canteen_enrolled),
            'canteen_fee': float(s.canteen_fee_required),
            'canteen_paid': float(s.get_daily_canteen_paid(today)),
            'canteen_balance': float(s.get_daily_canteen_balance(today)),
            'canteen_credit': float(can_credit),
            'canteen_has_partial': can_has_partial,
            'canteen_remainder_due': float(can_remainder_due),
            'canteen_credit_days': s.get_canteen_credit_days(today),
        }

        if cls_key:
            if cls_key not in all_students_by_class:
                all_students_by_class[cls_key] = []
            all_students_by_class[cls_key].append(s_dict)

        if is_scoped_fee_teacher and assigned_bus and s.bus_id == assigned_bus.id:
            all_bus_riders.append(s_dict)
            if cls_key:
                if cls_key not in scoped_students_by_class:
                    scoped_students_by_class[cls_key] = []
                scoped_students_by_class[cls_key].append(s_dict)

    scoped_classes = []
    if is_scoped_fee_teacher and assigned_bus:
        for sc in SchoolClass.objects.filter(is_active=True).order_by('name'):
            sc_id_str = str(sc.id)
            count = len(scoped_students_by_class.get(sc_id_str, []))
            if count > 0:
                scoped_classes.append({
                    'id': sc_id_str,
                    'name': f'{sc.name} ({count} riders)'
                })

    all_classes = [
        {'id': str(sc.id), 'name': sc.name}
        for sc in SchoolClass.objects.filter(is_active=True).order_by('name')
    ]

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
    }
    return render(request, 'attendance/payment_form.html', context)


@principal_required
def payment_delete_view(request, pk):
    """Delete a payment record."""
    payment = get_object_or_404(FeePayment, pk=pk)
    if request.method == 'POST':
        payment.delete()
        messages.success(request, f"Payment receipt {payment.receipt_number} deleted.")
        return redirect('payment_list')
    return render(request, 'attendance/confirm_delete.html', {
        'object': payment,
        'object_name': f"Payment #{payment.receipt_number} ({getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{payment.amount:.2f}) for {payment.student.full_name}",
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

    period = request.GET.get('period', getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026'))

    # Daily attendance summary by class
    classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    class_reports = []
    
    total_school_students = 0
    total_school_present = 0

    for cls in classes:
        cls_students = Student.objects.filter(student_class=cls, is_active=True)
        tot = cls_students.count()
        pres = ClockInRecord.objects.filter(date=target_date, student__in=cls_students).count()
        pct = round((pres / tot * 100), 1) if tot > 0 else 0
        total_school_students += tot
        total_school_present += pres
        class_reports.append({
            'class_name': cls,
            'total': tot,
            'present': pres,
            'absent': tot - pres,
            'pct': pct,
        })

    # Bus fee report by bus for target_date
    buses = Bus.objects.filter(is_active=True).order_by('name')
    bus_reports = []
    total_bus_exp = Decimal('0.00')
    total_bus_col = Decimal('0.00')

    for b in buses:
        riders = b.students.filter(is_active=True)
        r_count = riders.count()
        exp = Decimal(r_count) * b.fee
        total_bus_exp += exp
        col = FeePayment.objects.filter(fee_type='BUS', student__bus=b, payment_date=target_date).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
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
        'currency': getattr(settings, 'CURRENCY_SYMBOL', 'GH₵'),
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

    period = request.GET.get('period', getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026'))
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="daily_debtors_{target_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Student Name', 'Class', 'Bus Route', 'Bus Fee Required', 'Bus Fee Status', 'Bus Balance', 'Canteen Fee Required', 'Canteen Status', 'Canteen Balance', 'Total Owed Today', 'Date'])

    students = Student.objects.filter(is_active=True).select_related('bus').order_by('student_class', 'first_name')
    currency = getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')

    for s in students:
        bus_req = s.bus_fee_required
        bus_bal = s.get_daily_bus_balance(target_date)
        is_bus_paid = s.is_bus_paid_for_date(target_date)
        can_req = s.canteen_fee_required
        can_bal = s.get_daily_canteen_balance(target_date)
        is_can_paid = s.is_canteen_paid_for_date(target_date)
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

    items = []
    for p in all_payments:
        if p.fee_type == 'BUS':
            req = student.bus_fee_required
            bal = student.get_bus_balance(period)
            desc = f"Bus Transportation ({student.bus.name if student.bus else 'Walker'})"
        else:
            req = student.canteen_fee_required
            bal = student.get_canteen_balance(period)
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

    bus_balance = student.get_bus_balance(period)
    canteen_balance = student.get_canteen_balance(period)

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
        'currency': getattr(settings, 'CURRENCY_SYMBOL', 'GH₵'),
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
    """View and update system-wide school billing and default canteen settings."""
    current_canteen = get_default_canteen_fee()
    current_school_name = SchoolSetting.get_setting('SCHOOL_NAME') or getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School')
    current_period = SchoolSetting.get_setting('CURRENT_ACADEMIC_PERIOD') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    current_currency = SchoolSetting.get_setting('CURRENCY_SYMBOL') or getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')
    current_allow_registration = (SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION') == 'True')

    if request.method == 'POST':
        form = SchoolSettingsForm(request.POST)
        if form.is_valid():
            new_canteen = form.cleaned_data['default_canteen_fee']
            new_school_name = form.cleaned_data['school_name']
            new_period = form.cleaned_data['current_period']
            new_currency = form.cleaned_data['currency_symbol']
            new_allow_reg = form.cleaned_data.get('allow_teacher_student_registration', False)

            SchoolSetting.set_setting('DEFAULT_CANTEEN_FEE', new_canteen, "Standard default daily canteen lunch rate")
            SchoolSetting.set_setting('SCHOOL_NAME', new_school_name, "Official school name")
            SchoolSetting.set_setting('CURRENT_ACADEMIC_PERIOD', new_period, "Current active academic term")
            SchoolSetting.set_setting('CURRENCY_SYMBOL', new_currency, "Default currency symbol")
            SchoolSetting.set_setting('ALLOW_TEACHER_STUDENT_REGISTRATION', 'True' if new_allow_reg else 'False', "Permit teachers to register students during onboarding")

            reg_status_msg = "enabled (teachers can register students)" if new_allow_reg else "disabled (restricted to Principal only)"
            messages.success(request, f"✓ Settings updated! Onboarding registration is {reg_status_msg}.")
            return redirect('school_settings')
    else:
        form = SchoolSettingsForm(initial={
            'default_canteen_fee': current_canteen,
            'school_name': current_school_name,
            'current_period': current_period,
            'currency_symbol': current_currency,
            'allow_teacher_student_registration': current_allow_registration,
        })

    enrolled_canteen_count = Student.objects.filter(canteen_enrolled=True, is_active=True).count()
    daily_canteen_expected = Decimal(enrolled_canteen_count) * current_canteen

    context = {
        'form': form,
        'title': 'School & Canteen Settings',
        'current_canteen': current_canteen,
        'enrolled_canteen_count': enrolled_canteen_count,
        'daily_canteen_expected': daily_canteen_expected,
        'allow_teacher_registration': current_allow_registration,
    }
    return render(request, 'attendance/settings.html', context)


# ==========================================
# TEACHER ACCOUNT MANAGEMENT & STAFF ROSTER
# ==========================================
@finance_required
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
    teachers_qs = User.objects.filter(profile__role__in=STAFF_ROLES).select_related('profile').order_by('first_name', 'username')

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
    messages.info(request, f"Staff account '{teacher.username}' has been {status_str}.")
    return redirect('teacher_list')


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

