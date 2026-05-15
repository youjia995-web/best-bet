"""北京单场数据采集：捷报北单赛程 + 皇G欧 + 参考 SP。"""
import datetime
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from config import API_TIMEOUT
from models import BeidanMatch, BeidanOddsChange, SessionLocal
from time_utils import now_bj


BEIDAN_URL = os.environ.get(
    "BEIDAN_URL",
    "https://cp.nowscore.com/buy/DanChang.aspx?typeID=5",
).strip()
BEIDAN_OP_URL = os.environ.get("BEIDAN_OP_URL", "https://cp.nowscore.com/data/op5.xml").strip()
BEIDAN_SP_URL = os.environ.get("BEIDAN_SP_URL", "https://cp.nowscore.com/data/5.txt").strip()
BEIDAN_CACHE_TTL = int(os.environ.get("BEIDAN_CACHE_TTL", "60"))
BEIDAN_CROWN_COMPANY_ID = os.environ.get("BEIDAN_CROWN_COMPANY_ID", "3").strip()

_beidan_cache = {"expires_at": 0, "matches": None}


class BeidanSourceError(Exception):
    """Raised when the Beidan upstream source cannot be parsed or requested."""


def _to_decimal(value):
    try:
        text = str(value or "").replace(",", "").strip()
        if text in {"", "-", ".00", "0", "0.00"}:
            return None
        number = float(text)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _to_float(value, default=None):
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def _clean_text(node):
    return node.get_text(" ", strip=True) if node else ""


def _request_text(url, service="北单数据"):
    import ssl

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml,application/javascript,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://cp.nowscore.com/buy/DanChang.aspx?typeID=5",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=ctx) as resp:
            raw = resp.read()
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise BeidanSourceError(f"{service} HTTP {e.code}: {url}") from e
    except Exception as e:
        raise BeidanSourceError(f"{service}请求失败: {url} | {e}") from e


def _url_with_cache_bust(url):
    sep = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{sep}t={int(time.time() * 1000)}"


def _parse_issue_num(html):
    found = re.search(r'IssueNum\s*=\s*"([^"]+)"', html)
    return found.group(1).strip() if found else ""


def _parse_js_datetime(html, field_index):
    pattern = re.compile(
        rf"M\[(\d+)\]\[{field_index}\]\s*=\s*new Date\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)"
    )
    values = {}
    for match in pattern.finditer(html):
        idx = match.group(1)
        year, month_zero, day, hour, minute, second = [int(v) for v in match.groups()[1:]]
        try:
            dt = datetime.datetime(year, month_zero + 1, day, hour, minute, second)
        except ValueError:
            continue
        values[idx] = dt
    return values


def _parse_sp_text(text):
    sp_map = {}
    for item in str(text or "").split(";"):
        if not item.strip() or "," not in item:
            continue
        key, value = item.split(",", 1)
        parts = key.strip().split("_")
        if len(parts) != 2:
            continue
        match_no, option = parts
        if option not in {"1", "2", "3"}:
            continue
        sp_map.setdefault(match_no, {})[option] = _to_decimal(value)
    return {
        match_no: {
            "home": values.get("1"),
            "draw": values.get("2"),
            "away": values.get("3"),
        }
        for match_no, values in sp_map.items()
    }


def _parse_crown_xml(text, expected_issue=None):
    text = str(text or "").lstrip("\ufeff")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise BeidanSourceError(f"北单皇G欧 XML 解析失败: {e}") from e

    issue_num = root.attrib.get("issueNum", "")
    if expected_issue and issue_num and issue_num != expected_issue:
        raise BeidanSourceError(f"北单皇G欧期号不一致: 页面 {expected_issue}, XML {issue_num}")

    odds_map = {}
    for node in root.findall("i"):
        fields = (node.text or "").split(",")
        if len(fields) < 9 or fields[0] != BEIDAN_CROWN_COMPANY_ID:
            continue
        odds_map[fields[1]] = {
            "odds_id": fields[2],
            "home": _to_decimal(fields[6]),
            "draw": _to_decimal(fields[7]),
            "away": _to_decimal(fields[8]),
        }
    return odds_map


def _parse_beidan_html(html, sp_updates=None):
    issue_num = _parse_issue_num(html)
    start_times = _parse_js_datetime(html, 2)
    stop_times = _parse_js_datetime(html, 3)
    sp_updates = sp_updates or {}

    soup = BeautifulSoup(html, "html.parser")
    matches = []
    for row in soup.select('tr[id^="row_"][matchid]'):
        match_no = str(row.get("matchid") or "").strip()
        if not match_no:
            continue

        home_el = row.select_one('a[id^="HomeTeam_"]')
        away_el = row.select_one('a[id^="GuestTeam_"]')
        if not (home_el and away_el):
            continue

        cells = row.find_all("td")
        start_dt = start_times.get(match_no)
        stop_dt = stop_times.get(match_no)
        row_date = (row.get("name") or "").strip()
        match_date = start_dt.strftime("%Y-%m-%d") if start_dt else row_date
        start_time = start_dt.strftime("%H:%M") if start_dt else (_clean_text(cells[2])[:5] if len(cells) > 2 else "")
        stop_time = stop_dt.strftime("%H:%M") if stop_dt else (_clean_text(cells[3])[:5] if len(cells) > 3 else "")

        html_sp = {
            "home": _to_decimal(_clean_text(soup.find(id=f"sp_{match_no}_1"))),
            "draw": _to_decimal(_clean_text(soup.find(id=f"sp_{match_no}_2"))),
            "away": _to_decimal(_clean_text(soup.find(id=f"sp_{match_no}_3"))),
        }
        latest_sp = sp_updates.get(match_no) or {}
        beidan_sp = {
            "home": latest_sp.get("home") or html_sp["home"],
            "draw": latest_sp.get("draw") or html_sp["draw"],
            "away": latest_sp.get("away") or html_sp["away"],
        }
        if not (match_date and start_time):
            continue

        matches.append({
            "issue_num": issue_num,
            "match_no": match_no,
            "league": row.get("gamename") or (_clean_text(cells[1]) if len(cells) > 1 else ""),
            "home_team": _clean_text(home_el),
            "away_team": _clean_text(away_el),
            "match_date": match_date,
            "start_time": start_time,
            "stop_time": stop_time,
            "handicap": _to_float(row.get("polygoal"), 0.0),
            "status": start_time,
            "beidan_home": beidan_sp["home"],
            "beidan_draw": beidan_sp["draw"],
            "beidan_away": beidan_sp["away"],
            "nowscore_match_id": (home_el.get("id") or "").split("_")[-1],
        })
    return issue_num, matches


def fetch_beidan_matches():
    """Fetch and merge current Beidan matches, reference SP and Crown odds."""
    now_ts = time.time()
    if _beidan_cache["matches"] is not None and now_ts < _beidan_cache["expires_at"]:
        return _beidan_cache["matches"]

    html = _request_text(BEIDAN_URL, "北单页面")
    try:
        sp_updates = _parse_sp_text(_request_text(_url_with_cache_bust(BEIDAN_SP_URL), "北单参考SP"))
    except BeidanSourceError as e:
        print(f"  北单参考SP更新不可用，使用页面SP: {e}")
        sp_updates = {}

    issue_num, matches = _parse_beidan_html(html, sp_updates)
    if not matches:
        raise BeidanSourceError("北单页面未解析到可用比赛")

    crown_map = _parse_crown_xml(_request_text(_url_with_cache_bust(BEIDAN_OP_URL), "北单皇G欧"), issue_num)
    odds_time = now_bj().strftime("%Y-%m-%d %H:%M:%S")
    for match in matches:
        crown = crown_map.get(match["match_no"]) or {}
        match.update({
            "home_odds": crown.get("home"),
            "draw_odds": crown.get("draw"),
            "away_odds": crown.get("away"),
            "odds_time": odds_time,
            "odds_missing": not (crown.get("home") and crown.get("draw") and crown.get("away")),
        })

    print(f"  北单采集: {len(matches)} 场, Crown匹配 {sum(1 for m in matches if not m['odds_missing'])} 场")
    _beidan_cache["matches"] = matches
    _beidan_cache["expires_at"] = now_ts + BEIDAN_CACHE_TTL
    return matches


def collect_beidan_data():
    """Collect Beidan data into dedicated tables and record Crown odds changes."""
    db = SessionLocal()
    try:
        matches_data = fetch_beidan_matches()
        now = now_bj()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        existing = {
            (m.issue_num or "", m.match_no or ""): m
            for m in db.query(BeidanMatch).all()
        }
        seen_keys = set()
        updated_count = 0

        for row in matches_data:
            key = (row.get("issue_num") or "", row.get("match_no") or "")
            seen_keys.add(key)
            match = existing.get(key)
            home_odds = row.get("home_odds")
            draw_odds = row.get("draw_odds")
            away_odds = row.get("away_odds")

            if match:
                old_home = match.home_odds or 0
                old_draw = match.draw_odds or 0
                old_away = match.away_odds or 0
                for opt_name, old_val, new_val in [
                    ("主胜变化", old_home, home_odds),
                    ("平局变化", old_draw, draw_odds),
                    ("客胜变化", old_away, away_odds),
                ]:
                    if old_val and new_val and abs(old_val - new_val) > 0.005:
                        db.add(BeidanOddsChange(
                            time=time_str,
                            league=row.get("league", ""),
                            home=row.get("home_team", ""),
                            away=row.get("away_team", ""),
                            change_type=opt_name,
                            from_odds=round(old_val, 2),
                            to_odds=round(new_val, 2),
                            odds=f"{home_odds:.2f}/{draw_odds:.2f}/{away_odds:.2f}" if home_odds else "",
                        ))
            else:
                match = BeidanMatch(issue_num=row.get("issue_num"), match_no=row.get("match_no"))
                db.add(match)

            match.league = row.get("league")
            match.home_team = row.get("home_team")
            match.away_team = row.get("away_team")
            match.match_date = row.get("match_date")
            match.start_time = row.get("start_time")
            match.stop_time = row.get("stop_time")
            match.handicap = row.get("handicap")
            match.status = row.get("status") or row.get("start_time")
            match.home_odds = home_odds
            match.draw_odds = draw_odds
            match.away_odds = away_odds
            match.beidan_home = row.get("beidan_home")
            match.beidan_draw = row.get("beidan_draw")
            match.beidan_away = row.get("beidan_away")
            match.odds_time = row.get("odds_time") or time_str
            updated_count += 1

        for match in db.query(BeidanMatch).all():
            key = (match.issue_num or "", match.match_no or "")
            if match.issue_num == (matches_data[0].get("issue_num") if matches_data else "") and key not in seen_keys:
                db.delete(match)

        db.commit()
        print(f"[{time_str}] 北单采集完成: {len(matches_data)} 场比赛, {updated_count} 条更新")
        return True
    except Exception as e:
        db.rollback()
        print(f"北单采集错误: {e}")
        return False
    finally:
        db.close()
