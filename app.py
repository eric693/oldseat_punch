"""
oldseat_punch — thin entry point.
All business logic lives in routes/ blueprints and shared modules.
"""
import os
import threading
import time
import urllib.request
from datetime import date as _date, datetime as _dt, timedelta as _td

from flask import Flask

from config import SECRET_KEY, RENDER_EXTERNAL_URL, TW_TZ
from db_init import init_all
from db import get_db
from utils import _calc_annual_leave_days

# ── Create Flask app ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ── Register blueprints ───────────────────────────────────────────────────────

from routes.admin       import bp as admin_bp
from routes.punch       import bp as punch_bp
from routes.line_punch  import bp as line_punch_bp
from routes.schedule    import bp as schedule_bp
from routes.shifts      import bp as shifts_bp
from routes.overtime    import bp as overtime_bp
from routes.leave       import bp as leave_bp
from routes.salary      import bp as salary_bp
from routes.announcement import bp as announcement_bp
from routes.holiday     import bp as holiday_bp
from routes.export      import bp as export_bp
from routes.finance     import bp as finance_bp
from routes.training    import bp as training_bp
from routes.expense     import bp as expense_bp
from routes.performance import bp as performance_bp
from routes.mobile      import bp as mobile_bp
from routes.webauthn    import bp as webauthn_bp

app.register_blueprint(admin_bp)
app.register_blueprint(punch_bp)
app.register_blueprint(line_punch_bp)
app.register_blueprint(schedule_bp)
app.register_blueprint(shifts_bp)
app.register_blueprint(overtime_bp)
app.register_blueprint(leave_bp)
app.register_blueprint(salary_bp)
app.register_blueprint(announcement_bp)
app.register_blueprint(holiday_bp)
app.register_blueprint(export_bp)
app.register_blueprint(finance_bp)
app.register_blueprint(training_bp)
app.register_blueprint(expense_bp)
app.register_blueprint(performance_bp)
app.register_blueprint(mobile_bp)
app.register_blueprint(webauthn_bp)

# ── Health endpoint ───────────────────────────────────────────────────────────

@app.route('/health')
def health():
    from flask import jsonify
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return jsonify({'status': 'ok', 'db': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

# ── DB init ───────────────────────────────────────────────────────────────────

init_all()

# ── Background: keep-alive ────────────────────────────────────────────────────

def _keep_alive():
    time.sleep(10)
    while True:
        try:
            base = RENDER_EXTERNAL_URL.rstrip('/') if RENDER_EXTERNAL_URL else 'http://localhost:5000'
            urllib.request.urlopen(
                urllib.request.Request(f'{base}/health', headers={'User-Agent': 'KeepAlive/1.0'}),
                timeout=10
            )
        except Exception as e:
            print(f"[keep-alive] ping failed: {e}")
        time.sleep(14 * 60)

threading.Thread(target=_keep_alive, daemon=True).start()

# ── Background: 特休自動同步 ──────────────────────────────────────────────────

def _run_annual_leave_sync():
    """每日自動更新特休餘額（依勞基法第38條）"""
    year = str(_date.today().year)
    try:
        with get_db() as conn:
            staff_list = conn.execute(
                "SELECT id, name, hire_date FROM punch_staff WHERE active=TRUE AND hire_date IS NOT NULL AND (salary_type IS NULL OR salary_type != 'hourly')"
            ).fetchall()
            lt = conn.execute("SELECT id FROM leave_types WHERE code='annual'").fetchone()
            if not lt:
                return
            lt_id = lt['id']
            for s in staff_list:
                days = _calc_annual_leave_days(s['hire_date'])
                conn.execute("""
                    INSERT INTO leave_balances (staff_id, leave_type_id, year, total_days, used_days)
                    VALUES (%s,%s,%s,%s,0)
                    ON CONFLICT (staff_id, leave_type_id, year) DO UPDATE
                      SET total_days=EXCLUDED.total_days, updated_at=NOW()
                """, (s['id'], lt_id, int(year), days))
    except Exception as e:
        print(f"[annual_leave_sync] {e}")


def _annual_leave_sync_loop():
    import time as _t
    _t.sleep(10)
    _run_annual_leave_sync()
    while True:
        now    = _dt.now(TW_TZ)
        tmr    = (now + _td(days=1)).date()
        target = _dt(tmr.year, tmr.month, tmr.day, 0, 5, tzinfo=TW_TZ)
        secs   = (target - now).total_seconds()
        if secs < 0:
            secs = 3600
        _t.sleep(secs)
        _run_annual_leave_sync()

threading.Thread(target=_annual_leave_sync_loop, daemon=True).start()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
