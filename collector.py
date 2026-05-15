"""赔率数据采集器 — 支持多数据源"""
import datetime
import concurrent.futures
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from sqlalchemy.orm import Session
from models import Match, OddsChange, SessionLocal
from time_utils import BEIJING_TZ, fromtimestamp_bj, now_bj, today_bj
from config import (
    API_TIMEOUT, MAX_RETRIES, RETRY_BACKOFF,
    DATA_SOURCE_STATE_FILE, DEFAULT_DATA_SOURCE,
    SPORTTERY_API_BASE, OKOOO_JINGCAI_URL,
    ODDS_API_IO_BASE, ODDS_API_IO_KEY, ODDS_API_IO_KEY_FILE,
    ODDS_API_IO_BOOKMAKERS, ODDS_API_IO_EVENT_LIMIT, ODDS_API_IO_CACHE_TTL,
    ODDS_API_IO_BATCH_SIZE, ODDS_API_IO_FREE_REQUESTS_PER_HOUR,
    ODDS_API_IO_MAX_REQUESTS_PER_CYCLE, ODDS_API_IO_MIN_INTERVAL,
    ODDS_API_IO_ODDS_STALE_AFTER, ODDS_API_IO_REQUEST_RESERVE,
    ODDS_API_IO_STATE_FILE,
)

# 数据源配置
DATA_SOURCE = DEFAULT_DATA_SOURCE  # 可选: "nowscore_crown", "odds_api_io", "okooo", "20002028", "mock"
REAL_API_BASE = "https://www.20002028.xyz/jingcai/api"

SOURCE_OPTIONS = {
    "20002028": {
        "name": "主接口",
        "description": "原 20002028 竞彩接口",
        "requires_key": False,
    },
    "odds_api_io": {
        "name": "澳客+欧赔",
        "description": "澳客竞彩赛程 + Odds-API.io 欧赔",
        "requires_key": True,
    },
    "nowscore_crown": {
        "name": "捷报皇冠+竞彩",
        "description": "捷报竞彩赛程 + nowscore Crown 欧赔",
        "requires_key": False,
    },
    "okooo": {
        "name": "澳客竞彩",
        "description": "澳客竞彩赛程与胜平负赔率",
        "requires_key": False,
    },
    "mock": {
        "name": "模拟数据",
        "description": "本地模拟数据，用于断网演示",
        "requires_key": False,
    },
}

_odds_api_io_cache = {"expires_at": 0, "matches": None}
_nowscore_crown_cache = {"expires_at": 0, "matches": None}
CHINA_TZ = BEIJING_TZ
OKOOO_HISTORY_DAYS = int(os.environ.get("OKOOO_HISTORY_DAYS", "1"))
OKOOO_FUTURE_DAYS = int(os.environ.get("OKOOO_FUTURE_DAYS", "2"))
NOWSCORE_CROWN_CACHE_TTL = int(os.environ.get("NOWSCORE_CROWN_CACHE_TTL", "60"))
NOWSCORE_CP_URL = os.environ.get("NOWSCORE_CP_URL", "https://cp.nowscore.com/").strip()
NOWSCORE_1X2_BASE = os.environ.get("NOWSCORE_1X2_BASE", "https://1x2.nowscore.com").rstrip("/")
NOWSCORE_CROWN_WORKERS = max(1, int(os.environ.get("NOWSCORE_CROWN_WORKERS", "6")))


class OddsApiBudgetError(Exception):
    """Raised when Odds-API.io should not be called under the free plan budget."""


class ExternalApiError(Exception):
    """External API request failed with context safe for logs."""


def _redact_url(url):
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, "***" if key.lower() in {"apikey", "api_key", "key", "token"} else value)
        for key, value in query
    ]
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(redacted),
        parsed.fragment,
    ))


def _http_error_message(service, url, error):
    try:
        body = error.read(500).decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    message = f"{service} HTTP {error.code} {error.reason or ''}".strip()
    message = f"{message}: {_redact_url(url)}"
    if body:
        message = f"{message} | {body}"
    return message


def _load_data_source():
    default_source = DEFAULT_DATA_SOURCE if DEFAULT_DATA_SOURCE in SOURCE_OPTIONS else "nowscore_crown"
    try:
        with open(DATA_SOURCE_STATE_FILE, encoding="utf-8") as f:
            source = json.load(f).get("source")
        return source if source in SOURCE_OPTIONS else default_source
    except (OSError, ValueError, TypeError):
        return default_source


def get_data_source():
    global DATA_SOURCE
    DATA_SOURCE = _load_data_source()
    return DATA_SOURCE


def set_data_source(source: str):
    global DATA_SOURCE
    if source not in SOURCE_OPTIONS:
        raise ValueError(f"未知数据源: {source}")
    DATA_SOURCE = source
    with open(DATA_SOURCE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"source": source}, f, ensure_ascii=False, indent=2)
    return DATA_SOURCE


def get_source_options():
    active = get_data_source()
    odds_api_io_configured = bool(_get_odds_api_io_key())
    return {
        "active": active,
        "sources": [
            {
                "key": key,
                "active": key == active,
                "configured": key != "odds_api_io" or odds_api_io_configured,
                **meta,
            }
            for key, meta in SOURCE_OPTIONS.items()
        ],
    }


def fetch_from_20002028():
    """从 20002028.xyz API 获取真实赔率数据（直连+指数退避重试）"""
    import ssl
    import urllib.request
    import json

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # 构建无代理 opener，绕过系统代理加速连接
    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(opener)

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{REAL_API_BASE}/matches",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
                matches_data = json.loads(resp.read().decode())

            if isinstance(matches_data, list):
                elapsed = RETRY_BACKOFF[attempt - 1] if attempt > 0 else 0
                if attempt > 0:
                    print(f"  API 第{attempt + 1}次重试成功")
                return matches_data
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                print(f"  API 第{attempt + 1}次失败，{delay}s 后重试... ({e})")
                time.sleep(delay)
            else:
                print(f"  API 全部 {MAX_RETRIES} 次请求失败: {e}")
    return None


def fetch_status_from_20002028():
    """获取源站采集状态，用于对齐休息/刷新展示。"""
    import ssl
    import urllib.request
    import json

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    proxy_handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)
    urllib.request.install_opener(opener)

    try:
        req = urllib.request.Request(
            f"{REAL_API_BASE}/status",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
            status = json.loads(resp.read().decode())
        return status if isinstance(status, dict) else None
    except Exception:
        return None


def collect_real_data(fetcher=fetch_from_20002028):
    """采集真实赔率数据"""
    db = SessionLocal()
    try:
        matches_data = fetcher()
        if matches_data is None:
            return False

        now = now_bj()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # 数据新鲜度检查：如果所有 odds_time 都超过 2 小时，提示可能未更新
        fresh_count = 0
        for m in matches_data:
            ot = m.get("odds_time", "")
            try:
                ot_dt = datetime.datetime.strptime(ot, "%Y-%m-%d %H:%M:%S")
                if (now - ot_dt).total_seconds() < 7200:
                    fresh_count += 1
            except (ValueError, TypeError):
                pass
        if fresh_count == 0 and len(matches_data) > 0:
            sample_ot = matches_data[0].get("odds_time", "N/A")
            print(f"  ⚠️ 数据可能未更新（odds_time={sample_ot}），继续采集")

        existing = {}
        for m in db.query(Match).all():
            key = (m.match_date, m.match_no, (m.start_time or "")[:5])
            existing[key] = m
        updated_count = 0
        seen_keys = set()
        is_full_snapshot = any(m.get("full_snapshot") for m in matches_data)
        preserve_missing = any(m.get("preserve_missing") for m in matches_data)

        for m in matches_data:
            match_no = m.get("match_no", "")
            league = m.get("league", "")
            home = m.get("home_team", "")
            away = m.get("away_team", "")
            match_date = m.get("real_date") or m.get("match_date", "")
            start_time = m.get("start_time", "")
            seen_keys.add((match_date, match_no, (start_time or "")[:5]))
            home_odds = m.get("home_odds")
            draw_odds = m.get("draw_odds")
            away_odds = m.get("away_odds")
            jc_home = m.get("jingcai_home")
            jc_draw = m.get("jingcai_draw")
            jc_away = m.get("jingcai_away")
            odds_time = m.get("odds_time", time_str)

            match = existing.get((match_date, match_no, (start_time or "")[:5]))
            if match:
                old_home = match.home_odds or 0
                old_draw = match.draw_odds or 0
                old_away = match.away_odds or 0
                clear_stale_odds = bool(m.get("clear_stale_odds"))
                should_clear_odds = False
                if clear_stale_odds and not (home_odds and draw_odds and away_odds):
                    if m.get("force_clear_odds"):
                        should_clear_odds = True
                    else:
                        try:
                            old_ot = datetime.datetime.strptime((match.odds_time or "")[:19], "%Y-%m-%d %H:%M:%S")
                            should_clear_odds = (now - old_ot).total_seconds() > ODDS_API_IO_ODDS_STALE_AFTER
                        except (TypeError, ValueError):
                            should_clear_odds = True

                if should_clear_odds:
                    new_home_odds = new_draw_odds = new_away_odds = None
                    new_odds_time = odds_time or time_str
                else:
                    new_home_odds = home_odds if home_odds is not None else match.home_odds
                    new_draw_odds = draw_odds if draw_odds is not None else match.draw_odds
                    new_away_odds = away_odds if away_odds is not None else match.away_odds
                    if home_odds and draw_odds and away_odds:
                        new_odds_time = odds_time
                    elif clear_stale_odds and not (match.home_odds and match.draw_odds and match.away_odds):
                        new_odds_time = odds_time or time_str
                    else:
                        new_odds_time = match.odds_time

                # 检测赔率变化
                for opt_name, old_val, new_val in [
                    ("主胜变化", old_home, home_odds),
                    ("平局变化", old_draw, draw_odds),
                    ("客胜变化", old_away, away_odds),
                ]:
                    if old_val and new_val and abs(old_val - new_val) > 0.005:
                        change = OddsChange(
                            time=time_str,
                            league=league,
                            home=home,
                            away=away,
                            change_type=opt_name,
                            from_odds=round(old_val, 2),
                            to_odds=round(new_val, 2),
                            odds=f"{home_odds:.2f}/{draw_odds:.2f}/{away_odds:.2f}" if home_odds else "",
                        )
                        db.add(change)

                match.league = league
                match.home_team = home
                match.away_team = away
                match.status = m.get("status", start_time) or start_time
                match.home_odds = new_home_odds
                match.draw_odds = new_draw_odds
                match.away_odds = new_away_odds
                match.jingcai_home = jc_home
                match.jingcai_draw = jc_draw
                match.jingcai_away = jc_away
                match.odds_time = new_odds_time or odds_time or time_str
                match.match_date = match_date
                match.start_time = start_time
                updated_count += 1
            else:
                match = Match(
                    match_no=match_no,
                    league=league,
                    home_team=home,
                    away_team=away,
                    match_date=match_date,
                    start_time=start_time,
                    status=m.get("status", start_time) or start_time,
                    home_odds=home_odds,
                    draw_odds=draw_odds,
                    away_odds=away_odds,
                    jingcai_home=jc_home,
                    jingcai_draw=jc_draw,
                    jingcai_away=jc_away,
                    odds_time=odds_time or time_str,
                )
                db.add(match)
                updated_count += 1

        if is_full_snapshot:
            cutoff = now - datetime.timedelta(hours=2)
            stale_cutoff = now - datetime.timedelta(hours=24)
            removed_count = 0
            for match in db.query(Match).all():
                key = (match.match_date, match.match_no, (match.start_time or "")[:5])
                if key in seen_keys:
                    continue
                try:
                    kickoff = datetime.datetime.strptime(
                        f"{(match.match_date or '')[:10]} {(match.start_time or '00:00')[:5]}",
                        "%Y-%m-%d %H:%M",
                    )
                except (TypeError, ValueError):
                    continue
                if preserve_missing:
                    should_remove = kickoff < stale_cutoff
                else:
                    should_remove = kickoff >= cutoff
                if should_remove:
                    db.delete(match)
                    removed_count += 1
            if removed_count:
                action = "清理过期历史比赛" if preserve_missing else "清理非当前竞彩快照比赛"
                print(f"  {action}: {removed_count} 场")

        duplicate_groups = {}
        for match in db.query(Match).all():
            key = (
                match.match_date,
                match.match_no,
                (match.start_time or "")[:5],
                match.home_team,
                match.away_team,
            )
            duplicate_groups.setdefault(key, []).append(match)
        deduped_count = 0
        for rows in duplicate_groups.values():
            if len(rows) <= 1:
                continue
            rows.sort(
                key=lambda row: (
                    bool(row.home_odds and row.draw_odds and row.away_odds),
                    row.odds_time or "",
                    row.id or 0,
                ),
                reverse=True,
            )
            for duplicate in rows[1:]:
                db.delete(duplicate)
                deduped_count += 1
        if deduped_count:
            print(f"  清理重复比赛记录: {deduped_count} 条")

        db.commit()
        print(f"[{time_str}] 采集完成: {len(matches_data)} 场比赛, {updated_count} 条更新")
        return True
    except Exception as e:
        db.rollback()
        print(f"采集错误: {e}")
        return False
    finally:
        db.close()


def _load_odds_api_io_state():
    default = {
        "request_times": [],
        "last_attempt_at": 0,
        "last_success_at": 0,
        "cooldown_until": 0,
        "last_status": "idle",
        "last_message": "",
        "rate_limit_limit": None,
        "rate_limit_remaining": None,
        "rate_limit_reset": None,
    }
    try:
        with open(ODDS_API_IO_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            default.update(state)
    except (OSError, ValueError, TypeError):
        pass
    return default


def _save_odds_api_io_state(state):
    try:
        with open(ODDS_API_IO_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  Odds-API.io 状态写入失败: {e}")


def _prune_odds_api_io_state(state, now_ts=None):
    now_ts = now_ts or time.time()
    state["request_times"] = [
        ts for ts in state.get("request_times", [])
        if isinstance(ts, (int, float)) and now_ts - ts < 3600
    ]
    return state


def _set_odds_api_io_status(status, message, **extra):
    state = _prune_odds_api_io_state(_load_odds_api_io_state())
    state.update({
        "last_status": status,
        "last_message": message,
        **extra,
    })
    _save_odds_api_io_state(state)
    return state


def _odds_api_io_usage(now_ts=None):
    state = _prune_odds_api_io_state(_load_odds_api_io_state(), now_ts)
    return state, len(state.get("request_times", []))


def _odds_api_io_budget_available(needed, now_ts=None):
    now_ts = now_ts or time.time()
    state, used = _odds_api_io_usage(now_ts)
    cooldown_until = float(state.get("cooldown_until") or 0)
    if now_ts < cooldown_until:
        wait = int(cooldown_until - now_ts)
        return False, state, f"限频冷却中，约 {wait} 秒后再试"

    last_attempt_at = float(state.get("last_attempt_at") or 0)
    if last_attempt_at and now_ts - last_attempt_at < ODDS_API_IO_MIN_INTERVAL:
        wait = int(ODDS_API_IO_MIN_INTERVAL - (now_ts - last_attempt_at))
        return False, state, f"免费额度保护中，约 {wait} 秒后再请求 Odds-API.io"

    hourly_cap = max(0, ODDS_API_IO_FREE_REQUESTS_PER_HOUR - ODDS_API_IO_REQUEST_RESERVE)
    if used + needed > hourly_cap:
        return False, state, f"免费额度保护：近 1 小时已用 {used}/{ODDS_API_IO_FREE_REQUESTS_PER_HOUR} 次，请稍后再试"
    if needed > ODDS_API_IO_MAX_REQUESTS_PER_CYCLE:
        return False, state, f"本轮预计 {needed} 次请求，超过每轮上限 {ODDS_API_IO_MAX_REQUESTS_PER_CYCLE} 次"
    return True, state, ""


def _odds_api_io_hourly_available(needed, now_ts=None):
    state, used = _odds_api_io_usage(now_ts)
    hourly_cap = max(0, ODDS_API_IO_FREE_REQUESTS_PER_HOUR - ODDS_API_IO_REQUEST_RESERVE)
    if used + needed > hourly_cap:
        return False, state, f"免费额度保护：近 1 小时已用 {used}/{ODDS_API_IO_FREE_REQUESTS_PER_HOUR} 次，请稍后再试"
    return True, state, ""


def _mark_odds_api_io_requests(count, status="requesting", message=""):
    now_ts = time.time()
    state = _prune_odds_api_io_state(_load_odds_api_io_state(), now_ts)
    state["request_times"] = state.get("request_times", []) + [now_ts] * count
    state["last_attempt_at"] = now_ts
    state["last_status"] = status
    if message:
        state["last_message"] = message
    _save_odds_api_io_state(state)
    return state


def _odds_api_io_cooldown(seconds, message):
    return _set_odds_api_io_status(
        "cooldown",
        message,
        cooldown_until=time.time() + seconds,
    )


def _header_value(headers, *names):
    for name in names:
        value = headers.get(name)
        if value not in ("", None):
            return value
    return None


def _remember_odds_api_headers(headers):
    state = _prune_odds_api_io_state(_load_odds_api_io_state())
    state["rate_limit_limit"] = _header_value(headers, "x-ratelimit-limit", "X-RateLimit-Limit")
    state["rate_limit_remaining"] = _header_value(headers, "x-ratelimit-remaining", "X-RateLimit-Remaining")
    state["rate_limit_reset"] = _header_value(headers, "x-ratelimit-reset", "X-RateLimit-Reset")
    _save_odds_api_io_state(state)


def _odds_api_request(path, params):
    import ssl

    _mark_odds_api_io_requests(1, "requesting", f"请求 {path}")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
    url = f"{ODDS_API_IO_BASE}/{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
            _remember_odds_api_headers(resp.headers)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ExternalApiError(_http_error_message("Odds-API.io", url, e)) from e


def _odds_api_multi_request(event_ids, api_key, bookmakers=None):
    return _odds_api_request("odds/multi", {
        "apiKey": api_key,
        "eventIds": ",".join(str(event_id) for event_id in event_ids if event_id),
        "bookmakers": bookmakers or _odds_api_io_bookmakers(),
    })


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _extract_multi_odds_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "odds", "events", "response"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            items = []
            for event_id, item in value.items():
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("id", event_id)
                    item.setdefault("eventId", event_id)
                    items.append(item)
            if items:
                return items
    items = []
    for event_id, item in payload.items():
        if isinstance(item, dict):
            item = dict(item)
            item.setdefault("id", event_id)
            item.setdefault("eventId", event_id)
            items.append(item)
    return items


def get_odds_api_io_status():
    now_ts = time.time()
    state, used = _odds_api_io_usage(now_ts)
    cooldown_until = float(state.get("cooldown_until") or 0)
    last_attempt_at = float(state.get("last_attempt_at") or 0)
    next_allowed_at = max(
        cooldown_until,
        last_attempt_at + ODDS_API_IO_MIN_INTERVAL if last_attempt_at else 0,
    )
    return {
        "limit_per_hour": ODDS_API_IO_FREE_REQUESTS_PER_HOUR,
        "reserve": ODDS_API_IO_REQUEST_RESERVE,
        "used_last_hour": used,
        "remaining_protected": max(0, ODDS_API_IO_FREE_REQUESTS_PER_HOUR - ODDS_API_IO_REQUEST_RESERVE - used),
        "bookmakers": _odds_api_io_bookmakers(),
        "min_interval": ODDS_API_IO_MIN_INTERVAL,
        "max_requests_per_cycle": ODDS_API_IO_MAX_REQUESTS_PER_CYCLE,
        "batch_size": ODDS_API_IO_BATCH_SIZE,
        "last_attempt_at": fromtimestamp_bj(last_attempt_at).strftime("%Y-%m-%d %H:%M:%S") if last_attempt_at else None,
        "last_success_at": fromtimestamp_bj(state.get("last_success_at") or 0).strftime("%Y-%m-%d %H:%M:%S") if state.get("last_success_at") else None,
        "next_allowed_at": fromtimestamp_bj(next_allowed_at).strftime("%Y-%m-%d %H:%M:%S") if next_allowed_at > now_ts else None,
        "cooldown_until": fromtimestamp_bj(cooldown_until).strftime("%Y-%m-%d %H:%M:%S") if cooldown_until > now_ts else None,
        "rate_limit_limit": state.get("rate_limit_limit"),
        "rate_limit_remaining": state.get("rate_limit_remaining"),
        "rate_limit_reset": state.get("rate_limit_reset"),
        "last_status": state.get("last_status", "idle"),
        "last_message": state.get("last_message", ""),
    }


def _sporttery_request(path, params=None):
    import ssl

    params = params or {}
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in ("", None)})
    url = f"{SPORTTERY_API_BASE}/{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.lottery.gov.cn",
            "Referer": "https://www.lottery.gov.cn/jc/index.html",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise ExternalApiError(_http_error_message("体彩竞彩接口", url, e)) from e


def _get_odds_api_io_key():
    env_key = os.environ.get("ODDS_API_IO_KEY", "").strip()
    if env_key:
        return env_key
    if ODDS_API_IO_KEY:
        return ODDS_API_IO_KEY
    try:
        with open(ODDS_API_IO_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _odds_api_io_bookmakers():
    return ",".join(_odds_api_io_bookmaker_list())


def _odds_api_io_bookmaker_list():
    bookmakers = [b.strip() for b in ODDS_API_IO_BOOKMAKERS.split(",") if b.strip()]
    return (bookmakers or ["Bet365"])[:2]


def _odds_api_io_bookmaker_order():
    return {
        name.strip().lower(): idx
        for idx, name in enumerate(_odds_api_io_bookmakers().split(","))
        if name.strip()
    }


def _extract_list(payload, *keys):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_start_time(raw):
    if not raw:
        return "", ""
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo:
            dt = dt.astimezone(CHINA_TZ).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        date_part = str(raw)[:10]
        time_part = str(raw)[11:16] if len(str(raw)) >= 16 else ""
        return date_part, time_part


def _pick_event_field(event, *names):
    for name in names:
        value = event.get(name)
        if value not in ("", None):
            if isinstance(value, dict) and value.get("name"):
                return value.get("name")
            return value
    return ""


def _pick_source_field(row, *names):
    for name in names:
        value = row.get(name)
        if value not in ("", None):
            return value
    return ""


def _to_decimal(value):
    if value in ("", None):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _normalize_event(event, idx):
    home = _pick_event_field(event, "home_team", "homeTeam", "home", "team_home", "participant1")
    away = _pick_event_field(event, "away_team", "awayTeam", "away", "team_away", "participant2")
    league = _pick_event_field(event, "league", "leagueName", "competition", "tournament", "sport_title", "sport")
    raw_start = _pick_event_field(event, "commence_time", "startTime", "start_time", "date", "startsAt")
    match_date, start_time = _parse_start_time(raw_start)
    return {
        "id": _pick_event_field(event, "id", "eventId", "event_id"),
        "match_no": f"{idx:03d}",
        "league": league or "Football",
        "home_team": home,
        "away_team": away,
        "match_date": match_date,
        "start_time": start_time,
    }


def _outcome_name(outcome):
    return str(_pick_event_field(outcome, "name", "label", "participant", "selection")).lower()


def _outcome_price(outcome):
    for key in ("price", "odds", "decimal", "value"):
        value = outcome.get(key)
        if value is None:
            continue
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            continue
    return None


def _extract_1x2_from_bookmaker(bookmaker, home, away):
    markets = bookmaker if isinstance(bookmaker, list) else _extract_list(bookmaker, "markets")
    if not markets and isinstance(bookmaker, dict):
        markets = [bookmaker]

    for market in markets:
        market_key = str(_pick_event_field(market, "key", "name", "market", "type")).lower()
        if market_key and market_key not in ("ml", "h2h", "1x2", "match_winner", "winner", "moneyline"):
            continue

        outcomes = _extract_list(market, "outcomes", "selections", "prices", "odds")
        if not outcomes:
            continue

        parsed = {"home": None, "draw": None, "away": None}
        for outcome in outcomes:
            if all(k in outcome for k in ("home", "draw", "away")):
                try:
                    parsed = {
                        "home": round(float(outcome["home"]), 2),
                        "draw": round(float(outcome["draw"]), 2),
                        "away": round(float(outcome["away"]), 2),
                    }
                    break
                except (TypeError, ValueError):
                    pass
            name = _outcome_name(outcome)
            price = _outcome_price(outcome)
            if price is None:
                continue
            if name in ("draw", "x", "平", "平局"):
                parsed["draw"] = price
            elif home and home.lower() in name:
                parsed["home"] = price
            elif away and away.lower() in name:
                parsed["away"] = price
            elif name in ("home", "1"):
                parsed["home"] = price
            elif name in ("away", "2"):
                parsed["away"] = price

        if parsed["home"] and parsed["draw"] and parsed["away"]:
            return parsed
    return None


def _extract_bookmaker_odds(payload, home, away):
    raw_bookmakers = payload.get("bookmakers") if isinstance(payload, dict) else None
    if isinstance(raw_bookmakers, dict):
        bookmakers = [{"name": name, "markets": markets} for name, markets in raw_bookmakers.items()]
    else:
        bookmakers = _extract_list(payload, "bookmakers", "odds", "data", "sportsbooks")
    odds_sets = []
    bookmaker_order = _odds_api_io_bookmaker_order()
    for bookmaker in bookmakers:
        odds = _extract_1x2_from_bookmaker(bookmaker, home, away)
        if odds:
            name = str(_pick_event_field(bookmaker, "name", "title", "bookmaker", "key")).strip()
            odds["_bookmaker_order"] = bookmaker_order.get(name.lower(), 999)
            odds_sets.append(odds)
    odds_sets.sort(key=lambda odds: odds.get("_bookmaker_order", 999))
    return odds_sets


TEAM_ALIASES = {
    "神户胜利船": ["vissel kobe", "kobe"],
    "神户胜利": ["vissel kobe", "kobe"],
    "京都不死鸟": ["kyoto sanga", "kyoto"],
    "京都": ["kyoto sanga", "kyoto"],
    "富川fc": ["bucheon fc", "bucheon fc 1995", "bucheon"],
    "富川FC": ["bucheon fc", "bucheon fc 1995", "bucheon"],
    "全北现代": ["jeonbuk", "jeonbuk hyundai", "jeonbuk fc"],
    "比利亚雷亚尔": ["villarreal"],
    "比利亚雷": ["villarreal"],
    "塞维利亚": ["sevilla", "seville"],
    "西班牙人": ["espanyol", "rcd espanyol"],
    "毕尔巴鄂": ["athletic bilbao", "athletic club"],
    "布雷斯特": ["brest"],
    "斯特拉斯堡": ["strasbourg"],
    "曼城": ["manchester city", "man city"],
    "水晶宫": ["crystal palace"],
    "拉齐奥": ["lazio"],
    "国际米兰": ["inter milan", "internazionale"],
    "国米": ["inter milan", "internazionale"],
    "朗斯": ["lens", "rc lens"],
    "巴黎圣曼": ["paris saint-germain", "psg", "paris sg"],
    "巴黎圣日耳曼": ["paris saint-germain", "psg", "paris sg"],
    "阿拉维斯": ["alaves", "deportivo alaves"],
    "巴伦西亚": ["valencia", "valencia cf"],
    "巴列卡诺": ["rayo vallecano"],
    "巴萨": ["barcelona", "fc barcelona"],
    "巴塞罗那": ["barcelona", "fc barcelona"],
    "赫罗纳": ["girona", "girona fc"],
    "皇家社会": ["real sociedad", "real sociedad san sebastian"],
    "皇马": ["real madrid"],
    "皇家马德里": ["real madrid"],
    "皇家奥维耶多": ["real oviedo", "oviedo"],
    "奥维耶多": ["real oviedo", "oviedo"],
    "赫塔费": ["getafe"],
    "马洛卡": ["mallorca", "real mallorca"],
    "阿德莱德联": ["adelaide united", "adelaide united fc"],
    "阿德莱德": ["adelaide united", "adelaide united fc"],
    "奥克兰FC": ["auckland fc", "auckland"],
    "奥克兰fc": ["auckland fc", "auckland"],
    "达曼协定": ["al ittifaq", "al-ittifaq", "al ittifaq fc", "ettifaq"],
    "吉达联合": ["al ittihad", "al-ittihad", "al ittihad club"],
    "胡巴尔卡德西亚": ["al qadsiah", "al-qadsiah", "al qadisiya", "al-qadisiya"],
    "胡巴卡德": ["al qadsiah", "al-qadsiah", "al qadisiya", "al-qadisiya"],
    "拉斯决心": ["al hazm", "al-hazm"],
    "达马克": ["damac", "damac fc"],
    "迈季迈阿宽广": ["al fayha", "al-fayha", "al fayha fc"],
    "迈季宽广": ["al fayha", "al-fayha", "al fayha fc"],
    "布赖代合作": ["al taawoun", "al-taawoun", "al taawon"],
    "布赖合作": ["al taawoun", "al-taawoun", "al taawon"],
    "利雅得": ["al riyadh", "al-riyadh"],
    "圣埃蒂安": ["saint etienne", "st etienne", "as saint etienne"],
    "罗德兹": ["rodez", "rodez aveyron"],
    "阿斯顿维拉": ["aston villa", "villa"],
    "维拉": ["aston villa", "villa"],
    "利物浦": ["liverpool"],
    "町田泽维": ["machida zelvia"],
    "东京绿茵": ["tokyo verdy"],
    "安养fc": ["fc anyang", "anyang"],
    "金泉尚武": ["gimcheon sangmu", "gimcheon"],
    "蔚山现代": ["ulsan", "ulsan hd", "ulsan hyundai"],
    "济州联": ["jeju", "jeju united", "jeju sk"],
}


LEAGUE_ALIASES = {
    "日本职业联赛": ["japan", "j-league", "j1 league"],
    "日职": ["japan", "j-league", "j1 league"],
    "韩国职业联赛": ["korea", "k league"],
    "韩职": ["korea", "k league"],
    "西班牙甲级联赛": ["spain", "la liga"],
    "西甲": ["spain", "la liga"],
    "英格兰超级联赛": ["england", "premier league"],
    "英超": ["england", "premier league"],
    "法国甲级联赛": ["france", "ligue 1"],
    "法甲": ["france", "ligue 1"],
    "澳大利亚超级联赛": ["australia", "a-league"],
    "澳超": ["australia", "a-league"],
    "沙特职业联赛": ["saudi", "saudi pro league"],
    "沙特联": ["saudi", "saudi pro league"],
    "沙职": ["saudi", "saudi pro league"],
    "意大利杯": ["italy", "coppa italia"],
    "意杯": ["italy", "coppa italia"],
}


def _normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _load_team_aliases():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet365_team_aliases.json")
    aliases = dict(TEAM_ALIASES)
    try:
        with open(path, encoding="utf-8") as f:
            user_aliases = json.load(f)
        if isinstance(user_aliases, dict):
            for key, value in user_aliases.items():
                if isinstance(value, list):
                    aliases[key] = value
    except (OSError, ValueError, TypeError):
        pass
    return aliases


def _aliases_for(name, aliases):
    values = aliases.get(name) or aliases.get(str(name).lower()) or []
    values = values + [str(name)]
    normalized = []
    generic_tokens = {"fc", "cf", "sc", "afc", "club", "team"}
    for value in values:
        alias = _normalize_text(value)
        if not alias or alias in generic_tokens:
            continue
        normalized.append(alias)
    return normalized


def _team_match_score(cn_name, en_name, aliases):
    en = _normalize_text(en_name)
    if not en:
        return 0
    best = 0
    for alias in _aliases_for(cn_name, aliases):
        if not alias:
            continue
        if alias == en or alias in en or en in alias:
            best = max(best, 100)
        else:
            best = max(best, int(difflib.SequenceMatcher(None, alias, en).ratio() * 100))
    return best


def _league_match_score(cn_name, odds_league):
    league = _normalize_text(odds_league)
    best = 0
    for alias in LEAGUE_ALIASES.get(cn_name, []) + LEAGUE_ALIASES.get(str(cn_name).lower(), []):
        alias = _normalize_text(alias)
        if alias and alias in league:
            best = max(best, 20)
    return best


def _parse_sporttery_dt(item):
    try:
        return datetime.datetime.strptime(
            f"{item.get('matchDate', '')} {item.get('matchTime', '')}",
            "%Y-%m-%d %H:%M",
        )
    except (TypeError, ValueError):
        return None


def _parse_odds_event_dt(event):
    date_str, time_str = _parse_start_time(event.get("date") or event.get("startTime") or event.get("commence_time"))
    try:
        return datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def _match_sporttery_to_odds_event(jc_match, events, aliases):
    jc_dt = _parse_sporttery_dt(jc_match)
    if not jc_dt:
        return None

    best = (0, None)
    for event in events:
        event_dt = _parse_odds_event_dt(event)
        if not event_dt:
            continue
        diff_minutes = abs((event_dt - jc_dt).total_seconds()) / 60
        if diff_minutes > 120:
            continue

        home_score = _team_match_score(jc_match.get("homeTeamAllName") or jc_match.get("homeTeamAbbName"), event.get("home"), aliases)
        away_score = _team_match_score(jc_match.get("awayTeamAllName") or jc_match.get("awayTeamAbbName"), event.get("away"), aliases)
        reverse_home_score = _team_match_score(jc_match.get("homeTeamAllName") or jc_match.get("homeTeamAbbName"), event.get("away"), aliases)
        reverse_away_score = _team_match_score(jc_match.get("awayTeamAllName") or jc_match.get("awayTeamAbbName"), event.get("home"), aliases)
        if reverse_home_score + reverse_away_score > home_score + away_score:
            home_score, away_score = 0, 0
        if home_score < 55 or away_score < 55:
            continue

        time_score = max(0, 30 - int(diff_minutes))
        league_obj = event.get("league") if isinstance(event.get("league"), dict) else {}
        league_name = league_obj.get("name") or event.get("league") or ""
        league_score = max(
            _league_match_score(jc_match.get("leagueAllName"), league_name),
            _league_match_score(jc_match.get("leagueAbbName"), league_name),
        )
        score = home_score + away_score + time_score + league_score
        if score > best[0]:
            best = (score, event)

    return best[1] if best[0] >= 150 else None


def _latest_had_odds(jc_match):
    for odds in jc_match.get("oddsList", []):
        if odds.get("poolCode") == "HAD":
            try:
                return {
                    "home": round(float(odds["h"]), 2),
                    "draw": round(float(odds["d"]), 2),
                    "away": round(float(odds["a"]), 2),
                }
            except (KeyError, TypeError, ValueError):
                return None
    return None


def fetch_from_sporttery_jingcai():
    payload = _sporttery_request(
        "gateway/uniform/football/getMatchListV1.qry",
        {"clientCode": "3001"},
    )
    if str(payload.get("errorCode")) != "0":
        print(f"  体彩竞彩接口返回错误: {payload.get('errorMessage')}")
        return None

    matches = []
    for group in payload.get("value", {}).get("matchInfoList", []):
        for item in group.get("subMatchList", []):
            had = _latest_had_odds(item)
            if not had:
                continue
            matches.append({"raw": item, "had": had})
    return matches


def _okooo_url_for_date(day=None):
    base = OKOOO_JINGCAI_URL.rstrip("/")
    if not day:
        return f"{base}/"
    if re.search(r"/jingcai(?:/\d{4}-\d{2}-\d{2})?/?$", base):
        base = re.sub(r"/jingcai(?:/\d{4}-\d{2}-\d{2})?/?$", "/jingcai", base)
    return f"{base}/{day.strftime('%Y-%m-%d')}/"


def _okooo_request(url=None):
    import ssl

    url = url or _okooo_url_for_date()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.okooo.com/",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
            return resp.read().decode("gb18030", errors="replace")
    except urllib.error.HTTPError as e:
        raise ExternalApiError(_http_error_message("澳客竞彩", url, e)) from e
    except Exception as e:
        raise ExternalApiError(f"澳客竞彩请求失败: {_redact_url(url)} | {e}") from e


def _okooo_odd(match, wf, wz):
    box = match.select_one(f'[data-wf="{wf}"][data-wz="{wz}"]')
    if not box:
        return None
    odd = _to_decimal(box.get("data-sp"))
    if odd:
        return odd
    peilv = box.select_one(".peilv")
    if peilv:
        return _to_decimal(peilv.get_text(strip=True))
    return None


def _okooo_team(match, wf, wz, data_key):
    box = match.select_one(f'[data-wf="{wf}"][data-wz="{wz}"]')
    name = ""
    if box:
        team = box.select_one(".zhum")
        if team:
            name = team.get("title") or team.get_text(strip=True)
    return name or match.get(data_key) or ""


def _okooo_match_datetime(match):
    time_el = match.select_one(".shijian")
    title = time_el.get("title") if time_el else ""
    found = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", title or "")
    if found:
        return found.group(1), found.group(2)
    return "", (time_el.get("mtime") if time_el else "") or ""


def _parse_okooo_jingcai_html(html, include_ended=True):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    matches = []
    for item in soup.select("div.touzhu_1[data-mid]"):
        if not include_ended and item.get("data-end") == "1":
            continue

        had = {
            "home": _okooo_odd(item, "0", "0"),
            "draw": _okooo_odd(item, "0", "1"),
            "away": _okooo_odd(item, "0", "2"),
        }
        if not all(had.values()):
            continue

        match_date, start_time = _okooo_match_datetime(item)
        order_cn = item.get("data-ordercn") or ""
        match_no = re.sub(r"\D+", "", order_cn)[-3:] or (item.get("data-morder") or "")[-3:]
        league_el = item.select_one(".saiming")
        league = (league_el.get("title") if league_el else "") or (league_el.get_text(strip=True) if league_el else "")
        home = _okooo_team(item, "0", "0", "data-hname")
        away = _okooo_team(item, "0", "2", "data-aname")
        if not (match_no and league and home and away and match_date and start_time):
            continue

        raw = {
            "matchNumStr": match_no,
            "matchNum": match_no,
            "leagueAbbName": league,
            "leagueAllName": league,
            "homeTeamAbbName": home,
            "homeTeamAllName": home,
            "awayTeamAbbName": away,
            "awayTeamAllName": away,
            "matchDate": match_date,
            "matchTime": start_time,
            "oddsList": [{
                "poolCode": "HAD",
                "h": had["home"],
                "d": had["draw"],
                "a": had["away"],
            }],
            "okoooMid": item.get("data-mid"),
            "orderCn": order_cn,
            "rq": item.get("data-rq"),
        }
        matches.append({"raw": raw, "had": had})

    return matches or None


def fetch_from_okooo_jingcai(include_ended=True):
    """从澳客竞彩页面提取近期赛程与胜平负 SP。"""
    today = today_bj()
    cutoff = now_bj() - datetime.timedelta(hours=2)
    urls = [_okooo_url_for_date()]
    for offset in range(-OKOOO_HISTORY_DAYS, OKOOO_FUTURE_DAYS + 1):
        urls.append(_okooo_url_for_date(today + datetime.timedelta(days=offset)))

    matches = {}
    errors = []
    for url in dict.fromkeys(urls):
        try:
            page_matches = _parse_okooo_jingcai_html(_okooo_request(url), include_ended=include_ended) or []
        except ExternalApiError as e:
            errors.append(str(e))
            continue
        for item in page_matches:
            raw = item["raw"]
            try:
                kickoff = datetime.datetime.strptime(
                    f"{raw.get('matchDate', '')} {raw.get('matchTime', '')}",
                    "%Y-%m-%d %H:%M",
                )
            except (TypeError, ValueError):
                continue
            if kickoff < cutoff:
                continue
            key = (
                raw.get("matchDate"),
                str(raw.get("matchNumStr") or raw.get("matchNum") or "")[-3:],
                (raw.get("matchTime") or "")[:5],
                raw.get("homeTeamAllName"),
                raw.get("awayTeamAllName"),
            )
            matches[key] = item

    if matches:
        return sorted(
            matches.values(),
            key=lambda item: (
                item["raw"].get("matchDate") or "",
                item["raw"].get("matchTime") or "",
                item["raw"].get("matchNumStr") or "",
            ),
        )
    if errors:
        raise ExternalApiError("；".join(errors[:2]))
    return None


def fetch_from_okooo():
    jingcai_matches = fetch_from_okooo_jingcai()
    if not jingcai_matches:
        return None
    return [
        _sporttery_row(jc["raw"], jc["had"], clear_stale_odds=True, preserve_missing=True)
        for jc in jingcai_matches
    ]


def _nowscore_request(url, service="捷报比分"):
    import ssl

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml,application/javascript;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://cp.nowscore.com/",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
            raw = resp.read()
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ExternalApiError(_http_error_message(service, url, e)) from e
    except Exception as e:
        raise ExternalApiError(f"{service}请求失败: {_redact_url(url)} | {e}") from e


def _nowscore_abs_url(value):
    value = str(value or "")
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("/"):
        return f"https://cp.nowscore.com{value}"
    return value


def _nowscore_text(node):
    return node.get_text(" ", strip=True) if node else ""


def _parse_nowscore_match_datetime(row):
    for td in row.find_all("td"):
        title = td.get("title") or ""
        found = re.search(r"开赛时间：(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})", title)
        if found:
            date_part = found.group(1)
            try:
                dt = datetime.datetime.strptime(f"{date_part} {found.group(2)}", "%Y-%m-%d %H:%M")
                return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            except ValueError:
                return date_part, found.group(2)
    return "", ""


def _parse_nowscore_cp_html(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    matches = []
    for row in soup.select('tr[id^="row_"][matchid]'):
        lottery_id = str(row.get("matchid") or "").strip()
        if not lottery_id:
            continue

        home_el = row.select_one('a[id^="HomeTeam_"]')
        away_el = row.select_one('a[id^="GuestTeam_"]')
        if not (home_el and away_el):
            continue

        odds_link = row.select_one('a[href*="/1x2/"]')
        odds_href = odds_link.get("href") if odds_link else ""
        match_id_match = re.search(r"/1x2/(\d+)\.htm", odds_href or "")
        if not match_id_match:
            match_id_match = re.search(r"_(\d+)$", home_el.get("id") or "")
        nowscore_match_id = match_id_match.group(1) if match_id_match else ""
        if not nowscore_match_id:
            continue

        first_td = row.find("td")
        match_no = re.sub(r"\D+", "", _nowscore_text(first_td))[-3:]
        league = row.get("gamename") or _nowscore_text(row.find_all("td")[1] if len(row.find_all("td")) > 1 else None)
        match_date, start_time = _parse_nowscore_match_datetime(row)

        had = {
            "home": _to_decimal(_nowscore_text(soup.find(id=f"sp_{lottery_id}_52"))),
            "draw": _to_decimal(_nowscore_text(soup.find(id=f"sp_{lottery_id}_53"))),
            "away": _to_decimal(_nowscore_text(soup.find(id=f"sp_{lottery_id}_54"))),
        }
        if not (match_no and league and match_date and start_time and all(had.values())):
            continue

        raw = {
            "matchNumStr": match_no,
            "matchNum": match_no,
            "leagueAbbName": league,
            "leagueAllName": league,
            "homeTeamAbbName": _nowscore_text(home_el),
            "homeTeamAllName": _nowscore_text(home_el),
            "awayTeamAbbName": _nowscore_text(away_el),
            "awayTeamAllName": _nowscore_text(away_el),
            "matchDate": match_date,
            "matchTime": start_time,
            "oddsList": [{
                "poolCode": "HAD",
                "h": had["home"],
                "d": had["draw"],
                "a": had["away"],
            }],
            "nowscoreMatchId": nowscore_match_id,
            "nowscoreLotteryId": lottery_id,
            "nowscoreOddsUrl": _nowscore_abs_url(odds_href),
        }
        matches.append({"raw": raw, "had": had})
    return matches or None


def _parse_nowscore_js_datetime(value):
    parts = str(value or "").split(",")
    if len(parts) < 6:
        return ""
    try:
        year = int(parts[0])
        month_expr = parts[1].strip()
        if "-" in month_expr:
            month_index = sum(int(part) if idx == 0 else -int(part) for idx, part in enumerate(month_expr.split("-")))
        else:
            month_index = int(month_expr)
        dt_utc = datetime.datetime(
            year,
            month_index + 1,
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
            tzinfo=datetime.UTC,
        )
        return dt_utc.astimezone(CHINA_TZ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _parse_nowscore_crown_js(js_text):
    array_match = re.search(r"var\s+game\s*=\s*Array\((.*?)\);\s*var\s+", js_text, re.S)
    if not array_match:
        array_match = re.search(r"var\s+game\s*=\s*Array\((.*?)\);\s*$", js_text, re.S)
    if not array_match:
        return None

    rows = re.findall(r'"((?:[^"\\]|\\.)*)"', array_match.group(1))
    for raw_row in rows:
        fields = raw_row.split("|")
        if len(fields) < 21:
            continue
        company_name = fields[2].strip()
        short_name = fields[21].strip() if len(fields) > 21 else ""
        if fields[0] != "545" and company_name.lower() != "crown" and not short_name.lower().startswith("crow"):
            continue
        odds = {
            "home": _to_decimal(fields[10]),
            "draw": _to_decimal(fields[11]),
            "away": _to_decimal(fields[12]),
            "odds_time": _parse_nowscore_js_datetime(fields[20]),
            "source_company": company_name or short_name,
            "source_company_id": fields[0],
        }
        if odds["home"] and odds["draw"] and odds["away"]:
            return odds
    return None


def _fetch_nowscore_crown_odds(match_id):
    url = f"{NOWSCORE_1X2_BASE}/{match_id}.js"
    return _parse_nowscore_crown_js(_nowscore_request(url, service="捷报Crown欧赔"))


def _jingcai_match_key(jc_match):
    return (
        str(jc_match.get("matchDate") or "")[:10],
        str(jc_match.get("matchNumStr") or jc_match.get("matchNum") or "")[-3:],
        str(jc_match.get("matchTime") or "")[:5],
    )


def fetch_from_nowscore_crown():
    """从捷报竞彩页提取赛程/竞彩 SP，再取 nowscore Crown 即时欧赔合并。"""
    now_ts = time.time()
    if _nowscore_crown_cache["matches"] is not None and now_ts < _nowscore_crown_cache["expires_at"]:
        return _nowscore_crown_cache["matches"]

    try:
        jingcai_matches = _parse_nowscore_cp_html(_nowscore_request(NOWSCORE_CP_URL, service="捷报竞彩"))
    except ExternalApiError as e:
        print(f"  捷报竞彩接口不可用: {e}")
        return None

    if not jingcai_matches:
        print("  捷报竞彩未返回可用胜平负比赛")
        return None

    crown_map = {}
    crown_errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=NOWSCORE_CROWN_WORKERS) as executor:
        future_map = {
            executor.submit(_fetch_nowscore_crown_odds, jc["raw"].get("nowscoreMatchId")): jc
            for jc in jingcai_matches
            if jc["raw"].get("nowscoreMatchId")
        }
        for future in concurrent.futures.as_completed(future_map):
            jc = future_map[future]
            match_id = jc["raw"].get("nowscoreMatchId")
            try:
                primary = future.result()
            except ExternalApiError as e:
                crown_errors.append(f"{match_id}: {e}")
                primary = None
            except Exception as e:
                crown_errors.append(f"{match_id}: {e}")
                primary = None
            if primary:
                crown_map[_jingcai_match_key(jc["raw"])] = primary

    matches = []
    for jc in jingcai_matches:
        key = _jingcai_match_key(jc["raw"])
        matches.append(
            _sporttery_row(
                jc["raw"],
                jc["had"],
                primary=crown_map.get(key),
                clear_stale_odds=True,
                force_clear_odds=True,
                preserve_missing=True,
            )
        )

    crown_matched_count = len(crown_map)
    error_text = f", 错误 {len(crown_errors)} 场" if crown_errors else ""
    print(
        f"  捷报皇冠采集: 竞彩 {len(jingcai_matches)} 场, "
        f"Crown匹配 {crown_matched_count} 场{error_text}"
    )
    _nowscore_crown_cache["matches"] = matches
    _nowscore_crown_cache["expires_at"] = now_ts + NOWSCORE_CROWN_CACHE_TTL
    return matches


def _jingcai_match_from_20002028(row):
    had = {
        "home": _to_decimal(_pick_source_field(row, "jingcai_home", "jc_home", "home_jingcai", "spf_home")),
        "draw": _to_decimal(_pick_source_field(row, "jingcai_draw", "jc_draw", "draw_jingcai", "spf_draw")),
        "away": _to_decimal(_pick_source_field(row, "jingcai_away", "jc_away", "away_jingcai", "spf_away")),
    }
    if not all(had.values()):
        return None

    match_date = _pick_source_field(row, "real_date", "match_date", "date")
    start_time = str(_pick_source_field(row, "start_time", "match_time", "time"))[:5]
    match_no = str(_pick_source_field(row, "match_no", "matchNumStr", "matchNum"))
    raw = {
        "matchNumStr": match_no[-3:],
        "matchNum": match_no[-3:],
        "leagueAbbName": _pick_source_field(row, "league", "leagueAbbName", "leagueAllName"),
        "leagueAllName": _pick_source_field(row, "league", "leagueAllName", "leagueAbbName"),
        "homeTeamAbbName": _pick_source_field(row, "home_team", "homeTeamAbbName", "homeTeamAllName"),
        "homeTeamAllName": _pick_source_field(row, "home_team", "homeTeamAllName", "homeTeamAbbName"),
        "awayTeamAbbName": _pick_source_field(row, "away_team", "awayTeamAbbName", "awayTeamAllName"),
        "awayTeamAllName": _pick_source_field(row, "away_team", "awayTeamAllName", "awayTeamAbbName"),
        "matchDate": str(match_date)[:10],
        "matchTime": start_time,
        "oddsList": [{
            "poolCode": "HAD",
            "h": had["home"],
            "d": had["draw"],
            "a": had["away"],
        }],
    }
    if not (raw["matchDate"] and raw["matchTime"] and raw["homeTeamAllName"] and raw["awayTeamAllName"]):
        return None
    return {"raw": raw, "had": had}


def fetch_from_20002028_jingcai():
    """Use the main source as the Jingcai match list when Sporttery is blocked."""
    rows = fetch_from_20002028()
    if not rows:
        return None

    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _jingcai_match_from_20002028(row)
        if item:
            matches.append(item)
    return matches or None


def _sporttery_row(jc_match, had, primary=None, clear_stale_odds=False, force_clear_odds=False, preserve_missing=False):
    now_str = now_bj().strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "match_no": str(jc_match.get("matchNumStr") or jc_match.get("matchNum") or "")[-3:],
        "league": jc_match.get("leagueAbbName") or jc_match.get("leagueAllName") or "",
        "home_team": jc_match.get("homeTeamAbbName") or jc_match.get("homeTeamAllName"),
        "away_team": jc_match.get("awayTeamAbbName") or jc_match.get("awayTeamAllName"),
        "real_date": jc_match.get("matchDate"),
        "match_date": jc_match.get("matchDate"),
        "start_time": jc_match.get("matchTime"),
        "status": jc_match.get("matchTime"),
        "home_odds": None,
        "draw_odds": None,
        "away_odds": None,
        "jingcai_home": had["home"],
        "jingcai_draw": had["draw"],
        "jingcai_away": had["away"],
        "clear_stale_odds": clear_stale_odds,
        "force_clear_odds": force_clear_odds,
        "full_snapshot": True,
        "preserve_missing": preserve_missing,
    }
    if primary:
        row.update({
            "home_odds": primary["home"],
            "draw_odds": primary["draw"],
            "away_odds": primary["away"],
            "odds_time": primary.get("odds_time") or now_str,
        })
    return row


def fetch_from_odds_api_io():
    """从澳客获取赛程/竞彩赔率，再从 Odds-API.io 获取主备博彩公司 1X2 欧赔。"""
    now_ts = time.time()
    if _odds_api_io_cache["matches"] is not None and now_ts < _odds_api_io_cache["expires_at"]:
        return _odds_api_io_cache["matches"]

    api_key = _get_odds_api_io_key()
    if not api_key:
        message = "备用接口未配置 ODDS_API_IO_KEY"
        _set_odds_api_io_status("error", message)
        print(f"  {message}")
        return None

    jingcai_source_label = "澳客"
    try:
        try:
            jingcai_matches = fetch_from_okooo_jingcai()
        except ExternalApiError as e:
            print(f"  澳客竞彩接口不可用，继续尝试主接口赛程: {e}")
            jingcai_matches = None

        if not jingcai_matches:
            jingcai_source_label = "主接口"
            jingcai_matches = fetch_from_20002028_jingcai()
            if not jingcai_matches:
                message = "澳客和主接口也未返回可用胜平负比赛"
                _set_odds_api_io_status("error", message)
                print(f"  {message}")
                return None

        jingcai_only_matches = [
            _sporttery_row(jc["raw"], jc["had"], preserve_missing=True)
            for jc in jingcai_matches
        ]
        clearable_jingcai_matches = [
            _sporttery_row(jc["raw"], jc["had"], clear_stale_odds=True, preserve_missing=True)
            for jc in jingcai_matches
        ]

        event_bookmakers = _odds_api_io_bookmaker_list()
        ok_budget, _, budget_message = _odds_api_io_budget_available(len(event_bookmakers), now_ts)
        if not ok_budget:
            _set_odds_api_io_status("skipped", budget_message)
            print(f"  Odds-API.io {budget_message}，本轮仅更新{jingcai_source_label}竞彩比赛")
            return jingcai_only_matches

        now_utc = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        events = []
        seen_event_keys = set()
        event_errors = []
        for bookmaker in event_bookmakers:
            try:
                events_payload = _odds_api_request("events", {
                    "apiKey": api_key,
                    "sport": "football",
                    "status": "pending,live",
                    "from": now_utc,
                    "limit": ODDS_API_IO_EVENT_LIMIT,
                    "bookmaker": bookmaker,
                })
            except Exception as e:
                event_errors.append(f"{bookmaker}: {e}")
                continue
            for event in _extract_list(events_payload, "data", "events", "response"):
                event_id = str(_pick_event_field(event, "id", "eventId", "event_id") or "")
                event_key = event_id or "|".join([
                    str(_pick_event_field(event, "home", "homeTeam", "home_team")),
                    str(_pick_event_field(event, "away", "awayTeam", "away_team")),
                    str(_pick_event_field(event, "date", "startTime", "commence_time")),
                ])
                if event_key in seen_event_keys:
                    continue
                seen_event_keys.add(event_key)
                events.append(event)
        if not events:
            detail = f"：{'；'.join(event_errors)}" if event_errors else ""
            _set_odds_api_io_status("error", f"Odds-API.io 未返回比赛列表{detail}")
            print(f"  Odds-API.io 未返回比赛列表{detail}")
            return clearable_jingcai_matches

        aliases = _load_team_aliases()
        candidates = []
        seen_event_ids = set()
        for idx, jc in enumerate(jingcai_matches, start=1):
            jc_match = jc["raw"]
            had = jc["had"]
            event = _match_sporttery_to_odds_event(jc_match, events, aliases)
            if not event:
                continue

            normalized = _normalize_event(event, idx)
            event_id = normalized["id"]
            if not event_id:
                continue
            candidates.append((jc_match, had, normalized))
            seen_event_ids.add(str(event_id))

        if not candidates:
            message = f"备用接口未匹配到欧赔赛事（{jingcai_source_label} {len(jingcai_matches)} 场，博彩公司 {_odds_api_io_bookmakers()}）"
            _set_odds_api_io_status("error", message)
            print(f"  {message}")
            return clearable_jingcai_matches

        event_ids = list(seen_event_ids)
        odds_batches = list(_chunked(event_ids, ODDS_API_IO_BATCH_SIZE))
        total_requests = len(event_bookmakers) + len(odds_batches)
        if total_requests > ODDS_API_IO_MAX_REQUESTS_PER_CYCLE:
            allowed_batches = max(0, ODDS_API_IO_MAX_REQUESTS_PER_CYCLE - len(event_bookmakers))
            odds_batches = odds_batches[:allowed_batches]
            allowed_ids = set(event_id for batch in odds_batches for event_id in batch)
            candidates = [
                item for item in candidates
                if str(item[2]["id"]) in allowed_ids
            ]
            total_requests = len(event_bookmakers) + len(odds_batches)

        ok_hourly, _, hourly_message = _odds_api_io_hourly_available(len(odds_batches))
        if not ok_hourly:
            _set_odds_api_io_status("skipped", hourly_message)
            print(f"  Odds-API.io {hourly_message}，本轮仅更新{jingcai_source_label}竞彩比赛")
            return jingcai_only_matches

        odds_payloads_by_event_id = {}
        active_odds_bookmakers = _odds_api_io_bookmakers()
        for batch in odds_batches:
            try:
                multi_payload = _odds_api_multi_request(batch, api_key, active_odds_bookmakers)
            except Exception as e:
                if getattr(e, "code", None) != 403 or "," not in active_odds_bookmakers:
                    raise
                fallback_bookmaker = _odds_api_io_bookmaker_list()[0]
                active_odds_bookmakers = fallback_bookmaker
                total_requests += 1
                print(f"  Odds-API.io 当前账号未放行 {ODDS_API_IO_BOOKMAKERS}，本轮退回 {fallback_bookmaker}")
                multi_payload = _odds_api_multi_request(batch, api_key, fallback_bookmaker)
            for item in _extract_multi_odds_items(multi_payload):
                item_id = str(_pick_event_field(item, "id", "eventId", "event_id"))
                if item_id:
                    odds_payloads_by_event_id[item_id] = item

        matched_rows = {}
        for jc_match, had, normalized in candidates:
            odds_payload = odds_payloads_by_event_id.get(str(normalized["id"]))
            if not odds_payload:
                continue
            odds_sets = _extract_bookmaker_odds(
                odds_payload,
                normalized["home_team"],
                normalized["away_team"],
            )
            if not odds_sets:
                continue
            primary = odds_sets[0]
            key = (
                jc_match.get("matchDate"),
                str(jc_match.get("matchNumStr") or jc_match.get("matchNum") or "")[-3:],
                (jc_match.get("matchTime") or "")[:5],
            )
            matched_rows[key] = _sporttery_row(jc_match, had, primary, preserve_missing=True)

        matches = []
        for jc in jingcai_matches:
            jc_match = jc["raw"]
            key = (
                jc_match.get("matchDate"),
                str(jc_match.get("matchNumStr") or jc_match.get("matchNum") or "")[-3:],
                (jc_match.get("matchTime") or "")[:5],
            )
            matches.append(
                matched_rows.get(key)
                or _sporttery_row(jc_match, jc["had"], clear_stale_odds=True, preserve_missing=True)
            )

        matched_count = len(matched_rows)
        if matches:
            _odds_api_io_cache["matches"] = matches
            _odds_api_io_cache["expires_at"] = now_ts + ODDS_API_IO_CACHE_TTL
            _set_odds_api_io_status(
                "success",
                f"{jingcai_source_label} {len(jingcai_matches)} 场，欧赔匹配 {matched_count} 场，博彩公司 {active_odds_bookmakers}，本轮约 {total_requests} 次请求",
                last_success_at=time.time(),
            )
            print(f"  备用接口采集: {jingcai_source_label} {len(jingcai_matches)} 场, 欧赔匹配 {matched_count} 场, 本轮约 {total_requests} 次请求")
            return matches
        message = f"备用接口未匹配到欧赔赔率（{jingcai_source_label} {len(jingcai_matches)} 场，博彩公司 {_odds_api_io_bookmakers()}，本轮约 {total_requests} 次请求）"
        _set_odds_api_io_status("error", message)
        print(f"  {message}")
        return None
    except OddsApiBudgetError as e:
        _set_odds_api_io_status("skipped", str(e))
        print(f"  Odds-API.io {e}")
        return None
    except Exception as e:
        if getattr(e, "code", None) == 429:
            _odds_api_io_cooldown(1800, "Odds-API.io 429 Too Many Requests，冷却 30 分钟")
        else:
            _set_odds_api_io_status("error", f"备用接口请求失败: {e}")
        print(f"  备用接口请求失败: {e}")
        return None


def collect_mock_data():
    """采集模拟数据（作为 fallback）"""
    import random

    db = SessionLocal()
    try:
        now = now_bj()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        today_str = now.strftime("%Y-%m-%d")

        existing = {}
        for m in db.query(Match).all():
            key = (m.match_date, m.match_no)
            existing[key] = m

        # 更真实的赔率分布
        for i, (league, home, away, start_time) in enumerate(MOCK_LEAGUES):
            match_no = f"{i + 1:03d}"
            pattern = random.choice(["home_strong", "balanced", "away_strong"])
            if pattern == "home_strong":
                h, d, a = round(random.uniform(1.2, 2.5), 2), round(random.uniform(3.0, 5.5), 2), round(random.uniform(3.0, 8.0), 2)
            elif pattern == "away_strong":
                h, d, a = round(random.uniform(3.0, 8.0), 2), round(random.uniform(3.0, 5.5), 2), round(random.uniform(1.2, 2.5), 2)
            else:
                h, d, a = round(random.uniform(2.0, 3.5), 2), round(random.uniform(2.8, 4.0), 2), round(random.uniform(2.0, 3.5), 2)
            jh, jd, ja = round(h * random.uniform(0.85, 1.08), 2), round(d * random.uniform(0.88, 1.05), 2), round(a * random.uniform(0.85, 1.08), 2)

            match = existing.get((today_str, match_no))
            if match:
                old_h, old_d, old_a = match.home_odds or 0, match.draw_odds or 0, match.away_odds or 0
                for opt_name, old_val, new_val in [("主胜变化", old_h, h), ("平局变化", old_d, d), ("客胜变化", old_a, a)]:
                    if old_val and abs(old_val - new_val) > 0.01:
                        db.add(OddsChange(time=time_str, league=league, home=home, away=away,
                                          change_type=opt_name, from_odds=round(old_val, 2),
                                          to_odds=round(new_val, 2), odds=f"{h:.2f}/{d:.2f}/{a:.2f}"))
                match.home_odds, match.draw_odds, match.away_odds = h, d, a
                match.jingcai_home, match.jingcai_draw, match.jingcai_away = jh, jd, ja
                match.odds_time = time_str
            else:
                db.add(Match(match_no=match_no, league=league, home_team=home, away_team=away,
                             match_date=today_str, start_time=start_time, status=start_time,
                             home_odds=h, draw_odds=d, away_odds=a,
                             jingcai_home=jh, jingcai_draw=jd, jingcai_away=ja, odds_time=time_str))

        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"采集错误: {e}")
        return False
    finally:
        db.close()


# 统一入口
def collect_data():
    source = get_data_source()
    if source == "20002028":
        ok = collect_real_data()
        if not ok:
            print("⚠️ 真实数据源不可用，跳过本轮采集（不 fallback 模拟数据）")
            return False
        return ok
    elif source == "okooo":
        ok = collect_real_data(fetch_from_okooo)
        if not ok:
            print("⚠️ 澳客竞彩数据源不可用，跳过本轮采集（不 fallback 模拟数据）")
            return False
        return ok
    elif source == "odds_api_io":
        ok = collect_real_data(fetch_from_odds_api_io)
        if not ok:
            print("⚠️ 备用数据源不可用，尝试主接口 fallback（不 fallback 模拟数据）")
            fallback_ok = collect_real_data()
            if fallback_ok:
                print("  已使用主接口完成本轮 fallback 采集")
                return True
            print("⚠️ 主接口 fallback 也不可用，跳过本轮采集")
            return False
        return ok
    elif source == "nowscore_crown":
        ok = collect_real_data(fetch_from_nowscore_crown)
        if not ok:
            print("⚠️ 捷报皇冠数据源不可用，跳过本轮采集（不 fallback 模拟数据）")
            return False
        return ok
    elif source == "mock":
        return collect_mock_data()
    else:
        print(f"未知数据源: {source}")
        return False


# 模拟联赛数据供 mock 模式使用
MOCK_LEAGUES = [
    ("日职联", "大阪樱花", "长崎航海", "15:00"),
    ("韩K联", "光州FC", "江原FC", "15:30"),
    ("韩K联", "大田市民", "浦项制铁", "18:00"),
    ("德乙", "波鸿", "汉诺威96", "19:00"),
    ("英超", "利物浦", "切尔西", "19:30"),
    ("英超", "阿森纳", "曼联", "22:00"),
    ("西甲", "巴塞罗那", "皇家马德里", "22:30"),
    ("意甲", "AC米兰", "国际米兰", "23:00"),
    ("法甲", "巴黎圣日耳曼", "里昂", "23:30"),
    ("德甲", "拜仁慕尼黑", "多特蒙德", "00:30"),
    ("荷甲", "阿贾克斯", "费耶诺德", "01:00"),
    ("葡超", "本菲卡", "波尔图", "02:00"),
    ("挪超", "萨普斯堡", "腓特烈", "20:00"),
    ("芬超", "坦山猫", "AC奥卢", "23:00"),
    ("瑞典超", "代格福什", "米亚尔比", "21:00"),
    ("美职联", "洛杉矶FC", "迈阿密国际", "01:00"),
    ("巴甲", "弗拉门戈", "桑托斯", "03:00"),
    ("阿甲", "博卡青年", "河床", "04:00"),
    ("日乙", "清水鼓动", "山形山神", "16:00"),
    ("日乙", "东京绿茵", "冈山绿雉", "17:00"),
    ("法乙", "阿纳西", "罗德兹", "02:00"),
    ("法乙", "甘冈", "瓦朗谢讷", "02:00"),
    ("英冠", "赫尔城", "米尔沃尔", "03:00"),
    ("英冠", "谢周三", "伊普斯维奇", "03:00"),
    ("沙特联", "新未来SC", "利雅青年", "00:50"),
    ("荷乙", "威廉二世", "瓦尔韦克", "22:30"),
    ("苏超", "凯尔特人", "流浪者", "20:00"),
    ("俄超", "泽尼特", "莫斯科中央陆军", "21:30"),
    ("土超", "加拉塔萨雷", "费内巴切", "22:00"),
    ("墨超", "蓝十字", "美洲狮", "06:00"),
]
