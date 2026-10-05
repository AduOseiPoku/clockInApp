import csv
import json
from decimal import Decimal
from datetime import datetime
from io import TextIOWrapper
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.conf import settings

from .models import Bus, Student, FeePayment, ClockInRecord, UserProfile, NotificationLog, SchoolClass, SchoolSetting, get_default_canteen_fee
from .forms import BusForm, StudentForm, FeePaymentForm, SchoolSettingsForm

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

@ensure_csrf_cookie
def dashboard_view(request):
    """
    Teacher Clock-In & Fee Verification Dashboard.
    Provides instant visibility of dynamic bus fees, canteen fees, and 1-click clock-in.
    """
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
        'currency': getattr(settings, 'CURRENCY_SYMBOL', 'GH₵'),
    }
    return render(request, 'attendance/dashboard.html', context)


@require_POST
def toggle_clock_in_api(request):
    """
    AJAX API endpoint for 1-click Clock-In and Undo Clock-In.
    Returns JSON response for smooth micro-animations and instantaneous UI update.
    """
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

            # Automatically log parent notification
            NotificationLog.objects.create(
                student=student,
                notification_type=NotificationLog.TYPE_CLOCK_IN,
                recipient="Parent / Guardian",
                message=f"Dear Parent, {student.full_name} arrived safely and clocked in at {time_str} today ({target_date})."
            )

        # Fetch updated quick counters
        period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        stats = get_stats_data(target_date, period)

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
                    p_bus = FeePayment.objects.create(
                        student=student,
                        fee_type='BUS',
                        amount=bus_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_bus)

                if canteen_amount > Decimal('0.00'):
                    p_can = FeePayment.objects.create(
                        student=student,
                        fee_type='CANTEEN',
                        amount=canteen_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
                        notes=notes,
                        recorded_by=recorded_by
                    )
                    created_payments.append(p_can)
            else:
                amount = Decimal(str(data.get('amount', 0)))
                if amount > Decimal('0.00'):
                    p = FeePayment.objects.create(
                        student=student,
                        fee_type=fee_type,
                        amount=amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=shared_receipt,
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


# ==========================================
# BUS MANAGEMENT VIEWS
# ==========================================
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
# STUDENT MANAGEMENT VIEWS
# ==========================================
def student_list_view(request):
    """Student roster listing with bus assignments and canteen status."""
    class_filter = request.GET.get('class', '')
    bus_filter = request.GET.get('bus', '')
    search = request.GET.get('q', '').strip()

    students = Student.objects.filter(is_active=True).select_related('bus').order_by('student_class', 'first_name', 'last_name')

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

    classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    buses = Bus.objects.filter(is_active=True).order_by('name')

    context = {
        'students': students,
        'classes': classes,
        'buses': buses,
        'class_filter': class_filter,
        'bus_filter': bus_filter,
        'search': search,
        'total_count': students.count(),
    }
    return render(request, 'attendance/students.html', context)


def student_create_view(request):
    """Enroll a new student."""
    if request.method == 'POST':
        form = StudentForm(request.POST)
        if form.is_valid():
            student = form.save()
            messages.success(request, f"Student '{student.full_name}' enrolled in {student.student_class}!")
            return redirect('student_list')
    else:
        form = StudentForm()
    return render(request, 'attendance/student_form.html', {'form': form, 'title': 'Enroll New Student'})


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
def payment_list_view(request):
    """Payments ledger showing history of bus and canteen fee payments."""
    payments = FeePayment.objects.select_related('student', 'student__bus').order_by('-payment_date', '-created_at')

    fee_type = request.GET.get('fee_type', '')
    student_class = request.GET.get('class', '')
    search = request.GET.get('q', '').strip()
    date_preset = request.GET.get('preset', '')
    date_str = request.GET.get('date', '').strip()

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
    if search:
        payments = payments.filter(
            Q(student__first_name__icontains=search) |
            Q(student__last_name__icontains=search) |
            Q(receipt_number__icontains=search)
        )

    classes = Student.objects.filter(is_active=True).values_list('student_class', flat=True).distinct().order_by('student_class')
    total_amount = payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    # Today's cashier collections
    today_payments = FeePayment.objects.filter(payment_date=today)
    today_total = today_payments.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_bus_total = today_payments.filter(fee_type='BUS').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    today_canteen_total = today_payments.filter(fee_type='CANTEEN').aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    context = {
        'payments': payments,
        'classes': classes,
        'fee_type': fee_type,
        'student_class': student_class,
        'search': search,
        'date_preset': date_preset,
        'date_str': date_str,
        'total_amount': total_amount,
        'payment_count': payments.count(),
        'today_total': today_total,
        'today_bus_total': today_bus_total,
        'today_canteen_total': today_canteen_total,
    }
    return render(request, 'attendance/payments.html', context)


def payment_create_view(request):
    """Form to record a daily or advance fee payment."""
    student_id = request.GET.get('student_id')
    initial = {}
    selected_class_id = None

    if student_id:
        target_student = Student.objects.filter(id=student_id, is_active=True).first()
        if target_student:
            initial['student'] = target_student.id
            if target_student.bus:
                initial['bus'] = target_student.bus.id
            if target_student.school_class:
                initial['student_class'] = target_student.school_class.id
                selected_class_id = str(target_student.school_class.id)
            else:
                cls_obj = SchoolClass.objects.filter(name=target_student.student_class).first()
                if cls_obj:
                    initial['student_class'] = cls_obj.id
                    selected_class_id = str(cls_obj.id)

    fee_type_arg = request.GET.get('fee_type')
    if fee_type_arg in ('BUS', 'CANTEEN'):
        initial['fee_type'] = fee_type_arg

    if request.method == 'POST':
        form = FeePaymentForm(request.POST)
        if form.is_valid():
            student = form.cleaned_data['student']

            # Update student's bus assignment if bus was selected in the form
            if 'bus' in request.POST:
                selected_bus = form.cleaned_data.get('bus')
                if student.bus != selected_bus:
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

            created_payments = []
            with transaction.atomic():
                if bus_amount > Decimal('0.00'):
                    p_bus = FeePayment.objects.create(
                        student=student,
                        fee_type='BUS',
                        amount=bus_amount,
                        payment_date=payment_date,
                        period=period,
                        payment_method=payment_method,
                        receipt_number=receipt_number,
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
    students_by_class = {}
    for s in Student.objects.filter(is_active=True).select_related('bus', 'school_class'):
        cls_key = str(s.school_class.id) if s.school_class else ""
        if not cls_key:
            cls_obj = SchoolClass.objects.filter(name=s.student_class).first()
            if cls_obj:
                cls_key = str(cls_obj.id)
        if cls_key:
            if cls_key not in students_by_class:
                students_by_class[cls_key] = []
            students_by_class[cls_key].append({
                'id': s.id,
                'name': s.full_name,
                'bus_id': s.bus.id if s.bus else '',
                'bus_name': s.bus.name if s.bus else 'Walker',
                'has_bus': bool(s.bus),
                'bus_fee': float(s.bus_fee_required),
                'bus_paid': float(s.get_daily_bus_paid(today)),
                'bus_balance': float(s.get_daily_bus_balance(today)),
                'bus_credit_days': s.get_bus_credit_days(today),
                'canteen_enrolled': bool(s.canteen_enrolled),
                'canteen_fee': float(s.canteen_fee_required),
                'canteen_paid': float(s.get_daily_canteen_paid(today)),
                'canteen_balance': float(s.get_daily_canteen_balance(today)),
                'canteen_credit_days': s.get_canteen_credit_days(today),
            })

    buses_list = [
        {'id': b.id, 'name': b.name, 'fee': float(b.fee)}
        for b in Bus.objects.filter(is_active=True).order_by('name')
    ]

    context = {
        'form': form,
        'title': 'Record Fee Payment',
        'students_by_class_json': json.dumps(students_by_class),
        'buses_json': json.dumps(buses_list),
        'initial_student_id': student_id or '',
        'initial_class_id': selected_class_id or '',
    }
    return render(request, 'attendance/payment_form.html', context)


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
            next_url = request.GET.get('next') or 'dashboard'
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
                canteen_enrolled = canteen_str in ('yes', 'true', '1', 'y')

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
def school_settings_view(request):
    """View and update system-wide school billing and default canteen settings."""
    current_canteen = get_default_canteen_fee()
    current_school_name = SchoolSetting.get_setting('SCHOOL_NAME') or getattr(settings, 'SCHOOL_NAME', 'Geosaka Model School')
    current_period = SchoolSetting.get_setting('CURRENT_ACADEMIC_PERIOD') or getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
    current_currency = SchoolSetting.get_setting('CURRENCY_SYMBOL') or getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')

    if request.method == 'POST':
        form = SchoolSettingsForm(request.POST)
        if form.is_valid():
            new_canteen = form.cleaned_data['default_canteen_fee']
            new_school_name = form.cleaned_data['school_name']
            new_period = form.cleaned_data['current_period']
            new_currency = form.cleaned_data['currency_symbol']

            SchoolSetting.set_setting('DEFAULT_CANTEEN_FEE', new_canteen, "Standard default daily canteen lunch rate")
            SchoolSetting.set_setting('SCHOOL_NAME', new_school_name, "Official school name")
            SchoolSetting.set_setting('CURRENT_ACADEMIC_PERIOD', new_period, "Current active academic term")
            SchoolSetting.set_setting('CURRENCY_SYMBOL', new_currency, "Default currency symbol")

            messages.success(request, f"✓ Settings updated! Default canteen fee is now {new_currency}{new_canteen:.2f}/day.")
            return redirect('school_settings')
    else:
        form = SchoolSettingsForm(initial={
            'default_canteen_fee': current_canteen,
            'school_name': current_school_name,
            'current_period': current_period,
            'currency_symbol': current_currency,
        })

    enrolled_canteen_count = Student.objects.filter(canteen_enrolled=True, is_active=True).count()
    daily_canteen_expected = Decimal(enrolled_canteen_count) * current_canteen

    context = {
        'form': form,
        'title': 'School & Canteen Settings',
        'current_canteen': current_canteen,
        'enrolled_canteen_count': enrolled_canteen_count,
        'daily_canteen_expected': daily_canteen_expected,
    }
    return render(request, 'attendance/settings.html', context)
