/**
 * School Clock-In & Fee Verification Dynamic Interactions
 */

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icon = type === 'success' ? '✓' : '⚠️';
    toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;

    container.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => {
        toast.classList.add('show');
    });

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

function updateLiveStats(stats) {
    if (!stats) return;

    const clockedCountElem = document.getElementById('stat-clocked-count');
    const clockedPctElem = document.getElementById('stat-clocked-pct');
    const busPctElem = document.getElementById('stat-bus-pct');
    const canteenPctElem = document.getElementById('stat-canteen-pct');

    if (clockedCountElem) clockedCountElem.textContent = `${stats.clocked_in_count} / ${stats.total_students}`;
    if (clockedPctElem) clockedPctElem.textContent = `${stats.clocked_in_pct}% Present`;
    if (busPctElem) busPctElem.textContent = `${stats.bus_paid_pct}% Paid (${stats.bus_paid_count}/${stats.bus_students_count})`;
    if (canteenPctElem) canteenPctElem.textContent = `${stats.canteen_paid_pct}% Paid (${stats.canteen_paid_count}/${stats.canteen_students_count})`;
}

document.addEventListener('DOMContentLoaded', () => {
    const csrftoken = getCookie('csrftoken');

    // 1. Clock-in button click handler
    document.addEventListener('click', async (e) => {
        const clockInBtn = e.target.closest('.btn-clock-in');
        const undoBtn = e.target.closest('.btn-undo-clockin');

        if (!clockInBtn && !undoBtn) return;

        e.preventDefault();
        const actionElem = clockInBtn || undoBtn;
        const studentId = actionElem.dataset.studentId;
        const action = actionElem.dataset.action || 'toggle';
        const dateStr = document.getElementById('date-picker') ? document.getElementById('date-picker').value : '';

        // Visual feedback
        actionElem.style.opacity = '0.5';
        actionElem.style.pointerEvents = 'none';

        try {
            const response = await fetch('/api/clock-in/toggle/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrftoken,
                },
                body: JSON.stringify({
                    student_id: studentId,
                    date: dateStr,
                    action: action,
                }),
            });

            const data = await response.json();

            if (data.success) {
                const cell = document.getElementById(`clockin-action-cell-${studentId}`);
                if (cell) {
                    if (data.is_clocked_in) {
                        cell.innerHTML = `
                            <div class="clocked-in-badge pulse-target">
                                <span>✓ ${data.clock_in_time}</span>
                                <button type="button" class="btn-undo-clockin" data-student-id="${studentId}" data-action="undo" title="Undo clock-in">
                                    ✕
                                </button>
                            </div>
                        `;
                    } else {
                        cell.innerHTML = `
                            <button type="button" class="btn-clock-in" data-student-id="${studentId}" data-action="clock_in">
                                <span>⏱️</span> Clock In
                            </button>
                        `;
                    }
                }

                // Update row dataset for filter
                const row = document.getElementById(`student-row-${studentId}`);
                if (row) {
                    row.dataset.clockedIn = data.is_clocked_in ? 'true' : 'false';
                }

                showToast(data.message, 'success');
                if (data.stats) updateLiveStats(data.stats);
            } else {
                showToast(data.error || 'Failed to update clock-in.', 'error');
                actionElem.style.opacity = '1';
                actionElem.style.pointerEvents = 'auto';
            }
        } catch (err) {
            console.error('Clock-in error:', err);
            showToast('Network error while processing clock-in.', 'error');
            actionElem.style.opacity = '1';
            actionElem.style.pointerEvents = 'auto';
        }
    });

    // 2. Quick Pay Modal Handling
    const paymentModal = document.getElementById('quick-payment-modal');
    const paymentForm = document.getElementById('quick-payment-form');
    const modalStudentName = document.getElementById('modal-student-name');
    const modalStudentIdInput = document.getElementById('modal-student-id');
    const modalFeeTypeSelect = document.getElementById('modal-fee-type');
    const modalAmountInput = document.getElementById('modal-amount');

    document.addEventListener('click', (e) => {
        const payBtn = e.target.closest('.btn-quick-pay');
        if (payBtn) {
            e.preventDefault();
            const sId = payBtn.dataset.studentId;
            const sName = payBtn.dataset.studentName;
            const feeType = payBtn.dataset.feeType || 'BUS';
            const defaultAmt = payBtn.dataset.defaultAmount || '';

            if (modalStudentName) modalStudentName.textContent = sName;
            if (modalStudentIdInput) modalStudentIdInput.value = sId;
            if (modalFeeTypeSelect) modalFeeTypeSelect.value = feeType;
            if (modalAmountInput) modalAmountInput.value = defaultAmt;

            if (paymentModal) paymentModal.classList.add('active');
        }

        const closeBtn = e.target.closest('.btn-close-modal');
        if (closeBtn || (e.target === paymentModal)) {
            if (paymentModal) paymentModal.classList.remove('active');
        }
    });

    if (paymentForm) {
        paymentForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const submitBtn = paymentForm.querySelector('button[type="submit"]');
            submitBtn.disabled = true;
            submitBtn.textContent = 'Saving...';

            const payload = {
                student_id: modalStudentIdInput.value,
                fee_type: modalFeeTypeSelect.value,
                amount: modalAmountInput.value,
                payment_method: document.getElementById('modal-payment-method').value,
                receipt_number: document.getElementById('modal-receipt-number').value,
                period: document.getElementById('modal-period').value,
            };

            try {
                const response = await fetch('/api/payment/quick/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrftoken,
                    },
                    body: JSON.stringify(payload),
                });

                const resData = await response.json();

                if (resData.success) {
                    showToast(resData.message, 'success');
                    paymentModal.classList.remove('active');
                    paymentForm.reset();

                    // Update student row badges
                    const studentId = resData.student_id;
                    const busCell = document.getElementById(`bus-fee-cell-${studentId}`);
                    const canteenCell = document.getElementById(`canteen-fee-cell-${studentId}`);
                    const curr = document.body.dataset.currency || '$';

                    if (busCell && resData.bus_status) {
                        if (resData.bus_status.is_paid) {
                            busCell.innerHTML = `<span class="fee-badge fee-badge-paid">🟢 Paid (${curr}${resData.bus_status.required.toFixed(2)})</span>`;
                        } else {
                            busCell.innerHTML = `<span class="fee-badge fee-badge-unpaid">🔴 Owes ${curr}${resData.bus_status.balance.toFixed(2)}</span>`;
                        }
                    }

                    if (canteenCell && resData.canteen_status) {
                        if (resData.canteen_status.is_paid) {
                            canteenCell.innerHTML = `<span class="fee-badge fee-badge-paid">🟢 Paid (${curr}${resData.canteen_status.required.toFixed(2)})</span>`;
                        } else {
                            canteenCell.innerHTML = `<span class="fee-badge fee-badge-unpaid">🔴 Owes ${curr}${resData.canteen_status.balance.toFixed(2)}</span>`;
                        }
                    }

                    if (resData.stats) updateLiveStats(resData.stats);
                } else {
                    showToast(resData.error || 'Failed to record payment.', 'error');
                }
            } catch (err) {
                console.error(err);
                showToast('Error recording payment.', 'error');
            } finally {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Save Payment';
            }
        });
    }

    // 3. Date picker change auto-submit
    const datePicker = document.getElementById('date-picker');
    if (datePicker) {
        datePicker.addEventListener('change', () => {
            document.getElementById('filter-form').submit();
        });
    }
});
