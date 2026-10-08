import io
import json
import datetime
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import Bus, Student, FeePayment, ClockInRecord, UserProfile, NotificationLog, SchoolClass
from .forms import StudentForm


User = get_user_model()


class SchoolClockInSystemTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Users and roles
        self.admin_user = User.objects.create_superuser('testadmin', 'admin@test.com', 'pass123')
        self.teacher_user = User.objects.create_user('testteacher', 'teacher@test.com', 'pass123')
        self.bursar_user = User.objects.create_user('testbursar', 'bursar@test.com', 'pass123')

        UserProfile.objects.filter(user=self.teacher_user).update(role=UserProfile.ROLE_TEACHER)
        UserProfile.objects.filter(user=self.bursar_user).update(role=UserProfile.ROLE_ACCOUNTANT)
        self.client.force_login(self.admin_user)

        # Dynamic Buses
        self.tema_bus = Bus.objects.create(name='Tema Bus', fee=Decimal('80.00'), route_description='Comm 1 - 25')
        self.madina_bus = Bus.objects.create(name='Madina Bus', fee=Decimal('50.00'), route_description='Madina - Legon')

        # Students (No student ID)
        self.student_tema = Student.objects.create(
            first_name='Kwesi',
            last_name='Arthur',
            student_class='Class 1A',
            bus=self.tema_bus,
            canteen_enrolled=True
        )

        self.student_madina = Student.objects.create(
            first_name='Kofi',
            last_name='Adu',
            student_class='Class 1A',
            bus=self.madina_bus,
            canteen_enrolled=False
        )

        self.student_walker = Student.objects.create(
            first_name='Ama',
            last_name='Serwaa',
            student_class='Class 2B',
            bus=None,
            canteen_enrolled=True
        )

    def test_dynamic_bus_fees_assignment(self):
        """Verify each bus has its distinct configured fee and students inherit it."""
        self.assertEqual(self.tema_bus.fee, Decimal('80.00'))
        self.assertEqual(self.madina_bus.fee, Decimal('50.00'))

        self.assertEqual(self.student_tema.bus_fee_required, Decimal('80.00'))
        self.assertEqual(self.student_madina.bus_fee_required, Decimal('50.00'))
        self.assertEqual(self.student_walker.bus_fee_required, Decimal('0.00'))

    def test_walker_is_bus_paid_automatically(self):
        """Walkers who do not board a bus should be marked as bus fee paid (no fee required)."""
        self.assertTrue(self.student_walker.is_bus_paid())
        self.assertEqual(self.student_walker.get_bus_balance(), Decimal('0.00'))

    def test_bus_fee_payment_and_balances(self):
        """Test partial and full payments for bus fees."""
        self.assertFalse(self.student_tema.is_bus_paid())
        self.assertEqual(self.student_tema.get_bus_balance(), Decimal('80.00'))

        # Partial payment
        FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=Decimal('30.00'),
            period='Term 1 - 2026'
        )
        self.assertFalse(self.student_tema.is_bus_paid())
        self.assertEqual(self.student_tema.get_bus_balance(), Decimal('50.00'))

        # Remaining payment
        FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=Decimal('50.00'),
            period='Term 1 - 2026'
        )
        self.assertTrue(self.student_tema.is_bus_paid())
        self.assertEqual(self.student_tema.get_bus_balance(), Decimal('0.00'))

    def test_clock_in_toggle_and_parent_notification(self):
        """Test 1-click clock-in creates ClockInRecord and logs parent arrival notification."""
        url = reverse('api_clock_in_toggle')
        
        response = self.client.post(
            url,
            data=json.dumps({'student_id': self.student_tema.id, 'action': 'clock_in'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['is_clocked_in'])

        # Verify parent notification log was created
        notif = NotificationLog.objects.filter(student=self.student_tema, notification_type=NotificationLog.TYPE_CLOCK_IN).first()
        self.assertIsNotNone(notif)
        self.assertIn("Kwesi Arthur", notif.message)

    def test_quick_payment_api(self):
        """Test quick payment recording via AJAX endpoint."""
        url = reverse('api_quick_payment')
        response = self.client.post(
            url,
            data=json.dumps({
                'student_id': self.student_madina.id,
                'fee_type': 'BUS',
                'amount': '50.00',
                'payment_method': 'MOBILE_MONEY',
            }),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['bus_status']['is_paid'])
        self.assertEqual(data['bus_status']['balance'], 0.0)

    def test_bulk_student_csv_import(self):
        """Test importing students from CSV."""
        csv_content = (
            "first_name,last_name,student_class,bus_name,canteen_enrolled\n"
            "Yaw,Osei,Class 3A,Tema Bus,yes\n"
            "Afia,Kusi,Class 3A,,no\n"
        )
        csv_file = io.BytesIO(csv_content.encode('utf-8'))
        csv_file.name = 'import_test.csv'

        response = self.client.post(reverse('student_import'), {'csv_file': csv_file}, follow=True)
        self.assertEqual(response.status_code, 200)

        yaw = Student.objects.filter(first_name='Yaw', last_name='Osei').first()
        self.assertIsNotNone(yaw)
        self.assertEqual(yaw.student_class, 'Class 3A')
        self.assertEqual(yaw.bus, self.tema_bus)
        self.assertTrue(yaw.canteen_enrolled)

        afia = Student.objects.filter(first_name='Afia', last_name='Kusi').first()
        self.assertIsNotNone(afia)
        self.assertIsNone(afia.bus)
        self.assertFalse(afia.canteen_enrolled)

    def test_csv_exports(self):
        """Test attendance and debtors CSV exports."""
        # 1. Attendance CSV
        res_att = self.client.get(reverse('export_attendance_csv'))
        self.assertEqual(res_att.status_code, 200)
        self.assertEqual(res_att['Content-Type'], 'text/csv')
        self.assertIn(b'Student Name,Class,Bus Route,Status', res_att.content)

        # 2. Debtors CSV
        res_deb = self.client.get(reverse('export_debtors_csv'))
        self.assertEqual(res_deb.status_code, 200)
        self.assertEqual(res_deb['Content-Type'], 'text/csv')
        self.assertIn(b'Total Owed', res_deb.content)

    def test_payment_receipt_view(self):
        """Test receipt voucher generation."""
        payment = FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=Decimal('80.00'),
            receipt_number='REC-TEST-999',
            period='Term 1 - 2026'
        )
        url = reverse('payment_receipt', args=[payment.id])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'REC-TEST-999')
        self.assertContains(res, 'Kwesi Arthur')
        self.assertContains(res, 'Official School Fee Payment Receipt')

    def test_login_and_logout(self):
        """Test user login and logout."""
        self.client.logout()
        login_url = reverse('login')
        res = self.client.post(login_url, {'username': 'testteacher', 'password': 'pass123'}, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.context['user'].is_authenticated)

        logout_url = reverse('logout')
        res_logout = self.client.get(logout_url, follow=True)
        self.assertEqual(res_logout.status_code, 200)
        self.assertFalse(res_logout.context['user'].is_authenticated)

    def test_class_dependent_payment_recording(self):
        """Test admin-created SchoolClass and two-step fee recording workflow."""
        # 1. Verify SchoolClasses were auto-created
        classes = SchoolClass.objects.all()
        self.assertGreater(classes.count(), 0)

        # 2. Test GET payment_create view returns class mapping
        url = reverse('payment_create')
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertIn('students_by_class_json', res.context)
        parsed_json = json.loads(res.context['students_by_class_json'])
        self.assertIsInstance(parsed_json, dict)

        # 3. Test pre-selection via student_id query parameter
        res_prefill = self.client.get(f"{url}?student_id={self.student_tema.id}")
        self.assertEqual(res_prefill.status_code, 200)
        self.assertEqual(str(res_prefill.context['initial_student_id']), str(self.student_tema.id))
        self.assertTrue(bool(res_prefill.context['initial_class_id']))

        # 4. Test submitting payment form for selected student
        post_data = {
            'student_class': self.student_tema.school_class.id,
            'student': self.student_tema.id,
            'fee_type': 'BUS',
            'amount': '80.00',
            'period': 'Term 1 - 2026',
            'payment_method': 'CASH',
            'receipt_number': 'REC-DEP-001',
            'notes': 'Test payment with class selector'
        }
        res_post = self.client.post(url, post_data, follow=True)
        self.assertEqual(res_post.status_code, 200)
        payment = FeePayment.objects.filter(receipt_number='REC-DEP-001').first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.student, self.student_tema)
        self.assertEqual(payment.amount, Decimal('80.00'))

    def test_dual_fee_payment_recording(self):
        """Test recording both Bus fee and Canteen fee in a single atomic transaction."""
        url = reverse('payment_create')
        post_data = {
            'student_class': self.student_tema.school_class.id,
            'student': self.student_tema.id,
            'bus_amount': '80.00',
            'canteen_amount': '45.00',
            'period': 'Term 1 - 2026',
            'payment_method': 'MOBILE_MONEY',
            'receipt_number': 'REC-DUAL-001',
            'notes': 'Dual fee payment'
        }
        res_post = self.client.post(url, post_data, follow=True)
        self.assertEqual(res_post.status_code, 200)

        # Verify two records were created with the same receipt number
        payments = FeePayment.objects.filter(receipt_number='REC-DUAL-001')
        self.assertEqual(payments.count(), 2)

        bus_pay = payments.filter(fee_type='BUS').first()
        canteen_pay = payments.filter(fee_type='CANTEEN').first()

        self.assertIsNotNone(bus_pay)
        self.assertEqual(bus_pay.amount, Decimal('80.00'))

        self.assertIsNotNone(canteen_pay)
        self.assertEqual(canteen_pay.amount, Decimal('45.00'))

        # Verify student balances are now zero
        self.assertTrue(self.student_tema.is_bus_paid())
        self.assertTrue(self.student_tema.is_canteen_paid())

        # Verify multi-item receipt view displays both items
        receipt_url = reverse('payment_receipt', args=[bus_pay.id])
        res_receipt = self.client.get(receipt_url)
        self.assertEqual(res_receipt.status_code, 200)
        self.assertContains(res_receipt, 'REC-DUAL-001')
        self.assertContains(res_receipt, '80.00')
        self.assertContains(res_receipt, '45.00')
        self.assertContains(res_receipt, '125.00')

    def test_streamlined_payment_recording(self):
        """Test recording fee payment with only student and amounts, auto-generating all metadata."""
        url = reverse('payment_create')
        post_data = {
            'student_class': self.student_tema.school_class.id,
            'student': self.student_tema.id,
            'bus_amount': '80.00',
            'canteen_amount': '0.00',
        }
        res_post = self.client.post(url, post_data, follow=True)
        self.assertEqual(res_post.status_code, 200)

        # Verify payment was created with auto-defaults
        payment = FeePayment.objects.filter(student=self.student_tema, fee_type='BUS').latest('created_at')
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal('80.00'))
        self.assertEqual(payment.payment_method, 'CASH')
        self.assertTrue(payment.receipt_number.startswith('REC-'))
        self.assertEqual(payment.period, 'Term 1 - 2026')

    def test_payment_recording_with_bus_selection(self):
        """Test that selecting a bus on the payment form updates the student's bus and enables payment."""
        url = reverse('payment_create')
        # Initially, walker student has no bus
        self.assertIsNone(self.student_walker.bus)

        # GET payment form should include buses_json and bus field
        res_get = self.client.get(url)
        self.assertEqual(res_get.status_code, 200)
        self.assertIn('buses_json', res_get.context)
        self.assertContains(res_get, 'id_bus')

        # Submit payment selecting Tema Bus for the walker student
        post_data = {
            'student_class': self.student_walker.school_class.id,
            'student': self.student_walker.id,
            'bus': self.tema_bus.id,
            'bus_amount': '80.00',
            'canteen_amount': '0.00',
        }
        res_post = self.client.post(url, post_data, follow=True)
        self.assertEqual(res_post.status_code, 200)

        # Verify student bus was updated
        self.student_walker.refresh_from_db()
        self.assertEqual(self.student_walker.bus, self.tema_bus)

        # Verify bus payment was created
        payment = FeePayment.objects.filter(student=self.student_walker, fee_type='BUS').latest('created_at')
        self.assertEqual(payment.amount, Decimal('80.00'))

    def test_daily_fee_reset_across_dates(self):
        """Test that paying for Day 1 marks student paid for Day 1, but resets to unpaid on Day 2."""
        day1 = timezone.localdate()
        day2 = day1 + datetime.timedelta(days=1)

        # Pay exactly 1 day bus fee on Day 1
        FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=self.student_tema.bus.fee,
            payment_date=day1
        )
        # Clock in on Day 1
        ClockInRecord.objects.create(
            student=self.student_tema,
            date=day1,
            clock_in_time=datetime.time(8, 0),
            status='PRESENT'
        )

        # Day 1 checks
        self.assertTrue(self.student_tema.is_bus_paid_for_date(day1))
        self.assertEqual(self.student_tema.get_daily_bus_balance(day1), Decimal('0.00'))
        self.assertEqual(self.student_tema.get_bus_credit_days(day1), 0)

        # Day 2 morning reset checks
        self.assertFalse(self.student_tema.is_bus_paid_for_date(day2))
        self.assertEqual(self.student_tema.get_daily_bus_balance(day2), self.student_tema.bus.fee)
        self.assertEqual(self.student_tema.get_bus_credit_days(day2), 0)

    def test_advance_payment_multi_day_credit(self):
        """Test multi-day advance prepayment and wallet credit consumption upon clocking in."""
        monday = timezone.localdate()
        tuesday = monday + datetime.timedelta(days=1)
        thursday = monday + datetime.timedelta(days=3)

        daily_rate = self.student_tema.bus.fee  # 80.00
        # Prepay 5 days (400.00) on Monday
        FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=daily_rate * 5,
            payment_date=monday
        )

        # Monday morning before clock-in: Covered by credit, 4 additional credit days available
        self.assertTrue(self.student_tema.is_bus_paid_for_date(monday))
        self.assertEqual(self.student_tema.get_bus_credit_days(monday), 4)

        # Clock in Monday
        ClockInRecord.objects.create(
            student=self.student_tema,
            date=monday,
            clock_in_time=datetime.time(8, 0),
            status='PRESENT'
        )

        # Tuesday morning: 1 day consumed, 3 credit days remaining after today
        self.assertTrue(self.student_tema.is_bus_paid_for_date(tuesday))
        self.assertEqual(self.student_tema.get_bus_credit_days(tuesday), 3)

        # Student is absent on Wednesday (no clock in record created)
        # Thursday morning: Monday was the only attendance day, so still 3 credit days remaining after Thursday
        self.assertTrue(self.student_tema.is_bus_paid_for_date(thursday))
        self.assertEqual(self.student_tema.get_bus_credit_days(thursday), 3)

    def test_quick_payment_both_fees_api(self):
        """Test AJAX Quick Payment API when recording both Bus and Canteen fees atomically."""
        url = reverse('api_quick_payment')
        today = timezone.localdate()

        canteen_req = str(self.student_tema.canteen_fee_required)
        post_data = {
            'student_id': self.student_tema.id,
            'fee_type': 'BOTH',
            'bus_amount': str(self.student_tema.bus.fee),
            'canteen_amount': canteen_req,
            'payment_date': today.strftime('%Y-%m-%d'),
            'payment_method': 'CASH',
        }
        res = self.client.post(url, data=json.dumps(post_data), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['bus_status']['is_paid'])
        self.assertTrue(data['canteen_status']['is_paid'])
        self.assertEqual(data['bus_status']['balance'], 0.0)
        self.assertEqual(data['canteen_status']['balance'], 0.0)

        # Verify two FeePayment records were created with same receipt number
        payments = FeePayment.objects.filter(student=self.student_tema, payment_date=today)
        self.assertEqual(payments.count(), 2)
        receipt_nos = set(payments.values_list('receipt_number', flat=True))
        self.assertEqual(len(receipt_nos), 1)

    def test_daily_cashier_reports_reconciliation(self):
        """Test daily reports view reconciles daily expected, collected, and outstanding balances."""
        today = timezone.localdate()
        # Record a payment for today
        FeePayment.objects.create(
            student=self.student_tema,
            fee_type='BUS',
            amount=Decimal('80.00'),
            payment_date=today
        )

        res = self.client.get(reverse('reports') + f'?date={today.strftime("%Y-%m-%d")}')
        self.assertEqual(res.status_code, 200)
        self.assertIn('grand_total_exp', res.context)
        self.assertIn('grand_total_col', res.context)
        self.assertIn('grand_total_bal', res.context)
        self.assertGreaterEqual(res.context['grand_total_col'], Decimal('80.00'))

    def test_change_default_canteen_rate_from_app(self):
        """Test changing default canteen rate from within the app updates settings and student requirements dynamically."""
        url = reverse('school_settings')

        # Initial GET request
        res_get = self.client.get(url)
        self.assertEqual(res_get.status_code, 200)
        self.assertContains(res_get, 'School &amp; Canteen Settings')

        # POST new canteen rate of 15.00
        post_data = {
            'default_canteen_fee': '15.00',
            'school_name': 'Geosaka Model School',
            'current_period': 'Term 1 - 2026',
            'currency_symbol': 'GH₵',
        }
        res_post = self.client.post(url, post_data, follow=True)
        self.assertEqual(res_post.status_code, 200)
        self.assertContains(res_post, 'Settings updated')

        # Verify model dynamic function returns 15.00
        from .models import get_default_canteen_fee
        self.assertEqual(get_default_canteen_fee(), Decimal('15.00'))

        # Verify student with standard canteen enrollment now requires 15.00 daily
        self.assertEqual(self.student_tema.canteen_fee_required, Decimal('15.00'))


class RoleBasedAuthorizationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser('test_principal', 'principal@school.com', 'pass123')
        self.teacher_user = User.objects.create_user('test_teacher_auth', 'teacher_auth@school.com', 'pass123')
        self.bursar_user = User.objects.create_user('test_bursar_auth', 'bursar_auth@school.com', 'pass123')

        teacher_profile = self.teacher_user.profile
        teacher_profile.role = UserProfile.ROLE_TEACHER
        teacher_profile.save()

        bursar_profile = self.bursar_user.profile
        bursar_profile.role = UserProfile.ROLE_ACCOUNTANT
        bursar_profile.save()

        self.bus = Bus.objects.create(name='Auth Test Bus', fee=Decimal('50.00'))
        self.student = Student.objects.create(
            first_name='Ama',
            last_name='Koduah',
            student_class='Class 1A',
            bus=self.bus,
            canteen_enrolled=True
        )

    def test_unauthenticated_user_redirected_to_login(self):
        """Unauthenticated requests to protected endpoints redirect to login."""
        protected_urls = [
            reverse('dashboard'),
            reverse('payment_list'),
            reverse('payment_create'),
            reverse('reports'),
            reverse('bus_list'),
            reverse('student_list'),
            reverse('school_settings'),
            reverse('notifications'),
        ]
        for url in protected_urls:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 302)
            self.assertIn('/login/', res.url)

    def test_teacher_role_permissions(self):
        """Teacher without fee permission cannot record payments; teacher with fee permission can."""
        self.client.force_login(self.teacher_user)

        # 1. By default, teacher has can_collect_fees=False
        res_dash = self.client.get(reverse('dashboard'))
        self.assertEqual(res_dash.status_code, 200)
        self.assertContains(res_dash, 'Class Roster')
        self.assertNotContains(res_dash, '+ Record Fee')
        self.assertContains(res_dash, 'Review Only')
        # Total revenue card must NOT be in HTML for teachers
        self.assertNotContains(res_dash, "Today's Revenue")
        # Hidden nav links
        self.assertNotContains(res_dash, reverse('reports'))
        self.assertNotContains(res_dash, reverse('school_settings'))
        self.assertNotContains(res_dash, reverse('bus_list'))

        # 2. Blocked from payment create view
        res_pay = self.client.get(reverse('payment_create'), follow=True)
        self.assertContains(res_pay, 'You are not authorized to collect or record fee payments')

        # 3. Blocked from quick payment API
        quick_data = {
            'student_id': self.student.id,
            'fee_type': 'CANTEEN',
            'amount': '10.00',
            'canteen_amount': '10.00',
            'payment_method': 'CASH',
        }
        res_api = self.client.post(
            reverse('api_quick_payment'),
            json.dumps(quick_data),
            content_type='application/json'
        )
        self.assertEqual(res_api.status_code, 403)
        self.assertFalse(res_api.json()['success'])

        # 4. Now grant fee collection permission
        teacher_profile = self.teacher_user.profile
        teacher_profile.can_collect_fees = True
        teacher_profile.save()

        # Authorized teacher now sees + Record Fee and can record payments
        res_dash_auth = self.client.get(reverse('dashboard'))
        self.assertContains(res_dash_auth, '+ Record Fee')
        self.assertContains(res_dash_auth, 'Pay')

        res_pay_auth = self.client.get(reverse('payment_create'))
        self.assertEqual(res_pay_auth.status_code, 200)

        res_api_auth = self.client.post(
            reverse('api_quick_payment'),
            json.dumps(quick_data),
            content_type='application/json'
        )
        self.assertEqual(res_api_auth.status_code, 200)
        self.assertTrue(res_api_auth.json()['success'])

        # 5. Denied from reports and settings
        res_rep = self.client.get(reverse('reports'), follow=True)
        self.assertContains(res_rep, 'Access denied')

        res_set = self.client.get(reverse('school_settings'), follow=True)
        self.assertContains(res_set, 'Principal administrator privileges required')

        # 6. Denied from payment delete
        payment = FeePayment.objects.first()
        res_del = self.client.get(reverse('payment_delete', args=[payment.id]), follow=True)
        self.assertContains(res_del, 'Access denied')

    def test_bursar_role_permissions(self):
        """Bursar has strict read-only financial access: redirected from attendance, cannot record/delete payments."""
        self.client.force_login(self.bursar_user)

        # 1. Attendance view is removed: visiting dashboard redirects to payment_list
        res_dash = self.client.get(reverse('dashboard'))
        self.assertEqual(res_dash.status_code, 302)
        self.assertEqual(res_dash.url, reverse('payment_list'))

        # 2. Blocked from attendance clock-in API
        res_clock = self.client.post(
            reverse('api_clock_in_toggle'),
            json.dumps({'student_id': self.student.id, 'action': 'clock_in'}),
            content_type='application/json'
        )
        self.assertEqual(res_clock.status_code, 403)
        self.assertFalse(res_clock.json()['success'])

        # 3. Blocked from attendance CSV export
        res_att_csv = self.client.get(reverse('export_attendance_csv'), follow=True)
        self.assertContains(res_att_csv, 'Principal administrator privileges required')

        # 4. Access payments and reports
        res_pay = self.client.get(reverse('payment_list'))
        self.assertEqual(res_pay.status_code, 200)
        self.assertContains(res_pay, 'Fee Payments Ledger')
        # '+ Record Payment' must be hidden on payments page for Bursar
        self.assertNotContains(res_pay, '+ Record Payment')
        # Navbar must NOT have Dashboard or Class Roster for Bursar
        self.assertNotContains(res_pay, 'Class Roster')
        self.assertNotContains(res_pay, '>Dashboard<')

        # 5. Reports access granted (debtors CSV available)
        res_rep = self.client.get(reverse('reports'))
        self.assertEqual(res_rep.status_code, 200)
        self.assertNotContains(res_rep, 'Attendance CSV')

        # 6. Blocked from recording payment
        res_create = self.client.get(reverse('payment_create'), follow=True)
        self.assertContains(res_create, 'Bursar account is designated for revenue review')

        # 7. Blocked from quick payment API
        quick_data = {
            'student_id': self.student.id,
            'fee_type': 'CANTEEN',
            'amount': '10.00',
            'canteen_amount': '10.00',
            'payment_method': 'CASH',
        }
        res_api = self.client.post(
            reverse('api_quick_payment'),
            json.dumps(quick_data),
            content_type='application/json'
        )
        self.assertEqual(res_api.status_code, 403)
        self.assertFalse(res_api.json()['success'])

        # 8. Blocked from deleting payments (read-only enforcement)
        payment = FeePayment.objects.create(
            student=self.student,
            fee_type='CANTEEN',
            amount=Decimal('10.00'),
            receipt_number='DEL-TEST-001',
            recorded_by=self.admin_user
        )
        res_del = self.client.get(reverse('payment_delete', args=[payment.id]), follow=True)
        self.assertContains(res_del, 'Principal administrator privileges required')

        # 9. Blocked from school settings
        res_set = self.client.get(reverse('school_settings'), follow=True)
        self.assertContains(res_set, 'Principal administrator privileges required')

    def test_principal_role_permissions(self):
        """Principal has full access to all sections, settings, and payment actions."""
        self.client.force_login(self.admin_user)

        res_dash = self.client.get(reverse('dashboard'))
        self.assertEqual(res_dash.status_code, 200)
        self.assertContains(res_dash, "Today's Revenue")
        self.assertNotContains(res_dash, '+ Record Fee')
        self.assertContains(res_dash, reverse('school_settings'))

        res_pay = self.client.get(reverse('payment_create'))
        self.assertEqual(res_pay.status_code, 200)

        res_set = self.client.get(reverse('school_settings'))
        self.assertEqual(res_set.status_code, 200)

        res_rep = self.client.get(reverse('reports'))
        self.assertEqual(res_rep.status_code, 200)

    def test_principal_can_access_teacher_management(self):
        """Principal has access to teacher list and teacher creation form."""
        self.client.force_login(self.admin_user)

        res = self.client.get(reverse('teacher_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Teachers & Staff Management')
        self.assertContains(res, reverse('teacher_create'))

        res_form = self.client.get(reverse('teacher_create'))
        self.assertEqual(res_form.status_code, 200)
        self.assertContains(res_form, 'Add New Teacher')

    def test_non_principal_cannot_manage_teachers_but_bursar_has_read_only(self):
        """Teachers cannot access staff roster; Bursars can view staff roster in read-only mode."""
        # 1. Teacher attempt is completely denied
        self.client.force_login(self.teacher_user)
        res_teacher = self.client.get(reverse('teacher_list'), follow=True)
        self.assertContains(res_teacher, 'Access denied')

        res_create = self.client.get(reverse('teacher_create'), follow=True)
        self.assertContains(res_create, 'Principal administrator privileges required')

        # 2. Bursar has read-only access to teacher_list
        self.client.force_login(self.bursar_user)
        res_bursar = self.client.get(reverse('teacher_list'))
        self.assertEqual(res_bursar.status_code, 200)
        self.assertContains(res_bursar, 'Auditing Mode (Read-Only)')
        self.assertContains(res_bursar, 'Fee Collectors')
        # '+ Add Teacher' must NOT be visible to Bursar
        self.assertNotContains(res_bursar, '+ Add Teacher')
        # Edit and password reset links must NOT be visible to Bursar
        self.assertNotContains(res_bursar, reverse('teacher_create'))
        self.assertNotContains(res_bursar, reverse('teacher_update', args=[self.teacher_user.pk]))

        # 3. Bursar cannot modify teachers
        res_bursar_create = self.client.get(reverse('teacher_create'), follow=True)
        self.assertContains(res_bursar_create, 'Principal administrator privileges required')

        res_bursar_update = self.client.get(reverse('teacher_update', args=[self.teacher_user.pk]), follow=True)
        self.assertContains(res_bursar_update, 'Principal administrator privileges required')

        res_bursar_reset = self.client.get(reverse('teacher_password_reset', args=[self.teacher_user.pk]), follow=True)
        self.assertContains(res_bursar_reset, 'Principal administrator privileges required')

    def test_bursar_authorized_fee_collectors_visibility_and_ledger_filters(self):
        """Bursar view of staff roster strictly excludes unauthorized teachers and displays daily remittance."""
        # Authorize teacher to collect fees and assign class
        teacher_profile = self.teacher_user.profile
        teacher_profile.can_collect_fees = True
        teacher_profile.assigned_class = 'Class 1A'
        teacher_profile.save()

        # Create an unauthorized teacher (attendance only)
        unauth_teacher = User.objects.create_user('unauth_teacher_bob', 'bob@school.com', 'pass123')
        unauth_profile = unauth_teacher.profile
        unauth_profile.role = UserProfile.ROLE_TEACHER
        unauth_profile.can_collect_fees = False
        unauth_profile.save()

        # Record a payment by the authorized teacher
        payment = FeePayment.objects.create(
            student=self.student,
            fee_type='BUS',
            amount=Decimal('50.00'),
            receipt_number='REC-AUTH-001',
            recorded_by=self.teacher_user
        )

        self.client.force_login(self.bursar_user)

        # 1. Staff roster automatically shows only authorized fee collectors (default visit)
        res_teachers = self.client.get(reverse('teacher_list'))
        self.assertEqual(res_teachers.status_code, 200)
        self.assertContains(res_teachers, self.teacher_user.username)
        self.assertContains(res_teachers, 'Fee Collector')
        self.assertContains(res_teachers, '50.00')  # Cash to remit today
        # Unauthorized teacher MUST NOT be visible to Bursar
        self.assertNotContains(res_teachers, 'unauth_teacher_bob')

        # 2. Payments ledger displays authorized collectors overview with remittance
        res_payments = self.client.get(reverse('payment_list'))
        self.assertEqual(res_payments.status_code, 200)
        self.assertContains(res_payments, 'Authorized Fee Collection Teachers')
        self.assertContains(res_payments, 'Received By')
        self.assertContains(res_payments, self.teacher_user.username)
        self.assertContains(res_payments, '50.00')

        # 3. Filter payments by collector
        res_filtered = self.client.get(reverse('payment_list') + f'?collector={self.teacher_user.id}')
        self.assertEqual(res_filtered.status_code, 200)
        self.assertContains(res_filtered, 'REC-AUTH-001')

    def test_principal_creates_teacher_account_successfully(self):
        """Principal creates a new teacher, verifying role, assigned class, and login."""
        self.client.force_login(self.admin_user)

        data = {
            'username': 'mr_mensah',
            'first_name': 'Kofi',
            'last_name': 'Mensah',
            'email': 'mensah@school.com',
            'phone_number': '+233 24 555 1234',
            'assigned_class': 'Class 1A',
            'password': 'StrongPassword123!',
            'confirm_password': 'StrongPassword123!',
            'is_active': True,
            'can_collect_fees': True,
        }
        res = self.client.post(reverse('teacher_create'), data, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Teacher account created for Kofi Mensah')

        # Verify user in database
        new_teacher = User.objects.get(username='mr_mensah')
        self.assertEqual(new_teacher.first_name, 'Kofi')
        self.assertEqual(new_teacher.last_name, 'Mensah')
        self.assertEqual(new_teacher.profile.role, UserProfile.ROLE_TEACHER)
        self.assertEqual(new_teacher.profile.assigned_class, 'Class 1A')
        self.assertEqual(new_teacher.profile.phone_number, '+233 24 555 1234')
        self.assertTrue(new_teacher.profile.can_collect_fees)

        # Verify new teacher can sign in and defaults to their assigned class on dashboard
        self.client.logout()
        login_success = self.client.login(username='mr_mensah', password='StrongPassword123!')
        self.assertTrue(login_success)

        res_dash = self.client.get(reverse('dashboard'))
        self.assertEqual(res_dash.status_code, 200)
        # Dashboard should auto-filter to Class 1A
        self.assertEqual(res_dash.context['class_filter'], 'Class 1A')
        student_items = res_dash.context['student_items']
        for item in student_items:
            self.assertEqual(item['student'].student_class, 'Class 1A')

    def test_principal_updates_teacher_details(self):
        """Principal edits an existing teacher's assigned class, name, phone, and fee permission."""
        self.client.force_login(self.admin_user)

        self.assertFalse(self.teacher_user.profile.can_collect_fees)

        update_data = {
            'first_name': 'Grace',
            'last_name': 'Ansah',
            'email': 'grace@school.com',
            'phone_number': '+233 20 999 8888',
            'assigned_class': 'Class 2B',
            'is_active': True,
            'can_collect_fees': True,
        }
        res = self.client.post(reverse('teacher_update', args=[self.teacher_user.pk]), update_data, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'updated successfully')

        self.teacher_user.refresh_from_db()
        self.assertEqual(self.teacher_user.first_name, 'Grace')
        self.assertEqual(self.teacher_user.profile.assigned_class, 'Class 2B')
        self.assertEqual(self.teacher_user.profile.phone_number, '+233 20 999 8888')
        self.assertTrue(self.teacher_user.profile.can_collect_fees)

    def test_principal_resets_teacher_password(self):
        """Principal resets password for a teacher and teacher logs in with new password."""
        self.client.force_login(self.admin_user)

        reset_data = {
            'new_password': 'BrandNewPassword999!',
            'confirm_password': 'BrandNewPassword999!',
        }
        res = self.client.post(reverse('teacher_password_reset', args=[self.teacher_user.pk]), reset_data, follow=True)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Password reset for')

        # Test login with old password fails, new password succeeds
        self.client.logout()
        old_login = self.client.login(username=self.teacher_user.username, password='pass123')
        self.assertFalse(old_login)

        new_login = self.client.login(username=self.teacher_user.username, password='BrandNewPassword999!')
        self.assertTrue(new_login)

    def test_principal_toggles_teacher_active_status(self):
        """Principal can toggle active status to disable or enable accounts."""
        self.client.force_login(self.admin_user)

        self.assertTrue(self.teacher_user.is_active)
        res_deact = self.client.get(reverse('teacher_toggle_status', args=[self.teacher_user.pk]), follow=True)
        self.assertEqual(res_deact.status_code, 200)

        self.teacher_user.refresh_from_db()
        self.assertFalse(self.teacher_user.is_active)

        # Deactivated teacher cannot log in
        self.client.logout()
        deact_login = self.client.login(username='testteacher', password='pass123')
        self.assertFalse(deact_login)

        # Reactivate
        self.client.force_login(self.admin_user)
        self.client.get(reverse('teacher_toggle_status', args=[self.teacher_user.pk]), follow=True)
        self.teacher_user.refresh_from_db()
        self.assertTrue(self.teacher_user.is_active)

    def test_teacher_cannot_change_student_regular_bus_on_payment(self):
        """Teachers recording a one-off bus fare cannot alter the student's enrolled regular bus route."""
        # Create second bus route
        bus_b = Bus.objects.create(name='Madina Bus', fee=Decimal('60.00'))

        # Authorize teacher to record payments
        self.teacher_user.profile.can_collect_fees = True
        self.teacher_user.profile.save()

        # Student initially enrolled in Tema Bus (self.bus)
        self.assertEqual(self.student.bus, self.bus)

        self.client.force_login(self.teacher_user)
        post_data = {
            'student_class': self.student.school_class.id,
            'student': self.student.id,
            'bus': bus_b.id,
            'bus_amount': '60.00',
            'canteen_amount': '0.00',
            'payment_date': timezone.localdate().strftime('%Y-%m-%d'),
            'is_one_off_bus': 'True',
        }
        res = self.client.post(reverse('payment_create'), data=post_data, follow=True)
        self.assertEqual(res.status_code, 200)

        # 1. Student regular bus remains untouched!
        self.student.refresh_from_db()
        self.assertEqual(self.student.bus, self.bus, "Student regular bus must not be changed by teacher!")

        # 2. Payment record captures the one-off bus route and temporary flag
        payment = FeePayment.objects.filter(student=self.student, fee_type='BUS', amount=Decimal('60.00')).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.bus, bus_b)
        self.assertTrue(payment.is_temporary_bus)
        self.assertEqual(payment.recorded_by, self.teacher_user)

    def test_one_off_bus_payment_adjusts_bursar_target_dynamically(self):
        """When a teacher collects a one-off bus trip from a guest student, that amount is added to their bursar target."""
        bus_b = Bus.objects.create(name='Madina Bus', fee=Decimal('60.00'))

        # Teacher A is assigned to Class 1A (has self.student with self.bus fee 50)
        teacher_a = self.teacher_user
        teacher_a.profile.can_collect_fees = True
        teacher_a.profile.assigned_class = 'Class 1A'
        teacher_a.profile.save()

        # Teacher B is assigned to Class 2B
        teacher_b = User.objects.create_user('teacher_b', 'b@school.com', 'pass123', first_name='Kofi', last_name='Mensah')
        teacher_b.profile.role = UserProfile.ROLE_TEACHER
        teacher_b.profile.can_collect_fees = True
        teacher_b.profile.assigned_class = 'Class 2B'
        teacher_b.profile.assigned_bus = bus_b
        teacher_b.profile.save()

        # Teacher B records a one-off bus payment for self.student on bus_b
        FeePayment.objects.create(
            student=self.student,
            bus=bus_b,
            is_temporary_bus=True,
            fee_type='BUS',
            amount=Decimal('60.00'),
            receipt_number='REC-ONEOFF-01',
            recorded_by=teacher_b
        )

        self.client.force_login(self.bursar_user)

        # Bursar views staff roster:
        res = self.client.get(reverse('teacher_list'))
        self.assertEqual(res.status_code, 200)

        # Teacher B appears with remittance (+60.00) and one-off bus addition
        self.assertContains(res, 'teacher_b')
        self.assertContains(res, '60.00')
        self.assertContains(res, 'one-off bus')

        # Bursar views payments ledger:
        res_pay = self.client.get(reverse('payment_list'))
        self.assertEqual(res_pay.status_code, 200)
        self.assertContains(res_pay, 'REC-ONEOFF-01')
        self.assertContains(res_pay, 'One-Off Trip')


class BusPaymentAnalyticsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser('analytics_admin', 'admin@school.com', 'pass123')
        self.bursar_user = User.objects.create_user('analytics_bursar', 'bursar@school.com', 'pass123')
        self.bursar_user.profile.role = UserProfile.ROLE_ACCOUNTANT
        self.bursar_user.profile.save()

        self.teacher_user = User.objects.create_user('analytics_teacher', 'teacher@school.com', 'pass123')
        self.teacher_user.profile.role = UserProfile.ROLE_TEACHER
        self.teacher_user.profile.save()

        self.bus = Bus.objects.create(name='Tema Express', fee=Decimal('40.00'), driver_name='Kweku Boateng', plate_number='GT 1234-22')

        # 2 students in Class 1A, 1 student in Class 2B
        self.s1_1a = Student.objects.create(first_name='Kwame', last_name='Nkrumah', student_class='Class 1A', bus=self.bus)
        self.s2_1a = Student.objects.create(first_name='Akosua', last_name='Agyeman', student_class='Class 1A', bus=self.bus)
        self.s3_2b = Student.objects.create(first_name='Yaw', last_name='Manu', student_class='Class 2B', bus=self.bus)

    def test_bus_analytics_permissions(self):
        """Only users with financial authority (Principal, Bursar) can view bus payment analytics."""
        url = reverse('bus_analytics', args=[self.bus.pk])

        # Unauthenticated redirects to login
        res_anon = self.client.get(url)
        self.assertEqual(res_anon.status_code, 302)

        # Teacher denied
        self.client.force_login(self.teacher_user)
        res_teacher = self.client.get(url, follow=True)
        self.assertContains(res_teacher, 'Access denied: You do not have permission to access financial records')

        # Bursar allowed
        self.client.force_login(self.bursar_user)
        res_bursar = self.client.get(url)
        self.assertEqual(res_bursar.status_code, 200)

        # Principal allowed
        self.client.force_login(self.admin_user)
        res_admin = self.client.get(url)
        self.assertEqual(res_admin.status_code, 200)
        self.assertContains(res_admin, 'Tema Express — Payment Analytics')

    def test_class_by_class_breakdown_and_live_payment_updates(self):
        """Analytics calculates paid vs unpaid correctly class by class, and updates immediately when payment occurs."""
        self.client.force_login(self.admin_user)
        url = reverse('bus_analytics', args=[self.bus.pk])

        # Initially, 0 paid, 3 unpaid
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        summary = res.context['summary']
        self.assertEqual(summary['total_riders'], 3)
        self.assertEqual(summary['paid_count'], 0)
        self.assertEqual(summary['unpaid_count'], 3)
        self.assertEqual(summary['total_expected'], Decimal('120.00'))
        self.assertEqual(summary['total_collected'], Decimal('0.00'))

        class_breakdown = res.context['class_breakdown']
        self.assertEqual(len(class_breakdown), 2)
        c1a = next(c for c in class_breakdown if c['class_name'] == 'Class 1A')
        c2b = next(c for c in class_breakdown if c['class_name'] == 'Class 2B')

        self.assertEqual(c1a['riders_count'], 2)
        self.assertEqual(c1a['paid_count'], 0)
        self.assertEqual(c1a['unpaid_count'], 2)

        self.assertEqual(c2b['riders_count'], 1)
        self.assertEqual(c2b['paid_count'], 0)
        self.assertEqual(c2b['unpaid_count'], 1)

        # Now record payment for Kwame in Class 1A
        today = timezone.localdate()
        FeePayment.objects.create(
            student=self.s1_1a,
            fee_type='BUS',
            amount=Decimal('40.00'),
            payment_date=today,
            period='Term 1 - 2026',
            recorded_by=self.admin_user
        )

        # Re-fetch analytics: Kwame is now Paid, Class 1A has 1 Paid and 1 Unpaid
        res_after = self.client.get(url)
        summary_after = res_after.context['summary']
        self.assertEqual(summary_after['paid_count'], 1)
        self.assertEqual(summary_after['unpaid_count'], 2)
        self.assertEqual(summary_after['total_collected'], Decimal('40.00'))
        self.assertEqual(summary_after['total_outstanding'], Decimal('80.00'))

        class_breakdown_after = res_after.context['class_breakdown']
        c1a_after = next(c for c in class_breakdown_after if c['class_name'] == 'Class 1A')
        self.assertEqual(c1a_after['paid_count'], 1)
        self.assertEqual(c1a_after['unpaid_count'], 1)
        self.assertEqual(c1a_after['compliance_pct'], 50.0)

        # Check student details in Class 1A
        kwame = next(st for st in c1a_after['students'] if st['student'].id == self.s1_1a.id)
        akosua = next(st for st in c1a_after['students'] if st['student'].id == self.s2_1a.id)
        self.assertTrue(kwame['is_paid'])
        self.assertEqual(kwame['balance'], Decimal('0.00'))
        self.assertFalse(akosua['is_paid'])
        self.assertEqual(akosua['balance'], Decimal('40.00'))

    def test_bus_analytics_custom_date_filtering(self):
        """Analytics respects custom date query parameter."""
        self.client.force_login(self.admin_user)
        yesterday = timezone.localdate() - timezone.timedelta(days=1)

        # Payment made yesterday
        FeePayment.objects.create(
            student=self.s3_2b,
            fee_type='BUS',
            amount=Decimal('40.00'),
            payment_date=yesterday,
            period='Term 1 - 2026',
            recorded_by=self.admin_user
        )

        url = reverse('bus_analytics', args=[self.bus.pk])
        # Query yesterday's date
        res_yesterday = self.client.get(f"{url}?date={yesterday.strftime('%Y-%m-%d')}")
        self.assertEqual(res_yesterday.status_code, 200)
        self.assertEqual(res_yesterday.context['target_date'], yesterday.strftime('%Y-%m-%d'))
        self.assertEqual(res_yesterday.context['summary']['paid_count'], 1)

    def test_bus_analytics_unpaid_filter_and_class_dropdown_elements(self):
        """Analytics provides all_unpaid_students context and renders interactive dropdown/filter controls."""
        self.client.force_login(self.admin_user)
        url = reverse('bus_analytics', args=[self.bus.pk])

        # Initially, 3 riders are all unpaid
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        all_unpaid = res.context['all_unpaid_students']
        self.assertEqual(len(all_unpaid), 3)

        # Check DOM elements
        self.assertContains(res, 'id="classFilterSelect"')
        self.assertContains(res, 'value="__unpaid_all__"')
        self.assertContains(res, 'id="pill-filter-unpaid"')
        self.assertContains(res, 'id="unpaid-stat-card"')
        self.assertContains(res, 'id="unified-unpaid-section"')
        self.assertContains(res, 'data-status="unpaid"')

        # Pay for Kwame
        FeePayment.objects.create(
            student=self.s1_1a,
            fee_type='BUS',
            amount=Decimal('40.00'),
            payment_date=timezone.localdate(),
            period='Term 1 - 2026',
            recorded_by=self.admin_user
        )

        # Refetch: all_unpaid_students now has 2 students
        res2 = self.client.get(url)
        all_unpaid2 = res2.context['all_unpaid_students']
        self.assertEqual(len(all_unpaid2), 2)
        unpaid_ids = [item['student'].id for item in all_unpaid2]
        self.assertNotIn(self.s1_1a.id, unpaid_ids)
        self.assertIn(self.s2_1a.id, unpaid_ids)
        self.assertIn(self.s3_2b.id, unpaid_ids)


class StudentLunchProgramEnrollmentTests(TestCase):
    """Tests for automatic lunch program enrollment and 1-click toggle functionality."""

    def setUp(self):
        User = get_user_model()
        self.admin_user = User.objects.create_superuser(
            username='admin_lunch',
            email='admin_lunch@example.com',
            password='password123'
        )
        self.admin_user.profile.role = UserProfile.ROLE_ADMIN
        self.admin_user.profile.save()

        self.teacher_user = User.objects.create_user(
            username='teacher_lunch',
            email='teacher_lunch@example.com',
            password='password123'
        )
        self.teacher_user.profile.role = UserProfile.ROLE_TEACHER
        self.teacher_user.profile.save()

        self.student = Student.objects.create(
            first_name='Kofi',
            last_name='Boateng',
            student_class='Class 1A',
        )

    def test_default_lunch_enrollment(self):
        """Newly created students must automatically have lunch included by default."""
        self.assertTrue(self.student.canteen_enrolled)
        form = StudentForm()
        self.assertTrue(form.fields['canteen_enrolled'].initial)

    def test_toggle_student_lunch_permissions(self):
        """Only Principal/Admin can toggle student lunch enrollment."""
        url = reverse('student_toggle_lunch', args=[self.student.pk])

        # Anonymous user redirects to login
        res_anon = self.client.post(url)
        self.assertEqual(res_anon.status_code, 302)

        # Teacher is blocked
        self.client.force_login(self.teacher_user)
        res_teacher = self.client.post(url, follow=True)
        self.assertContains(res_teacher, 'Principal administrator privileges required')

        # Principal is allowed
        self.client.force_login(self.admin_user)
        res_admin = self.client.post(url)
        self.assertEqual(res_admin.status_code, 302)

    def test_toggle_student_lunch_ajax_and_state_inversion(self):
        """1-Click toggle inverts lunch enrollment state and returns JSON for AJAX."""
        self.client.force_login(self.admin_user)
        url = reverse('student_toggle_lunch', args=[self.student.pk])

        # Initially True
        self.assertTrue(self.student.canteen_enrolled)

        # Toggle OFF via AJAX
        res = self.client.post(
            url,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            content_type='application/json'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertFalse(data['canteen_enrolled'])
        self.assertIn('No Lunch', data['status_text'])

        self.student.refresh_from_db()
        self.assertFalse(self.student.canteen_enrolled)

        # Toggle back ON via AJAX
        res2 = self.client.post(
            url,
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            content_type='application/json'
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2['success'])
        self.assertTrue(data2['canteen_enrolled'])
        self.assertIn('Lunch Included', data2['status_text'])

        self.student.refresh_from_db()
        self.assertTrue(self.student.canteen_enrolled)

    def test_students_list_renders_toggle_button(self):
        """Students directory displays interactive 1-click lunch toggle pill."""
        self.client.force_login(self.admin_user)
        res = self.client.get(reverse('student_list'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'lunch-toggle-pill')
        self.assertContains(res, '🍽️ Lunch Included')







