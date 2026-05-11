import json as _json
from datetime import datetime as _dt, date as _date

from flask import Blueprint, request, jsonify, session

from config import TW_TZ
from db import get_db
from auth_utils import login_required, require_module
from utils import (
    leave_type_row, leave_req_row, leave_balance_row,
    _calc_leave_days, _calc_annual_leave_days, _calc_annual_leave_schedule,
    _notify_review_result,
)

bp = Blueprint('leave', __name__)

@bp.route('/api/leave/types', methods=['GET'])
@require_module('leave')
def api_leave_types_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types ORDER BY sort_order, id").fetchall()
    return jsonify([leave_type_row(r) for r in rows])

@bp.route('/api/leave/types/public', methods=['GET'])
def api_leave_types_public():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM leave_types WHERE active=TRUE ORDER BY sort_order, id").fetchall()
    return jsonify([leave_type_row(r) for r in rows])

@bp.route('/api/leave/types', methods=['POST'])
@require_module('leave')
def api_leave_type_create():
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_types (name,code,pay_rate,max_days,description,color,sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate',1.0)),
              b.get('max_days') or None, b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)))).fetchone()
    return jsonify(leave_type_row(row)), 201

@bp.route('/api/leave/types/<int:tid>', methods=['PUT'])
@require_module('leave')
def api_leave_type_update(tid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_types SET name=%s,code=%s,pay_rate=%s,max_days=%s,
              description=%s,color=%s,sort_order=%s,active=%s
            WHERE id=%s RETURNING *
        """, (b['name'], b['code'], float(b.get('pay_rate',1.0)),
              b.get('max_days') or None, b.get('description',''),
              b.get('color','#4a7bda'), int(b.get('sort_order',0)),
              bool(b.get('active',True)), tid)).fetchone()
    return jsonify(leave_type_row(row)) if row else ('', 404)

@bp.route('/api/leave/types/<int:tid>', methods=['DELETE'])
@require_module('leave')
def api_leave_type_delete(tid):
    with get_db() as conn:
        conn.execute("DELETE FROM leave_types WHERE id=%s", (tid,))
    return jsonify({'deleted': tid})

# ── Leave Requests ────────────────────────────────────────────────

@bp.route('/api/leave/requests', methods=['GET'])
@require_module('leave')
def api_leave_requests_list():
    status   = request.args.get('status', '')
    month    = request.args.get('month', '')
    staff_id = request.args.get('staff_id', '')
    conds, params = ['TRUE'], []
    if status:   conds.append('lr.status=%s');                            params.append(status)
    if staff_id: conds.append('lr.staff_id=%s');                          params.append(int(staff_id))
    if month:    conds.append("to_char(lr.start_date,'YYYY-MM')=%s");     params.append(month)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT lr.*, ps.name as staff_name, ps.role as staff_role,
                   lt.name as leave_type_name, lt.code as leave_code,
                   lt.pay_rate, lt.color as leave_color
            FROM leave_requests lr
            JOIN punch_staff ps ON ps.id=lr.staff_id
            JOIN leave_types  lt ON lt.id=lr.leave_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY lr.start_date DESC, lr.created_at DESC LIMIT 300
        """, params).fetchall()
    result = []
    for r in rows:
        d = leave_req_row(r)
        d['staff_name']      = r['staff_name']
        d['staff_role']      = r['staff_role']
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['pay_rate']        = float(r['pay_rate'])
        d['leave_color']     = r['leave_color']
        result.append(d)
    return jsonify(result)

@bp.route('/api/leave/requests', methods=['POST'])
@require_module('leave')
def api_leave_request_admin_create():
    """管理員直接建立請假記錄"""
    b = request.get_json(force=True)
    sid           = b.get('staff_id')
    leave_type_id = b.get('leave_type_id')
    start_date    = b.get('start_date', '').strip()
    end_date      = b.get('end_date', '').strip()
    start_half    = bool(b.get('start_half', False))
    end_half      = bool(b.get('end_half', False))
    reason        = b.get('reason', '').strip()
    status        = b.get('status', 'approved')

    start_time = b.get('start_time', '').strip() or None
    end_time   = b.get('end_time', '').strip() or None

    if not all([sid, leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    if start_time and end_time:
        from datetime import time as _t
        try:
            sh, sm = map(int, start_time.split(':'))
            eh, em = map(int, end_time.split(':'))
            st = _t(sh, sm); et = _t(eh, em)
            from datetime import datetime as _dt
            hrs = (_dt.combine(_dt.today(), et) - _dt.combine(_dt.today(), st)).seconds / 3600
            if hrs <= 0:
                return jsonify({'error': '結束時間需晚於開始時間'}), 400
            total_days = round(hrs / 8, 2)
        except Exception:
            return jsonify({'error': '時間格式錯誤'}), 400
    else:
        total_days = _calc_leave_days(start_date, end_date, start_half, end_half)
        if total_days <= 0:
            return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, start_half, end_half,
               start_time, end_time, total_days, reason, status, reviewed_by, reviewed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              CASE WHEN %s='approved' THEN NOW() ELSE NULL END)
            RETURNING *
        """, (sid, leave_type_id, start_date, end_date, start_half, end_half,
              start_time, end_time, total_days, reason, status,
              b.get('reviewed_by','管理員'), status)).fetchone()
        if status == 'approved':
            _update_leave_balance(conn, sid, leave_type_id, start_date[:4], total_days)
    return jsonify(leave_req_row(row)), 201

@bp.route('/api/leave/requests/<int:rid>', methods=['PUT'])
@require_module('leave')
def api_leave_request_review(rid):
    b           = request.get_json(force=True)
    action      = b.get('action')
    reviewed_by = b.get('reviewed_by', '').strip()
    review_note = b.get('review_note', '').strip()
    if action not in ('approve', 'reject'):
        return jsonify({'error': 'invalid action'}), 400
    new_status = 'approved' if action == 'approve' else 'rejected'
    with get_db() as conn:
        old = conn.execute("SELECT * FROM leave_requests WHERE id=%s", (rid,)).fetchone()
        if not old: return ('', 404)
        row = conn.execute("""
            UPDATE leave_requests
            SET status=%s, reviewed_by=%s, review_note=%s,
                reviewed_at=NOW(), updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (new_status, reviewed_by, review_note, rid)).fetchone()
        old_status = old['status']
        if action == 'approve' and old_status != 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], float(old['total_days']))
        elif action == 'reject' and old_status == 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], -float(old['total_days']))
            _unconfirm_salary_for_leave(conn, old['staff_id'],
                                        str(old['start_date']), str(old['end_date']))
    if row:
        if old.get('start_time') and old.get('end_time'):
            st = str(old['start_time'])[:5]; et = str(old['end_time'])[:5]
            hrs = round(float(old['total_days']) * 8, 1)
            extra = f"{str(old['start_date'])} {st}～{et}（{hrs} 小時）"
        elif str(old['start_date']) == str(old['end_date']):
            extra = f"{str(old['start_date'])} 共 {float(old['total_days'])} 天"
        else:
            extra = f"{str(old['start_date'])} ~ {str(old['end_date'])} 共 {float(old['total_days'])} 天"
        if review_note: extra += f"\n審核意見：{review_note}"
        _notify_review_result(old['staff_id'], '請假申請', action, extra)
    return jsonify(leave_req_row(row)) if row else ('', 404)

@bp.route('/api/leave/requests/<int:rid>', methods=['DELETE'])
@require_module('leave')
def api_leave_request_delete(rid):
    with get_db() as conn:
        old = conn.execute("SELECT * FROM leave_requests WHERE id=%s", (rid,)).fetchone()
        if not old:
            return jsonify({'error': '找不到該假單'}), 404
        conn.execute("DELETE FROM leave_requests WHERE id=%s", (rid,))
        if old['status'] == 'approved':
            _update_leave_balance(conn, old['staff_id'], old['leave_type_id'],
                                  str(old['start_date'])[:4], -float(old['total_days']))
            _unconfirm_salary_for_leave(conn, old['staff_id'],
                                        str(old['start_date']), str(old['end_date']))
    return jsonify({'deleted': rid})

def _update_leave_balance(conn, staff_id, leave_type_id, year_str, delta_days):
    year = int(year_str)
    conn.execute("""
        INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
        VALUES (%s, %s, %s, 0, %s)
        ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
          SET used_days = leave_balances.used_days + EXCLUDED.used_days,
              updated_at = NOW()
    """, (staff_id, leave_type_id, year, delta_days))

def _unconfirm_salary_for_leave(conn, staff_id, start_date_str, end_date_str):
    """Revert confirmed salary records to draft when an approved leave is rejected/deleted.
    Returns list of affected month strings."""
    from datetime import date as _dl
    start = _dl.fromisoformat(str(start_date_str)[:10])
    end   = _dl.fromisoformat(str(end_date_str)[:10])
    months, cur = set(), start.replace(day=1)
    while cur <= end.replace(day=1):
        months.add(cur.strftime('%Y-%m'))
        cur = (cur.replace(day=28) + __import__('datetime').timedelta(days=4)).replace(day=1)
    affected = []
    for m in months:
        row = conn.execute("""
            UPDATE salary_records SET status='draft', updated_at=NOW()
            WHERE staff_id=%s AND month=%s AND status='confirmed'
            RETURNING month
        """, (staff_id, m)).fetchone()
        if row:
            affected.append(row['month'])
    return affected

# ── Employee: submit leave request ────────────────────────────────

@bp.route('/api/leave/my-requests', methods=['GET'])
def api_leave_my_list():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        rows = conn.execute("""
            SELECT lr.*, lt.name as leave_type_name, lt.code as leave_code,
                   lt.color as leave_color, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s
            ORDER BY lr.start_date DESC LIMIT 30
        """, (sid,)).fetchall()
    result = []
    for r in rows:
        d = leave_req_row(r)
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['leave_color']     = r['leave_color']
        d['pay_rate']        = float(r['pay_rate'])
        result.append(d)
    return jsonify(result)

@bp.route('/api/leave/my-requests', methods=['POST'])
def api_leave_submit():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': 'not logged in'}), 401
    b             = request.get_json(force=True)
    leave_type_id = b.get('leave_type_id')
    start_date    = b.get('start_date', '').strip()
    end_date      = b.get('end_date',   '').strip()
    start_time    = b.get('start_time', '').strip() or None
    end_time      = b.get('end_time',   '').strip() or None
    start_half    = bool(b.get('start_half', False))
    end_half      = bool(b.get('end_half',   False))
    reason        = b.get('reason', '').strip()
    substitute    = b.get('substitute_name', '').strip()
    document_id   = b.get('document_id') or None

    if not all([leave_type_id, start_date, end_date]):
        return jsonify({'error': '缺少必要欄位'}), 400

    is_hour_mode = bool(start_time and end_time)
    if is_hour_mode:
        from datetime import datetime as _dt
        try:
            st = _dt.strptime(start_time, '%H:%M')
            et = _dt.strptime(end_time,   '%H:%M')
            hrs = (et - st).seconds / 3600
            if hrs <= 0:
                return jsonify({'error': '結束時間需晚於開始時間'}), 400
            total_days = round(hrs / 8, 2)
        except Exception:
            return jsonify({'error': '時間格式錯誤'}), 400
    else:
        total_days = _calc_leave_days(start_date, end_date, start_half, end_half)
        if total_days <= 0:
            return jsonify({'error': '請假天數不合理，請檢查日期'}), 400

    with get_db() as conn:
        # Check balance for types with limits
        lt = conn.execute("SELECT * FROM leave_types WHERE id=%s", (leave_type_id,)).fetchone()
        if lt and lt['max_days'] is not None:
            year = start_date[:4]
            # 確保餘額列存在，再用 FOR UPDATE 鎖定，防止 race condition
            conn.execute("""
                INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                VALUES (%s, %s, %s, 0, 0)
                ON CONFLICT (staff_id, leave_type_id, year) DO NOTHING
            """, (sid, leave_type_id, year))
            bal = conn.execute("""
                SELECT COALESCE(used_days,0) as used
                FROM leave_balances
                WHERE staff_id=%s AND leave_type_id=%s AND year=%s
                FOR UPDATE
            """, (sid, leave_type_id, year)).fetchone()
            used = float(bal['used']) if bal else 0.0
            if used + total_days > float(lt['max_days']):
                remaining = float(lt['max_days']) - used
                return jsonify({'error': f'{lt["name"]}剩餘 {remaining} 天，無法申請 {total_days} 天'}), 422

        row = conn.execute("""
            INSERT INTO leave_requests
              (staff_id, leave_type_id, start_date, end_date, start_half, end_half,
               start_time, end_time, total_days, reason, substitute_name, document_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (sid, leave_type_id, start_date, end_date, start_half, end_half,
              start_time, end_time, total_days, reason, substitute, document_id)).fetchone()
    return jsonify(leave_req_row(row)), 201

# ── Leave Balance ─────────────────────────────────────────────────

@bp.route('/api/leave/balances', methods=['GET'])
def api_leave_balances():
    """管理員和員工都可以查詢，員工只能查自己的"""
    year     = request.args.get('year', '')
    staff_id = request.args.get('staff_id', '')

    # 員工端：只能查自己
    if not session.get('logged_in'):
        sid = session.get('punch_staff_id')
        if not sid:
            return jsonify({'error': 'not logged in'}), 401
        staff_id = str(sid)   # 強制只查自己
    if not year:
        from datetime import date as _d2
        year = str(_d2.today().year)
    conds, params = ["lb.year=%s"], [int(year)]
    if staff_id: conds.append("lb.staff_id=%s"); params.append(int(staff_id))
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT lb.*, ps.name as staff_name, lt.name as leave_type_name,
                   lt.code as leave_code, lt.max_days, lt.color as leave_color
            FROM leave_balances lb
            JOIN punch_staff  ps ON ps.id=lb.staff_id
            JOIN leave_types  lt ON lt.id=lb.leave_type_id
            WHERE {' AND '.join(conds)}
            ORDER BY ps.name, lt.sort_order
        """, params).fetchall()
    result = []
    for r in rows:
        d = leave_balance_row(r)
        d['staff_name']      = r['staff_name']
        d['leave_type_name'] = r['leave_type_name']
        d['leave_code']      = r['leave_code']
        d['leave_color']     = r['leave_color']
        d['max_days']        = float(r['max_days']) if r['max_days'] is not None else None
        result.append(d)
    return jsonify(result)

@bp.route('/api/leave/balances/init', methods=['POST'])
@require_module('leave')
def api_leave_balance_init():
    """初始化/更新員工特休天數（依勞基法第38條，以到職日精確計算）"""
    b    = request.get_json(force=True)
    year = b.get('year', '')
    if not year:
        from datetime import date as _d3
        year = str(_d3.today().year)

    with get_db() as conn:
        staff_list = conn.execute(
            "SELECT id, name, hire_date FROM punch_staff WHERE active=TRUE AND (salary_type IS NULL OR salary_type != 'hourly')"
        ).fetchall()
        lt = conn.execute("SELECT id FROM leave_types WHERE code='annual'").fetchone()
        if not lt: return jsonify({'error': '找不到特休假類型'}), 404
        lt_id   = lt['id']
        updated = 0
        details = []

        for s in staff_list:
            days = _calc_annual_leave_days(s['hire_date'])
            # 未滿6個月的員工也記錄（0天），方便後續追蹤
            conn.execute("""
                INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                VALUES (%s,%s,%s,%s,0)
                ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
                  SET total_days=EXCLUDED.total_days, updated_at=NOW()
            """, (s['id'], lt_id, int(year), days))
            updated += 1
            details.append({
                'name':      s['name'],
                'hire_date': str(s['hire_date']) if s['hire_date'] else None,
                'days':      days,
            })

    return jsonify({'ok': True, 'updated': updated, 'year': year, 'details': details})


@bp.route('/api/leave/annual-schedule/<int:staff_id>', methods=['GET'])
@require_module('leave')
def api_annual_leave_schedule(staff_id):
    """回傳員工特休天數完整排程（各里程碑日期與天數）"""
    with get_db() as conn:
        staff = conn.execute(
            "SELECT name, hire_date FROM punch_staff WHERE id=%s", (staff_id,)
        ).fetchone()
    if not staff:
        return ('', 404)
    schedule = _calc_annual_leave_schedule(staff['hire_date'])
    current  = _calc_annual_leave_days(staff['hire_date'])
    return jsonify({
        'staff_id':      staff_id,
        'name':          staff['name'],
        'hire_date':     str(staff['hire_date']) if staff['hire_date'] else None,
        'current_days':  current,
        'schedule':      schedule,
    })


@bp.route('/api/leave/annual-schedule/public', methods=['GET'])
def api_annual_leave_schedule_public():
    """員工查看自己的特休排程"""
    sid = session.get('punch_staff_id')
    if not sid:
        return jsonify({'error': 'not logged in'}), 401
    with get_db() as conn:
        staff = conn.execute(
            "SELECT name, hire_date, salary_type FROM punch_staff WHERE id=%s", (sid,)
        ).fetchone()
    if not staff:
        return ('', 404)
    is_hourly = (staff.get('salary_type') or 'monthly') == 'hourly'
    schedule = [] if is_hourly else _calc_annual_leave_schedule(staff['hire_date'])
    current  = 0 if is_hourly else _calc_annual_leave_days(staff['hire_date'])
    return jsonify({
        'name':         staff['name'],
        'hire_date':    str(staff['hire_date']) if staff['hire_date'] else None,
        'salary_type':  staff.get('salary_type') or 'monthly',
        'current_days': current,
        'schedule':     schedule,
    })


@bp.route('/api/leave/balances/<int:bid>', methods=['PUT'])
@require_module('leave')
def api_leave_balance_update(bid):
    b = request.get_json(force=True)
    with get_db() as conn:
        row = conn.execute("""
            UPDATE leave_balances SET total_days=%s, used_days=%s, note=%s, updated_at=NOW()
            WHERE id=%s RETURNING *
        """, (float(b.get('total_days',0)), float(b.get('used_days',0)),
              b.get('note',''), bid)).fetchone()
    return jsonify(leave_balance_row(row)) if row else ('', 404)

# ── Leave Summary (for salary integration) ───────────────────────

@bp.route('/api/leave/summary/<int:staff_id>/<month>', methods=['GET'])
@require_module('leave')
def api_leave_summary(staff_id, month):
    """取得員工某月請假摘要（供薪資計算用）"""
    import calendar as _cals
    from datetime import date as _ds2, timedelta as _tds2
    y2, m2 = int(month[:4]), int(month[5:])
    mf = _ds2(y2, m2, 1)
    ml = _ds2(y2, m2, _cals.monthrange(y2, m2)[1])

    with get_db() as conn:
        rows = conn.execute("""
            SELECT lr.*, lt.name as leave_type_name, lt.code, lt.pay_rate
            FROM leave_requests lr
            JOIN leave_types lt ON lt.id=lr.leave_type_id
            WHERE lr.staff_id=%s
              AND lr.status='approved'
              AND lr.start_date <= %s AND lr.end_date >= %s
            ORDER BY lr.start_date
        """, (staff_id, ml, mf)).fetchall()

    def _days_in_month(r):
        sd = r['start_date'].date() if hasattr(r['start_date'], 'date') else _ds2.fromisoformat(str(r['start_date']))
        ed = r['end_date'].date()   if hasattr(r['end_date'],   'date') else _ds2.fromisoformat(str(r['end_date']))
        if sd >= mf and ed <= ml:
            return float(r['total_days'])
        cur, cnt = max(sd, mf), 0.0
        while cur <= min(ed, ml):
            if cur.weekday() != 6:
                cnt += 1.0
            cur += _tds2(days=1)
        return cnt

    total_leave_days = 0.0
    unpaid_days      = 0.0
    half_pay_days    = 0.0
    items = []
    for r in rows:
        d = _days_in_month(r)
        pay_r = float(r['pay_rate'])
        total_leave_days += d
        if pay_r == 0:   unpaid_days   += d
        elif pay_r < 1:  half_pay_days += d
        items.append({
            'leave_type': r['leave_type_name'],
            'code':       r['code'],
            'days':       d,
            'pay_rate':   pay_r,
            'start_date': r['start_date'].isoformat() if hasattr(r['start_date'], 'isoformat') else str(r['start_date']),
            'end_date':   r['end_date'].isoformat()   if hasattr(r['end_date'],   'isoformat') else str(r['end_date']),
        })
    return jsonify({
        'staff_id':         staff_id,
        'month':            month,
        'total_leave_days': total_leave_days,
        'unpaid_days':      unpaid_days,
        'half_pay_days':    half_pay_days,
        'items':            items,
    })

# ═══════════════════════════════════════════════════════════════════

@bp.route('/api/leave/upload-cert', methods=['POST'])
def api_leave_upload_cert():
    sid = session.get('punch_staff_id')
    if not sid: return jsonify({'error': '請先登入'}), 401
    file = request.files.get('file')
    if not file: return jsonify({'error': '請上傳圖片'}), 400
    raw = file.read()
    if len(raw) > 10 * 1024 * 1024:
        return jsonify({'error': '檔案不可超過 10MB'}), 400
    import base64 as _b64c
    image_data = 'data:' + (file.content_type or 'image/jpeg') + ';base64,' + _b64c.b64encode(raw).decode()
    try:
        with get_db() as conn:
            doc = conn.execute("""
                INSERT INTO finance_documents (filename, doc_type, image_data, upload_date)
                VALUES (%s, 'medical_cert', %s, CURRENT_DATE) RETURNING id
            """, (file.filename, image_data)).fetchone()
        return jsonify({'document_id': doc['id'], 'filename': file.filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/documents/<int:doc_id>/image', methods=['GET'])
def api_document_image(doc_id):
    """Return a simple HTML page embedding the stored image as a data URL."""
    if not (session.get('logged_in') or session.get('punch_staff_id')):
        return jsonify({'error': 'unauthorized'}), 401
    with get_db() as conn:
        doc = conn.execute("SELECT image_data, filename FROM finance_documents WHERE id=%s", (doc_id,)).fetchone()
    if not doc or not doc['image_data']:
        return jsonify({'error': '找不到圖片'}), 404
    from flask import Response
    fname = (doc['filename'] or '').replace('"', '')
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>{fname}</title>'
        '<style>body{margin:0;background:#111;display:flex;justify-content:center;align-items:flex-start}'
        'img{max-width:100%;height:auto}</style></head>'
        f'<body><img src="{doc["image_data"]}" alt="{fname}"></body></html>'
    )
    return Response(html, mimetype='text/html')


# ── Admin endpoints ─────────────────────────────────────────────
