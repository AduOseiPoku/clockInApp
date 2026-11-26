import json
from decimal import Decimal
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import Bus, Student, FeePayment, UserProfile, DebtRecord

User = get_user_model()


class BusRosterFixesTests(TestCase):
    """Tests for Bus Roster Mark Unpaid UI, Debt Badges, and Bus Fleet back-link permissions for teachers."""

    def setUp(self):
        self.bus_8 = Bus.objects.create(name='East Legon Bus', fee=Decimal('8.00'), is_active=True)
        self.bus_10 = Bus.objects.create(name='Airport Bus', fee=Decimal('10.00'), is_active=True)

        self.bus_teacher = User.objects.create_user('bus_roster_t', 'brt@test.com', 'pw')
        self.bus_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.bus_teacher.profile.can_collect_fees = True
        self.bus_teacher.profile.assigned_bus = self.bus_8
        self.bus_teacher.profile.save()

        self.unassigned_teacher = User.objects.create_user('plain_t', 'pt@test.com', 'pw')
        self.unassigned_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.unassigned_teacher.profile.save()

        self.student = Student.objects.create(
            first_name='Kwaku', last_name='Addo', student_class='Class 1A', bus=self.bus_8, canteen_enrolled=True
        )

    def test_assigned_bus_teacher_can_access_bus_list_while_unassigned_is_blocked(self):
        self.client.force_login(self.bus_teacher)
        res = self.client.get(reverse('bus_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'East Legon Bus')
        self.assertNotContains(res, 'Airport Bus')

        self.client.force_login(self.unassigned_teacher)
        res_unassigned = self.client.get(reverse('bus_list'))
        self.assertEqual(res_unassigned.status_code, 302)

    def test_bus_analytics_renders_unpaid_buttons_and_debt_badges(self):
        today = timezone.localdate()
        self.client.force_login(self.bus_teacher)
        self.client.post(
            reverse('api_mark_unpaid'),
            data=json.dumps({'student_id': self.student.id, 'fee_type': 'BUS', 'date': str(today)}),
            content_type='application/json',
        )

        res = self.client.get(reverse('bus_analytics', kwargs={'pk': self.bus_8.pk}))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Mark Guest Unpaid')
        self.assertContains(res, 'openBusMarkUnpaidModal')
        self.assertContains(res, 'Debt (1d):')


class DatabaseEfficiencyAndGuardsTests(TestCase):
    """Verify O(1) query scaling on debtors_list/export and strict input/query-param guards."""

    def setUp(self):
        self.bus = Bus.objects.create(name='Route A', fee=Decimal('10.00'), is_active=True)
        self.admin = User.objects.create_superuser('guard_admin', 'ga@test.com', 'pw')
        self.teacher = User.objects.create_user('guard_t', 'gt@test.com', 'pw')
        self.teacher.profile.role = UserProfile.ROLE_TEACHER
        self.teacher.profile.can_collect_fees = True
        self.teacher.profile.assigned_bus = self.bus
        self.teacher.profile.save()

        self.students = [
            Student.objects.create(first_name=f'Stu{i}', last_name='Test', student_class='Class 1A', bus=self.bus)
            for i in range(6)
        ]

    def test_debtors_list_and_payment_export_have_no_bus_n_plus_one(self):
        today = timezone.localdate()
        for s in self.students:
            DebtRecord.objects.create(
                student=s, date=today, fee_type='BUS', bus=self.bus, amount_owed=Decimal('10.00')
            )
            FeePayment.objects.create(
                student=s, bus=None, fee_type='CANTEEN', amount=Decimal('7.00'), payment_date=today, recorded_by=self.teacher
            )

        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as ctx_debt:
            res_debt = self.client.get(reverse('debtors_list'))
        self.assertEqual(res_debt.status_code, 200)
        bus_queries_debt = [q['sql'] for q in ctx_debt if 'attendance_bus' in q['sql']]
        self.assertLessEqual(len(bus_queries_debt), 3)

        with CaptureQueriesContext(connection) as ctx_exp:
            res_exp = self.client.get(reverse('payment_export_csv'))
        self.assertEqual(res_exp.status_code, 200)
        bus_queries_exp = [q['sql'] for q in ctx_exp if 'attendance_bus' in q['sql']]
        self.assertEqual(len(bus_queries_exp), 1)

    def test_invalid_numeric_inputs_return_400_instead_of_500(self):
        self.client.force_login(self.teacher)
        s = self.students[0]

        for bad_payload in [
            {'student_id': s.id, 'fee_type': 'BUS', 'days': -2},
            {'student_id': s.id, 'fee_type': 'BUS', 'days': 'abc'},
            {'student_id': s.id, 'fee_type': 'BUS', 'amount': 'NaN'},
            {'student_id': s.id, 'fee_type': 'BUS', 'bus_amount': '99999999999'},
        ]:
            res = self.client.post(
                reverse('api_collect_both_fees'),
                data=json.dumps(bad_payload),
                content_type='application/json',
            )
            self.assertEqual(res.status_code, 400)

        res_q = self.client.post(
            reverse('api_quick_payment'),
            data=json.dumps({'student_id': s.id, 'fee_type': 'BUS', 'amount': 'NaN'}),
            content_type='application/json',
        )
        self.assertEqual(res_q.status_code, 400)

    def test_malformed_integer_query_params_do_not_500(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('payment_list') + '?collector=abc').status_code, 200)
        self.assertEqual(self.client.get(reverse('payment_export_csv') + '?collector=abc').status_code, 200)
        self.assertEqual(self.client.get(reverse('audit_log_list') + '?actor=abc').status_code, 200)
        self.assertEqual(self.client.get(reverse('dashboard') + '?bus=abc').status_code, 200)
