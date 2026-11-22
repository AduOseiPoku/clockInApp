import datetime
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, F
from django.conf import settings
from django.utils import timezone
from attendance.models import (
    Student, FeePayment, ClockInRecord, CreditTransaction, DebtPaymentAllocation,
    SchoolSetting, get_default_canteen_fee
)


class Command(BaseCommand):
    help = (
        "Audits and reconciles historical student prepaid wallet balances (bus and canteen) "
        "against past advance payments and clock-in deductions. Safe and idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--student-id',
            type=int,
            help='Reconcile only a specific student by ID',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Simulate reconciliation without saving changes to database',
        )

    def handle(self, *args, **options):
        student_id = options.get('student_id')
        dry_run = options.get('dry_run', False)

        self.stdout.write(self.style.NOTICE("==> Starting Student Wallet Ledger Reconciliation..."))
        if dry_run:
            self.stdout.write(self.style.WARNING("    [DRY-RUN MODE] No database modifications will be saved."))

        default_canteen_fee = get_default_canteen_fee()

        students_qs = Student.objects.select_related('bus').all()
        if student_id:
            students_qs = students_qs.filter(id=student_id)

        students_list = list(students_qs)
        total_students = len(students_list)
        student_ids = [s.id for s in students_list]

        # 1. Preload all fee payments grouped by (student_id, fee_type)
        all_payments = (
            FeePayment.objects.filter(student_id__in=student_ids)
            .select_related('bus')
            .order_by('payment_date', 'created_at', 'id')
        )
        payments_map = {}
        for p in all_payments:
            if (p.notes or '').startswith('Debt clearance:'):
                continue
            key = (p.student_id, p.fee_type)
            payments_map.setdefault(key, {}).setdefault(p.payment_date, []).append(p)

        # Preload prior-date debt allocations per payment so debt clearance isn't double-counted as wallet top-up
        prior_debt_alloc_by_payment = dict(
            DebtPaymentAllocation.objects.filter(
                payment__student_id__in=student_ids,
                debt_record__date__lt=F('payment__payment_date'),
            )
            .values('payment_id')
            .annotate(total=Sum('amount_applied'))
            .values_list('payment_id', 'total')
        )

        # 2. Preload all clock-in records where student was present
        all_clockins = (
            ClockInRecord.objects.filter(student_id__in=student_ids, status__in=['PRESENT', 'LATE'])
            .order_by('date', 'clock_in_time', 'id')
        )
        clockins_map = {}
        for ci in all_clockins:
            clockins_map.setdefault(ci.student_id, {})[ci.date] = ci

        # 3. Preload all existing CreditTransactions
        existing_txs = (
            CreditTransaction.objects.filter(student_id__in=student_ids)
            .order_by('date', 'created_at', 'id')
        )
        existing_tx_map = {}
        for tx in existing_txs:
            key = (tx.student_id, tx.fee_type, tx.date, tx.transaction_type)
            existing_tx_map.setdefault(key, []).append(tx)

        corrections_count = 0
        deductions_created_count = 0
        topups_created_count = 0

        with transaction.atomic():
            for student in students_list:
                s_clockins = clockins_map.get(student.id, {})
                changed_fields = []

                for fee_type in ('BUS', 'CANTEEN'):
                    wallet_field = 'bus_credit_wallet' if fee_type == 'BUS' else 'canteen_credit_wallet'
                    current_wallet = getattr(student, wallet_field) or Decimal('0.00')

                    if fee_type == 'BUS':
                        daily_fee = student.bus.fee if student.bus_id else Decimal('0.00')
                    else:
                        if student.canteen_enrolled:
                            daily_fee = (
                                student.custom_canteen_fee
                                if student.custom_canteen_fee is not None
                                else default_canteen_fee
                            )
                        else:
                            daily_fee = Decimal('0.00')

                    daily_fee = max(Decimal('0.00'), daily_fee or Decimal('0.00'))
                    date_payments = payments_map.get((student.id, fee_type), {})
                    all_dates = sorted(set(s_clockins.keys()) | set(date_payments.keys()))

                    running_wallet = Decimal('0.00')

                    for d in all_dates:
                        day_payments = date_payments.get(d, [])
                        clockin = s_clockins.get(d)

                        direct_cash_on_day = Decimal('0.00')
                        deducted_on_day = Decimal('0.00')

                        # Process direct payments on date d
                        for p in day_payments:
                            prior_alloc = prior_debt_alloc_by_payment.get(p.id, Decimal('0.00'))
                            if fee_type == 'BUS' and p.is_temporary_bus:
                                guest_fee = (p.bus.fee if p.bus_id and p.bus else daily_fee) or Decimal('0.00')
                                guest_fee = max(Decimal('0.00'), guest_fee)
                                if p.amount > guest_fee:
                                    excess = max(Decimal('0.00'), p.amount - guest_fee - prior_alloc)
                                    if excess > Decimal('0.00'):
                                        running_wallet += excess
                                        topup_key = (student.id, fee_type, d, CreditTransaction.TYPE_TOP_UP)
                                        existing_topups = existing_tx_map.get(topup_key, [])
                                        matching_topup = next((tx for tx in existing_topups if tx.payment_id == p.id), None)
                                        if not matching_topup and not dry_run:
                                            tx = CreditTransaction.objects.create(
                                                student=student,
                                                fee_type=fee_type,
                                                transaction_type=CreditTransaction.TYPE_TOP_UP,
                                                amount=excess,
                                                balance_after=running_wallet,
                                                date=d,
                                                payment=p,
                                                recorded_by=p.recorded_by,
                                                notes=f"Reconciled guest bus credit from payment {p.receipt_number or p.id}",
                                            )
                                            existing_tx_map.setdefault(topup_key, []).append(tx)
                                            topups_created_count += 1
                                continue

                            p_fee = daily_fee
                            if p_fee <= Decimal('0.00') and fee_type == 'BUS' and p.bus_id and p.bus:
                                p_fee = p.bus.fee
                            owed_before = max(Decimal('0.00'), p_fee - direct_cash_on_day - deducted_on_day)

                            if p.amount > owed_before:
                                excess = max(Decimal('0.00'), p.amount - owed_before - prior_alloc)
                                direct_cash_on_day += p.amount
                                if excess > Decimal('0.00'):
                                    running_wallet += excess

                                    # Verify TOP_UP transaction exists
                                    topup_key = (student.id, fee_type, d, CreditTransaction.TYPE_TOP_UP)
                                    existing_topups = existing_tx_map.get(topup_key, [])
                                    matching_topup = next((tx for tx in existing_topups if tx.payment_id == p.id), None)
                                    if not matching_topup and not dry_run:
                                        tx = CreditTransaction.objects.create(
                                            student=student,
                                            fee_type=fee_type,
                                            transaction_type=CreditTransaction.TYPE_TOP_UP,
                                            amount=excess,
                                            balance_after=running_wallet,
                                            date=d,
                                            payment=p,
                                            recorded_by=p.recorded_by,
                                            notes=f"Reconciled bulk credit from payment {p.receipt_number or p.id}",
                                        )
                                        existing_tx_map.setdefault(topup_key, []).append(tx)
                                        topups_created_count += 1

                            elif p.amount < owed_before and running_wallet > Decimal('0.00'):
                                owed_after_cash = owed_before - p.amount
                                use_wallet = min(running_wallet, owed_after_cash)
                                direct_cash_on_day += p.amount
                                if use_wallet > Decimal('0.00'):
                                    running_wallet -= use_wallet
                                    deducted_on_day += use_wallet
                                    ded_key = (student.id, fee_type, d, CreditTransaction.TYPE_DEDUCTION)
                                    existing_deds = existing_tx_map.get(ded_key, [])
                                    matching_ded = next((tx for tx in existing_deds if tx.payment_id == p.id), None)
                                    if not matching_ded and not dry_run:
                                        tx = CreditTransaction.objects.create(
                                            student=student,
                                            fee_type=fee_type,
                                            transaction_type=CreditTransaction.TYPE_DEDUCTION,
                                            amount=use_wallet,
                                            balance_after=running_wallet,
                                            date=d,
                                            payment=p,
                                            clock_in=clockin,
                                            recorded_by=p.recorded_by,
                                            notes=f"Reconciled wallet credit applied with payment {p.receipt_number or p.id}",
                                        )
                                        existing_tx_map.setdefault(ded_key, []).append(tx)
                                        deductions_created_count += 1
                            else:
                                direct_cash_on_day += p.amount

                        # If clocked in on date d and daily fee owed was not covered by cash, deduct from wallet
                        if clockin and daily_fee > Decimal('0.00') and running_wallet > Decimal('0.00'):
                            remaining_owed = max(Decimal('0.00'), daily_fee - direct_cash_on_day - deducted_on_day)
                            if remaining_owed > Decimal('0.00'):
                                use_wallet = min(running_wallet, remaining_owed)
                                if use_wallet > Decimal('0.00'):
                                    running_wallet -= use_wallet
                                    deducted_on_day += use_wallet

                                    # Check if deduction transaction already exists
                                    ded_key = (student.id, fee_type, d, CreditTransaction.TYPE_DEDUCTION)
                                    existing_deds = existing_tx_map.get(ded_key, [])
                                    matching_ded = next(
                                        (tx for tx in existing_deds if tx.clock_in_id == clockin.id or (not tx.payment_id and tx.amount == use_wallet)),
                                        None
                                    )
                                    if not matching_ded and not dry_run:
                                        tx = CreditTransaction.objects.create(
                                            student=student,
                                            fee_type=fee_type,
                                            transaction_type=CreditTransaction.TYPE_DEDUCTION,
                                            amount=use_wallet,
                                            balance_after=running_wallet,
                                            date=d,
                                            clock_in=clockin,
                                            recorded_by=clockin.recorded_by,
                                            notes=f"Reconciled daily clock-in deduction for {d.isoformat()}",
                                        )
                                        existing_tx_map.setdefault(ded_key, []).append(tx)
                                        deductions_created_count += 1

                    # Check if final wallet balance diverges from database value
                    if running_wallet != current_wallet:
                        if not dry_run:
                            setattr(student, wallet_field, running_wallet)
                            changed_fields.append(wallet_field)
                        self.stdout.write(
                            f"  [FIX] Student {student.full_name} ({fee_type}): "
                            f"Wallet adjusted from GH₵{current_wallet:.2f} -> GH₵{running_wallet:.2f}"
                        )
                        corrections_count += 1

                if changed_fields and not dry_run:
                    student.save(update_fields=changed_fields)

            if dry_run:
                # Force rollback in dry-run mode
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f"==> Reconciliation complete: {total_students} students checked. "
            f"{corrections_count} wallet balances corrected. "
            f"{deductions_created_count} missing deductions backfilled. "
            f"{topups_created_count} missing top-ups backfilled."
        ))
