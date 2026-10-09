import io
import json
import datetime
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from .models import Bus, Student, FeePayment, ClockInRecord, UserProfile, NotificationLog, SchoolClass
from .forms import StudentForm, TeacherCreationForm, TeacherUpdateForm
from .views import get_collectors_financial_summary


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

        # Verify ClockInRecord exists and notifications are omitted as requested
        record = ClockInRecord.objects.filter(student=self.student_tema).first()
        self.assertIsNotNone(record)
        notif = NotificationLog.objects.filter(student=self.student_tema, notification_type=NotificationLog.TYPE_CLOCK_IN).first()
        self.assertIsNone(notif)

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
        self.assertContains(res_dash, 'Clock In')
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


class FeeTeacherArchitectureTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser('admin_fee', 'admin_fee@test.com', 'pass123')
        
        # Fee teacher assigned to Bus A (no assigned classroom)
        self.bus_a = Bus.objects.create(name='Route Alpha', fee=Decimal('15.00'))
        self.fee_teacher = User.objects.create_user('fee_teacher_1', 'feeteacher@test.com', 'pass123')
        self.fee_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.fee_teacher.profile.can_collect_fees = True
        self.fee_teacher.profile.assigned_bus = self.bus_a
        self.fee_teacher.profile.assigned_class = ''
        self.fee_teacher.profile.save()

        # Classroom teacher assigned to Class 1A (no bus)
        self.class_teacher = User.objects.create_user('class_teacher_1', 'classteacher@test.com', 'pass123')
        self.class_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.class_teacher.profile.can_collect_fees = True
        self.class_teacher.profile.assigned_class = 'Class 1A'
        self.class_teacher.profile.save()

        # Student 1: Rides Bus A, in Class 1A, enrolled in canteen (10.00)
        self.s1 = Student.objects.create(
            first_name='Kweku',
            last_name='Baah',
            student_class='Class 1A',
            bus=self.bus_a,
            canteen_enrolled=True,
            custom_canteen_fee=Decimal('10.00'),
            is_active=True
        )

        # Student 2: Rides Bus A, in Class 2B, not enrolled in canteen
        self.s2 = Student.objects.create(
            first_name='Akosua',
            last_name='Mansah',
            student_class='Class 2B',
            bus=self.bus_a,
            canteen_enrolled=False,
            is_active=True
        )

        # Student 3: Walker (no bus), in Class 1A, enrolled in canteen (custom 8.00)
        self.s3_walker = Student.objects.create(
            first_name='Yaw',
            last_name='Osei',
            student_class='Class 1A',
            bus=None,
            canteen_enrolled=True,
            custom_canteen_fee=Decimal('8.00'),
            is_active=True
        )

    def test_fee_teacher_target_combines_bus_and_canteen_without_classroom(self):
        """Fee Teacher's target includes both bus and canteen fees for all students riding their bus."""
        today = timezone.localdate()
        summary = get_collectors_financial_summary([self.fee_teacher, self.class_teacher], today)
        
        ft_meta = summary[self.fee_teacher.id]
        # Student 1: Bus 15 + Canteen 10 = 25
        # Student 2: Bus 15 + Canteen 0 = 15
        # Fee teacher total = 40.00
        self.assertEqual(ft_meta['bus_expected'], Decimal('30.00'))
        self.assertEqual(ft_meta['canteen_expected'], Decimal('10.00'))
        self.assertEqual(ft_meta['baseline_target'], Decimal('40.00'))
        self.assertEqual(ft_meta['student_count'], 2)

        # Class teacher target should NOT double count Student 1's canteen fee!
        # Only Walker Student 3 (Canteen 8.00) belongs to classroom teacher's morning target.
        ct_meta = summary[self.class_teacher.id]
        self.assertEqual(ct_meta['canteen_expected'], Decimal('8.00'))
        self.assertEqual(ct_meta['bus_expected'], Decimal('0.00'))
        self.assertEqual(ct_meta['baseline_target'], Decimal('8.00'))
        self.assertEqual(ct_meta['student_count'], 1)

    def test_fee_teacher_dashboard_redirects_to_bus_analytics(self):
        """Fee Teacher with assigned bus and no classroom visiting / redirects directly to bus_analytics."""
        self.client.force_login(self.fee_teacher)
        res = self.client.get(reverse('dashboard'))
        self.assertEqual(res.status_code, 302)
        self.assertRedirects(res, reverse('bus_analytics', args=[self.bus_a.pk]))

    def test_collect_both_fees_api_atomic_collection(self):
        """1-tap API collects both Bus Fare and Canteen Fee atomically."""
        self.client.force_login(self.fee_teacher)
        url = reverse('api_collect_both_fees')
        
        res = self.client.post(
            url,
            json.dumps({'student_id': self.s1.id, 'fee_type': 'BOTH'}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['bus_collected'])
        self.assertTrue(data['canteen_collected'])
        self.assertEqual(data['bus_amount'], 15.0)
        self.assertEqual(data['canteen_amount'], 10.0)
        self.assertEqual(data['total_amount'], 25.0)

        # Verify DB records created
        today = timezone.localdate()
        bus_p = FeePayment.objects.filter(student=self.s1, fee_type='BUS', payment_date=today).first()
        can_p = FeePayment.objects.filter(student=self.s1, fee_type='CANTEEN', payment_date=today).first()
        self.assertIsNotNone(bus_p)
        self.assertIsNotNone(can_p)
        self.assertEqual(bus_p.amount, Decimal('15.00'))
        self.assertEqual(can_p.amount, Decimal('10.00'))
        self.assertEqual(bus_p.bus, self.bus_a)
        self.assertEqual(bus_p.recorded_by, self.fee_teacher)

    def test_collect_both_fees_api_idempotent(self):
        """Attempting to re-collect fees that are already paid returns 0.0 with status message."""
        self.client.force_login(self.fee_teacher)
        url = reverse('api_collect_both_fees')
        
        # First collection
        self.client.post(url, json.dumps({'student_id': self.s1.id}), content_type='application/json')

        # Re-collection attempt
        res2 = self.client.post(url, json.dumps({'student_id': self.s1.id}), content_type='application/json')
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2['success'])
        self.assertFalse(data2['bus_collected'])
        self.assertFalse(data2['canteen_collected'])
        self.assertEqual(data2['total_amount'], 0.0)


class AdvancePaymentAndCreditDeductionTests(TestCase):
    """
    Test suite for Student Advance Payments, Multi-Day Weekly Prepayments,
    Day-by-Day Credit Depletion, and Bursar Cash Remittance Reconciliation.
    """

    def setUp(self):
        self.principal = User.objects.create_user(username='principal_adv', password='password123')
        self.principal.profile.role = UserProfile.ROLE_ADMIN
        self.principal.profile.save()

        self.accountant = User.objects.create_user(username='accountant_adv', password='password123')
        self.accountant.profile.role = UserProfile.ROLE_ACCOUNTANT
        self.accountant.profile.save()

        self.bus = Bus.objects.create(name='Madina Bus', fee=Decimal('10.00'))

        self.fee_teacher = User.objects.create_user(username='teacher_kwasi', password='password123', first_name='Kwasi')
        self.fee_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.fee_teacher.profile.can_collect_fees = True
        self.fee_teacher.profile.assigned_bus = self.bus
        self.fee_teacher.profile.save()

        self.student = Student.objects.create(
            first_name='Abena',
            last_name='Osei',
            student_class='Class 3',
            bus=self.bus,
            canteen_enrolled=True,
            custom_canteen_fee=Decimal('8.00'),
            is_active=True
        )

    def test_weekly_advance_payment_recording_and_credit_days(self):
        """Advance payment for 5 school days accurately creates credit balance and credit days."""
        monday = datetime.date(2026, 10, 5)  # Monday
        
        # 5 days: Bus = GH₵50.00, Canteen = GH₵40.00
        p_bus = FeePayment.objects.create(
            student=self.student,
            fee_type='BUS',
            amount=Decimal('50.00'),
            payment_date=monday,
            is_advance=True,
            days_covered=5,
            bus=self.bus,
            recorded_by=self.fee_teacher
        )
        p_can = FeePayment.objects.create(
            student=self.student,
            fee_type='CANTEEN',
            amount=Decimal('40.00'),
            payment_date=monday,
            is_advance=True,
            days_covered=5,
            recorded_by=self.fee_teacher
        )

        # On Monday (payment day): covers today, leaves 4 prepaid days
        self.assertTrue(self.student.is_bus_paid_for_date(monday))
        self.assertTrue(self.student.is_canteen_paid_for_date(monday))
        self.assertEqual(self.student.get_bus_credit_days(monday), 4)
        self.assertEqual(self.student.get_canteen_credit_days(monday), 4)

        status = self.student.get_prepaid_status(monday)
        self.assertTrue(status['has_credit'])
        self.assertTrue(status['bus_has_credit'])
        self.assertEqual(status['bus_credit_days'], 4)
        self.assertTrue(status['canteen_has_credit'])
        self.assertEqual(status['canteen_credit_days'], 4)

    def test_day_by_day_credit_depletion(self):
        """Credit depletes school day by school day until fully exhausted."""
        monday = datetime.date(2026, 10, 5)
        tuesday = datetime.date(2026, 10, 6)
        wednesday = datetime.date(2026, 10, 7)
        thursday = datetime.date(2026, 10, 8)
        friday = datetime.date(2026, 10, 9)
        next_monday = datetime.date(2026, 10, 12)

        # Record 5 days advance on Monday
        FeePayment.objects.create(
            student=self.student,
            fee_type='BUS',
            amount=Decimal('50.00'),
            payment_date=monday,
            is_advance=True,
            days_covered=5,
            bus=self.bus
        )

        # Tuesday (1 day consumed): 3 days remaining
        self.assertTrue(self.student.is_bus_paid_for_date(tuesday))
        self.assertEqual(self.student.get_bus_credit_days(tuesday), 3)

        # Wednesday (2 days consumed): 2 days remaining
        self.assertTrue(self.student.is_bus_paid_for_date(wednesday))
        self.assertEqual(self.student.get_bus_credit_days(wednesday), 2)

        # Thursday (3 days consumed): 1 day remaining
        self.assertTrue(self.student.is_bus_paid_for_date(thursday))
        self.assertEqual(self.student.get_bus_credit_days(thursday), 1)

        # Friday (4 days consumed): 0 days remaining after Friday
        self.assertTrue(self.student.is_bus_paid_for_date(friday))
        self.assertEqual(self.student.get_bus_credit_days(friday), 0)

        # Next Monday (5 school days consumed): Credit is 0 -> Student owes fee again!
        self.assertFalse(self.student.is_bus_paid_for_date(next_monday))
        self.assertEqual(self.student.get_bus_credit_days(next_monday), 0)
        self.assertEqual(self.student.get_daily_bus_balance(next_monday), Decimal('10.00'))

    def test_bursar_cash_reconciliation_no_phantom_deficit(self):
        """
        On Monday: Teacher collects full advance cash -> target expands to match cash.
        On Tuesday: Fee is settled via credit -> target contracts so teacher has 0 shortage.
        """
        monday = datetime.date(2026, 10, 5)
        tuesday = datetime.date(2026, 10, 6)

        # Monday collection: Student pays 5 days advance (GH₵50 Bus + GH₵40 Lunch = GH₵90)
        FeePayment.objects.create(
            student=self.student,
            fee_type='BUS',
            amount=Decimal('50.00'),
            payment_date=monday,
            is_advance=True,
            days_covered=5,
            bus=self.bus,
            recorded_by=self.fee_teacher
        )
        FeePayment.objects.create(
            student=self.student,
            fee_type='CANTEEN',
            amount=Decimal('40.00'),
            payment_date=monday,
            is_advance=True,
            days_covered=5,
            recorded_by=self.fee_teacher
        )

        # Check Monday summary for Kwasi
        monday_summary = get_collectors_financial_summary([self.fee_teacher], target_date=monday)
        kwasi_mon = monday_summary[self.fee_teacher.id]
        self.assertEqual(kwasi_mon['today_collected'], Decimal('90.00'))
        # Baseline = 10 + 8 = 18; Advance collected = 40 + 32 = 72; Adjusted target = 18 + 72 = 90
        self.assertEqual(kwasi_mon['adjusted_target'], Decimal('90.00'))
        self.assertEqual(kwasi_mon['balance'], Decimal('0.00'))

        # Check Tuesday summary for Kwasi: No payment collected on Tuesday
        tuesday_summary = get_collectors_financial_summary([self.fee_teacher], target_date=tuesday)
        kwasi_tue = tuesday_summary[self.fee_teacher.id]
        self.assertEqual(kwasi_tue['today_collected'], Decimal('0.00'))
        # Student is covered by prepaid credit -> prepaid_deductions = 18.00
        # Adjusted target = baseline (18.00) - prepaid_deductions (18.00) = 0.00!
        self.assertEqual(kwasi_tue['prepaid_deductions'], Decimal('18.00'))
        self.assertEqual(kwasi_tue['adjusted_target'], Decimal('0.00'))
        self.assertEqual(kwasi_tue['balance'], Decimal('0.00'))  # NO FALSE DEFICIT!

    def test_collect_both_fees_api_multi_day_advance(self):
        """API accepts days=5 and records both fees with is_advance=True and days_covered=5."""
        self.client.force_login(self.fee_teacher)
        url = reverse('api_collect_both_fees')
        
        res = self.client.post(
            url,
            json.dumps({'student_id': self.student.id, 'fee_type': 'BOTH', 'days': 5}),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['bus_amount'], 50.0)    # 10 * 5
        self.assertEqual(data['canteen_amount'], 40.0) # 8 * 5
        self.assertEqual(data['total_amount'], 90.0)

        today = timezone.localdate()
        p_bus = FeePayment.objects.filter(student=self.student, fee_type='BUS', payment_date=today).first()
        p_can = FeePayment.objects.filter(student=self.student, fee_type='CANTEEN', payment_date=today).first()
        self.assertTrue(p_bus.is_advance)
        self.assertEqual(p_bus.days_covered, 5)
        self.assertTrue(p_can.is_advance)
        self.assertEqual(p_can.days_covered, 5)


class FeeTeacherRecordPaymentScopingTests(TestCase):
    def setUp(self):
        self.bus_1 = Bus.objects.create(name='Bus 1 - Morning Route', fee=Decimal('10.00'))
        self.bus_2 = Bus.objects.create(name='Bus 2 - Afternoon Route', fee=Decimal('15.00'))

        self.class_1a = SchoolClass.objects.create(name='Class 1A')
        self.class_2b = SchoolClass.objects.create(name='Class 2B')

        # Rider on Bus 1 in Class 1A
        self.student_bus1_1a = Student.objects.create(
            first_name='Kofi', last_name='Bus1',
            school_class=self.class_1a, student_class='Class 1A',
            bus=self.bus_1
        )
        # Rider on Bus 1 in Class 2B
        self.student_bus1_2b = Student.objects.create(
            first_name='Ama', last_name='Bus1',
            school_class=self.class_2b, student_class='Class 2B',
            bus=self.bus_1
        )
        # Rider on Bus 2 in Class 1A
        self.student_bus2 = Student.objects.create(
            first_name='Kwame', last_name='Bus2',
            school_class=self.class_1a, student_class='Class 1A',
            bus=self.bus_2
        )
        # Walker in Class 1A
        self.student_walker = Student.objects.create(
            first_name='Akosua', last_name='Walker',
            school_class=self.class_1a, student_class='Class 1A',
            bus=None
        )

        # Fee teacher assigned to Bus 1
        self.fee_teacher = User.objects.create_user(username='fee_teacher_bus1', password='pass123')
        self.fee_teacher.profile.role = UserProfile.ROLE_TEACHER
        self.fee_teacher.profile.can_collect_fees = True
        self.fee_teacher.profile.assigned_bus = self.bus_1
        self.fee_teacher.profile.save()

        # Admin user
        self.admin_user = User.objects.create_user(username='school_admin', password='pass123')
        self.admin_user.profile.role = UserProfile.ROLE_ADMIN
        self.admin_user.profile.save()

    def test_fee_teacher_scoped_dropdowns_on_payment_create(self):
        """Fee Teacher filters by actual class and scoped students only contain their bus riders in that class."""
        self.client.force_login(self.fee_teacher)
        res = self.client.get(reverse('payment_create'))
        self.assertEqual(res.status_code, 200)

        ctx = res.context
        self.assertTrue(ctx['is_scoped_fee_teacher'])
        self.assertEqual(ctx['assigned_bus'], self.bus_1)
        self.assertEqual(ctx['scoped_rider_count'], 2)
        self.assertEqual(ctx['initial_class_id'], '')

        scoped_data = json.loads(ctx['scoped_students_by_class_json'])
        # Virtual __ALL_BUS__ is NOT in scoped_data
        self.assertNotIn('__ALL_BUS__', scoped_data)

        # In class 1A scoped list: only student_bus1_1a is present
        class1a_key = str(self.class_1a.id)
        class1a_riders = [s['id'] for s in scoped_data[class1a_key]]
        self.assertEqual(len(class1a_riders), 1)
        self.assertEqual(class1a_riders[0], self.student_bus1_1a.id)
        self.assertNotIn(self.student_bus2.id, class1a_riders)
        self.assertNotIn(self.student_walker.id, class1a_riders)

        # In class 2B scoped list: only student_bus1_2b is present
        class2b_key = str(self.class_2b.id)
        class2b_riders = [s['id'] for s in scoped_data[class2b_key]]
        self.assertEqual(len(class2b_riders), 1)
        self.assertEqual(class2b_riders[0], self.student_bus1_2b.id)

    def test_guest_rider_mode_payment_recording(self):
        """Fee teacher can record payment for a student from another bus without altering student permanent bus."""
        self.client.force_login(self.fee_teacher)
        today = timezone.localdate()

        post_data = {
            'student_class': str(self.class_1a.id),
            'student': self.student_bus2.id,
            'bus': self.bus_1.id,
            'is_one_off_bus': True,
            'bus_amount': '10.00',
            'canteen_amount': '0.00',
            'payment_date': today.strftime('%Y-%m-%d'),
        }

        res = self.client.post(reverse('payment_create'), post_data)
        self.assertEqual(res.status_code, 302)

        payment = FeePayment.objects.filter(student=self.student_bus2, fee_type='BUS', payment_date=today).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal('10.00'))
        self.assertEqual(payment.bus, self.bus_1)
        self.assertTrue(payment.is_temporary_bus)
        self.assertIn('Guest ride on Bus 1 - Morning Route', payment.notes)

        # Student's permanent bus registration MUST BE UNTOUCHED!
        self.student_bus2.refresh_from_db()
        self.assertEqual(self.student_bus2.bus, self.bus_2)

    def test_admin_retains_unscoped_access(self):
        """Admin user sees full school scope without fee teacher restrictions."""
        self.client.force_login(self.admin_user)
        res = self.client.get(reverse('payment_create'))
        self.assertEqual(res.status_code, 200)

        ctx = res.context
        self.assertFalse(ctx['is_scoped_fee_teacher'])
        self.assertIsNone(ctx['assigned_bus'])


class PartialCreditTopUpTests(TestCase):
    """
    Test suite for partial prepaid balance rollover and remainder top-up collection:
    1. Student with partial credit (< daily fare) is detected with bus_has_partial=True and remainder_due.
    2. Fee Teacher 1-tap API collects only the exact remainder without double-charging.
    3. Accountant reconciliation adjusts teacher target down by prepaid credit, resulting in zero deficit.
    4. Bus analytics roster presents credit details and top-up actions.
    """

    def setUp(self):
        self.bus_route = Bus.objects.create(name='Express Route', fee=Decimal('10.00'))
        self.school_class = SchoolClass.objects.create(name='Grade 4')

        self.student = Student.objects.create(
            first_name='Kofi',
            last_name='Mensah',
            student_class='Grade 4',
            school_class=self.school_class,
            bus=self.bus_route,
            canteen_enrolled=False
        )

        self.teacher_user = User.objects.create_user(username='fee_teacher_kofi', password='password123')
        self.teacher_user.profile.role = UserProfile.ROLE_TEACHER
        self.teacher_user.profile.can_collect_fees = True
        self.teacher_user.profile.assigned_bus = self.bus_route
        self.teacher_user.profile.save()

        # Simulate advance payment made yesterday of GH₵25.00 on a GH₵10/day bus
        # Attended yesterday (cost = 10.00), leaving GH₵15.00 credit
        # If student took a different GH₵10 guest bus or had GH₵15 paid for 2 days at GH₵10:
        # Let's directly simulate student having GH₵5.00 remaining credit on target_date
        self.target_date = timezone.localdate()
        self.yesterday = self.target_date - datetime.timedelta(days=1)

        # Yesterday student paid GH₵15.00 and attended yesterday:
        # Past cost = 1 day * GH₵10.00 = GH₵10.00
        # Available credit today before paying = GH₵15.00 - GH₵10.00 = GH₵5.00!
        FeePayment.objects.create(
            student=self.student,
            fee_type='BUS',
            amount=Decimal('15.00'),
            payment_date=self.yesterday,
            is_advance=True,
            days_covered=1,
            bus=self.bus_route,
            recorded_by=self.teacher_user
        )
        ClockInRecord.objects.create(
            student=self.student,
            date=self.yesterday,
            recorded_by=self.teacher_user
        )

    def test_partial_credit_detection_and_remainder_due(self):
        """Student with GH₵5 credit on GH₵10 daily route is detected with partial credit and GH₵5 remainder due."""
        credit_bal = self.student.get_bus_credit_balance(self.target_date)
        self.assertEqual(credit_bal, Decimal('5.00'))

        self.assertFalse(self.student.is_bus_paid_for_date(self.target_date))
        self.assertEqual(self.student.get_daily_bus_balance(self.target_date), Decimal('5.00'))

        status = self.student.get_prepaid_status(self.target_date)
        self.assertTrue(status['bus_has_partial'])
        self.assertEqual(status['bus_credit_balance'], Decimal('5.00'))
        self.assertEqual(status['bus_remainder_due'], Decimal('5.00'))

    def test_collect_both_fees_api_collects_exact_remainder(self):
        """1-tap fee collection API charges only the exact remainder (GH₵5.00), not the full GH₵10 fare."""
        self.client.force_login(self.teacher_user)

        res = self.client.post(
            reverse('api_collect_both_fees'),
            data=json.dumps({
                'student_id': self.student.id,
                'fee_type': 'BUS',
                'days': 1,
                'date': self.target_date.strftime('%Y-%m-%d')
            }),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertTrue(data['bus_collected'])
        self.assertEqual(data['bus_amount'], 5.0)  # Charged only GH₵5.00!
        self.assertEqual(data['total_amount'], 5.0)

        # Verify payment record in database
        today_payment = FeePayment.objects.filter(
            student=self.student,
            fee_type='BUS',
            payment_date=self.target_date
        ).first()
        self.assertIsNotNone(today_payment)
        self.assertEqual(today_payment.amount, Decimal('5.00'))
        self.assertIn('remainder top-up', today_payment.notes.lower())

        # Student is now fully paid for today
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_bus_paid_for_date(self.target_date))
        self.assertEqual(self.student.get_daily_bus_balance(self.target_date), Decimal('0.00'))

        status = self.student.get_prepaid_status(self.target_date)
        self.assertFalse(status['bus_has_partial'])

    def test_accountant_reconciliation_zero_deficit_on_topup(self):
        """Accountant audit reconciliation credits GH₵5 non-cash from past credit, resulting in GH₵0 balance for teacher."""
        # Fee teacher collects the GH₵5.00 top-up today
        self.client.force_login(self.teacher_user)
        self.client.post(
            reverse('api_collect_both_fees'),
            data=json.dumps({
                'student_id': self.student.id,
                'fee_type': 'BUS',
                'days': 1,
                'date': self.target_date.strftime('%Y-%m-%d')
            }),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        # Run financial summary for accountant audit
        summary = get_collectors_financial_summary([self.teacher_user], self.target_date)
        meta = summary[self.teacher_user.id]

        self.assertEqual(meta['bus_expected'], Decimal('10.00'))
        self.assertEqual(meta['prepaid_deductions'], Decimal('5.00'))  # GH₵5 non-cash credit from past
        self.assertEqual(meta['today_collected'], Decimal('5.00'))     # GH₵5 physical cash collected today
        self.assertEqual(meta['adjusted_target'], Decimal('5.00'))     # Cash expected to hand over = GH₵5
        self.assertEqual(meta['balance'], Decimal('0.00'))             # ZERO DEFICIT / BALANCED!

    def test_bus_analytics_view_displays_partial_credit(self):
        """Bus analytics page renders partial credit information and remainder due."""
        self.client.force_login(self.teacher_user)
        res = self.client.get(reverse('bus_analytics', kwargs={'pk': self.bus_route.pk}))
        self.assertEqual(res.status_code, 200)

        # Verify class breakdown in context
        c_students = res.context['class_breakdown'][0]['students']
        item = [s for s in c_students if s['student'].id == self.student.id][0]
        self.assertTrue(item['has_partial_credit'])
        self.assertEqual(item['credit_available'], Decimal('5.00'))
        self.assertEqual(item['remainder_due'], Decimal('5.00'))
        self.assertEqual(item['combined_due'], Decimal('5.00'))

    def test_collect_both_fees_api_custom_multipay_bus(self):
        """Fee teacher can enter custom multi-pay amount (e.g. GH₵70 for 7 days) instead of fixed days."""
        # Clean existing payments for today
        FeePayment.objects.filter(student=self.student, payment_date=self.target_date).delete()

        self.client.force_login(self.teacher_user)
        res = self.client.post(
            reverse('api_collect_both_fees'),
            data=json.dumps({
                'student_id': self.student.id,
                'fee_type': 'BUS',
                'amount': 70.00,
                'method': 'CASH',
                'date': self.target_date.strftime('%Y-%m-%d')
            }),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        payment = FeePayment.objects.filter(
            student=self.student, fee_type='BUS', payment_date=self.target_date
        ).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal('70.00'))
        self.assertTrue(payment.is_advance)
        self.assertEqual(payment.days_covered, 7)
        self.assertIn("~7 days credit", payment.notes)

    def test_collect_both_fees_api_custom_multipay_split_both(self):
        """Fee teacher custom multi-pay splits proportionately between bus and canteen."""
        # Enable canteen enrollment on student: Bus is GH₵10, Canteen is GH₵5 (ratio 2:1)
        self.student.canteen_enrolled = True
        self.student.custom_canteen_fee = Decimal('5.00')
        self.student.save()
        FeePayment.objects.filter(student=self.student, payment_date=self.target_date).delete()

        self.client.force_login(self.teacher_user)
        res = self.client.post(
            reverse('api_collect_both_fees'),
            data=json.dumps({
                'student_id': self.student.id,
                'fee_type': 'BOTH',
                'amount': 75.00,
                'method': 'MOMO',
                'date': self.target_date.strftime('%Y-%m-%d')
            }),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])

        bus_p = FeePayment.objects.filter(student=self.student, fee_type='BUS', payment_date=self.target_date).first()
        canteen_p = FeePayment.objects.filter(student=self.student, fee_type='CANTEEN', payment_date=self.target_date).first()

        self.assertIsNotNone(bus_p)
        self.assertIsNotNone(canteen_p)
        self.assertEqual(bus_p.amount, Decimal('50.00'))
        self.assertEqual(bus_p.days_covered, 5)
        self.assertTrue(bus_p.is_advance)
        self.assertEqual(canteen_p.amount, Decimal('25.00'))
        self.assertEqual(canteen_p.days_covered, 5)
        self.assertTrue(canteen_p.is_advance)
        self.assertEqual(bus_p.payment_method, 'MOMO')

    def test_bus_analytics_contains_multipay_modal(self):
        """Bus analytics template contains the custom amount Multi-Pay modal."""
        self.client.force_login(self.teacher_user)
        res = self.client.get(reverse('bus_analytics', kwargs={'pk': self.bus_route.pk}))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'multipay-modal-backdrop')
        self.assertContains(res, 'openMultipayModal')
        self.assertContains(res, 'modal-mp-amount')

    def test_fee_teacher_navbar_links_to_assigned_bus_roster(self):
        """Fee Teacher sees 'Bus Roster' linking directly to their assigned bus route analytics."""
        self.client.force_login(self.teacher_user)
        res = self.client.get(reverse('bus_analytics', kwargs={'pk': self.bus_route.pk}))
        self.assertEqual(res.status_code, 200)
        expected_url = reverse('bus_analytics', kwargs={'pk': self.bus_route.pk})
        self.assertContains(res, f'href="{expected_url}"')
        self.assertContains(res, 'Bus Roster')

    def test_fee_teacher_dashboard_auto_scopes_to_assigned_bus_students(self):
        """Fee Teacher accessing the dashboard is auto-scoped to their assigned bus riders."""
        other_bus = Bus.objects.create(name='Other Bus Route', fee=Decimal('8.00'))
        Student.objects.create(
            first_name='Ama', last_name='Other', student_class='Grade 4',
            school_class=self.school_class, bus=other_bus
        )
        Student.objects.create(
            first_name='Kwame', last_name='Walker', student_class='Grade 4',
            school_class=self.school_class, bus=None
        )

        self.client.force_login(self.teacher_user)
        self.teacher_user.profile.assigned_class = 'Grade 4'
        self.teacher_user.profile.save()

        res = self.client.get(reverse('dashboard'))
        self.assertEqual(res.status_code, 200)
        items = res.context['student_items']
        item_student_ids = [item['student'].id for item in items]
        self.assertIn(self.student.id, item_student_ids)
        for item in items:
            self.assertEqual(item['student'].bus_id, self.bus_route.id)

    def test_fee_teacher_teachers_page_contains_bus_roster_link(self):
        """Teachers directory and remittance page contains direct Bus Roster button for Fee Teachers."""
        principal = User.objects.create_user(username='test_principal_user', password='password123')
        principal.profile.role = UserProfile.ROLE_ADMIN
        principal.profile.save()
        self.client.force_login(principal)
        res = self.client.get(reverse('teacher_list'))
        self.assertEqual(res.status_code, 200)
        expected_url = reverse('bus_analytics', kwargs={'pk': self.bus_route.pk})
        self.assertContains(res, expected_url)
        self.assertContains(res, 'Bus Roster')


class PrincipalAdminManagementTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.principal = User.objects.create_user(
            username='main_principal',
            password='password123',
            is_staff=True,
            is_superuser=True
        )
        self.principal.profile.role = UserProfile.ROLE_ADMIN
        self.principal.profile.save()

        self.teacher = User.objects.create_user(
            username='regular_teacher',
            password='password123',
            is_staff=True
        )
        self.teacher.profile.role = UserProfile.ROLE_TEACHER
        self.teacher.profile.save()

    def test_teacher_creation_as_admin(self):
        """Creating an account with role=ADMIN sets is_superuser and is_principal."""
        form_data = {
            'username': 'new_vice_principal',
            'first_name': 'Vice',
            'last_name': 'Principal',
            'email': 'vp@school.edu',
            'phone_number': '0241234567',
            'password': 'Password123!',
            'confirm_password': 'Password123!',
            'is_active': True,
            'role': UserProfile.ROLE_ADMIN,
            'can_collect_fees': True,
            'assigned_class': '',
        }
        form = TeacherCreationForm(data=form_data)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.profile.role, UserProfile.ROLE_ADMIN)
        self.assertTrue(user.profile.is_principal)

    def test_promote_teacher_to_admin_via_update_form(self):
        """Updating a teacher's role to ADMIN promotes them with is_superuser and is_principal."""
        form_data = {
            'first_name': 'Regular',
            'last_name': 'Teacher',
            'email': 'teacher@school.edu',
            'phone_number': '0249876543',
            'role': UserProfile.ROLE_ADMIN,
            'is_active': True,
            'can_collect_fees': False,
            'assigned_class': '',
        }
        form = TeacherUpdateForm(data=form_data, user_obj=self.teacher)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.profile.role, UserProfile.ROLE_ADMIN)
        self.assertTrue(user.profile.is_principal)

    def test_promoted_principal_can_access_principal_required_views(self):
        """A user promoted to Admin can successfully access views protected by @principal_required."""
        # Before promotion, teacher gets redirected from principal-required view
        self.client.force_login(self.teacher)
        response_before = self.client.get(reverse('teacher_create'))
        self.assertEqual(response_before.status_code, 302)

        # Promote teacher
        self.teacher.is_superuser = True
        self.teacher.profile.role = UserProfile.ROLE_ADMIN
        self.teacher.profile.save()
        self.teacher.save()

        # After promotion, succeeds
        response_after = self.client.get(reverse('teacher_create'))
        self.assertEqual(response_after.status_code, 200)

    def test_superuser_syncs_with_role_admin_signal(self):
        """Saving a user as superuser ensures profile.role is synced to ROLE_ADMIN."""
        staff_user = User.objects.create_user(username='staff_member', password='password123')
        self.assertEqual(staff_user.profile.role, UserProfile.ROLE_TEACHER)

        staff_user.is_superuser = True
        staff_user.save()
        staff_user.profile.refresh_from_db()
        self.assertEqual(staff_user.profile.role, UserProfile.ROLE_ADMIN)
        self.assertTrue(staff_user.profile.is_principal)

    def test_class_management_principal_and_uniformity(self):
        """Principal can create, update, and manage school classes; teachers cannot."""
        # Teacher is blocked from creating classes
        self.client.force_login(self.teacher)
        res = self.client.get(reverse('class_create'))
        self.assertEqual(res.status_code, 302)

        # Principal can create class
        self.client.force_login(self.principal)
        res_post = self.client.post(reverse('class_create'), {
            'name': 'Grade 4 Elite',
            'grade_level': 'Primary',
            'is_active': True,
        })
        self.assertEqual(res_post.status_code, 302)
        sc = SchoolClass.objects.filter(name='Grade 4 Elite').first()
        self.assertIsNotNone(sc)
        self.assertEqual(sc.grade_level, 'Primary')

        # Form dropdown includes the new class
        form = StudentForm()
        class_choices = [c[0] for c in form.fields['student_class'].widget.choices]
        self.assertIn('Grade 4 Elite', class_choices)

    def test_student_onboarding_toggle_and_teacher_registration(self):
        """Principal toggles teacher registration ON/OFF; teachers can only register when ON."""
        from .models import SchoolSetting

        # Create a standardized class
        sc = SchoolClass.objects.create(name='Basic 5', grade_level='Primary', is_active=True)

        # Ensure setting is OFF initially
        SchoolSetting.objects.filter(key='ALLOW_TEACHER_STUDENT_REGISTRATION').delete()

        # Teacher cannot access student enrollment
        self.client.force_login(self.teacher)
        res_teacher = self.client.get(reverse('student_create'))
        self.assertEqual(res_teacher.status_code, 302)

        # Principal toggles onboarding mode ON
        self.client.force_login(self.principal)
        res_toggle_on = self.client.post(reverse('toggle_onboarding_registration'))
        self.assertEqual(res_toggle_on.status_code, 302)
        self.assertEqual(SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION', 'False'), 'True')

        # Now teacher can access student creation form
        self.client.force_login(self.teacher)
        res_teacher_access = self.client.get(reverse('student_create'))
        self.assertEqual(res_teacher_access.status_code, 200)

        # Teacher enrolls a student
        res_enroll = self.client.post(reverse('student_create'), {
            'first_name': 'Kweku',
            'last_name': 'Baah',
            'student_class': 'Basic 5',
            'canteen_enrolled': True,
            'is_active': True,
        })
        self.assertEqual(res_enroll.status_code, 302)
        new_student = Student.objects.filter(first_name='Kweku', last_name='Baah').first()
        self.assertIsNotNone(new_student)
        self.assertEqual(new_student.student_class, 'Basic 5')
        self.assertEqual(new_student.school_class, sc)

        # Principal toggles onboarding mode OFF
        self.client.force_login(self.principal)
        res_toggle_off = self.client.post(reverse('toggle_onboarding_registration'))
        self.assertEqual(res_toggle_off.status_code, 302)
        self.assertEqual(SchoolSetting.get_setting('ALLOW_TEACHER_STUDENT_REGISTRATION', 'False'), 'False')

        # Teacher is blocked again
        self.client.force_login(self.teacher)
        res_teacher_blocked = self.client.get(reverse('student_create'))
        self.assertEqual(res_teacher_blocked.status_code, 302)
