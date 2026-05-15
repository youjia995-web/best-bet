"""足球竞彩赔率实时监控系统 — FastAPI 主应用"""
import json
import datetime
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from apscheduler.schedulers.background import BackgroundScheduler

from models import (init_db, get_db, SessionLocal, Match, OddsChange, ValueMatch,
                    Backtest2x1, SingleBet, FallBet, DrawBet,
                    BeidanMatch, BeidanOddsChange, BeidanValueMatch,
                    BeidanBacktest2x1, BeidanSingleBet, BeidanFallBet,
                    BeidanDrawBet)
from collector import (
    collect_data, fetch_status_from_20002028,
    get_data_source, get_odds_api_io_status, get_source_options, set_data_source,
)
from beidan_collector import collect_beidan_data
from beidan_detector import detect_beidan_value_bets
from time_utils import format_dt_bj, now_bj, today_bj
from value_detector import detect_value_bets
from config import (ADMIN_PASSWORD, REFRESH_INTERVAL,
                    BASE_DIR,
                    BACKTEST_2X1_MAX_HUANGG, SINGLE_BET_MAX_HUANGG,
                    FALL_BET_MAX_HUANGG, FALL_BET_MIN_CHANGE, REST_HOURS)

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    collect_data()
    detect_value_bets()
    if collect_beidan_data():
        detect_beidan_value_bets()
    scheduler.add_job(
        lambda: (_collect_and_detect()),
        'interval',
        seconds=REFRESH_INTERVAL,
        id='collect',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=5
    )
    scheduler.start()
    yield
    scheduler.shutdown()


def _collect_and_detect():
    try:
        collect_data()
        detect_value_bets()
        if collect_beidan_data():
            detect_beidan_value_bets()
        print(f"[{now_bj()}] 采集完成")
    except Exception as e:
        print(f"定时采集错误: {e}")


app = FastAPI(lifespan=lifespan)


# ==================== 静态文件 ====================

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/beidan", response_class=HTMLResponse)
def beidan_page():
    with open(os.path.join(BASE_DIR, "static", "beidan.html"), encoding="utf-8") as f:
        return f.read()


# ==================== 工具函数 ====================

def verify_password(password: str | None) -> bool:
    return password == ADMIN_PASSWORD


def format_backtest(item):
    """将 Backtest2x1 记录转为前端期望的嵌套格式"""
    return {
        "id": item.id,
        "created_at": item.created_at,
        "match1": {
            "no": item.match1_no,
            "league": item.match1_league,
            "home": item.match1_home,
            "away": item.match1_away,
            "option": item.match1_option,
            "odds": item.match1_odds,
            "huangg": item.match1_huangg,
            "final": item.match1_final,
            "start_time": item.match1_start_time,
            "match_date": item.match1_match_date,
        },
        "match2": {
            "no": item.match2_no,
            "league": item.match2_league,
            "home": item.match2_home,
            "away": item.match2_away,
            "option": item.match2_option,
            "odds": item.match2_odds,
            "huangg": item.match2_huangg,
            "final": item.match2_final,
            "start_time": item.match2_start_time,
            "match_date": item.match2_match_date,
        },
        "total_odds": item.total_odds,
        "bet_amount": item.bet_amount,
        "potential_win": item.potential_win,
        "result": item.result,
        "result_at": item.result_at,
    }


def format_bet_item(item):
    return {
        "id": item.id,
        "created_at": item.created_at,
        "match_no": item.match_no,
        "league": item.league,
        "home_team": item.home_team,
        "away_team": item.away_team,
        "match_date": item.match_date,
        "start_time": item.start_time,
        "option_type": item.option_type,
        "odds": item.odds,
        "huangg": item.huangg,
        "bet_amount": item.bet_amount,
        "potential_win": item.potential_win,
        "change_pct": item.change_pct,
        "result": item.result,
        "result_at": item.result_at,
        "final": item.final,
    }


def is_resting_now(now: datetime.datetime | None = None) -> bool:
    now = now or now_bj()
    return now.hour in REST_HOURS


def kickoff_dt(m):
    try:
        date_str = (m.match_date or "").strip()[:10]
        time_str = (m.start_time or "00:00").strip()[:5]
        return datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def odds_dt(m):
    try:
        return datetime.datetime.strptime((m.odds_time or "").strip()[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def upcoming_matches_query(db: Session):
    matches = db.query(Match).all()
    cutoff = now_bj() - datetime.timedelta(hours=2)
    upcoming = [m for m in matches if kickoff_dt(m) and kickoff_dt(m) >= cutoff]
    return sorted(
        upcoming,
        key=lambda m: (kickoff_dt(m), m.match_no or "", m.id or 0),
    )


def beidan_upcoming_matches_query(db: Session):
    init_db()
    matches = db.query(BeidanMatch).all()
    cutoff = now_bj() - datetime.timedelta(hours=2)
    upcoming = [m for m in matches if kickoff_dt(m) and kickoff_dt(m) >= cutoff]
    return sorted(
        upcoming,
        key=lambda m: (kickoff_dt(m), _safe_int(m.match_no), m.id or 0),
    )


def beidan_all_matches_query(db: Session):
    init_db()
    return sorted(
        db.query(BeidanMatch).all(),
        key=lambda m: (
            (m.match_date or ""),
            (m.start_time or ""),
            _safe_int(m.match_no),
            m.id or 0,
        ),
    )


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_beidan_match(m, now=None):
    now = now or now_bj()
    parsed_odds_dt = odds_dt(m)
    return {
        "id": m.id,
        "issue_num": m.issue_num,
        "match_no": m.match_no,
        "league": m.league,
        "real_date": m.match_date,
        "start_time": m.start_time,
        "stop_time": m.stop_time,
        "handicap": m.handicap,
        "home_team": m.home_team,
        "away_team": m.away_team,
        "status": m.status,
        "date_str": m.match_date,
        "home_odds": m.home_odds,
        "draw_odds": m.draw_odds,
        "away_odds": m.away_odds,
        "beidan_home": m.beidan_home,
        "beidan_draw": m.beidan_draw,
        "beidan_away": m.beidan_away,
        "odds_time": m.odds_time,
        "odds_missing": not (m.home_odds and m.draw_odds and m.away_odds),
        "odds_age_minutes": (
            int((now - parsed_odds_dt).total_seconds() // 60)
            if parsed_odds_dt
            else None
        ),
        "odds_stale": (
            parsed_odds_dt is None
            or int((now - parsed_odds_dt).total_seconds() // 60) > 20
        ),
    }


# ==================== API 端点 ====================

@app.get("/api/matches")
def api_matches(db: Session = Depends(get_db)):
    upcoming = upcoming_matches_query(db)
    now = now_bj()

    return [
        {
            "id": m.id,
            "match_no": m.match_no,
            "league": m.league,
            "real_date": m.match_date,
            "start_time": m.start_time,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "status": m.status,
            "date_str": m.match_date,
            "home_odds": m.home_odds,
            "draw_odds": m.draw_odds,
            "away_odds": m.away_odds,
            "jingcai_home": m.jingcai_home,
            "jingcai_draw": m.jingcai_draw,
            "jingcai_away": m.jingcai_away,
            "odds_time": m.odds_time,
            "odds_missing": not (m.home_odds and m.draw_odds and m.away_odds),
            "odds_age_minutes": (
                int((now - odds_dt(m)).total_seconds() // 60)
                if odds_dt(m)
                else None
            ),
            "odds_stale": (
                odds_dt(m) is None
                or int((now - odds_dt(m)).total_seconds() // 60) > 20
            ),
        }
        for m in upcoming
    ]


@app.get("/api/changes")
def api_changes(db: Session = Depends(get_db)):
    changes = db.query(OddsChange).order_by(OddsChange.time.desc()).limit(20).all()
    return [
        {
            "time": c.time,
            "league": c.league,
            "home": c.home,
            "away": c.away,
            "change_type": c.change_type,
            "from": c.from_odds,
            "to": c.to_odds,
            "odds": c.odds,
        }
        for c in changes
    ]


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    upcoming = upcoming_matches_query(db)
    total_matches = len(upcoming)
    total_changes = db.query(OddsChange).count()
    now = now_bj()
    odds_times = [odds_dt(m) for m in upcoming]
    latest_odds_dt = max((dt for dt in odds_times if dt), default=None)
    odds_age_minutes = int((now - latest_odds_dt).total_seconds() // 60) if latest_odds_dt else None
    stale_count = sum(
        1
        for dt in odds_times
        if dt is None or int((now - dt).total_seconds() // 60) > 20
    )
    return {
        "total_matches": total_matches,
        "total_changes": total_changes,
        "latest_odds_time": latest_odds_dt.strftime("%Y-%m-%d %H:%M:%S") if latest_odds_dt else None,
        "odds_age_minutes": odds_age_minutes,
        "stale_matches": stale_count,
        "fresh_matches": max(0, total_matches - stale_count),
        "data_stale": stale_count > 0,
    }


@app.get("/api/status")
def api_status():
    now = now_bj()
    job = scheduler.get_job('collect')
    next_run = format_dt_bj(job.next_run_time) if job and job.next_run_time else None
    upstream = fetch_status_from_20002028() if get_data_source() == "20002028" else None
    resting = bool(upstream.get("resting")) if upstream else is_resting_now(now)
    refresh_interval = upstream.get("refresh_interval", REFRESH_INTERVAL) if upstream else REFRESH_INTERVAL
    next_update = upstream.get("next_update", 0 if resting else refresh_interval) if upstream else (0 if resting else refresh_interval)
    next_collection = upstream.get("next_collection") if upstream else next_run
    return {
        "running": scheduler.running and not resting,
        "resting": resting,
        "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
        "next_update": next_update,
        "success_count": upstream.get("success_count", 1) if upstream else 1,
        "fail_count": upstream.get("fail_count", 0) if upstream else 0,
        "round": upstream.get("round", 1) if upstream else 1,
        "refresh_interval": refresh_interval,
        "next_collection": next_collection,
    }


@app.get("/api/value-bets")
def api_value_bets(db: Session = Depends(get_db)):
    today = today_bj().strftime("%Y-%m-%d")
    bets = (db.query(ValueMatch)
            .filter(ValueMatch.created_date == today)
            .order_by(ValueMatch.id.desc())
            .all())
    return {
        "value_matches": [
            {
                "id": v.id,
                "match_id": v.match_id,
                "capture_time": v.capture_time,
                "created_date": v.created_date,
                "match_no": v.match_no,
                "league": v.league,
                "home_team": v.home_team,
                "away_team": v.away_team,
                "match_date": v.match_date,
                "start_time": v.start_time,
                "huangg_odds": v.huangg_odds,
                "jingcai_odds": v.jingcai_odds,
                "selected_option": v.selected_option,
                "backtest_odds": v.backtest_odds,
                "huangg_option": v.huangg_option,
                "huangg_home": v.huangg_home,
                "huangg_draw": v.huangg_draw,
                "huangg_away": v.huangg_away,
                "current_odds": v.current_odds,
                "change_pct": v.change_pct,
                "backtest_eligible": v.backtest_eligible,
            }
            for v in bets
        ]
    }


@app.get("/api/backtest")
def api_backtest(db: Session = Depends(get_db)):
    items = db.query(Backtest2x1).order_by(Backtest2x1.id.desc()).all()
    return [format_backtest(item) for item in items]


@app.post("/api/backtest/{item_id}")
def api_backtest_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        item = db.query(Backtest2x1).filter(Backtest2x1.id == item_id).first()
        if item:
            item.result = result
            item.result_at = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/api/backtest/{item_id}")
def api_backtest_delete(item_id: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        db.query(Backtest2x1).filter(Backtest2x1.id == item_id).delete()
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/single-bet")
def api_single_bet(db: Session = Depends(get_db)):
    items = db.query(SingleBet).order_by(SingleBet.id.desc()).all()
    return [
        {
            "id": s.id,
            "created_at": s.created_at,
            "match_no": s.match_no,
            "league": s.league,
            "home_team": s.home_team,
            "away_team": s.away_team,
            "match_date": s.match_date,
            "start_time": s.start_time,
            "option_type": s.option_type,
            "odds": s.odds,
            "huangg": s.huangg,
            "bet_amount": s.bet_amount,
            "potential_win": s.potential_win,
            "change_pct": s.change_pct,
            "result": s.result,
            "result_at": s.result_at,
            "final": s.final,
        }
        for s in items
    ]


@app.post("/api/single-bet/{item_id}")
def api_single_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        item = db.query(SingleBet).filter(SingleBet.id == item_id).first()
        if item:
            item.result = result
            item.result_at = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/api/single-bet/{item_id}")
def api_single_bet_delete(item_id: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        db.query(SingleBet).filter(SingleBet.id == item_id).delete()
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/fall-bet")
def api_fall_bet(db: Session = Depends(get_db)):
    items = db.query(FallBet).order_by(FallBet.id.desc()).all()
    return [
        {
            "id": f.id,
            "created_at": f.created_at,
            "match_no": f.match_no,
            "league": f.league,
            "home_team": f.home_team,
            "away_team": f.away_team,
            "match_date": f.match_date,
            "start_time": f.start_time,
            "option_type": f.option_type,
            "odds": f.odds,
            "huangg": f.huangg,
            "bet_amount": f.bet_amount,
            "potential_win": f.potential_win,
            "change_pct": f.change_pct,
            "result": f.result,
            "result_at": f.result_at,
            "final": f.final,
        }
        for f in items
    ]


@app.post("/api/fall-bet/{item_id}")
def api_fall_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        item = db.query(FallBet).filter(FallBet.id == item_id).first()
        if item:
            item.result = result
            item.result_at = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/api/fall-bet/{item_id}")
def api_fall_bet_delete(item_id: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        db.query(FallBet).filter(FallBet.id == item_id).delete()
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/draw-bet")
def api_draw_bet(db: Session = Depends(get_db)):
    items = db.query(DrawBet).order_by(DrawBet.id.desc()).all()
    return [
        {
            "id": d.id,
            "created_at": d.created_at,
            "match_no": d.match_no,
            "league": d.league,
            "home_team": d.home_team,
            "away_team": d.away_team,
            "match_date": d.match_date,
            "start_time": d.start_time,
            "option_type": d.option_type,
            "odds": d.odds,
            "huangg": d.huangg,
            "bet_amount": d.bet_amount,
            "potential_win": d.potential_win,
            "change_pct": d.change_pct,
            "result": d.result,
            "result_at": d.result_at,
            "final": d.final,
        }
        for d in items
    ]


@app.post("/api/draw-bet/{item_id}")
def api_draw_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        item = db.query(DrawBet).filter(DrawBet.id == item_id).first()
        if item:
            item.result = result
            item.result_at = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return {"success": True}
    finally:
        db.close()


@app.delete("/api/draw-bet/{item_id}")
def api_draw_bet_delete(item_id: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        db.query(DrawBet).filter(DrawBet.id == item_id).delete()
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/beidan/matches")
def api_beidan_matches(db: Session = Depends(get_db)):
    try:
        upcoming = beidan_upcoming_matches_query(db)
        now = now_bj()
        return [format_beidan_match(m, now) for m in upcoming]
    except Exception as e:
        print(f"北单比赛接口错误: {e}")
        return []


@app.get("/api/beidan/matches-all")
def api_beidan_matches_all(db: Session = Depends(get_db)):
    try:
        matches = beidan_all_matches_query(db)
        now = now_bj()
        return [format_beidan_match(m, now) for m in matches]
    except Exception as e:
        print(f"北单全量比赛接口错误: {e}")
        return []


@app.get("/api/beidan/changes")
def api_beidan_changes(db: Session = Depends(get_db)):
    try:
        init_db()
        changes = db.query(BeidanOddsChange).order_by(BeidanOddsChange.time.desc()).limit(30).all()
        return [
            {
                "time": c.time,
                "league": c.league,
                "home": c.home,
                "away": c.away,
                "change_type": c.change_type,
                "from": c.from_odds,
                "to": c.to_odds,
                "odds": c.odds,
            }
            for c in changes
        ]
    except Exception as e:
        print(f"北单变化接口错误: {e}")
        return []


@app.get("/api/beidan/stats")
def api_beidan_stats(db: Session = Depends(get_db)):
    try:
        upcoming = beidan_upcoming_matches_query(db)
        total_matches = len(upcoming)
        total_changes = db.query(BeidanOddsChange).count()
        now = now_bj()
        odds_times = [odds_dt(m) for m in upcoming]
        latest_odds_dt = max((dt for dt in odds_times if dt), default=None)
        odds_age_minutes = int((now - latest_odds_dt).total_seconds() // 60) if latest_odds_dt else None
        stale_count = sum(
            1
            for dt in odds_times
            if dt is None or int((now - dt).total_seconds() // 60) > 20
        )
        value_count = db.query(BeidanValueMatch).filter(BeidanValueMatch.created_date == today_bj().strftime("%Y-%m-%d")).count()
        return {
            "total_matches": total_matches,
            "total_changes": total_changes,
            "value_count": value_count,
            "latest_odds_time": latest_odds_dt.strftime("%Y-%m-%d %H:%M:%S") if latest_odds_dt else None,
            "odds_age_minutes": odds_age_minutes,
            "stale_matches": stale_count,
            "fresh_matches": max(0, total_matches - stale_count),
            "data_stale": stale_count > 0,
        }
    except Exception as e:
        print(f"北单统计接口错误: {e}")
        return {
            "total_matches": 0,
            "total_changes": 0,
            "value_count": 0,
            "latest_odds_time": None,
            "odds_age_minutes": None,
            "stale_matches": 0,
            "fresh_matches": 0,
            "data_stale": True,
            "error": str(e),
        }


@app.get("/api/beidan/value-bets")
def api_beidan_value_bets(db: Session = Depends(get_db)):
    try:
        init_db()
        today = today_bj().strftime("%Y-%m-%d")
        bets = (db.query(BeidanValueMatch)
                .filter(BeidanValueMatch.created_date == today)
                .order_by(BeidanValueMatch.id.desc())
                .all())
        return {
            "value_matches": [
                {
                    "id": v.id,
                    "match_id": v.match_id,
                    "capture_time": v.capture_time,
                    "created_date": v.created_date,
                    "match_no": v.match_no,
                    "league": v.league,
                    "home_team": v.home_team,
                    "away_team": v.away_team,
                    "match_date": v.match_date,
                    "start_time": v.start_time,
                    "huangg_odds": v.huangg_odds,
                    "jingcai_odds": v.jingcai_odds,
                    "selected_option": v.selected_option,
                    "backtest_odds": v.backtest_odds,
                    "huangg_option": v.huangg_option,
                    "huangg_home": v.huangg_home,
                    "huangg_draw": v.huangg_draw,
                    "huangg_away": v.huangg_away,
                    "current_odds": v.current_odds,
                    "change_pct": v.change_pct,
                    "backtest_eligible": v.backtest_eligible,
                }
                for v in bets
            ]
        }
    except Exception as e:
        print(f"北单价值接口错误: {e}")
        return {"value_matches": []}


@app.get("/api/beidan/backtest")
def api_beidan_backtest(db: Session = Depends(get_db)):
    try:
        init_db()
        items = db.query(BeidanBacktest2x1).order_by(BeidanBacktest2x1.id.desc()).all()
        return [format_backtest(item) for item in items]
    except Exception as e:
        print(f"北单2串1接口错误: {e}")
        return []


@app.post("/api/beidan/backtest/{item_id}")
def api_beidan_backtest_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    return _set_beidan_result(BeidanBacktest2x1, item_id, result, password)


@app.delete("/api/beidan/backtest/{item_id}")
def api_beidan_backtest_delete(item_id: int, password: str):
    return _delete_beidan_item(BeidanBacktest2x1, item_id, password)


@app.get("/api/beidan/single-bet")
def api_beidan_single_bet(db: Session = Depends(get_db)):
    try:
        init_db()
        return [format_bet_item(item) for item in db.query(BeidanSingleBet).order_by(BeidanSingleBet.id.desc()).all()]
    except Exception as e:
        print(f"北单单关接口错误: {e}")
        return []


@app.post("/api/beidan/single-bet/{item_id}")
def api_beidan_single_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    return _set_beidan_result(BeidanSingleBet, item_id, result, password)


@app.delete("/api/beidan/single-bet/{item_id}")
def api_beidan_single_bet_delete(item_id: int, password: str):
    return _delete_beidan_item(BeidanSingleBet, item_id, password)


@app.get("/api/beidan/fall-bet")
def api_beidan_fall_bet(db: Session = Depends(get_db)):
    try:
        init_db()
        return [format_bet_item(item) for item in db.query(BeidanFallBet).order_by(BeidanFallBet.id.desc()).all()]
    except Exception as e:
        print(f"北单跌水接口错误: {e}")
        return []


@app.post("/api/beidan/fall-bet/{item_id}")
def api_beidan_fall_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    return _set_beidan_result(BeidanFallBet, item_id, result, password)


@app.delete("/api/beidan/fall-bet/{item_id}")
def api_beidan_fall_bet_delete(item_id: int, password: str):
    return _delete_beidan_item(BeidanFallBet, item_id, password)


@app.get("/api/beidan/draw-bet")
def api_beidan_draw_bet(db: Session = Depends(get_db)):
    try:
        init_db()
        return [format_bet_item(item) for item in db.query(BeidanDrawBet).order_by(BeidanDrawBet.id.desc()).all()]
    except Exception as e:
        print(f"北单平局接口错误: {e}")
        return []


@app.post("/api/beidan/draw-bet/{item_id}")
def api_beidan_draw_bet_result(item_id: int, result: int = Form(...), password: str = Form(...)):
    return _set_beidan_result(BeidanDrawBet, item_id, result, password)


@app.delete("/api/beidan/draw-bet/{item_id}")
def api_beidan_draw_bet_delete(item_id: int, password: str):
    return _delete_beidan_item(BeidanDrawBet, item_id, password)


@app.post("/api/beidan/collect")
def trigger_beidan_collect():
    ok = collect_beidan_data()
    if ok:
        detect_beidan_value_bets()
    return {"success": ok}


def _set_beidan_result(model, item_id: int, result: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        item = db.query(model).filter(model.id == item_id).first()
        if item:
            item.result = result
            item.result_at = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return {"success": True}
    finally:
        db.close()


def _delete_beidan_item(model, item_id: int, password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        db.query(model).filter(model.id == item_id).delete()
        db.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/api/export-all")
def api_export_all(password: str):
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    db = SessionLocal()
    try:
        backtests = db.query(Backtest2x1).order_by(Backtest2x1.id.desc()).all()
        data = [format_backtest(item) for item in backtests]
        return JSONResponse(data)
    finally:
        db.close()


@app.post("/api/collect")
def trigger_collect():
    ok = collect_data()
    if ok:
        detect_value_bets()
    return {"success": ok}


@app.get("/api/data-source")
def api_data_source():
    state = get_source_options()
    if state.get("active") == "odds_api_io":
        state["odds_api_io"] = get_odds_api_io_status()
    return state


@app.post("/api/data-source")
async def api_set_data_source(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    source = payload.get("source")
    try:
        active = set_data_source(source)
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)

    ok = collect_data()
    if ok:
        detect_value_bets()
    return {
        "success": ok,
        "active": active,
        "sources": get_source_options()["sources"],
        "odds_api_io": get_odds_api_io_status() if active == "odds_api_io" else None,
        "message": "已切换并采集成功" if ok else "已切换，但当前数据源采集失败",
    }


@app.post("/api/generate-backtest")
def generate_backtest(password: str = Form(...)):
    """手动触发 2串1 组合生成（全量生成，不限数量）"""
    if not verify_password(password):
        return JSONResponse({"error": "密码错误"}, status_code=403)
    from models import SessionLocal as SL
    from value_detector import _generate_backtest_combos
    db = SL()
    try:
        today = today_bj().strftime("%Y-%m-%d")
        eligible = (db.query(ValueMatch)
                    .filter(ValueMatch.created_date == today,
                            ValueMatch.backtest_eligible == 1)
                    .order_by(ValueMatch.id.desc())
                    .all())

        # 按 (日期, 编号, 选项) 去重，取每组最新的一条
        seen = {}
        for v in eligible:
            key = (v.match_date, v.match_no, v.selected_option)
            if key not in seen:
                seen[key] = v

        # 全量配对（不受每批次5条限制）
        created = _generate_backtest_combos(db, list(seen.values()), now_bj())

        return {"success": True, "created": created}
    except Exception as e:
        db.rollback()
        return {"success": False, "error": str(e)}
    finally:
        db.close()


@app.get("/api/config")
def api_config():
    return {
        "backtest_2x1_max_huangg": BACKTEST_2X1_MAX_HUANGG,
        "single_bet_max_huangg": SINGLE_BET_MAX_HUANGG,
        "fall_bet_max_huangg": FALL_BET_MAX_HUANGG,
        "fall_bet_min_change": FALL_BET_MIN_CHANGE,
        "refresh_interval": REFRESH_INTERVAL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
