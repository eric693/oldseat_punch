import json as _json
import os
import calendar as _cal
from datetime import datetime as _dt, date as _date, timedelta as _td, timezone as _tz
from io import StringIO

from flask import Blueprint, request, jsonify, session

from config import TW_TZ
from db import get_db, _hash_pw
from auth_utils import login_required, require_module
from utils import (
    salary_item_row, salary_record_row, punch_staff_row,
    _get_salary_config, _eval_formula, _calc_service_years,
    _calc_annual_leave_days, _calc_ot_pay, _calc_leave_days,
    _calc_punch_hours, _build_shift_time_map, _shift_aware_day_map,
    _clamp_to_shift, _notify_review_result,
)

bp = Blueprint('salary', __name__)

def _auto_generate_salary(conn, staff, month, work_days=None):
    """
    自動產生員工月薪資料
    ─ 月薪制：底薪 + 薪資項目公式 + 加班費 - 請假扣款
    ─ 時薪制：打卡實際工時 × 時薪 + 加班費 - 請假扣款
    """
    import calendar as _cal2
    from datetime import date as _d5, timedelta as _td5, datetime as _dts5, timezone as _tz5
    _TW5 = _tz5(_td5(hours=8))
    _today5 = _dts5.now(_TW5).date()
    y, m = int(month[:4]), int(month[5:])
    total_work_days = work_days
    scheduled_dates = set()

    if total_work_days is None:
        # 1. 優先從排班取工作日
        shift_date_rows = conn.execute("""
            SELECT DISTINCT shift_date FROM shift_assignments
            WHERE staff_id=%s AND TO_CHAR(shift_date,'YYYY-MM')=%s
            ORDER BY shift_date
        """, (staff['id'], month)).fetchall()
        if shift_date_rows:
            scheduled_dates = {r['shift_date'].isoformat() if hasattr(r['shift_date'], 'isoformat') else str(r['shift_date']) for r in shift_date_rows}
            total_work_days = len(scheduled_dates)
        else:
            # 2. 備援：日曆扣除週日 + 國定假日
            holiday_rows = conn.execute("""
                SELECT date FROM public_holidays
                WHERE TO_CHAR(date,'YYYY-MM')=%s
            """, (month,)).fetchall()
            holiday_dates = {r['date'].isoformat() if hasattr(r['date'], 'isoformat') else str(r['date']) for r in holiday_rows}
            days_in_month = _cal2.monthrange(y, m)[1]
            for _d in range(1, days_in_month + 1):
                _dt = _d5(y, m, _d)
                _ds = _dt.isoformat()
                if _dt.weekday() not in (5, 6) and _ds not in holiday_dates:
                    scheduled_dates.add(_ds)
            total_work_days = len(scheduled_dates)

    salary_type    = staff.get('salary_type', 'monthly') or 'monthly'
    base_salary    = float(staff.get('base_salary')    or 0)
    hourly_rate    = float(staff.get('hourly_rate')    or 0)
    insured_salary = float(staff.get('insured_salary') or base_salary)
    daily_hours    = float(staff.get('daily_hours')    or 8)
    service_years  = _calc_service_years(staff.get('hire_date'))

    # ── 時薪制：從打卡記錄計算工時 ──────────────────────────
    actual_work_hours = 0.0
    punch_details     = []
    if salary_type == 'hourly':
        actual_work_hours, punch_work_days, punch_details = _calc_punch_hours(
            conn, staff['id'], month
        )
        # 時薪制的 base_salary 等於 實際工時 × 時薪
        hourly_base_pay = round(actual_work_hours * hourly_rate, 2)
    else:
        # 月薪制：daily_wage 用於請假扣款
        hourly_base_pay = 0.0

    # ── 已核准加班費 ────────────────────────────────────────
    ot_rows = conn.execute("""
        SELECT COALESCE(SUM(ot_pay), 0) as total
        FROM overtime_requests
        WHERE staff_id=%s AND status='approved'
          AND to_char(request_date,'YYYY-MM')=%s
    """, (staff['id'], month)).fetchone()
    ot_pay = float(ot_rows['total']) if ot_rows else 0.0

    # ── 請假資訊 ────────────────────────────────────────────
    month_first = _d5(y, m, 1)
    month_last  = _d5(y, m, _cal2.monthrange(y, m)[1])

    # Bug fix: 用日期範圍重疊取代只看 start_date，正確抓跨月請假
    leave_rows = conn.execute("""
        SELECT lr.total_days, lt.pay_rate, lt.code, lt.name as leave_name,
               lr.start_date, lr.end_date
        FROM leave_requests lr
        JOIN leave_types lt ON lt.id = lr.leave_type_id
        WHERE lr.staff_id=%s AND lr.status='approved'
          AND lr.start_date <= %s AND lr.end_date >= %s
    """, (staff['id'], month_last, month_first)).fetchall()

    def _leave_days_in_month(row):
        """計算某筆請假在當月的實際天數（處理跨月）"""
        sd = row['start_date']
        ed = row['end_date']
        if hasattr(sd, 'date'): sd = sd.date()
        else: sd = _d5.fromisoformat(str(sd))
        if hasattr(ed, 'date'): ed = ed.date()
        else: ed = _d5.fromisoformat(str(ed))
        if sd >= month_first and ed <= month_last:
            return float(row['total_days'])  # 同月：用已計算天數（含半天）
        # 跨月：重新計算在本月範圍內的非週日天數
        cur = max(sd, month_first)
        end = min(ed, month_last)
        cnt = 0.0
        while cur <= end:
            if cur.weekday() != 6:
                cnt += 1.0
            cur += _td5(days=1)
        return cnt

    leave_days  = sum(_leave_days_in_month(r) for r in leave_rows)
    unpaid_days = sum(_leave_days_in_month(r) for r in leave_rows if float(r['pay_rate']) == 0)
    # 部分薪假：保留每筆(天數, pay_rate, 假別名)，供後續正確計算扣款
    half_pay_rows = [
        (_leave_days_in_month(r), float(r['pay_rate']), r['leave_name'])
        for r in leave_rows if 0 < float(r['pay_rate']) < 1
    ]
    actual_days = total_work_days - leave_days

    # ── 日薪 / 時薪（用於請假扣款） ───────────────────────
    if salary_type == 'hourly':
        daily_wage  = hourly_rate * daily_hours   # 時薪制日薪 = 時薪 × 每日工時
        hourly_wage = hourly_rate
    else:
        daily_wage  = base_salary / 30 if base_salary > 0 else 0
        hourly_wage = daily_wage / daily_hours if daily_hours > 0 else 0

    # ── 組裝薪資項目 ────────────────────────────────────────
    items           = []
    allowance_total = 0.0
    deduction_total = 0.0
    # 員工個人金額覆寫 {str(item_id): amount}
    _overrides = staff.get('salary_item_overrides') or {}
    if isinstance(_overrides, str):
        try: _overrides = _json.loads(_overrides)
        except Exception: _overrides = {}

    def _apply_override(item_id, calculated_amt):
        """若員工設有個人金額，使用個人金額；否則使用計算值"""
        key = str(item_id)
        if key in _overrides and _overrides[key] is not None and _overrides[key] != '':
            return float(_overrides[key]), True   # (amount, is_overridden)
        return calculated_amt, False

    if salary_type == 'hourly':
        # 時薪制：第一筆項目是「本薪（工時計算）」
        items.append({
            'id': 'hourly_base', 'name': '本薪（工時）', 'type': 'allowance',
            'amount': hourly_base_pay, 'formula': '',
            'calc_note': (
                f'{actual_work_hours}h × 時薪${hourly_rate}'
                + (f'（{len(punch_details)}天出勤）' if punch_details else '')
            ),
        })
        allowance_total += hourly_base_pay

        # 時薪制加班費：僅採計核准的加班申請金額，不從打卡時數自動估算
        # （補打卡可能造成異常長班，自動估算會誤產生加班費）

        # 時薪制的保險費以 insured_salary 為準（若未設定則用月薪換算）
        if insured_salary == 0:
            insured_salary = round(hourly_rate * daily_hours * 30, 0)

        # 時薪制只加入保險類扣除項（若員工有指定則只取指定中的保險項）
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            salary_items_rows = conn.execute(f"""
                SELECT * FROM salary_items
                WHERE active=TRUE AND id IN ({placeholders})
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """, staff_item_ids).fetchall()
        else:
            salary_items_rows = conn.execute("""
                SELECT * FROM salary_items
                WHERE active=TRUE
                  AND item_type='deduction'
                  AND (formula LIKE '%insured_salary%' OR formula LIKE '%base_salary%')
                ORDER BY sort_order, id
            """).fetchall()
        for it in salary_items_rows:
            calc_amt = _eval_formula(it['formula'] or '', base_salary or insured_salary,
                                     insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else (it['formula'] or '')
            items.append({
                'id': it['id'], 'name': it['name'], 'type': 'deduction',
                'amount': round(amt, 2), 'formula': it['formula'] or '',
                'calc_note': note,
            })
            deduction_total += amt

    else:
        # 月薪制：跑啟用的薪資項目（若員工有指定則只跑指定項目）
        staff_item_ids = staff.get('salary_item_ids')
        if staff_item_ids:
            placeholders = ','.join(['%s'] * len(staff_item_ids))
            items_rows = conn.execute(
                f"SELECT * FROM salary_items WHERE active=TRUE AND id IN ({placeholders}) ORDER BY sort_order, id",
                staff_item_ids
            ).fetchall()
        else:
            items_rows = conn.execute(
                "SELECT * FROM salary_items WHERE active=TRUE ORDER BY sort_order, id"
            ).fetchall()
        for it in items_rows:
            formula  = it['formula'] or ''
            calc_amt = float(it['amount'] or 0)
            if formula:
                calc_amt = _eval_formula(formula, base_salary, insured_salary, service_years)
            amt, overridden = _apply_override(it['id'], calc_amt)
            note = f'手動設定 ${amt}' if overridden else formula
            items.append({
                'id':        it['id'],
                'name':      it['name'],
                'type':      it['item_type'],
                'amount':    round(amt, 2),
                'formula':   formula,
                'calc_note': note,
            })
            if it['item_type'] == 'allowance':
                allowance_total += amt
            else:
                deduction_total += amt

    # ── 加班費（申請核准） ──────────────────────────────────
    if ot_pay > 0:
        items.append({
            'id': 'ot', 'name': '加班費（申請）', 'type': 'allowance',
            'amount': round(ot_pay, 2), 'formula': '',
            'calc_note': '核准加班費合計',
        })
        allowance_total += ot_pay

    # ── 請假扣款 ────────────────────────────────────────────
    if unpaid_days > 0 and daily_wage > 0:
        leave_names = '、'.join(set(
            r['leave_name'] for r in leave_rows if float(r['pay_rate']) == 0
        ))
        deduct = round(daily_wage * unpaid_days, 2)
        items.append({
            'id': 'unpaid', 'name': f'無薪假扣款（{leave_names}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': f'{unpaid_days}天 × 日薪${round(daily_wage, 0)}',
        })
        deduction_total += deduct

    if half_pay_rows and daily_wage > 0:
        # Bug fix: 依每筆假別的實際 pay_rate 計算扣款，不再硬寫 0.5
        total_half_deduct = 0.0
        notes = []
        name_set = set()
        for days_here, pay_r, lname in half_pay_rows:
            deduct_rate = round(1.0 - pay_r, 4)
            d = round(daily_wage * days_here * deduct_rate, 2)
            total_half_deduct += d
            name_set.add(lname)
            notes.append(f'{lname} {days_here}天×扣{round(deduct_rate*100,0):.0f}%')
        deduct = round(total_half_deduct, 2)
        items.append({
            'id': 'halfpay', 'name': f'部分薪假扣款（{"、".join(name_set)}）', 'type': 'deduction',
            'amount': deduct, 'formula': '',
            'calc_note': '，'.join(notes) + f'（日薪${round(daily_wage, 0)}）',
        })
        deduction_total += deduct

    # ── 月薪制：缺勤扣款（打卡記錄核查） ─────────────────────
    absent_days = 0
    if salary_type == 'monthly' and scheduled_dates and daily_wage > 0:
        punch_rows = conn.execute("""
            SELECT DISTINCT (punched_at AT TIME ZONE 'Asia/Taipei')::date as work_date
            FROM punch_records WHERE staff_id=%s
              AND TO_CHAR(punched_at AT TIME ZONE 'Asia/Taipei','YYYY-MM')=%s
        """, (staff['id'], month)).fetchall()
        punched_dates = {r['work_date'].isoformat() if hasattr(r['work_date'], 'isoformat') else str(r['work_date']) for r in punch_rows}
        # 已核准請假日期集合 — 直接從 leave_rows 重建，不再發第二次查詢
        leave_date_set = set()
        for _lr in leave_rows:
            _ld = _lr['start_date']
            _le = _lr['end_date']
            if hasattr(_ld, 'date'): _ld = _ld.date()
            else: _ld = _d5.fromisoformat(str(_ld))
            if hasattr(_le, 'date'): _le = _le.date()
            else: _le = _d5.fromisoformat(str(_le))
            while _ld <= _le:
                leave_date_set.add(_ld.isoformat())
                _ld += _td5(days=1)
        # 缺勤 = 排班但未打卡且非假日，僅計算過去日期
        absent_date_list = sorted(
            ds for ds in scheduled_dates
            if ds not in punched_dates and ds not in leave_date_set
               and _d5.fromisoformat(ds) < _today5
        )
        absent_days = len(absent_date_list)
        if absent_days > 0:
            deduct = round(daily_wage * absent_days, 2)
            sample = '、'.join(absent_date_list[:3]) + ('等' if absent_days > 3 else '')
            items.append({
                'id': 'absent', 'name': f'缺勤扣款（{absent_days} 天）', 'type': 'deduction',
                'amount': deduct, 'formula': '',
                'calc_note': f'{absent_days} 天 × 日薪 ${round(daily_wage, 0)}（{sample}）',
            })
            deduction_total += deduct

    net_pay = round(allowance_total - deduction_total, 2)

    # ── 假別明細（供薪資單顯示） ────────────────────────────────
    PAY_LABEL5 = {1.0: '全薪', 0.5: '半薪', 0.0: '無薪'}
    leave_details = []
    for r in leave_rows:
        d5 = _leave_days_in_month(r)
        if d5 <= 0:
            continue
        pr = float(r['pay_rate'])
        leave_details.append({
            'leave_name': r['leave_name'],
            'days':       d5,
            'pay_rate':   pr,
            'pay_label':  PAY_LABEL5.get(pr, f'{int(pr*100)}%薪'),
            'start_date': str(r['start_date']),
            'end_date':   str(r['end_date']),
        })

    # ── 當月國定假日 ───────────────────────────────────────────
    holiday_rows5 = conn.execute("""
        SELECT date, name FROM public_holidays
        WHERE TO_CHAR(date,'YYYY-MM')=%s ORDER BY date
    """, (month,)).fetchall()
    holiday_dates_list = [
        {'date': str(r['date']), 'name': r['name']} for r in holiday_rows5
    ]

    return {
        'staff_id':           staff['id'],
        'month':              month,
        'salary_type':        salary_type,
        'base_salary':        base_salary if salary_type == 'monthly' else hourly_base_pay,
        'hourly_rate':        hourly_rate if salary_type == 'hourly' else 0,
        'hourly_base_pay':    hourly_base_pay if salary_type == 'hourly' else 0,
        'actual_work_hours':  actual_work_hours if salary_type == 'hourly' else 0,
        'insured_salary':     insured_salary,
        'work_days':          total_work_days,
        'actual_days':        max(0, actual_days - absent_days),
        'leave_days':         leave_days,
        'unpaid_days':        unpaid_days,
        'absent_days':        absent_days,
        'ot_pay':             ot_pay,
        'allowance_total':    round(allowance_total, 2),
        'deduction_total':    round(deduction_total, 2),
        'net_pay':            net_pay,
        'items':              items,
        'punch_details':      punch_details,
        'leave_details':      leave_details,
        'holiday_dates':      holiday_dates_list,
        'status':             'draft',
    }

# ── Employee: view own payslip ────────────────────────────────────


@bp.route('/api/salary/my-payslip', methods=['GET'])
def api_my_payslip():
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': '請先登入'}), 401
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _dp
        month = _dp.today().strftime('%Y-%m')
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.salary_type, ps.hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.staff_id = %s AND sr.month = %s
        """, (sid, month)).fetchone()
        if not row:
            return jsonify({'error': f'{month} 尚無薪資記錄，請聯絡管理員'}), 404
        # 時薪制：工時明細
        _st  = row['salary_type'] or 'monthly'
        _awk = 0.0; _pd = []
        if _st == 'hourly':
            _awk, _, _pd = _calc_punch_hours(conn, sid, month)
        # 假別明細
        from datetime import date as _dmp, timedelta as _tdmp
        import calendar as _calmp
        y_mp, m_mp = int(month[:4]), int(month[5:])
        mf = _dmp(y_mp, m_mp, 1)
        ml = _dmp(y_mp, m_mp, _calmp.monthrange(y_mp, m_mp)[1])
        lv_rows = conn.execute("""
            SELECT lr.total_days, lt.pay_rate, lt.name as leave_name,
                   lr.start_date, lr.end_date
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s AND lr.status='approved'
              AND lr.start_date<=%s AND lr.end_date>=%s
        """, (sid, ml, mf)).fetchall()
        PAY_LBL_MP = {1.0:'全薪', 0.5:'半薪', 0.0:'無薪'}
        lv_details = []
        for _lr in lv_rows:
            _sd = _lr['start_date'] if isinstance(_lr['start_date'], _dmp) else _dmp.fromisoformat(str(_lr['start_date']))
            _ed = _lr['end_date']   if isinstance(_lr['end_date'],   _dmp) else _dmp.fromisoformat(str(_lr['end_date']))
            if _sd >= mf and _ed <= ml:
                _d5 = float(_lr['total_days'])
            else:
                _sd2 = max(_sd, mf); _ed2 = min(_ed, ml)
                _d5 = sum(1 for i in range((_ed2-_sd2).days+1) if (_sd2+_tdmp(days=i)).weekday()!=6)
            if _d5 <= 0: continue
            _pr = float(_lr['pay_rate'])
            lv_details.append({
                'leave_name': _lr['leave_name'], 'days': _d5, 'pay_rate': _pr,
                'pay_label':  PAY_LBL_MP.get(_pr, f'{int(_pr*100)}%薪'),
                'start_date': str(_lr['start_date']), 'end_date': str(_lr['end_date']),
            })
        hol_rows_mp = conn.execute("""
            SELECT date, name FROM public_holidays
            WHERE TO_CHAR(date,'YYYY-MM')=%s ORDER BY date
        """, (month,)).fetchall()
        hol_list = [{'date': str(r['date']), 'name': r['name']} for r in hol_rows_mp]
    d = salary_record_row(row)
    d['staff_name']    = row['staff_name']
    d['staff_role']    = row['staff_role']
    d['employee_code'] = row['employee_code'] or ''
    d['department']    = row['department']    or ''
    d['salary_type']   = _st
    d['hourly_rate']   = float(row['hourly_rate'] or 0)
    d['actual_work_hours'] = _awk
    d['punch_details']     = _pd
    d['leave_details']     = lv_details
    d['holiday_dates']     = hol_list
    return jsonify(d)

# ── Salary Items CRUD ─────────────────────────────────────────────

@bp.route('/api/salary/items', methods=['GET'])
@require_module('salary')
def api_salary_items_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM salary_items ORDER BY sort_order, id").fetchall()
    return jsonify([salary_item_row(r) for r in rows])

@bp.route('/api/salary/items', methods=['POST'])
@require_module('salary')
def api_salary_item_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO salary_items (name, item_type, formula, amount, description, color, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b.get('item_type','allowance'), b.get('formula',''),
              float(b.get('amount',0)), b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)))).fetchone()
    return jsonify(salary_item_row(row)), 201

@bp.route('/api/salary/items/<int:iid>', methods=['PUT'])
@require_module('salary')
def api_salary_item_update(iid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_items SET name=%s, item_type=%s, formula=%s, amount=%s,
              description=%s, color=%s, sort_order=%s, active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b.get('item_type','allowance'), b.get('formula',''),
              float(b.get('amount',0)), b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)),
              bool(b.get('active',True)), iid)).fetchone()
    return jsonify(salary_item_row(row)) if row else ('', 404)

@bp.route('/api/salary/items/<int:iid>', methods=['DELETE'])
@require_module('salary')
def api_salary_item_delete(iid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_items WHERE id=%s", (iid,))
    return jsonify({'deleted': iid})

# ── Salary Records ─────────────────────────────────────────────────

@bp.route('/api/salary/records', methods=['GET'])
@require_module('salary')
def api_salary_records_list():
    month = request.args.get('month', '')
    if not month:
        from datetime import date as _d6
        month = _d6.today().strftime('%Y-%m')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department,
                   ps.salary_type as staff_salary_type,
                   ps.hourly_rate as staff_hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.month=%s
            ORDER BY ps.name
        """, (month,)).fetchall()
    result = []
    for r in rows:
        d = salary_record_row(r)
        d['staff_name']    = r['staff_name']
        d['staff_role']    = r['staff_role']
        d['employee_code'] = r['employee_code'] or ''
        d['department']    = r['department'] or ''
        if not d.get('salary_type'): d['salary_type'] = r['staff_salary_type'] or 'monthly'
        if not d.get('hourly_rate'): d['hourly_rate']  = float(r['staff_hourly_rate'] or 0)
        result.append(d)
    return jsonify(result)

@bp.route('/api/salary/records/generate', methods=['POST'])
@require_module('salary')
def api_salary_generate():
    """自動產生或更新該月薪資"""
    import calendar as _cal2
    from datetime import date as _d2
    b     = request.get_json(force=True)
    month = b.get('month', '').strip()
    if not month: return jsonify({'error': '請指定月份'}), 400
    try:
        year2, mo2 = map(int, month.split('-'))
    except (ValueError, AttributeError):
        return jsonify({'error': '月份格式錯誤，請使用 YYYY-MM'}), 400

    # 計算發薪日：薪資月份的下一個月的 pay_day
    cfg = _get_salary_config()
    pay_day = cfg['pay_day']
    pay_year, pay_mo = (year2, mo2 + 1) if mo2 < 12 else (year2 + 1, 1)
    effective_pay_day = min(pay_day, _cal2.monthrange(pay_year, pay_mo)[1])
    pay_date_str = _d2(pay_year, pay_mo, effective_pay_day).isoformat()

    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE"
        ).fetchall()
        generated = 0
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            items_json = _json.dumps(data['items'], ensure_ascii=False)
            conn.execute("""
                INSERT INTO salary_records
                  (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                   leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                   net_pay, items, pay_date, status, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'draft',NOW())
                ON CONFLICT (staff_id, month) DO UPDATE
                  SET base_salary     = CASE WHEN salary_records.status='confirmed' THEN salary_records.base_salary     ELSE EXCLUDED.base_salary     END,
                      insured_salary  = CASE WHEN salary_records.status='confirmed' THEN salary_records.insured_salary  ELSE EXCLUDED.insured_salary  END,
                      work_days       = CASE WHEN salary_records.status='confirmed' THEN salary_records.work_days       ELSE EXCLUDED.work_days       END,
                      actual_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.actual_days     ELSE EXCLUDED.actual_days     END,
                      leave_days      = CASE WHEN salary_records.status='confirmed' THEN salary_records.leave_days      ELSE EXCLUDED.leave_days      END,
                      unpaid_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.unpaid_days     ELSE EXCLUDED.unpaid_days     END,
                      ot_pay          = CASE WHEN salary_records.status='confirmed' THEN salary_records.ot_pay          ELSE EXCLUDED.ot_pay          END,
                      allowance_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.allowance_total ELSE EXCLUDED.allowance_total END,
                      deduction_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.deduction_total ELSE EXCLUDED.deduction_total END,
                      net_pay         = CASE WHEN salary_records.status='confirmed' THEN salary_records.net_pay         ELSE EXCLUDED.net_pay         END,
                      items           = CASE WHEN salary_records.status='confirmed' THEN salary_records.items           ELSE EXCLUDED.items::jsonb    END,
                      pay_date        = COALESCE(salary_records.pay_date, EXCLUDED.pay_date),
                      status          = CASE WHEN salary_records.status='confirmed' THEN 'confirmed' ELSE 'draft' END,
                      updated_at      = NOW()
            """, (
                data['staff_id'], month, data['base_salary'], data['insured_salary'],
                data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                data['ot_pay'], data['allowance_total'], data['deduction_total'],
                data['net_pay'], items_json, pay_date_str,
            ))
            generated += 1
    return jsonify({'ok': True, 'generated': generated, 'month': month, 'pay_date': pay_date_str})

@bp.route('/api/salary/records/<int:rid>', methods=['GET'])
@require_module('salary')
def api_salary_record_get(rid):
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.role as staff_role,
                   ps.employee_code, ps.department, ps.hire_date,
                   ps.salary_type as staff_salary_type,
                   ps.hourly_rate as staff_hourly_rate
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id=sr.staff_id
            WHERE sr.id=%s
        """, (rid,)).fetchone()
        if not row: return ('', 404)
        _st    = row.get('staff_salary_type') or 'monthly'
        _month = row['month']
        # 時薪制：重算每日工時明細
        _actual_work_hours = 0.0
        _punch_details     = []
        if _st == 'hourly':
            _actual_work_hours, _, _punch_details = _calc_punch_hours(
                conn, row['staff_id'], _month
            )
        # 假別明細
        from datetime import date as _dg
        _month_first = _dg(int(_month[:4]), int(_month[5:]), 1)
        import calendar as _calg
        _month_last  = _dg(int(_month[:4]), int(_month[5:]),
                           _calg.monthrange(int(_month[:4]), int(_month[5:]))[1])
        _leave_rows = conn.execute("""
            SELECT lr.total_days, lt.pay_rate, lt.name as leave_name,
                   lr.start_date, lr.end_date
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s AND lr.status='approved'
              AND lr.start_date <= %s AND lr.end_date >= %s
        """, (row['staff_id'], _month_last, _month_first)).fetchall()
        PAY_LBL = {1.0:'全薪', 0.5:'半薪', 0.0:'無薪'}
        _leave_details = []
        from datetime import timedelta as _tdg
        for _lr in _leave_rows:
            _sd = _lr['start_date'] if isinstance(_lr['start_date'], _dg) else _dg.fromisoformat(str(_lr['start_date']))
            _ed = _lr['end_date']   if isinstance(_lr['end_date'],   _dg) else _dg.fromisoformat(str(_lr['end_date']))
            if _sd >= _month_first and _ed <= _month_last:
                _d5 = float(_lr['total_days'])
            else:
                _sd2 = max(_sd, _month_first); _ed2 = min(_ed, _month_last)
                _d5 = sum(1 for i in range((_ed2-_sd2).days+1)
                          if (_sd2+_tdg(days=i)).weekday()!=6)
            if _d5 <= 0: continue
            _pr = float(_lr['pay_rate'])
            _leave_details.append({
                'leave_name': _lr['leave_name'], 'days': _d5, 'pay_rate': _pr,
                'pay_label':  PAY_LBL.get(_pr, f'{int(_pr*100)}%薪'),
                'start_date': str(_lr['start_date']), 'end_date': str(_lr['end_date']),
            })
        # 當月國定假日
        _hol_rows = conn.execute("""
            SELECT date, name FROM public_holidays
            WHERE TO_CHAR(date,'YYYY-MM')=%s ORDER BY date
        """, (_month,)).fetchall()
        _holiday_dates = [{'date': str(r['date']), 'name': r['name']} for r in _hol_rows]
    d = salary_record_row(row)
    d['staff_name']       = row['staff_name']
    d['staff_role']       = row['staff_role']
    d['employee_code']    = row['employee_code'] or ''
    d['department']       = row['department'] or ''
    d['hire_date']        = row['hire_date'].isoformat() if row['hire_date'] else ''
    if not d.get('salary_type'): d['salary_type'] = _st
    if not d.get('hourly_rate'): d['hourly_rate']  = float(row['staff_hourly_rate'] or 0)
    d['actual_work_hours'] = _actual_work_hours
    d['punch_details']     = _punch_details
    d['leave_details']     = _leave_details
    d['holiday_dates']     = _holiday_dates
    return jsonify(d)

@bp.route('/api/salary/records/<int:rid>', methods=['PUT'])
@require_module('salary')
def api_salary_record_update(rid):
    b = request.get_json(force=True)
    items_json = _json.dumps(b.get('items', []), ensure_ascii=False)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET
              allowance_total=%s, deduction_total=%s, net_pay=%s,
              items=%s::jsonb, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('allowance_total',0)), float(b.get('deduction_total',0)),
              float(b.get('net_pay',0)), items_json,
              b.get('note',''), rid)).fetchone()
    return jsonify(salary_record_row(row)) if row else ('', 404)

@bp.route('/api/salary/records/confirm-all', methods=['POST'])
@require_module('salary')
def api_salary_confirm_all():
    b    = request.get_json(force=True)
    month = b.get('month','').strip()
    by   = b.get('confirmed_by','管理員')
    if not month: return jsonify({'error': '請指定月份'}), 400
    with get_db() as conn:
        rows = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE month=%s AND status='draft'
            RETURNING id, staff_id, month, net_pay
        """, (by, month)).fetchall()
    confirmed = len(rows)
    for row in rows:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify({'ok': True, 'confirmed': confirmed})

@bp.route('/api/salary/records/<int:rid>/confirm', methods=['POST'])
@require_module('salary')
def api_salary_confirm(rid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE salary_records SET status='confirmed', confirmed_by=%s,
              confirmed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (b.get('confirmed_by','管理員'), rid)).fetchone()
    if row:
        extra = f"{row['month']} 薪資已確認\n實領金額：${float(row['net_pay'] or 0):,.0f}"
        _notify_review_result(row['staff_id'], '薪資', 'confirmed', extra)
    return jsonify(salary_record_row(row)) if row else ('', 404)

@bp.route('/api/salary/records/<int:rid>', methods=['DELETE'])
@require_module('salary')
def api_salary_record_delete(rid):
    with get_db() as conn:
        conn.execute("DELETE FROM salary_records WHERE id=%s", (rid,))
    return jsonify({'deleted': rid})

# ── Salary Staff Settings ─────────────────────────────────────────

@bp.route('/api/salary/staff', methods=['GET'])
@require_module('salary')
def api_salary_staff_list():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, name, username, role, active, employee_code, department,
                   position_title, hire_date, birth_date, base_salary, insured_salary,
                   daily_hours, ot_rate1, ot_rate2, salary_type, hourly_rate,
                   vacation_quota, salary_notes, salary_item_ids, salary_item_overrides,
                   national_id, gender, insurance_type, address
            FROM punch_staff ORDER BY name
        """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for f in ['base_salary','insured_salary','daily_hours','ot_rate1','ot_rate2','hourly_rate']:
            if d.get(f) is not None: d[f] = float(d[f])
        if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
        if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
        d['annual_leave_days'] = _calc_annual_leave_days(d.get('hire_date'))
        d['service_years']     = _calc_service_years(d.get('hire_date'))
        result.append(d)
    return jsonify(result)

@bp.route('/api/salary/staff/<int:sid>', methods=['PUT'])
@require_module('salary')
def api_salary_staff_update(sid):
    b = request.get_json(force=True)
    def _f(k, default=0): return float(b.get(k, default) or default)
    def _s(k): return b.get(k, '').strip() if b.get(k) else None
    with get_db() as conn:
        conn.execute("SELECT id FROM punch_staff WHERE id=%s FOR UPDATE", (sid,))
        salary_item_ids = b.get('salary_item_ids')
        salary_item_ids_json = _json.dumps(salary_item_ids) if salary_item_ids is not None else None
        overrides = b.get('salary_item_overrides')  # dict {str(item_id): amount}
        overrides_json = _json.dumps(overrides) if overrides else None
        conn.execute("""
            UPDATE punch_staff SET
              employee_code=%s, department=%s, position_title=%s,
              hire_date=%s, birth_date=%s,
              base_salary=%s, insured_salary=%s, daily_hours=%s,
              ot_rate1=%s, ot_rate2=%s, salary_type=%s,
              hourly_rate=%s, vacation_quota=%s, salary_notes=%s,
              salary_item_ids=%s, salary_item_overrides=%s,
              national_id=%s, gender=%s, insurance_type=%s, address=%s
            WHERE id=%s
        """, (_s('employee_code'), _s('department'), _s('position_title'),
              _s('hire_date'), _s('birth_date'),
              _f('base_salary'), _f('insured_salary'), _f('daily_hours') or 8,
              _f('ot_rate1') or 1.33, _f('ot_rate2') or 1.67,
              b.get('salary_type','monthly'),
              _f('hourly_rate'), b.get('vacation_quota') or None,
              b.get('salary_notes',''), salary_item_ids_json, overrides_json,
              (b.get('national_id') or '').strip(),
              (b.get('gender') or '').strip(),
              (b.get('insurance_type') or 'regular').strip(),
              (b.get('address') or '').strip(),
              sid))
        row = conn.execute("SELECT * FROM punch_staff WHERE id=%s", (sid,)).fetchone()
    return jsonify(punch_staff_row(row)) if row else ('', 404)


@bp.route('/api/salary/config', methods=['GET'])
@require_module('salary')
def api_salary_config_get():
    return jsonify(_get_salary_config())

@bp.route('/api/salary/config', methods=['PUT'])
@require_module('salary')
def api_salary_config_put():
    b = request.get_json(force=True)
    settlement_day = int(b.get('settlement_day', 1))
    pay_day        = int(b.get('pay_day', 5))
    if not (1 <= settlement_day <= 28):
        return jsonify({'error': '結算日需在 1–28 之間'}), 400
    if not (1 <= pay_day <= 28):
        return jsonify({'error': '發薪日需在 1–28 之間'}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE salary_config SET settlement_day=%s, pay_day=%s, updated_at=NOW() WHERE id=1",
            (settlement_day, pay_day)
        )
    return jsonify({'ok': True, 'settlement_day': settlement_day, 'pay_day': pay_day})


# ═══════════════════════════════════════════════════════════════════
# Announcement Module (公告管理)
# ═══════════════════════════════════════════════════════════════════

def init_announcement_db():
    try:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS announcements (
                    id          SERIAL PRIMARY KEY,
                    title       TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    category    TEXT DEFAULT 'general',
                    priority    TEXT DEFAULT 'normal',
                    is_pinned   BOOLEAN DEFAULT FALSE,
                    visible_to  TEXT DEFAULT 'all',
                    published_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at  TIMESTAMPTZ,
                    author      TEXT DEFAULT '管理員',
                    active      BOOLEAN DEFAULT TRUE,
                    view_count  INT DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
    except Exception as e:
        print(f"[announcement_init] {e}")


@bp.route('/api/salary/records/<int:rid>/pdf', methods=['GET'])
@require_module('salary')
def api_salary_pdf(rid):
    """回傳薪資單 HTML（供瀏覽器列印/另存 PDF）"""
    # 允許員工查看自己的薪資單
    if not session.get('logged_in'):
        sid = session.get('punch_staff_id')
        if not sid:
            return '未登入', 401
    with get_db() as conn:
        row = conn.execute("""
            SELECT sr.*, ps.name as staff_name, ps.employee_code,
                   ps.department, ps.role, ps.salary_type,
                   ps.hourly_rate, ps.hire_date
            FROM salary_records sr
            JOIN punch_staff ps ON ps.id = sr.staff_id
            WHERE sr.id = %s
        """, (rid,)).fetchone()
        if not row:
            return '找不到薪資記錄', 404
        # 時薪制：從打卡記錄重新計算每日工時明細（punch_details 未存入 salary_records）
        _pdf_punch_details = []
        if row.get('salary_type') == 'hourly':
            _, _, _pdf_punch_details = _calc_punch_hours(conn, row['staff_id'], row['month'])
    # 員工只能看自己的
    if not session.get('logged_in'):
        if row['staff_id'] != session.get('punch_staff_id'):
            return '無權限', 403

    d         = salary_record_row(row)
    items     = d.get('items') or []
    allow_items  = [i for i in items if i.get('type') == 'allowance']
    deduct_items = [i for i in items if i.get('type') == 'deduction']
    is_hourly = (row['salary_type'] == 'hourly')

    def money(v):
        try: return f"${float(v):,.0f}"
        except: return '$0'

    def esc_h(s):
        return str(s or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    allow_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num green">{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in allow_items)

    deduct_rows = ''.join(f"""
        <tr>
          <td>{esc_h(i['name'])}</td>
          <td class="num red">-{money(i['amount'])}</td>
          <td class="note">{esc_h(i.get('calc_note',''))}</td>
        </tr>""" for i in deduct_items)

    punch_table = ''
    if is_hourly and _pdf_punch_details:
        punch_rows = ''.join(f"""
            <tr>
              <td>{p['date']}</td>
              <td>{p['clock_in']}</td>
              <td>{p['clock_out']}</td>
              <td>{p.get('break_mins',0)} min</td>
              <td class="num">{p['net_hours']} h</td>
            </tr>""" for p in _pdf_punch_details)
        punch_table = f"""
        <h3>每日工時明細</h3>
        <table>
          <thead><tr><th>日期</th><th>上班</th><th>下班</th><th>休息</th><th>工時</th></tr></thead>
          <tbody>{punch_rows}</tbody>
          <tfoot><tr><td colspan="4"><strong>合計</strong></td><td class="num"><strong>{d.get('actual_work_hours',0)} h</strong></td></tr></tfoot>
        </table>"""

    status_str = '已確認' if row['status'] == 'confirmed' else '草稿（未確認）'
    sal_type   = '時薪制' if is_hourly else '月薪制'
    attend_str = (f"實際工時 {d.get('actual_work_hours',0)}h × 時薪 ${float(row['hourly_rate'] or 0):,.0f}"
                  if is_hourly else
                  f"出勤 {d.get('actual_days',0)} 天 / 工作日 {d.get('work_days',0)} 天")
    if float(d.get('leave_days',0)) > 0:
        attend_str += f"，請假 {d.get('leave_days',0)} 天"
    if float(d.get('unpaid_days',0)) > 0:
        attend_str += f"（無薪 {d.get('unpaid_days',0)} 天）"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>薪資單 {esc_h(row['staff_name'])} {esc_h(row['month'])}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Noto Sans TC', 'PingFang TC', 'Microsoft JhengHei', sans-serif;
          font-size: 13px; color: #1a2340; background: #fff; padding: 32px; }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start;
             border-bottom: 3px solid #1a2340; padding-bottom: 16px; margin-bottom: 24px; }}
  .company {{ font-size: 20px; font-weight: 800; color: #1a2340; }}
  .slip-title {{ font-size: 14px; color: #666; margin-top: 4px; }}
  .staff-info {{ font-size: 12px; color: #444; text-align: right; line-height: 1.8; }}
  .summary {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-bottom: 24px; }}
  .sum-card {{ border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 12px 16px; text-align: center; }}
  .sum-label {{ font-size: 10px; color: #888; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .06em; }}
  .sum-val {{ font-size: 22px; font-weight: 800; font-family: 'DM Mono', monospace; }}
  .sum-val.green {{ color: #2e9e6b; }}
  .sum-val.red   {{ color: #d64242; }}
  .sum-val.navy  {{ color: #1a2340; }}
  .attend {{ background: #f8fafc; border-radius: 6px; padding: 8px 14px;
             font-size: 12px; color: #666; margin-bottom: 20px; }}
  h3 {{ font-size: 12px; font-weight: 700; color: #888; letter-spacing: .08em;
        text-transform: uppercase; margin: 20px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f1f5f9; padding: 8px 12px; text-align: left;
        font-size: 11px; font-weight: 700; color: #666;
        border-bottom: 2px solid #e2e8f0; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #f0f2f8; }}
  td.num {{ text-align: right; font-family: 'DM Mono', monospace; font-weight: 600; }}
  td.note {{ font-size: 11px; color: #999; }}
  td.green {{ color: #2e9e6b; }}
  td.red   {{ color: #d64242; }}
  tfoot td {{ font-weight: 700; background: #f8fafc; border-top: 2px solid #e2e8f0; }}
  .net-row td {{ font-size: 16px; font-weight: 800; background: #1a2340; color: #fff; }}
  .net-row td.num {{ color: #f0c040; font-size: 20px; }}
  .footer {{ margin-top: 32px; padding-top: 16px; border-top: 1px solid #e2e8f0;
             display: flex; justify-content: space-between; font-size: 11px; color: #999; }}
  .sign-area {{ display: flex; gap: 48px; margin-top: 40px; }}
  .sign-box {{ flex: 1; border-top: 1px solid #ccc; padding-top: 6px; font-size: 11px; color: #666; }}
  @media print {{
    body {{ padding: 16px; }}
    @page {{ margin: 12mm; size: A4; }}
    .no-print {{ display: none !important; }}
  }}
</style>
</head>
<body>

<div class="no-print" style="text-align:right;margin-bottom:20px">
  <button onclick="window.print()"
    style="padding:10px 24px;background:#1a2340;color:#fff;border:none;border-radius:6px;
           font-size:13px;font-weight:700;cursor:pointer">列印 / 儲存 PDF</button>
</div>

<div class="header">
  <div>
    <div class="company">薪資明細單</div>
    <div class="slip-title">{esc_h(row['month'])} · {sal_type}</div>
  </div>
  <div class="staff-info">
    <div><strong>{esc_h(row['staff_name'])}</strong></div>
    <div>{esc_h(row['employee_code'] or '')}　{esc_h(row['department'] or '')}　{esc_h(row['role'] or '')}</div>
    <div>到職日：{esc_h(str(row['hire_date']) if row['hire_date'] else '—')}</div>
    <div>發薪日：<strong>{esc_h(str(d.get('pay_date','')) or '—')}</strong></div>
    <div>狀態：<strong>{status_str}</strong></div>
  </div>
</div>

<div class="summary">
  <div class="sum-card">
    <div class="sum-label">津貼合計</div>
    <div class="sum-val green">{money(d.get('allowance_total',0))}</div>
  </div>
  <div class="sum-card">
    <div class="sum-label">扣除合計</div>
    <div class="sum-val red">-{money(d.get('deduction_total',0))}</div>
  </div>
  <div class="sum-card" style="border-color:#1a2340">
    <div class="sum-label">實領金額</div>
    <div class="sum-val navy">{money(d.get('net_pay',0))}</div>
  </div>
</div>

<div class="attend">{attend_str}</div>

<h3>津貼項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{allow_rows}</tbody>
  <tfoot>
    <tr><td><strong>津貼合計</strong></td><td class="num green"><strong>{money(d.get('allowance_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<h3>扣除項目</h3>
<table>
  <thead><tr><th>項目</th><th style="text-align:right">金額</th><th>計算說明</th></tr></thead>
  <tbody>{deduct_rows if deduct_rows else '<tr><td colspan="3" style="color:#ccc;text-align:center;padding:12px">無扣除項目</td></tr>'}</tbody>
  <tfoot>
    <tr><td><strong>扣除合計</strong></td><td class="num red"><strong>-{money(d.get('deduction_total',0))}</strong></td><td></td></tr>
  </tfoot>
</table>

<table style="margin-top:12px">
  <tbody>
    <tr class="net-row">
      <td><strong>實領金額</strong></td>
      <td class="num">{money(d.get('net_pay',0))}</td>
      <td style="color:#ccc;font-size:11px">= 津貼 {money(d.get('allowance_total',0))} - 扣除 {money(d.get('deduction_total',0))}</td>
    </tr>
  </tbody>
</table>

{punch_table}

<div class="sign-area">
  <div class="sign-box">員工簽名</div>
  <div class="sign-box">主管確認</div>
  <div class="sign-box">人資確認</div>
</div>

<div class="footer">
  <span>本薪資單由系統自動產生</span>
  <span>列印日期：<script>document.write(new Date().toLocaleDateString('zh-TW'))</script></span>
</div>

</body>
</html>"""

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ═══════════════════════════════════════════════════════════════════
# Feature: Batch Review (批次審核)
# ═══════════════════════════════════════════════════════════════════

@bp.route('/api/salary/formula/preview', methods=['POST'])
@require_module('salary')
def api_formula_preview():
    """即時預覽公式計算結果"""
    b             = request.get_json(force=True)
    formula       = b.get('formula', '').strip()
    base_salary   = float(b.get('base_salary', 30000))
    insured_salary= float(b.get('insured_salary', 30000))
    service_years = float(b.get('service_years', 1))

    if not formula:
        return jsonify({'result': 0, 'error': None})
    try:
        result = _eval_formula(formula, base_salary, insured_salary, service_years)
        return jsonify({'result': round(result, 2), 'error': None})
    except Exception as e:
        return jsonify({'result': None, 'error': str(e)})

# ═══════════════════════════════════════════════════════════════════
# Finance Module (財務模組)
# ═══════════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

@bp.route('/api/salary/records/preview', methods=['POST'])
@require_module('salary')
def api_salary_preview():
    """預覽薪資計算結果（不儲存）"""
    b     = request.get_json(force=True) or {}
    month = b.get('month', '').strip()
    if not month:
        return jsonify({'error': '請指定月份'}), 400
    result = []
    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT * FROM punch_staff WHERE active=TRUE ORDER BY name"
        ).fetchall()
        for staff in staff_list:
            data = _auto_generate_salary(conn, dict(staff), month)
            # punch attendance days this month
            punch_days = conn.execute("""
                SELECT COUNT(DISTINCT punched_at::date) AS n
                FROM punch_records WHERE staff_id=%s
                  AND to_char(punched_at,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()['n']
            approved_ot = conn.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(ot_hours),0) AS hrs
                FROM overtime_requests WHERE staff_id=%s
                  AND status='approved'
                  AND to_char(request_date,'YYYY-MM')=%s
            """, (staff['id'], month)).fetchone()
            result.append({
                'staff_id':       data['staff_id'],
                'staff_name':     staff['name'],
                'department':     staff['department'],
                'salary_type':    staff['salary_type'],
                'punch_days':     punch_days,
                'work_days':      float(data['work_days']),
                'actual_days':    float(data['actual_days']),
                'leave_days':     float(data['leave_days']),
                'unpaid_days':    float(data['unpaid_days']),
                'ot_count':       int(approved_ot['n']),
                'ot_hours':       float(approved_ot['hrs']),
                'ot_pay':         float(data['ot_pay']),
                'base_salary':    float(data['base_salary']),
                'allowance_total': float(data['allowance_total']),
                'deduction_total': float(data['deduction_total']),
                'net_pay':        float(data['net_pay']),
            })
    return jsonify({'ok': True, 'month': month, 'records': result})



# ── Monthly salary auto-generate scheduler ────────────────────────────────────
# ─── Monthly Salary Auto-Generate Scheduler ───────────────────────────────────

def _job_auto_generate_salary():
    """
    每日 02:00 (TW) 檢查是否為結算日，若是則自動產生上個月薪資草稿。
    使用 pg_try_advisory_lock 確保多 worker 環境只執行一次。
    """
    from datetime import date as _dj, timedelta as _tdj
    import json as _jj
    import calendar as _calj

    today = _dj.today()

    # 讀取結算設定
    try:
        cfg = _get_salary_config()
    except Exception:
        cfg = {'settlement_day': 1, 'pay_day': 5}

    settlement_day = cfg['settlement_day']
    pay_day        = cfg['pay_day']

    # 確認今天是否為結算日（處理月底不足的情況，如二月只有 28/29 天）
    days_in_cur_month = _calj.monthrange(today.year, today.month)[1]
    effective_settlement = min(settlement_day, days_in_cur_month)
    if today.day != effective_settlement:
        return  # 非結算日，跳過

    # 結算的是「上個月」薪資
    first  = today.replace(day=1)
    last_m = (first - _tdj(days=1))
    month  = last_m.strftime('%Y-%m')

    # 計算發薪日（本月的 pay_day）
    days_in_pay_month = days_in_cur_month
    effective_pay_day = min(pay_day, days_in_pay_month)
    pay_date_str = _dj(today.year, today.month, effective_pay_day).isoformat()

    LOCK_KEY = 202604011  # 任意唯一整數，代表「薪資自動產生」鎖

    try:
        with get_db() as conn:
            locked = conn.execute(
                "SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,)
            ).fetchone()['ok']
            if not locked:
                return  # 其他 worker 已執行，跳過

            try:
                staff_list = conn.execute(
                    "SELECT * FROM punch_staff WHERE active=TRUE"
                ).fetchall()
                generated = 0
                for staff in staff_list:
                    data       = _auto_generate_salary(conn, dict(staff), month)
                    items_json = _jj.dumps(data['items'], ensure_ascii=False)
                    conn.execute("""
                        INSERT INTO salary_records
                          (staff_id, month, base_salary, insured_salary, work_days, actual_days,
                           leave_days, unpaid_days, ot_pay, allowance_total, deduction_total,
                           net_pay, items, pay_date, status, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'draft',NOW())
                        ON CONFLICT (staff_id, month) DO UPDATE
                          SET base_salary     = CASE WHEN salary_records.status='confirmed' THEN salary_records.base_salary     ELSE EXCLUDED.base_salary     END,
                              insured_salary  = CASE WHEN salary_records.status='confirmed' THEN salary_records.insured_salary  ELSE EXCLUDED.insured_salary  END,
                              work_days       = CASE WHEN salary_records.status='confirmed' THEN salary_records.work_days       ELSE EXCLUDED.work_days       END,
                              actual_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.actual_days     ELSE EXCLUDED.actual_days     END,
                              leave_days      = CASE WHEN salary_records.status='confirmed' THEN salary_records.leave_days      ELSE EXCLUDED.leave_days      END,
                              unpaid_days     = CASE WHEN salary_records.status='confirmed' THEN salary_records.unpaid_days     ELSE EXCLUDED.unpaid_days     END,
                              ot_pay          = CASE WHEN salary_records.status='confirmed' THEN salary_records.ot_pay          ELSE EXCLUDED.ot_pay          END,
                              allowance_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.allowance_total ELSE EXCLUDED.allowance_total END,
                              deduction_total = CASE WHEN salary_records.status='confirmed' THEN salary_records.deduction_total ELSE EXCLUDED.deduction_total END,
                              net_pay         = CASE WHEN salary_records.status='confirmed' THEN salary_records.net_pay         ELSE EXCLUDED.net_pay         END,
                              items           = CASE WHEN salary_records.status='confirmed' THEN salary_records.items           ELSE EXCLUDED.items::jsonb    END,
                              pay_date        = COALESCE(salary_records.pay_date, EXCLUDED.pay_date),
                              status          = CASE WHEN salary_records.status='confirmed' THEN 'confirmed' ELSE 'draft' END,
                              updated_at      = NOW()
                    """, (
                        data['staff_id'], month, data['base_salary'], data['insured_salary'],
                        data['work_days'], data['actual_days'], data['leave_days'], data['unpaid_days'],
                        data['ot_pay'], data['allowance_total'], data['deduction_total'],
                        data['net_pay'], items_json, pay_date_str,
                    ))
                    generated += 1
                print(f'[scheduler] 自動薪資產生完成：{month}，發薪日 {pay_date_str}，共 {generated} 筆', flush=True)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except Exception as e:
        print(f'[scheduler] 自動薪資產生失敗：{e}', flush=True)


def _start_salary_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone='Asia/Taipei')
    scheduler.add_job(
        _job_auto_generate_salary,
        trigger=CronTrigger(hour=2, minute=0, timezone='Asia/Taipei'),  # 每日 02:00 檢查結算日
        id='monthly_salary_generate',
        replace_existing=True,
    )
    scheduler.start()
    print('[scheduler] 薪資自動產生排程已啟動（每日 02:00 TW 檢查結算日）', flush=True)


# 啟動排程器（gunicorn 多 worker 時每個 worker 都會啟動，由 advisory lock 保證只執行一次）
try:
    _start_salary_scheduler()
except Exception as _sched_err:
    print(f'[scheduler] 排程啟動失敗：{_sched_err}', flush=True)
