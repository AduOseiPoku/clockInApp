import datetime
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import (
    Bus,
    ClockInRecord,
    FeePayment,
    SchoolClass,
    SchoolSetting,
    Student,
    UserProfile,
)


class DailyManagementFeeAndAutoClockInTests(TestCase):
    """Verify daily management fee, principal dashboard/reports cards, and auto-clock-in on payment."""

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser('mgmt_admin2', 'admin2@mgmt.test', 'pass123')
        self.admin_user.profile.role = UserProfile.ROLE_ADMIN
        self.admin_user.profile.save()

        self.bursar_user = User.objects.create_user('mgmt_bursar2', 'bursar2@mgmt.test', 'pass123')
        self.bursar_user.profile.role = UserProfile.ROLE_ACCOUNTANT
        self.bursar_user.profile.save()

        SchoolClass.objects.create(name='Class 1A')
        self.bus = Bus.objects.create(name='East Bus', fee=Decimal('40.00'))
        SchoolSetting.set_setting('DEFAULT_MANAGEMENT_FEE', '150.00', 'Default management fee')

        self.student = Student.objects.create(
            first_name='Yaw',
            last_name='Boakye',
            student_class='Class 1A',
            bus=self.bus,
            canteen_enrolled=True,
        )

    def test_daily_management_fee_resets_daily_and_supports_advance_wallet_deduction(self):
        day1 = timezone.localdate() - datetime.timedelta(days=1)
        day2 = timezone.localdate()
        self.student.pays_management_fee = True
        self.student.custom_management_fee = Decimal('8.00')
        self.student.save()

        self.client.force_login(self.admin_user)
        res_pay = self.client.post(
            reverse('api_collect_both_fees'),
            data=json.dumps({
                'student_id': self.student.id,
                'fee_type': 'MANAGEMENT',
                'amount': '24.00',
                'date': str(day1),
            }),
            content_type='application/json',
        )
        self.assertEqual(res_pay.status_code, 200)
        self.assertTrue(res_pay.json()['success'])

        self.student.refresh_from_db()
        self.assertTrue(self.student.is_management_fee_paid(day1))
        self.assertEqual(self.student.unified_credit_wallet, Decimal('16.00'))
        self.assertFalse(self.student.is_management_fee_settled_for_date(day2))
        self.assertTrue(self.student.is_management_fee_paid(day2))
        self.assertEqual(self.student.get_management_credit_balance(day2), Decimal('16.00'))

        res_ci = self.client.post(
            reverse('api_clock_in_toggle'),
            data=json.dumps({'student_id': self.student.id, 'date': str(day2), 'action': 'clock_in'}),
            content_type='application/json',
        )
        self.assertEqual(res_ci.status_code, 200)
        self.student.refresh_from_db()
        self.assertEqual(self.student.unified_credit_wallet, Decimal('8.00'))
        self.assertEqual(self.student.get_deducted_amount_for_date('MANAGEMENT', day2), Decimal('8.00'))

        self.client.force_login(self.bursar_user)
        report_res = self.client.get(reverse('reports') + f'?date={day2}')
        self.assertEqual(report_res.status_code, 200)
        self.assertEqual(report_res.context['management_expected'], Decimal('8.00'))
        self.assertEqual(report_res.context['management_prepaid'], Decimal('8.00'))
        self.assertEqual(report_res.context['management_collected'], Decimal('8.00'))
        self.assertEqual(report_res.context['management_balance'], Decimal('0.00'))
        self.assertContains(report_res, 'Daily Management Fee Reconciliation')

        self.client.force_login(self.admin_user)
        dash_res = self.client.get(reverse('dashboard') + f'?date={day2}')
        self.assertEqual(dash_res.status_code, 200)
        self.assertContains(dash_res, "Today's Management Fee")

    def test_auto_clock_in_on_today_bus_and_management_payment_only(self):
        today = timezone.localdate()
        yesterday = today - datetime.timedelta(days=1)

        bus_student = Student.objects.create(
            first_name='Kofi',
            last_name='Bus',
            student_class='Class 1A',
            bus=self.bus,
            is_active=True,
        )
        FeePayment.objects.create(
            student=bus_student,
            bus=self.bus,
            fee_type='BUS',
            amount=Decimal('10.00'),
            payment_date=today,
            period='Term 1',
            recorded_by=self.admin_user,
        )
        self.assertTrue(ClockInRecord.objects.filter(student=bus_student, date=today, status='PRESENT').exists())

        self.student.pays_management_fee = True
        self.student.custom_management_fee = Decimal('10.00')
        self.student.save()
        FeePayment.objects.create(
            student=self.student,
            fee_type='MANAGEMENT',
            amount=Decimal('10.00'),
            payment_date=today,
            period='Term 1',
            recorded_by=self.admin_user,
        )
        self.assertTrue(ClockInRecord.objects.filter(student=self.student, date=today, status='PRESENT').exists())

        back_student = Student.objects.create(
            first_name='Ama',
            last_name='Backdated',
            student_class='Class 1A',
            bus=self.bus,
            is_active=True,
        )
        FeePayment.objects.create(
            student=back_student,
            bus=self.bus,
            fee_type='BUS',
            amount=Decimal('10.00'),
            payment_date=yesterday,
            period='Term 1',
            recorded_by=self.admin_user,
        )
        self.assertFalse(ClockInRecord.objects.filter(student=back_student).exists())

        debt_student = Student.objects.create(
            first_name='Kwame',
            last_name='Debt',
            student_class='Class 1A',
            bus=self.bus,
            is_active=True,
        )
        p_debt = FeePayment(
            student=debt_student,
            bus=self.bus,
            fee_type='BUS',
            amount=Decimal('10.00'),
            payment_date=today,
            period='Term 1',
            recorded_by=self.admin_user,
            notes='Debt clearance: yesterday',
        )
        p_debt._is_debt_settlement = True
        p_debt.save()
        self.assertFalse(ClockInRecord.objects.filter(student=debt_student, date=today).exists())
