"""Project time helpers fixed to Beijing time."""
import datetime


BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")


def now_bj():
    return datetime.datetime.now(BEIJING_TZ).replace(tzinfo=None)


def today_bj():
    return now_bj().date()


def fromtimestamp_bj(timestamp):
    return datetime.datetime.fromtimestamp(float(timestamp), BEIJING_TZ).replace(tzinfo=None)


def format_dt_bj(dt, fmt="%Y-%m-%d %H:%M:%S"):
    if not dt:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return dt.strftime(fmt)
