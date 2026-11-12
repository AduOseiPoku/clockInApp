/**
 * School Clock-In & Fee Verification Dynamic Interactions
 * Minimalist interaction logic with clean feedback
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
    const icon = type === 'success' ? '✓' : '•';
    toast.innerHTML = `<span style="font-weight:700;">${icon}</span> <span>${message}</span>`;

    container.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => {
        toast.classList.add('show');
    });

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 250);
    }, 3000);
}

function updateLiveStats(stats) {
    if (!stats) return;

    const curr = document.body.dataset.currency || 'GH₵';
    const clockedCountElem = document.getElementById('stat-clocked-count');
    const clockedPctElem = document.getElementById('stat-clocked-pct');
    const busCountElem = document.getElementById('stat-bus-count');
    const busPctElem = document.getElementById('stat-bus-pct');
    const canteenCountElem = document.getElementById('stat-canteen-count');
    const canteenPctElem = document.getElementById('stat-canteen-pct');
    const dailyRevenueElem = document.getElementById('stat-daily-revenue');
    const busCollectedElem = document.getElementById('stat-bus-collected');
    const canteenCollectedElem = document.getElementById('stat-canteen-collected');

    if (clockedCountElem && stats.clocked_in_count !== undefined) {
        clockedCountElem.textContent = `${stats.clocked_in_count} / ${stats.total_students}`;
    }
    if (clockedPctElem && stats.clocked_in_pct !== undefined) {
        clockedPctElem.textContent = `${stats.clocked_in_pct}% Present`;
    }
    if (busCountElem && stats.bus_paid_count !== undefined) {
        busCountElem.innerHTML = `${stats.bus_paid_count} <span style="font-size:1rem;color:var(--text-muted);font-weight:400;">/ ${stats.bus_students_count}</span>`;
    }
    if (busPctElem && stats.bus_paid_pct !== undefined) {
        busPctElem.textContent = `${stats.bus_paid_pct}% Paid`;
        busPctElem.className = `stat-badge ${stats.bus_paid_pct >= 80 ? 'stat-badge-success' : 'stat-badge-danger'}`;
    }
    if (canteenCountElem && stats.canteen_paid_count !== undefined) {
        canteenCountElem.innerHTML = `${stats.canteen_paid_count} <span style="font-size:1rem;color:var(--text-muted);font-weight:400;">/ ${stats.canteen_students_count}</span>`;
    }
    if (canteenPctElem && stats.canteen_paid_pct !== undefined) {
        canteenPctElem.textContent = `${stats.canteen_paid_pct}% Paid`;
        canteenPctElem.className = `stat-badge ${stats.canteen_paid_pct >= 80 ? 'stat-badge-success' : 'stat-badge-danger'}`;
    }

    if (dailyRevenueElem && stats.daily_total_collected !== undefined) {
        dailyRevenueElem.textContent = `${curr}${parseFloat(stats.daily_total_collected).toFixed(2)}`;
    }
    if (busCollectedElem && stats.total_bus_collected !== undefined) {
        busCollectedElem.textContent = `${curr}${parseFloat(stats.total_bus_collected).toFixed(2)} collected`;
    }
    if (canteenCollectedElem && stats.total_canteen_collected !== undefined) {
        canteenCollectedElem.textContent = `${curr}${parseFloat(stats.total_canteen_collected).toFixed(2)} collected`;
    }

    // Classroom Teacher specific attendance counters
    const teacherPresentElem = document.getElementById('teacher-stat-present');
    const teacherAbsentElem = document.getElementById('teacher-stat-absent');
    const teacherPctElem = document.getElementById('teacher-stat-pct');
    if (stats.class_present !== undefined && teacherPresentElem) {
        teacherPresentElem.textContent = `${stats.class_present} / ${stats.class_enrolled}`;
    }
    if (stats.class_absent !== undefined && teacherAbsentElem) {
        teacherAbsentElem.textContent = `${stats.class_absent}`;
    }
    if (stats.class_pct !== undefined && teacherPctElem) {
        teacherPctElem.textContent = `${stats.class_pct}%`;
    }
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
        actionElem.style.opacity = '0.4';
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
                            <div class="clocked-in-badge" style="display: inline-flex; align-items: center;">
                                <span>✓ ${data.clock_in_time}</span>
                                <button type="button" class="btn-undo-clockin" data-student-id="${studentId}" data-action="undo" title="Undo clock-in">
                                    ✕
                                </button>
                            </div>
                        `;
                    } else {
                        cell.innerHTML = `
                            <button type="button" class="btn-clock-in" data-student-id="${studentId}" data-action="clock_in">
                                ✓ Clock In
                            </button>
                        `;
                    }
                }

                // Update separate Arrival Status badge cell if present (e.g., Students Directory)
                const statusCell = document.getElementById(`clockin-status-cell-${studentId}`);
                if (statusCell) {
                    if (data.is_clocked_in) {
                        statusCell.innerHTML = `
                            <span class="badge badge-success" style="font-size: 0.76rem; padding: 0.2rem 0.6rem;">
                                ● Present
                            </span>
                        `;
                    } else {
                        statusCell.innerHTML = `
                            <span class="badge" style="background: var(--surface-subtle); color: var(--text-muted); font-size: 0.76rem; padding: 0.2rem 0.6rem; border: 1px solid var(--border);">
                                ⏳ Not Clocked In
                            </span>
                        `;
                    }
                }

                // Update Mobile Card Action Cell & Card Dataset
                const mobileAction = document.getElementById(`mobile-clockin-action-${studentId}`);
                if (mobileAction) {
                    if (data.is_clocked_in) {
                        mobileAction.innerHTML = `
                            <div class="clocked-in-badge">
                                <span>✓ Clocked In at ${data.clock_in_time}</span>
                                <button type="button" class="btn-undo-clockin" data-student-id="${studentId}" data-action="undo" title="Undo clock-in" style="font-size: 1rem; padding: 0 0.5rem;">
                                    ✕
                                </button>
                            </div>
                        `;
                    } else {
                        mobileAction.innerHTML = `
                            <button type="button" class="btn-clock-in" data-student-id="${studentId}" data-action="clock_in">
                                ✓ Clock In Arrival
                            </button>
                        `;
                    }
                }

                // Update row & card datasets for filters
                const row = document.getElementById(`student-row-${studentId}`);
                if (row) {
                    row.dataset.clockedIn = data.is_clocked_in ? 'true' : 'false';
                }
                const mobileCard = document.getElementById(`mobile-student-card-${studentId}`);
                if (mobileCard) {
                    mobileCard.dataset.clockedIn = data.is_clocked_in ? 'true' : 'false';
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
    const modalStudentSub = document.getElementById('modal-student-sub');
    const modalStudentIdInput = document.getElementById('modal-student-id');
    const modalPaymentDateInput = document.getElementById('modal-payment-date');
    const modalFeeTypeSelect = document.getElementById('modal-fee-type');
    const modalAmountInput = document.getElementById('modal-amount');
    const modalBusAmountInput = document.getElementById('modal-bus-amount');
    const modalCanteenAmountInput = document.getElementById('modal-canteen-amount');
    const modalSingleGroup = document.getElementById('modal-single-amount-group');
    const modalDualGroup = document.getElementById('modal-dual-amount-group');

    const btn1Day = document.getElementById('modal-btn-1day');
    const btn5Days = document.getElementById('modal-btn-5days');
    const btn10Days = document.getElementById('modal-btn-10days');
    const btnBalance = document.getElementById('modal-btn-balance');

    let currentModalStudent = null;

    function syncModalFeeTypeView() {
        const ft = modalFeeTypeSelect ? modalFeeTypeSelect.value : 'BUS';
        if (ft === 'BOTH') {
            if (modalSingleGroup) modalSingleGroup.style.display = 'none';
            if (modalDualGroup) modalDualGroup.style.display = 'block';
            if (modalAmountInput) modalAmountInput.required = false;
        } else {
            if (modalSingleGroup) modalSingleGroup.style.display = 'block';
            if (modalDualGroup) modalDualGroup.style.display = 'none';
            if (modalAmountInput) modalAmountInput.required = true;
        }
    }

    function applyPreset(mode) {
        if (!currentModalStudent) return;
        const ft = modalFeeTypeSelect ? modalFeeTypeSelect.value : 'BUS';
        const busReq = currentModalStudent.busReq;
        const busBal = currentModalStudent.busBal;
        const canReq = currentModalStudent.canteenReq;
        const canBal = currentModalStudent.canteenBal;

        if (ft === 'BOTH') {
            if (mode === '1day') {
                if (modalBusAmountInput) modalBusAmountInput.value = (currentModalStudent.hasBus ? busReq : 0).toFixed(2);
                if (modalCanteenAmountInput) modalCanteenAmountInput.value = (currentModalStudent.hasCanteen ? canReq : 0).toFixed(2);
            } else if (mode === '5days') {
                if (modalBusAmountInput) modalBusAmountInput.value = (currentModalStudent.hasBus ? busReq * 5 : 0).toFixed(2);
                if (modalCanteenAmountInput) modalCanteenAmountInput.value = (currentModalStudent.hasCanteen ? canReq * 5 : 0).toFixed(2);
            } else if (mode === '10days') {
                if (modalBusAmountInput) modalBusAmountInput.value = (currentModalStudent.hasBus ? busReq * 10 : 0).toFixed(2);
                if (modalCanteenAmountInput) modalCanteenAmountInput.value = (currentModalStudent.hasCanteen ? canReq * 10 : 0).toFixed(2);
            } else {
                if (modalBusAmountInput) modalBusAmountInput.value = (currentModalStudent.hasBus ? (busBal > 0 ? busBal : busReq) : 0).toFixed(2);
                if (modalCanteenAmountInput) modalCanteenAmountInput.value = (currentModalStudent.hasCanteen ? (canBal > 0 ? canBal : canReq) : 0).toFixed(2);
            }
        } else if (ft === 'BUS') {
            if (mode === '1day') {
                if (modalAmountInput) modalAmountInput.value = busReq.toFixed(2);
            } else if (mode === '5days') {
                if (modalAmountInput) modalAmountInput.value = (busReq * 5).toFixed(2);
            } else if (mode === '10days') {
                if (modalAmountInput) modalAmountInput.value = (busReq * 10).toFixed(2);
            } else {
                if (modalAmountInput) modalAmountInput.value = (busBal > 0 ? busBal : busReq).toFixed(2);
            }
        } else {
            // CANTEEN
            if (mode === '1day') {
                if (modalAmountInput) modalAmountInput.value = canReq.toFixed(2);
            } else if (mode === '5days') {
                if (modalAmountInput) modalAmountInput.value = (canReq * 5).toFixed(2);
            } else if (mode === '10days') {
                if (modalAmountInput) modalAmountInput.value = (canReq * 10).toFixed(2);
            } else {
                if (modalAmountInput) modalAmountInput.value = (canBal > 0 ? canBal : canReq).toFixed(2);
            }
        }
    }

    if (modalFeeTypeSelect) {
        modalFeeTypeSelect.addEventListener('change', () => {
            syncModalFeeTypeView();
            applyPreset('balance');
        });
    }

    if (btn1Day) btn1Day.addEventListener('click', () => applyPreset('1day'));
    if (btn5Days) btn5Days.addEventListener('click', () => applyPreset('5days'));
    if (btn10Days) btn10Days.addEventListener('click', () => applyPreset('10days'));
    if (btnBalance) btnBalance.addEventListener('click', () => applyPreset('balance'));

    document.addEventListener('click', (e) => {
        const payBtn = e.target.closest('.btn-quick-pay[data-student-id]');
        if (payBtn) {
            e.preventDefault();
            const curr = document.body.dataset.currency || 'GH₵';
            const sId = payBtn.dataset.studentId;
            const sName = payBtn.dataset.studentName;
            const hasBus = payBtn.dataset.hasBus === 'true';
            const busPaid = payBtn.dataset.busPaid === 'true';
            const busReq = parseFloat(payBtn.dataset.busReq || 0);
            const busBal = parseFloat(payBtn.dataset.busBal || 0);
            const hasCanteen = payBtn.dataset.hasCanteen === 'true';
            const canteenPaid = payBtn.dataset.canteenPaid === 'true';
            const canteenReq = parseFloat(payBtn.dataset.canteenReq || 0);
            const canteenBal = parseFloat(payBtn.dataset.canteenBal || 0);

            let feeType = payBtn.dataset.feeType || 'BUS';
            if (hasBus && !busPaid && hasCanteen && !canteenPaid) {
                feeType = 'BOTH';
            } else if (hasBus && !busPaid) {
                feeType = 'BUS';
            } else if (hasCanteen && !canteenPaid) {
                feeType = 'CANTEEN';
            }

            currentModalStudent = {
                id: sId,
                name: sName,
                hasBus,
                busPaid,
                busReq,
                busBal,
                hasCanteen,
                canteenPaid,
                canteenReq,
                canteenBal,
            };

            if (modalStudentName) modalStudentName.textContent = sName;
            if (modalStudentSub) {
                const parts = [];
                if (hasBus) parts.push(`🚌 Bus: ${curr}${busReq.toFixed(2)}/day (${busPaid ? 'Paid' : 'Owes ' + curr + busBal.toFixed(2)})`);
                if (hasCanteen) parts.push(`🍽️ Lunch: ${curr}${canteenReq.toFixed(2)}/day (${canteenPaid ? 'Paid' : 'Owes ' + curr + canteenBal.toFixed(2)})`);
                modalStudentSub.textContent = parts.join(' • ');
            }
            if (modalStudentIdInput) modalStudentIdInput.value = sId;
            const dp = document.getElementById('date-picker');
            if (modalPaymentDateInput && dp) modalPaymentDateInput.value = dp.value;
            if (modalFeeTypeSelect) modalFeeTypeSelect.value = feeType;

            syncModalFeeTypeView();
            applyPreset('balance');

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

            const feeType = modalFeeTypeSelect ? modalFeeTypeSelect.value : 'BUS';
            const dp = document.getElementById('date-picker');
            const payload = {
                student_id: modalStudentIdInput.value,
                fee_type: feeType,
                payment_date: (dp && dp.value) ? dp.value : '',
                payment_method: document.getElementById('modal-payment-method').value,
                receipt_number: document.getElementById('modal-receipt-number').value,
                period: document.getElementById('modal-period').value,
            };

            if (feeType === 'BOTH') {
                payload.bus_amount = modalBusAmountInput ? modalBusAmountInput.value : '0.00';
                payload.canteen_amount = modalCanteenAmountInput ? modalCanteenAmountInput.value : '0.00';
            } else {
                payload.amount = modalAmountInput ? modalAmountInput.value : '0.00';
            }

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
                    const mobileBus = document.getElementById(`mobile-bus-fee-${studentId}`);
                    const mobileCanteen = document.getElementById(`mobile-canteen-fee-${studentId}`);
                    const curr = document.body.dataset.currency || 'GH₵';

                    if (resData.bus_status) {
                        const bStat = resData.bus_status;
                        let badgeHtml;
                        if (bStat.is_paid) {
                            const creditNotice = bStat.credit_days > 0 ? ` <small style="font-size:0.68rem;opacity:0.85;">(${bStat.credit_days}d credit)</small>` : '';
                            badgeHtml = `<span class="fee-badge fee-badge-paid">Paid (${curr}${bStat.required.toFixed(2)})${creditNotice}</span>`;
                        } else {
                            badgeHtml = `<span class="fee-badge fee-badge-unpaid">Owes ${curr}${bStat.balance.toFixed(2)}</span>`;
                        }
                        if (busCell) busCell.innerHTML = badgeHtml;
                        if (mobileBus) mobileBus.innerHTML = `<span style="font-weight:600; color:var(--text-muted);">Bus:</span> ${badgeHtml}`;
                    }

                    if (resData.canteen_status) {
                        const cStat = resData.canteen_status;
                        let badgeHtml;
                        if (cStat.is_paid) {
                            const creditNotice = cStat.credit_days > 0 ? ` <small style="font-size:0.68rem;opacity:0.85;">(${cStat.credit_days}d credit)</small>` : '';
                            badgeHtml = `<span class="fee-badge fee-badge-paid">Paid (${curr}${cStat.required.toFixed(2)})${creditNotice}</span>`;
                        } else {
                            badgeHtml = `<span class="fee-badge fee-badge-unpaid">Owes ${curr}${cStat.balance.toFixed(2)}</span>`;
                        }
                        if (canteenCell) canteenCell.innerHTML = badgeHtml;
                        if (mobileCanteen) mobileCanteen.innerHTML = `<span style="font-weight:600; color:var(--text-muted);">Canteen:</span> ${badgeHtml}`;
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

    // 4. Outside-click dismiss for contextual card menus
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.card-menu-wrapper')) {
            document.querySelectorAll('.card-menu-dropdown.show').forEach(dropdown => {
                dropdown.classList.remove('show');
            });
        }
    });

    // 5. Escape key to dismiss drawer
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeSideDrawer();
        }
    });

    // 6. Touch swipe-left to dismiss side drawer
    let touchStartX = 0;
    let touchStartY = 0;
    const sideDrawer = document.getElementById('side-drawer');
    if (sideDrawer) {
        sideDrawer.addEventListener('touchstart', (e) => {
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
        }, { passive: true });

        sideDrawer.addEventListener('touchend', (e) => {
            const touchEndX = e.changedTouches[0].clientX;
            const touchEndY = e.changedTouches[0].clientY;
            const diffX = touchEndX - touchStartX;
            const diffY = touchEndY - touchStartY;
            // Swipe left threshold: at least 45px left and horizontal movement > vertical movement
            if (diffX < -45 && Math.abs(diffX) > Math.abs(diffY)) {
                closeSideDrawer();
            }
        }, { passive: true });
    }
});

/**
 * Open off-canvas side drawer
 */
function openSideDrawer() {
    const drawer = document.getElementById('side-drawer');
    const backdrop = document.getElementById('side-drawer-backdrop');
    if (drawer) {
        drawer.classList.add('open');
        document.body.classList.add('drawer-open');
    }
    if (backdrop) {
        backdrop.classList.add('open');
    }
}

/**
 * Close off-canvas side drawer
 */
function closeSideDrawer() {
    const drawer = document.getElementById('side-drawer');
    const backdrop = document.getElementById('side-drawer-backdrop');
    if (drawer) {
        drawer.classList.remove('open');
        document.body.classList.remove('drawer-open');
    }
    if (backdrop) {
        backdrop.classList.remove('open');
    }
}

/**
 * Toggle off-canvas side drawer
 */
function toggleSideDrawer() {
    const drawer = document.getElementById('side-drawer');
    if (drawer && drawer.classList.contains('open')) {
        closeSideDrawer();
    } else {
        openSideDrawer();
    }
}

/**
 * Backward compatibility alias for legacy mobile menu calls
 */
function toggleMobileMenu() {
    toggleSideDrawer();
}

/**
 * Toggle mobile card 3-dots action menu
 */
function toggleCardMenu(button) {
    const wrapper = button.closest('.card-menu-wrapper');
    if (!wrapper) return;
    const dropdown = wrapper.querySelector('.card-menu-dropdown');
    if (!dropdown) return;

    // Close any other open dropdowns first
    document.querySelectorAll('.card-menu-dropdown.show').forEach(d => {
        if (d !== dropdown) d.classList.remove('show');
    });

    dropdown.classList.toggle('show');
}
