import os
import secrets
from datetime import timedelta as _td, timezone as _tz

_raw_db_url = os.environ.get('DATABASE_URL', '')
DATABASE_URL = (
    _raw_db_url.replace('postgres://', 'postgresql://', 1)
    if _raw_db_url.startswith('postgres://')
    else _raw_db_url
)

SECRET_KEY                = os.environ.get('SECRET_KEY', secrets.token_hex(32))
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
ADMIN_PASSWORD            = os.environ.get('ADMIN_PASSWORD', 'admin123')
RENDER_EXTERNAL_URL       = os.environ.get('RENDER_EXTERNAL_URL', '')

TW_TZ       = _tz(_td(hours=8))
WEEKDAY_ZH  = ['一', '二', '三', '四', '五', '六', '日']

print(f"[startup] DATABASE_URL prefix: {DATABASE_URL[:20] if DATABASE_URL else 'NOT SET'}")
