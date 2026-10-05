# School Clock-In & Dynamic Fee Tracking System

A purpose-built Django web application for schools that streamlines morning student arrival attendance with real-time dynamic bus and canteen fee verification.

---

## 🌟 Key Features

### 1. Dynamic Bus Route Pricing
- Configure buses with distinct fees set by the administration (e.g. **Tema Bus** at `$80.00`, **Madina Bus** at `$50.00`, **Spintex Bus** at `$70.00`, **Adenta Bus** at `$60.00`).
- Each bus has its own driver info, phone number, vehicle registration plate, and pickup route description.
- Students assigned to a bus automatically inherit that bus's fee requirement.
- Walkers / private drop-off students are automatically charged `$0.00` bus fee.

### 2. Teacher Daily Clock-In Dashboard (`/`)
- **1-Click Morning Arrival Clock-In**: Instant arrival timestamping via AJAX (zero full-page reload) with smooth checkmark animations and instant undo capability.
- **Color-Coded Fee Verification Badges**:
  - 🚌 **Bus Fee**: `🟢 Paid ($80.00)` vs `🔴 Owes $80.00` vs `⚪ No Bus`
  - 🍽️ **Canteen Fee**: `🟢 Paid ($45.00)` vs `🔴 Owes $45.00` vs `⚪ Opted Out`
- **Dynamic Counters**: Real-time stats on Total Students, Clocked In Today (Count & % Present), Bus Riders Paid %, and Canteen Fee Collection %.
- **Multi-Filter Toolbar**: Filter dynamically by Date, Student Name Search, Class, Bus Route, Fee Status (Unpaid Bus, Unpaid Canteen, Any Unpaid, Fully Paid), and Clock-In Status.
- **Quick-Pay Modal**: Collect payments directly from the dashboard row and watch the student's status badge flip from red to green immediately!

### 3. Bus Fleet Management (`/buses/`)
- Admin dashboard tracking bus routes, rider headcounts, expected route revenue, and actual collected fees.
- Add, edit, or remove buses with automatic fleet calculations.

### 4. Student Roster & Bulk CSV Import (`/students/`)
- Student enrollment with First Name, Last Name, and Class (e.g. `Class 1A`, `Class 2B`, `Nursery 1`) — **no student ID required**.
- Assign to designated bus routes or set as walkers.
- Configure canteen participation and optional custom fee overrides.
- **Bulk CSV Import (`/students/import/`)**: Upload an entire student roster from CSV in seconds with auto-bus route matching and downloadable template.

### 5. Fee Payments Ledger & Printable Receipts (`/payments/`)
- Complete financial record of bus and canteen fee payments.
- Filter by fee type, student class, date, or receipt number.
- **Official Payment Receipts (`/payments/<id>/receipt/`)**: Print-ready and PDF-styled receipt vouchers with student details, route rate, amount paid, balance remaining, cashier signature block, and official school header.

### 6. Daily Attendance & Reconciliation Reports (`/reports/`)
- Daily morning clock-in breakdown by class (Enrolled, Present, Absent, Attendance Rate %).
- Route-by-route bus fee collection vs outstanding balance.
- Canteen fee collection reconciliation.
- **CSV Data Exports**:
  - `📥 Attendance CSV`: Export daily attendance records for any selected date.
  - `📥 Debtors / Arrears CSV`: Export list of students with outstanding bus or canteen balances.

### 7. Parent Notification Audit Log (`/notifications/`)
- Automated logging of morning arrival alerts dispatched to parents when students clock in, plus fee reminder records.

### 8. Role-Based Authentication (`/login/`)
- Pre-configured user roles:
  - **👑 School Administrator (`admin` / `admin123`)**: Full control over buses, students, settings, and finances.
  - **⏱️ Teacher (`teacher` / `teacher123`)**: Morning clock-in attendance and roster fee verification.
  - **💳 Accountant / Bursar (`bursar` / `bursar123`)**: Fee collection, payments ledger, receipts, and reconciliation.

---

## 🛠️ Tech Stack & Setup

- **Backend**: Python 3.11 + Django 5.x
- **Database**: PostgreSQL with automatic SQLite zero-friction fallback (`.env` configured)
- **Frontend**: Django Templates + Modern Glassmorphic CSS Design System + Vanilla JS
- **Static Assets**: WhiteNoise

### Environment Configuration (`.env`)
```env
DEBUG=True
SECRET_KEY=django-insecure-school-clockin-demo-key-2026-secure!

# Database Configuration
# Set USE_SQLITE=False once you have configured PostgreSQL below
USE_SQLITE=True
DB_ENGINE=django.db.backends.postgresql
DB_NAME=school_clockin_db
DB_USER=postgres
DB_PASSWORD=your_postgres_password
DB_HOST=localhost
DB_PORT=5432

# School Billing Settings
DEFAULT_CANTEEN_FEE=45.00
CURRENT_ACADEMIC_PERIOD=Term 1 - 2026
SCHOOL_NAME=Geosaka Model School
CURRENCY_SYMBOL=GH₵
```

---

## 🚀 How to Run the Application

### 1. Apply Database Migrations
```bash
python manage.py makemigrations attendance
python manage.py migrate
```

### 2. Populate Realistic Demonstration Data
Pre-loads 4 buses (**Tema Bus**, **Madina Bus**, **Spintex Bus**, **Adenta Bus**), 19 students across 5 classes, sample fee payments, morning attendance, and default role logins:
```bash
python manage.py seed_data
```

### 3. Run Automated Tests
Verifies dynamic bus route pricing, payment balances, clock-in AJAX API, CSV imports/exports, receipts, and authentication:
```bash
python manage.py test attendance
```

### 4. Start the Development Server
```bash
python manage.py runserver
```
Then visit `http://127.0.0.1:8000/` in your browser.

---

## 🐳 Running with Docker

You can run this application entirely containerized with Docker and Docker Compose.

### Option A: Complete Stack with PostgreSQL (Recommended)
This starts both PostgreSQL and the Django app with health-checked dependency management and live file reload:

```bash
# Build images and start containers
docker compose up --build
```

Load demo buses, students, and accounts inside the container:
```bash
docker compose exec web python manage.py seed_data
```
Open **http://localhost:8000** in your browser!

To stop containers:
```bash
docker compose down
```

### Option B: Standalone Container (with SQLite)
To run a single lightweight container without external database setup:
```bash
# Build the Docker image
docker build -t clockin-app .

# Run container mapping port 8000
docker run -p 8000:8000 -e USE_SQLITE=True clockin-app
```


---

## 🔐 Default Demo Accounts

| Role | Username | Password | Primary Duties |
| :--- | :--- | :--- | :--- |
| **School Administrator** | `admin` | `admin123` | Full access, buses, students, and settings |
| **Classroom Teacher** | `teacher` | `teacher123` | Morning clock-in & fee verification |
| **Accountant / Bursar** | `bursar` | `bursar123` | Fee recording, payment receipts & reports |
