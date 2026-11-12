# Generated manually for explicit prepaid credit wallet backfill

from decimal import Decimal
from django.conf import settings
from django.db import migrations


def backfill_credit_wallets(apps, schema_editor):
    Student = apps.get_model('attendance', 'Student')
    FeePayment = apps.get_model('attendance', 'FeePayment')
    ClockInRecord = apps.get_model('attendance', 'ClockInRecord')
    CreditTransaction = apps.get_model('attendance', 'CreditTransaction')
    SchoolSetting = apps.get_model('attendance', 'SchoolSetting')

    setting_obj = SchoolSetting.objects.filter(key='DEFAULT_CANTEEN_FEE').first()
    if setting_obj and setting_obj.value is not None:
        try:
            default_canteen_fee = Decimal(str(setting_obj.value))
        except Exception:
            default_canteen_fee = Decimal(str(getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))))
    else:
        default_canteen_fee = Decimal(str(getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))))

    # Preload clock-ins by student_id -> {date: clock_in}
    clockins_by_student = {}
    for ci in ClockInRecord.objects.all().order_by('date', 'id'):
        clockins_by_student.setdefault(ci.student_id, {})[ci.date] = ci

    # Preload non-temporary payments by (student_id, fee_type) -> {date: [payments]}
    payments_by_key = {}
    for p in FeePayment.objects.select_related('bus').all().order_by('payment_date', 'id'):
        if p.fee_type == 'BUS' and p.is_temporary_bus:
            continue
        key = (p.student_id, p.fee_type)
        payments_by_key.setdefault(key, {}).setdefault(p.payment_date, []).append(p)

    students = Student.objects.select_related('bus').all()
    for student in students:
        clockin_map = clockins_by_student.get(student.id, {})

        for fee_type in ('BUS', 'CANTEEN'):
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
            date_payments = payments_by_key.get((student.id, fee_type), {})
            all_dates = sorted(set(clockin_map.keys()) | set(date_payments.keys()))

            wallet = Decimal('0.00')
            for d in all_dates:
                day_p_list = date_payments.get(d, [])
                clockin = clockin_map.get(d)

                direct_on_d = Decimal('0.00')
                deducted_on_d = Decimal('0.00')

                for p in day_p_list:
                    fee_for_p = daily_fee
                    if fee_for_p <= Decimal('0.00') and fee_type == 'BUS' and p.bus_id and p.bus:
                        fee_for_p = p.bus.fee
                    owed_before = max(Decimal('0.00'), fee_for_p - direct_on_d - deducted_on_d)

                    if p.amount > owed_before:
                        excess = p.amount - owed_before
                        wallet += excess
                        direct_on_d += p.amount
                        CreditTransaction.objects.create(
                            student=student,
                            fee_type=fee_type,
                            transaction_type='TOP_UP',
                            amount=excess,
                            balance_after=wallet,
                            date=d,
                            payment=p,
                            recorded_by=p.recorded_by,
                            notes=f"Backfilled bulk credit from payment {p.receipt_number or p.id}",
                        )
                    elif p.amount < owed_before and wallet > Decimal('0.00'):
                        owed_after_cash = owed_before - p.amount
                        use_wallet = min(wallet, owed_after_cash)
                        direct_on_d += p.amount
                        if use_wallet > Decimal('0.00'):
                            wallet -= use_wallet
                            deducted_on_d += use_wallet
                            CreditTransaction.objects.create(
                                student=student,
                                fee_type=fee_type,
                                transaction_type='DEDUCTION',
                                amount=use_wallet,
                                balance_after=wallet,
                                date=d,
                                payment=p,
                                clock_in=clockin,
                                recorded_by=p.recorded_by,
                                notes=f"Backfilled wallet credit with payment {p.receipt_number or p.id}",
                            )
                    else:
                        direct_on_d += p.amount

                # If clocked in on date d and today's fee wasn't covered by cash, deduct from wallet
                if clockin and daily_fee > Decimal('0.00') and wallet > Decimal('0.00'):
                    remaining_owed = max(Decimal('0.00'), daily_fee - direct_on_d - deducted_on_d)
                    if remaining_owed > Decimal('0.00'):
                        use_wallet = min(wallet, remaining_owed)
                        if use_wallet > Decimal('0.00'):
                            wallet -= use_wallet
                            deducted_on_d += use_wallet
                            CreditTransaction.objects.create(
                                student=student,
                                fee_type=fee_type,
                                transaction_type='DEDUCTION',
                                amount=use_wallet,
                                balance_after=wallet,
                                date=d,
                                clock_in=clockin,
                                recorded_by=clockin.recorded_by,
                                notes=f"Backfilled daily deduction for {d.isoformat()}",
                            )

            if fee_type == 'BUS':
                student.bus_credit_wallet = wallet
            else:
                student.canteen_credit_wallet = wallet

        student.save(update_fields=['bus_credit_wallet', 'canteen_credit_wallet'])


def reverse_backfill(apps, schema_editor):
    Student = apps.get_model('attendance', 'Student')
    CreditTransaction = apps.get_model('attendance', 'CreditTransaction')
    CreditTransaction.objects.all().delete()
    Student.objects.all().update(
        bus_credit_wallet=Decimal('0.00'),
        canteen_credit_wallet=Decimal('0.00'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0015_student_bus_credit_wallet_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_credit_wallets, reverse_backfill),
    ]
