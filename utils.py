"""Shared helpers used across multiple route modules."""
import json as _json
import math
from collections import defaultdict
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from linebot import LineBotApi
from linebot.models import TextSendMessage

from config import DATABASE_URL, TW_TZ
from db import get_db

# ── GPS ───────────────────────────────────────────────────────────────────────

def _gps_distance(lat1, lng1, lat2, lng2):
    R = 6371000
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lng2 - lng1) * p / 2) ** 2)
    return int(2 * R * math.asin(math.sqrt(a)))


# ── Row formatters ─────────────────────────────────────────────────────────────

def punch_staff_row(row):
    if not row: return None
    d = dict(row)
    d.pop('password_hash', None)
    if d.get('password_plain') is None: d['password_plain'] = ''
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('hire_date'):  d['hire_date']  = d['hire_date'].isoformat()
    if d.get('birth_date'): d['birth_date'] = d['birth_date'].isoformat()
    return d


def _parse_tw_datetime(s):
    """Parse datetime string treating naive strings as Taiwan time (UTC+8)."""
    if not s:
        return None
    dt = _dt.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TW_TZ)
    return dt


def punch_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['latitude', 'longitude']:
        if d.get(f) is not None: d[f] = float(d[f])
    for f in ['punched_at', 'created_at']:
        if d.get(f):
            from datetime import timezone as _utz
            dt = d[f]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_utz.utc)
            d[f] = dt.astimezone(TW_TZ).isoformat()
    return d


def loc_row(row):
    if not row: return None
    d = dict(row)
    for f in ['lat', 'lng']:
        if d.get(f) is not None: d[f] = float(d[f])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def wifi_network_row(row):
    if not row: return None
    d = dict(row)
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def punch_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('requested_at'): d['requested_at'] = d['requested_at'].isoformat()
    if d.get('reviewed_at'):  d['reviewed_at']  = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    return d


def ot_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('request_date'): d['request_date'] = d['request_date'].isoformat()
    if d.get('start_time'):   d['start_time']   = str(d['start_time'])[:5]
    if d.get('end_time'):     d['end_time']      = str(d['end_time'])[:5]
    if d.get('ot_pay'):       d['ot_pay']        = float(d['ot_pay'])
    if d.get('ot_hours'):     d['ot_hours']      = float(d['ot_hours'])
    if d.get('reviewed_at'):  d['reviewed_at']   = d['reviewed_at'].isoformat()
    if d.get('created_at'):   d['created_at']    = d['created_at'].isoformat()
    return d


def shift_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_time'): d['start_time'] = str(d['start_time'])[:5]
    if d.get('end_time'):   d['end_time']   = str(d['end_time'])[:5]
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def shift_assign_row(row):
    if not row: return None
    d = dict(row)
    if d.get('shift_date'): d['shift_date'] = d['shift_date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def sched_req_row(row):
    if not row: return None
    d = dict(row)
    if isinstance(d.get('dates'), str):
        try: d['dates'] = _json.loads(d['dates'])
        except: d['dates'] = []
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d


def leave_type_row(row):
    if not row: return None
    d = dict(row)
    if d.get('max_days') is not None: d['max_days'] = float(d['max_days'])
    if d.get('pay_rate') is not None: d['pay_rate'] = float(d['pay_rate'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def leave_req_row(row):
    if not row: return None
    d = dict(row)
    if d.get('start_date'): d['start_date'] = d['start_date'].isoformat()
    if d.get('end_date'):   d['end_date']   = d['end_date'].isoformat()
    if d.get('total_days'): d['total_days'] = float(d['total_days'])
    if d.get('start_time'): d['start_time'] = str(d['start_time'])[:5]
    if d.get('end_time'):   d['end_time']   = str(d['end_time'])[:5]
    if d.get('reviewed_at'): d['reviewed_at'] = d['reviewed_at'].isoformat()
    if d.get('created_at'):  d['created_at']  = d['created_at'].isoformat()
    if d.get('updated_at'):  d['updated_at']  = d['updated_at'].isoformat()
    return d


def leave_balance_row(row):
    if not row: return None
    d = dict(row)
    if d.get('total_days') is not None: d['total_days'] = float(d['total_days'])
    if d.get('used_days')  is not None: d['used_days']  = float(d['used_days'])
    if d.get('updated_at'): d['updated_at'] = d['updated_at'].isoformat()
    return d


def salary_item_row(row):
    if not row: return None
    d = dict(row)
    if d.get('amount') is not None: d['amount'] = float(d['amount'])
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


def salary_record_row(row):
    if not row: return None
    d = dict(row)
    for f in ['base_salary', 'insured_salary', 'work_days', 'actual_days', 'leave_days',
              'unpaid_days', 'ot_pay', 'allowance_total', 'deduction_total', 'net_pay']:
        if d.get(f) is not None: d[f] = float(d[f])
    if isinstance(d.get('items'), str):
        try: d['items'] = _json.loads(d['items'])
        except: d['items'] = []
    w = float(d.get('work_days') or 0)
    l = float(d.get('leave_days') or 0)
    a = float(d.get('actual_days') or 0)
    d['absent_days'] = max(0.0, round(w - l - a, 1))
    items = d.get('items') or []
    hourly_item = next((i for i in items if i.get('id') == 'hourly_base'), None)
    d['hourly_base_pay'] = float(hourly_item['amount']) if hourly_item else 0.0
    if d.get('pay_date'):     d['pay_date']     = d['pay_date'].isoformat()
    if d.get('confirmed_at'): d['confirmed_at'] = d['confirmed_at'].isoformat()
    if d.get('created_at'):   d['created_at']   = d['created_at'].isoformat()
    if d.get('updated_at'):   d['updated_at']   = d['updated_at'].isoformat()
    return d


def holiday_row(row):
    if not row: return None
    d = dict(row)
    if d.get('date'):       d['date']       = d['date'].isoformat()
    if d.get('created_at'): d['created_at'] = d['created_at'].isoformat()
    return d


# ── Schedule helpers ───────────────────────────────────────────────────────────

def get_schedule_config(conn, month):
    row = conn.execute("SELECT * FROM schedule_config WHERE month=%s", (month,)).fetchone()
    if not row:
        return {'month': month, 'max_off_per_day': 2, 'vacation_quota': 8, 'notes': ''}
    return dict(row)


def get_off_counts(conn, month):
    rows = conn.execute("""
        SELECT elem as d, COUNT(*) as cnt
        FROM schedule_requests,
             jsonb_array_elements_text(dates) as elem
        WHERE month=%s AND status IN ('approved','pending')
        GROUP BY elem
    """, (month,)).fetchall()
    return {r['d']: int(r['cnt']) for r in rows}


# ── Punch shift helpers ────────────────────────────────────────────────────────

def _shift_aware_day_map(raw_punches, tz):
    """
    把原始打卡記錄依「上班日期」分組，完整支援跨日班次。
    """
    from datetime import timezone as _tz0

    result = defaultdict(lambda: {
        'ins': [], 'outs': [], 'break_outs': [], 'break_ins': [], 'has_manual': False
    })
    last_in = {}

    for r in raw_punches:
        pa = r['punched_at']
        if pa.tzinfo is None:
            pa = pa.replace(tzinfo=_tz0.utc)
        pa_tw = pa.astimezone(tz)
        sid   = r['staff_id']
        ptype = r['punch_type']

        if ptype == 'in':
            work_date    = pa_tw.date()
            last_in[sid] = pa_tw
        else:
            prev_in = last_in.get(sid)
            if prev_in is not None and 0 < (pa_tw - prev_in).total_seconds() <= 28 * 3600:
                work_date = prev_in.date()
            else:
                work_date = pa_tw.date()

        key    = (sid, work_date.isoformat())
        bucket = result[key]
        if r.get('is_manual'):
            bucket['has_manual'] = True

        if   ptype == 'in':        bucket['ins'].append(pa_tw)
        elif ptype == 'out':       bucket['outs'].append(pa_tw)
        elif ptype == 'break_out': bucket['break_outs'].append(pa_tw)
        elif ptype == 'break_in':  bucket['break_ins'].append(pa_tw)

    return result


def _build_shift_time_map(conn, month, staff_ids=None):
    """
    回傳排班時段對照表。
    格式: {(staff_id, 'YYYY-MM-DD'): (shift_start_aware, shift_end_aware)}
    """
    from datetime import timedelta as _tdsh, datetime as _dtsh
    where = "TO_CHAR(sa.shift_date,'YYYY-MM')=%s"
    params = [month]
    if staff_ids:
        ph = ','.join(['%s'] * len(staff_ids))
        where += f" AND sa.staff_id IN ({ph})"
        params.extend(staff_ids)
    sr_rows = conn.execute(f"""
        SELECT sa.staff_id, sa.shift_date, st.start_time, st.end_time
        FROM shift_assignments sa
        JOIN shift_types st ON st.id=sa.shift_type_id
        WHERE {where}
    """, params).fetchall()
    shift_map = {}
    for sr in sr_rows:
        sd = sr['shift_date']
        if not hasattr(sd, 'year'):
            from datetime import date as _dsh2
            sd = _dsh2.fromisoformat(str(sd))
        ds = sd.isoformat()
        st_t, et_t = sr['start_time'], sr['end_time']
        s_start = _dtsh(sd.year, sd.month, sd.day, st_t.hour, st_t.minute, tzinfo=TW_TZ)
        if et_t < st_t:
            nd = sd + _tdsh(days=1)
            s_end = _dtsh(nd.year, nd.month, nd.day, et_t.hour, et_t.minute, tzinfo=TW_TZ)
        else:
            s_end = _dtsh(sd.year, sd.month, sd.day, et_t.hour, et_t.minute, tzinfo=TW_TZ)
        shift_map[(sr['staff_id'], ds)] = (s_start, s_end)
    return shift_map


def _clamp_to_shift(actual_start, actual_end, shift_map, staff_id, date_str):
    """依排班表夾住實際打卡時間：早到不提前算、晚走不延後算。"""
    key = (staff_id, date_str)
    if key not in shift_map:
        return actual_start, actual_end
    s_start, s_end = shift_map[key]
    ws = max(actual_start, s_start)
    we = min(actual_end, s_end)
    if ws >= we:
        return None, None
    return ws, we


def _calc_punch_hours(conn, staff_id, month):
    """
    從打卡記錄計算實際工時（時薪制用），支援跨日班次。
    回傳 (total_hours, work_days, details)
    """
    from datetime import timezone as _tzh, timedelta as _tdh
    TW = _tzh(_tdh(hours=8))

    rows = conn.execute("""
        SELECT %s as staff_id, punch_type, punched_at, is_manual
        FROM punch_records
        WHERE staff_id=%s
          AND punched_at AT TIME ZONE 'Asia/Taipei' >=
              date_trunc('month', TO_DATE(%s,'YYYY-MM')) - INTERVAL '1 day'
          AND punched_at AT TIME ZONE 'Asia/Taipei' <
              date_trunc('month', TO_DATE(%s,'YYYY-MM')) + INTERVAL '1 month 2 days'
        ORDER BY punched_at ASC
    """, (staff_id, staff_id, month, month)).fetchall()

    shift_map = _build_shift_time_map(conn, month, staff_ids=[staff_id])
    day_map   = _shift_aware_day_map(rows, TW)

    total_hours = 0.0
    details     = []
    for (sid, ds), bucket in sorted(day_map.items()):
        if not ds.startswith(month):
            continue
        ins   = bucket['ins']
        outs  = bucket['outs']
        b_out = bucket['break_outs']
        b_in  = bucket['break_ins']

        if not ins or not outs:
            continue

        work_start, work_end = _clamp_to_shift(min(ins), max(outs), shift_map, sid, ds)
        if work_start is None:
            continue

        gross_mins = (work_end - work_start).total_seconds() / 60

        break_mins = 0.0
        for bo in b_out:
            matched = [bi for bi in b_in if bi > bo]
            if matched:
                break_mins += (min(matched) - bo).total_seconds() / 60
        if gross_mins >= 540:
            break_mins = max(break_mins, 60.0)
        elif gross_mins >= 240:
            break_mins = max(break_mins, 30.0)

        net_mins = max(0.0, gross_mins - break_mins)
        net_hrs  = round(net_mins / 60, 2)
        total_hours += net_hrs
        details.append({
            'date':       ds,
            'clock_in':   work_start.strftime('%H:%M'),
            'clock_out':  work_end.strftime('%H:%M'),
            'break_mins': round(break_mins),
            'net_hours':  net_hrs,
        })

    work_days = len([k for k in day_map if k[0] == staff_id and k[1].startswith(month)])
    return round(total_hours, 2), work_days, details


# ── Leave calculations ─────────────────────────────────────────────────────────

def _calc_annual_leave_days(hire_date_str, ref_date_str=None):
    """勞基法第38條特休天數計算（2017年修正版）"""
    if not hire_date_str:
        return 0
    from datetime import date as _date
    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return 0

    ref = _date.today()
    if ref_date_str:
        try:
            ref = _date.fromisoformat(str(ref_date_str))
        except Exception:
            pass

    months = (ref.year - hire.year) * 12 + (ref.month - hire.month)
    if ref.day < hire.day:
        months -= 1
    if months < 0:
        months = 0

    years_complete = months // 12

    if months < 6:
        return 0
    elif months < 12:
        return 3
    elif years_complete < 2:
        return 7
    elif years_complete < 3:
        return 10
    elif years_complete < 5:
        return 14
    elif years_complete < 10:
        return 15
    else:
        extra = years_complete - 9
        return min(15 + extra, 30)


def _calc_annual_leave_schedule(hire_date_str):
    """回傳員工特休天數完整排程表，供前端顯示用。"""
    if not hire_date_str:
        return []
    from datetime import date as _date
    import calendar as _cal

    try:
        hire = _date.fromisoformat(str(hire_date_str))
    except Exception:
        return []

    today = _date.today()
    milestones = [
        (6,   3,  '滿6個月'),
        (12,  7,  '滿1年'),
        (24, 10,  '滿2年'),
        (36, 14,  '滿3年'),
        (60, 15,  '滿5年'),
        (120,16,  '滿10年'),
        (132,17,  '滿11年'),
        (144,18,  '滿12年'),
        (156,19,  '滿13年'),
        (168,20,  '滿14年'),
        (180,21,  '滿15年'),
        (192,22,  '滿16年'),
        (204,23,  '滿17年'),
        (216,24,  '滿18年'),
        (228,25,  '滿19年'),
        (240,30,  '滿20年（上限30天）'),
    ]

    result       = []
    current_days = _calc_annual_leave_days(hire_date_str)

    for months_needed, days, label in milestones:
        total_m = hire.month + months_needed
        y = hire.year + (total_m - 1) // 12
        m = (total_m - 1) % 12 + 1
        max_day = _cal.monthrange(y, m)[1]
        try:
            reached = _date(y, m, min(hire.day, max_day))
        except Exception:
            continue
        result.append({
            'label':        label,
            'days':         days,
            'date_reached': reached.isoformat(),
            'is_past':      reached <= today,
            'is_current':   (days == current_days and reached <= today),
        })
    return result


def _calc_leave_days(start_date_str, end_date_str, start_half=False, end_half=False):
    """計算請假天數（含半天選項），排除週日"""
    from datetime import date as _date, timedelta as _tdd
    try:
        s = _date.fromisoformat(start_date_str)
        e = _date.fromisoformat(end_date_str)
    except Exception:
        return 0.0
    if e < s: return 0.0
    days = 0.0
    cur  = s
    while cur <= e:
        if cur.weekday() != 6:
            if cur == s and cur == e:
                if start_half and end_half:
                    days += 1.0
                elif start_half or end_half:
                    days += 0.5
                else:
                    days += 1.0
            elif cur == s and start_half:
                days += 0.5
            elif cur == e and end_half:
                days += 0.5
            else:
                days += 1.0
        cur += _tdd(days=1)
    return days


# ── Overtime calculations ──────────────────────────────────────────────────────

def _calc_ot_pay(staff_row, ot_hours, day_type='weekday'):
    salary_type = staff_row.get('salary_type', 'monthly') or 'monthly'
    base_salary = float(staff_row.get('base_salary')  or 0)
    hourly_rate = float(staff_row.get('hourly_rate')  or 0)
    daily_hours = float(staff_row.get('daily_hours')  or 8)
    ot_rate1    = float(staff_row.get('ot_rate1')     or 1.33)
    ot_rate2    = float(staff_row.get('ot_rate2')     or 1.67)
    ot_rate3    = float(staff_row.get('ot_rate3')     or 2.0)

    if salary_type == 'hourly':
        base_hourly = hourly_rate
    else:
        base_hourly = base_salary / 30 / daily_hours if (base_salary and daily_hours) else 0

    if base_hourly <= 0:
        return 0.0, base_hourly

    h = float(ot_hours)
    if day_type in ('holiday', 'special'):
        pay = round(base_hourly * h * 2.0, 0)
    elif day_type == 'rest_day':
        if h <= 4:    billed = 4.0
        elif h <= 8:  billed = 8.0
        elif h <= 12: billed = 12.0
        else:         billed = h
        h1 = min(billed, 2.0); h2 = min(max(0.0, billed - 2.0), 6.0); h3 = max(0.0, billed - 8.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2 + h3 * ot_rate3), 0)
    else:
        h1 = min(h, 2.0); h2 = min(max(0.0, h - 2.0), 2.0); h3 = max(0.0, h - 4.0)
        pay = round(base_hourly * (h1 * ot_rate1 + h2 * ot_rate2 + h3 * ot_rate3), 0)

    return pay, base_hourly


# ── Salary helpers ─────────────────────────────────────────────────────────────

def _get_salary_config(conn=None):
    """讀取薪資結算設定，回傳 {'settlement_day': int, 'pay_day': int}"""
    def _query(c):
        row = c.execute("SELECT * FROM salary_config WHERE id=1").fetchone()
        if not row:
            return {'settlement_day': 1, 'pay_day': 5}
        return {
            'settlement_day': int(row['settlement_day'] or 1),
            'pay_day':        int(row['pay_day']        or 5),
        }
    if conn:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _eval_formula(formula, base_salary, insured_salary, service_years):
    """安全計算薪資公式（使用 AST，禁止任意程式碼執行）"""
    import ast as _ast
    import operator as _op
    if not formula: return 0.0
    _vars = {
        'base_salary':    float(base_salary or 0),
        'insured_salary': float(insured_salary or 0),
        'service_years':  float(service_years or 0),
    }
    _ops = {
        _ast.Add:  _op.add,  _ast.Sub: _op.sub,
        _ast.Mult: _op.mul,  _ast.Div: _op.truediv,
        _ast.Pow:  _op.pow,  _ast.Mod: _op.mod,
        _ast.USub: _op.neg,  _ast.UAdd: _op.pos,
    }
    def _safe_eval(node):
        if isinstance(node, _ast.Constant):
            if not isinstance(node.value, (int, float)):
                raise ValueError('非數字常數')
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id not in _vars:
                raise ValueError(f'未知變數: {node.id}')
            return _vars[node.id]
        if isinstance(node, _ast.BinOp):
            fn = _ops.get(type(node.op))
            if not fn: raise ValueError('不支援的運算子')
            return fn(_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, _ast.UnaryOp):
            fn = _ops.get(type(node.op))
            if not fn: raise ValueError('不支援的一元運算子')
            return fn(_safe_eval(node.operand))
        raise ValueError(f'不支援的語法: {type(node).__name__}')
    try:
        tree = _ast.parse(formula.strip(), mode='eval')
        return round(float(_safe_eval(tree.body)), 2)
    except Exception:
        return 0.0


def _calc_service_years(hire_date_str):
    if not hire_date_str: return 0.0
    from datetime import date as _d4
    try:
        hire = _d4.fromisoformat(str(hire_date_str))
        return round((_d4.today() - hire).days / 365.25, 2)
    except Exception:
        return 0.0


# ── Holiday ────────────────────────────────────────────────────────────────────

def _is_holiday(conn, date_str):
    """Check if a date is a public holiday"""
    row = conn.execute(
        "SELECT id FROM public_holidays WHERE date=%s", (date_str,)
    ).fetchone()
    return row is not None


# ── LINE notification helpers ─────────────────────────────────────────────────

_reply_token_map: dict = {}   # {line_user_id: reply_token} — consumed on first use


def get_line_punch_config():
    if not DATABASE_URL: return None
    try:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _send_line_punch(user_id, text):
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    try:
        api   = LineBotApi(cfg['channel_access_token'])
        token = _reply_token_map.pop(user_id, None)
        if token:
            api.reply_message(token, TextSendMessage(text=text))
        else:
            api.push_message(user_id, TextSendMessage(text=text))
    except Exception as e:
        print(f"[LINE PUNCH] send_message error: {e}")


def _send_line_with_quick_reply(user_id, text, items):
    """Send a message with Quick Reply buttons."""
    from linebot.models import QuickReply, QuickReplyButton, MessageAction
    cfg = get_line_punch_config()
    if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
        return
    qr_items = [
        QuickReplyButton(action=MessageAction(label=it['label'][:20], text=it['text']))
        for it in items[:13]
    ]
    msg = TextSendMessage(text=text, quick_reply=QuickReply(items=qr_items))
    try:
        api   = LineBotApi(cfg['channel_access_token'])
        token = _reply_token_map.pop(user_id, None)
        if token:
            api.reply_message(token, msg)
        else:
            api.push_message(user_id, msg)
    except Exception as e:
        print(f"[LINE PUNCH] send_message (qr) error: {e}")


def _notify_staff_line(staff_id, message):
    """Send LINE notification to a staff member if they have LINE bound."""
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            staff = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE id=%s", (staff_id,)
            ).fetchone()
            if not staff or not staff['line_user_id']:
                return
            cfg = conn.execute(
                "SELECT * FROM line_punch_config WHERE id=1"
            ).fetchone()
        if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
            return
        LineBotApi(cfg['channel_access_token']).push_message(
            staff['line_user_id'],
            TextSendMessage(text=message)
        )
    except Exception as e:
        print(f"[LINE notify] staff_id={staff_id}: {e}")


def _notify_review_result(staff_id, category, action, extra_info=''):
    """Send formatted LINE notification for review results."""
    ACTION_LABEL = {'approved': '核准', 'rejected': '退回', 'confirmed': '確認'}
    ACTION_ICON  = {'approved': '[核准]', 'rejected': '[退回]', 'confirmed': '[確認]'}
    label = ACTION_LABEL.get(action, action)
    icon  = ACTION_ICON.get(action, '')
    msg   = f"{icon} {category}{label}\n{extra_info}\n\n請至員工系統查看詳情。"
    _notify_staff_line(staff_id, msg.strip())


def _broadcast_announcement_line(title, content):
    """廣播公告給所有已綁定 LINE 的在職員工"""
    try:
        with get_db() as conn:
            cfg = conn.execute("SELECT * FROM line_punch_config WHERE id=1").fetchone()
            if not cfg or not cfg.get('enabled') or not cfg.get('channel_access_token'):
                return
            staff_rows = conn.execute(
                "SELECT line_user_id FROM punch_staff WHERE active=TRUE AND line_user_id IS NOT NULL"
            ).fetchall()
        if not staff_rows:
            return
        api     = LineBotApi(cfg['channel_access_token'])
        snippet = content[:60] + ('…' if len(content) > 60 else '')
        msg     = f"[公告] {title}\n{snippet}\n\n請至員工系統查看完整公告。"
        for s in staff_rows:
            try:
                api.push_message(s['line_user_id'], TextSendMessage(text=msg))
            except Exception as e:
                print(f"[LINE broadcast] {s['line_user_id']}: {e}")
    except Exception as e:
        print(f"[LINE broadcast] error: {e}")
