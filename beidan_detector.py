"""北单价值投注检测器 + 回测记录生成器。"""
import datetime
from collections import defaultdict

from sqlalchemy.orm import Session

from config import (BACKTEST_2X1_MAX_HUANGG, FALL_BET_MAX_HUANGG,
                    FALL_BET_MIN_CHANGE, PAIRING_STRATEGY,
                    SINGLE_BET_MAX_HUANGG)
from models import (BeidanBacktest2x1, BeidanDrawBet, BeidanFallBet,
                    BeidanMatch, BeidanSingleBet, BeidanValueMatch,
                    SessionLocal)
from time_utils import now_bj


def _kickoff_dt(match):
    try:
        date_str = (match.match_date or "").strip()[:10]
        time_str = (match.start_time or "00:00").strip()[:5]
        return datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def _parse_change_pct(change_pct: str) -> float:
    try:
        return float(change_pct.replace("%", "").replace("+", ""))
    except (ValueError, AttributeError):
        return 0.0


def _format_triplet(home, draw, away):
    return "/".join(f"{value:.2f}" if value else "--" for value in (home, draw, away))


def detect_beidan_value_bets():
    """检测北单参考SP高于皇G欧的机会，并生成独立回测记录。"""
    db = SessionLocal()
    try:
        now = now_bj()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")
        cutoff = now - datetime.timedelta(hours=2)

        matches = [
            m for m in db.query(BeidanMatch).all()
            if _kickoff_dt(m) and _kickoff_dt(m) >= cutoff
        ]

        today_bets = (db.query(BeidanValueMatch)
                      .filter(BeidanValueMatch.created_date == date_str)
                      .order_by(BeidanValueMatch.id.desc())
                      .all())
        latest_bets = {}
        for vb in today_bets:
            key = (vb.match_date, vb.match_no, vb.selected_option)
            if key not in latest_bets:
                latest_bets[key] = vb

        new_count = 0
        update_count = 0
        new_eligible_bets = []

        for m in matches:
            if not (m.home_odds and m.beidan_home):
                continue

            for opt_name, huangg_odds, beidan_odds in [
                ("主胜", m.home_odds, m.beidan_home),
                ("平局", m.draw_odds, m.beidan_draw),
                ("客胜", m.away_odds, m.beidan_away),
            ]:
                if not (huangg_odds and beidan_odds) or beidan_odds <= huangg_odds:
                    continue

                key = (m.match_date, m.match_no, opt_name)
                prev = latest_bets.get(key)
                if prev and prev.backtest_odds and prev.backtest_odds > 0 and prev.huangg_option and prev.huangg_option > 0:
                    old_ratio = prev.backtest_odds / prev.huangg_option
                    new_ratio = beidan_odds / huangg_odds
                    change = (new_ratio - old_ratio) * 100
                    if abs(change) > 20:
                        change_pct = "+0.00%"
                        prev = None
                    else:
                        change_pct = f"{change:+.2f}%"
                else:
                    change_pct = "+0.00%"

                backtest_eligible = 1 if huangg_odds < BACKTEST_2X1_MAX_HUANGG else 0
                if prev and abs((prev.current_odds or 0) - beidan_odds) < 0.005:
                    if prev.current_odds != round(beidan_odds, 2) or prev.change_pct != change_pct:
                        prev.current_odds = round(beidan_odds, 2)
                        prev.change_pct = change_pct
                        prev.backtest_eligible = backtest_eligible
                        prev.huangg_option = round(huangg_odds, 2)
                        prev.backtest_odds = round(beidan_odds, 2)
                        prev.huangg_home = round(m.home_odds, 2)
                        prev.huangg_draw = round(m.draw_odds, 2)
                        prev.huangg_away = round(m.away_odds, 2)
                        prev.huangg_odds = _format_triplet(m.home_odds, m.draw_odds, m.away_odds)
                        prev.jingcai_odds = _format_triplet(m.beidan_home, m.beidan_draw, m.beidan_away)
                        update_count += 1
                    continue

                value = BeidanValueMatch(
                    match_id=m.id,
                    capture_time=time_str,
                    created_date=date_str,
                    match_no=m.match_no,
                    league=m.league,
                    home_team=m.home_team,
                    away_team=m.away_team,
                    match_date=m.match_date,
                    start_time=m.start_time,
                    huangg_odds=_format_triplet(m.home_odds, m.draw_odds, m.away_odds),
                    jingcai_odds=_format_triplet(m.beidan_home, m.beidan_draw, m.beidan_away),
                    selected_option=opt_name,
                    backtest_odds=round(beidan_odds, 2),
                    huangg_option=round(huangg_odds, 2),
                    huangg_home=round(m.home_odds, 2),
                    huangg_draw=round(m.draw_odds, 2),
                    huangg_away=round(m.away_odds, 2),
                    current_odds=round(beidan_odds, 2),
                    change_pct=change_pct,
                    backtest_eligible=backtest_eligible,
                )
                db.add(value)
                new_count += 1
                if backtest_eligible:
                    new_eligible_bets.append(value)

        db.commit()

        single_count = _generate_single_bets(db, new_eligible_bets, now)
        fall_count = _generate_fall_bets(db, now)
        draw_count = _generate_draw_bets(db, now)
        combo_count = _generate_backtest_combos(db, new_eligible_bets, now)
        _update_final_odds(db)

        parts = []
        if new_count or update_count:
            parts.append(f"{new_count} 新")
            if update_count:
                parts.append(f"{update_count} 更新")
        if combo_count:
            parts.append(f"{combo_count} 组合")
        if single_count:
            parts.append(f"{single_count} 单关")
        if fall_count:
            parts.append(f"{fall_count} 跌水")
        if draw_count:
            parts.append(f"{draw_count} 平局")
        if parts:
            print(f"[{time_str}] 北单价值检测: " + ", ".join(parts))
        return True
    except Exception as e:
        db.rollback()
        print(f"北单价值检测错误: {e}")
        return False
    finally:
        db.close()


def _pair_list(db, bets, used, existing_pairs, time_str):
    created = 0
    i = 0
    while i < len(bets):
        v1 = bets[i]
        match_key1 = (v1.match_date, v1.match_no)
        if match_key1 in used:
            i += 1
            continue

        for j in range(i + 1, len(bets)):
            v2 = bets[j]
            match_key2 = (v2.match_date, v2.match_no)
            if match_key2 in used or match_key1 == match_key2:
                continue

            pair_key = (v1.match_date, v1.match_no, v1.selected_option,
                        v2.match_date, v2.match_no, v2.selected_option)
            if pair_key in existing_pairs:
                continue

            db.add(BeidanBacktest2x1(
                created_at=time_str,
                match1_no=v1.match_no, match1_league=v1.league,
                match1_home=v1.home_team, match1_away=v1.away_team,
                match1_option=v1.selected_option, match1_odds=v1.backtest_odds,
                match1_huangg=v1.huangg_option, match1_final=v1.current_odds,
                match1_match_date=v1.match_date, match1_start_time=v1.start_time,
                match2_no=v2.match_no, match2_league=v2.league,
                match2_home=v2.home_team, match2_away=v2.away_team,
                match2_option=v2.selected_option, match2_odds=v2.backtest_odds,
                match2_huangg=v2.huangg_option, match2_final=v2.current_odds,
                match2_match_date=v2.match_date, match2_start_time=v2.start_time,
                total_odds=round(v1.backtest_odds * v2.backtest_odds, 2),
                bet_amount=100,
                potential_win=round(v1.backtest_odds * v2.backtest_odds * 100, 2),
            ))
            existing_pairs.add(pair_key)
            used.add(match_key1)
            used.add(match_key2)
            created += 1
            break
        i += 1
    return created


def _generate_backtest_combos(db: Session, eligible_bets: list, now: datetime.datetime):
    if len(eligible_bets) < 2:
        return 0

    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    existing_pairs = set()
    for bt in db.query(BeidanBacktest2x1).all():
        existing_pairs.add((bt.match1_match_date, bt.match1_no, bt.match1_option,
                            bt.match2_match_date, bt.match2_no, bt.match2_option))
        existing_pairs.add((bt.match2_match_date, bt.match2_no, bt.match2_option,
                            bt.match1_match_date, bt.match1_no, bt.match1_option))

    created = 0
    used = set()
    if PAIRING_STRATEGY == "same_date":
        groups = defaultdict(list)
        for v in eligible_bets:
            groups[v.match_date].append(v)
        for date in sorted(groups.keys()):
            created += _pair_list(
                db,
                sorted(groups[date], key=lambda v: v.backtest_odds, reverse=True),
                used,
                existing_pairs,
                time_str,
            )
        remaining = [v for v in eligible_bets if (v.match_date, v.match_no) not in used]
        created += _pair_list(db, sorted(remaining, key=lambda v: v.backtest_odds, reverse=True), used, existing_pairs, time_str)
    else:
        created += _pair_list(db, sorted(eligible_bets, key=lambda v: v.backtest_odds, reverse=True), used, existing_pairs, time_str)

    if created:
        db.commit()
    return created


def _generate_single_bets(db: Session, eligible_bets: list, now: datetime.datetime):
    existing_keys = {(s.match_date, s.match_no, s.option_type) for s in db.query(BeidanSingleBet).all()}
    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    for v in eligible_bets:
        key = (v.match_date, v.match_no, v.selected_option)
        if key in existing_keys or v.huangg_option >= SINGLE_BET_MAX_HUANGG:
            continue
        db.add(BeidanSingleBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type=v.selected_option,
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=_parse_change_pct(v.change_pct), final=v.current_odds,
        ))
        existing_keys.add(key)
        created += 1
    if created:
        db.commit()
    return created


def _generate_fall_bets(db: Session, now: datetime.datetime):
    today = now.strftime("%Y-%m-%d")
    all_today = (db.query(BeidanValueMatch)
                 .filter(BeidanValueMatch.created_date == today)
                 .order_by(BeidanValueMatch.id.desc())
                 .all())
    latest = {}
    for v in all_today:
        latest.setdefault((v.match_date, v.match_no, v.selected_option), v)

    existing_keys = {(f.match_date, f.match_no, f.option_type) for f in db.query(BeidanFallBet).all()}
    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    for key, v in latest.items():
        if key in existing_keys or v.huangg_option >= FALL_BET_MAX_HUANGG:
            continue
        change_val = _parse_change_pct(v.change_pct)
        if abs(change_val) > 20 or change_val >= FALL_BET_MIN_CHANGE:
            continue
        db.add(BeidanFallBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type=v.selected_option,
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=change_val, final=v.current_odds,
        ))
        existing_keys.add(key)
        created += 1
    if created:
        db.commit()
    return created


def _generate_draw_bets(db: Session, now: datetime.datetime):
    today = now.strftime("%Y-%m-%d")
    all_today = (db.query(BeidanValueMatch)
                 .filter(BeidanValueMatch.created_date == today,
                         BeidanValueMatch.selected_option == "平局")
                 .order_by(BeidanValueMatch.id.desc())
                 .all())
    latest = {}
    for v in all_today:
        latest.setdefault((v.match_date, v.match_no), v)

    existing_keys = {(d.match_date, d.match_no) for d in db.query(BeidanDrawBet).all()}
    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    for key, v in latest.items():
        if key in existing_keys:
            continue
        db.add(BeidanDrawBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type="平局",
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=_parse_change_pct(v.change_pct), final=v.current_odds,
        ))
        existing_keys.add(key)
        created += 1
    if created:
        db.commit()
    return created


def _update_final_odds(db: Session):
    odds_map = {
        (m.match_date, m.match_no): {
            "home": m.beidan_home,
            "draw": m.beidan_draw,
            "away": m.beidan_away,
        }
        for m in db.query(BeidanMatch).all()
    }
    opt_map = {"主胜": "home", "平局": "draw", "客胜": "away"}

    for bt in db.query(BeidanBacktest2x1).filter(BeidanBacktest2x1.result == None).all():
        m1 = odds_map.get((bt.match1_match_date, bt.match1_no), {})
        m2 = odds_map.get((bt.match2_match_date, bt.match2_no), {})
        if opt_map.get(bt.match1_option) and m1.get(opt_map[bt.match1_option]):
            bt.match1_final = m1[opt_map[bt.match1_option]]
        if opt_map.get(bt.match2_option) and m2.get(opt_map[bt.match2_option]):
            bt.match2_final = m2[opt_map[bt.match2_option]]

    for model in (BeidanSingleBet, BeidanFallBet):
        for item in db.query(model).filter(model.result == None).all():
            values = odds_map.get((item.match_date, item.match_no), {})
            key = opt_map.get(item.option_type)
            if key and values.get(key):
                item.final = values[key]

    for item in db.query(BeidanDrawBet).filter(BeidanDrawBet.result == None).all():
        values = odds_map.get((item.match_date, item.match_no), {})
        if values.get("draw"):
            item.final = values["draw"]

    db.commit()
