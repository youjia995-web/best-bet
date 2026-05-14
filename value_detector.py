"""价值投注检测器 + 2串1组合生成器 + 单关/跌水/平局回测"""
import datetime
from collections import defaultdict
from sqlalchemy.orm import Session
from models import (SessionLocal, Match, ValueMatch,
                    Backtest2x1, SingleBet, FallBet, DrawBet)
from config import (BACKTEST_2X1_MAX_HUANGG, SINGLE_BET_MAX_HUANGG,
                    FALL_BET_MAX_HUANGG, FALL_BET_MIN_CHANGE, PAIRING_STRATEGY)


def detect_value_bets():
    """检测价值投注。每次采集后运行，积累全天记录（与原站一致）"""
    db = SessionLocal()
    try:
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")

        matches = db.query(Match).all()

        # 过滤已结束的比赛（开赛时间超过2小时的视为已结束）
        cutoff = now - datetime.timedelta(hours=2)

        def kickoff_dt(m):
            try:
                date_str = (m.match_date or "").strip()[:10]
                time_str = (m.start_time or "00:00").strip()[:5]
                return datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return None

        matches = [m for m in matches if kickoff_dt(m) and kickoff_dt(m) >= cutoff]

        # 查询今天已有的 value_bets，用于计算 change_pct 和避免重复
        today_bets = (db.query(ValueMatch)
                      .filter(ValueMatch.created_date == date_str)
                      .order_by(ValueMatch.id.desc())
                      .all())

        # 按 (match_date, match_no, selected_option) 找最新记录（加日期防止跨日期碰撞）
        latest_bets = {}
        for vb in today_bets:
            key = (vb.match_date, vb.match_no, vb.selected_option)
            if key not in latest_bets:
                latest_bets[key] = vb

        new_count = 0
        update_count = 0
        new_eligible_bets = []  # 记录本批次新增的 eligible value bets

        for m in matches:
            if not (m.home_odds and m.jingcai_home):
                continue

            for opt_name, huangg_odds, jingcai_odds in [
                ("主胜", m.home_odds, m.jingcai_home),
                ("平局", m.draw_odds, m.jingcai_draw),
                ("客胜", m.away_odds, m.jingcai_away),
            ]:
                if jingcai_odds <= huangg_odds:
                    continue

                key = (m.match_date, m.match_no, opt_name)
                prev = latest_bets.get(key)

                # 计算升跌百分比（比率公式，对标原站 4-5% 范围）
                # 升跌 = (新竞彩/新皇冠 - 旧竞彩/旧皇冠) × 100
                if prev and prev.backtest_odds and prev.backtest_odds > 0 and prev.huangg_option and prev.huangg_option > 0:
                    old_ratio = prev.backtest_odds / prev.huangg_option
                    new_ratio = jingcai_odds / huangg_odds
                    change = (new_ratio - old_ratio) * 100
                    if abs(change) > 20:
                        change_pct = "+0.00%"
                        prev = None  # 丢弃异常基准，按首次检测处理
                    else:
                        change_pct = f"{change:+.2f}%"
                else:
                    change_pct = "+0.00%"

                backtest_eligible = 1 if huangg_odds < BACKTEST_2X1_MAX_HUANGG else 0

                # 如果赔率没变化，跳过（不重复插入相同记录）
                if prev and abs(prev.current_odds - jingcai_odds) < 0.005:
                    # 但需要更新 current_odds 和 change_pct（可能其他字段变了）
                    if prev.current_odds != round(jingcai_odds, 2) or prev.change_pct != change_pct:
                        prev.current_odds = round(jingcai_odds, 2)
                        prev.change_pct = change_pct
                        prev.backtest_eligible = backtest_eligible
                        prev.huangg_option = round(huangg_odds, 2)
                        prev.backtest_odds = round(jingcai_odds, 2)
                        prev.huangg_home = round(m.home_odds, 2)
                        prev.huangg_draw = round(m.draw_odds, 2)
                        prev.huangg_away = round(m.away_odds, 2)
                        prev.huangg_odds = f"{m.home_odds:.2f}/{m.draw_odds:.2f}/{m.away_odds:.2f}"
                        prev.jingcai_odds = f"{m.jingcai_home:.2f}/{m.jingcai_draw:.2f}/{m.jingcai_away:.2f}"
                        update_count += 1
                    continue

                value = ValueMatch(
                    match_id=m.id,
                    capture_time=time_str,
                    created_date=date_str,
                    match_no=m.match_no,
                    league=m.league,
                    home_team=m.home_team,
                    away_team=m.away_team,
                    match_date=m.match_date,
                    start_time=m.start_time,
                    huangg_odds=f"{m.home_odds:.2f}/{m.draw_odds:.2f}/{m.away_odds:.2f}",
                    jingcai_odds=f"{m.jingcai_home:.2f}/{m.jingcai_draw:.2f}/{m.jingcai_away:.2f}",
                    selected_option=opt_name,
                    backtest_odds=round(jingcai_odds, 2),
                    huangg_option=round(huangg_odds, 2),
                    huangg_home=round(m.home_odds, 2),
                    huangg_draw=round(m.draw_odds, 2),
                    huangg_away=round(m.away_odds, 2),
                    current_odds=round(jingcai_odds, 2),
                    change_pct=change_pct,
                    backtest_eligible=backtest_eligible,
                )
                db.add(value)
                new_count += 1

                # 收集本批次新增的 eligible bets
                if backtest_eligible == 1:
                    new_eligible_bets.append(value)

        db.commit()

        # 从本批次新增的 eligible 中生成各种回测记录
        single_count = _generate_single_bets(db, new_eligible_bets, now)
        fall_count = _generate_fall_bets(db, now)
        draw_count = _generate_draw_bets(db, now)
        created = _generate_backtest_combos(db, new_eligible_bets, now)
        _update_final_odds(db, now)

        parts = []
        if new_count or update_count:
            parts.append(f"{new_count} 新")
            if update_count:
                parts.append(f"{update_count} 更新")
        if created:
            parts.append(f"{created} 组合")
        if single_count:
            parts.append(f"{single_count} 单关")
        if fall_count:
            parts.append(f"{fall_count} 跌水")
        if draw_count:
            parts.append(f"{draw_count} 平局")

        if parts:
            print(f"[{time_str}] 价值检测: " + ", ".join(parts))
        return True
    except Exception as e:
        db.rollback()
        print(f"价值检测错误: {e}")
        return False
    finally:
        db.close()


def _parse_change_pct(change_pct: str) -> float:
    try:
        return float(change_pct.replace("%", "").replace("+", ""))
    except (ValueError, AttributeError):
        return 0.0


def _pair_list(db, bets, used, existing_pairs, time_str):
    """对一组 bet 列表进行顺序配对，返回创建数量。
    每场比赛只参与一个组合（用 match_no 去重，避免同场做胆牵连多组）。"""
    created = 0
    i = 0
    while i < len(bets):
        v1 = bets[i]
        # 用 match_no 去重，确保每场比赛只进入一个 2串1
        match_key1 = (v1.match_date, v1.match_no)
        key1 = (v1.match_date, v1.match_no, v1.selected_option)
        if match_key1 in used:
            i += 1
            continue

        paired = False
        for j in range(i + 1, len(bets)):
            v2 = bets[j]
            match_key2 = (v2.match_date, v2.match_no)
            key2 = (v2.match_date, v2.match_no, v2.selected_option)
            if match_key2 in used:
                continue
            if v1.match_no == v2.match_no and v1.match_date == v2.match_date:
                continue

            pair_key = (v1.match_date, v1.match_no, v1.selected_option,
                        v2.match_date, v2.match_no, v2.selected_option)
            if pair_key in existing_pairs:
                continue

            potential = round(v1.backtest_odds * v2.backtest_odds * 100, 2)

            bt = Backtest2x1(
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
                potential_win=potential,
            )
            db.add(bt)
            existing_pairs.add(pair_key)
            used.add(match_key1)
            used.add(match_key2)
            created += 1
            paired = True
            break

        i += 1
    return created


def _generate_backtest_combos(db: Session, eligible_bets: list, now: datetime.datetime):
    """从 eligible value bets 中生成2串1组合。
    策略：同日期优先配对，余数跨日期配对。
    每个 (match_no, option) 最多参与 1 个组合，同一比赛不同选项不互串。
    """
    if len(eligible_bets) < 2:
        return 0

    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    existing_pairs = set()
    for bt in db.query(Backtest2x1).all():
        existing_pairs.add((bt.match1_match_date, bt.match1_no, bt.match1_option,
                            bt.match2_match_date, bt.match2_no, bt.match2_option))
        existing_pairs.add((bt.match2_match_date, bt.match2_no, bt.match2_option,
                            bt.match1_match_date, bt.match1_no, bt.match1_option))

    created = 0
    used = set()

    if PAIRING_STRATEGY == "same_date":
        # 按 match_date 分组，组内按赔率降序配对
        groups = defaultdict(list)
        for v in eligible_bets:
            groups[v.match_date].append(v)
        for date in sorted(groups.keys()):
            date_bets = sorted(groups[date], key=lambda v: v.backtest_odds, reverse=True)
            created += _pair_list(db, date_bets, used, existing_pairs, time_str)

        # 跨日期：收集未使用的（按 match_no 去重），按赔率降序配对
        remaining = [v for v in eligible_bets if (v.match_date, v.match_no) not in used]
        remaining.sort(key=lambda v: v.backtest_odds, reverse=True)
        created += _pair_list(db, remaining, used, existing_pairs, time_str)
    else:
        # odds_desc: 全局按赔率降序配对
        sorted_bets = sorted(eligible_bets, key=lambda v: v.backtest_odds, reverse=True)
        created += _pair_list(db, sorted_bets, used, existing_pairs, time_str)

    if created:
        db.commit()
    return created


def _generate_single_bets(db: Session, eligible_bets: list, now: datetime.datetime):
    """从 eligible value bets 中生成单关回测记录。
    条件：皇冠赔率 < 4.0，去重：(match_no, option)
    """
    existing_keys = set()
    for sb in db.query(SingleBet).all():
        existing_keys.add((sb.match_date, sb.match_no, sb.option_type))

    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    for v in eligible_bets:
        key = (v.match_date, v.match_no, v.selected_option)
        if key in existing_keys:
            continue
        if v.huangg_option >= SINGLE_BET_MAX_HUANGG:
            continue

        change_val = _parse_change_pct(v.change_pct)
        sb = SingleBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type=v.selected_option,
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=change_val, final=v.current_odds,
        )
        db.add(sb)
        existing_keys.add(key)
        created += 1

    if created:
        db.commit()
    return created


def _generate_fall_bets(db: Session, now: datetime.datetime):
    """扫描当天所有 value bet 生成跌水回测记录。
    条件：皇冠赔率 < 3.5 且 change_pct < -3%，去重：(match_no, option)
    """
    today = now.strftime("%Y-%m-%d")
    all_today = (db.query(ValueMatch)
                 .filter(ValueMatch.created_date == today)
                 .order_by(ValueMatch.id.desc())
                 .all())

    latest = {}
    for v in all_today:
        key = (v.match_date, v.match_no, v.selected_option)
        if key not in latest:
            latest[key] = v

    existing_keys = set()
    for fb in db.query(FallBet).all():
        existing_keys.add((fb.match_date, fb.match_no, fb.option_type))

    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    for (match_date, match_no, option), v in latest.items():
        if (match_date, match_no, option) in existing_keys:
            continue
        if v.huangg_option >= FALL_BET_MAX_HUANGG:
            continue
        change_val = _parse_change_pct(v.change_pct)
        # 过滤 API 数据抖动：比率变化超过20视为脏数据
        if abs(change_val) > 20:
            continue
        if change_val >= FALL_BET_MIN_CHANGE:
            continue

        fb = FallBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type=v.selected_option,
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=change_val, final=v.current_odds,
        )
        db.add(fb)
        existing_keys.add((match_no, option))
        created += 1

    if created:
        db.commit()
    return created


def _generate_draw_bets(db: Session, now: datetime.datetime):
    """从当天 value bet 中筛选平局生成回测记录。
    条件：selected_option == "平局"，去重：match_no
    """
    today = now.strftime("%Y-%m-%d")
    all_today = (db.query(ValueMatch)
                 .filter(ValueMatch.created_date == today,
                         ValueMatch.selected_option == "平局")
                 .order_by(ValueMatch.id.desc())
                 .all())

    latest = {}
    for v in all_today:
        key = (v.match_date, v.match_no)
        if key not in latest:
            latest[key] = v

    existing_nos = set()
    for dbet in db.query(DrawBet).all():
        existing_nos.add((dbet.match_date, dbet.match_no))

    created = 0
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    for (match_date, match_no), v in latest.items():
        if (match_date, match_no) in existing_nos:
            continue
        change_val = _parse_change_pct(v.change_pct)
        dbet = DrawBet(
            created_at=time_str,
            match_no=v.match_no, league=v.league,
            home_team=v.home_team, away_team=v.away_team,
            match_date=v.match_date, start_time=v.start_time,
            option_type="平局",
            odds=v.backtest_odds, huangg=v.huangg_option,
            bet_amount=100,
            potential_win=round(v.backtest_odds * 100, 2),
            change_pct=change_val, final=v.current_odds,
        )
        db.add(dbet)
        existing_nos.add(match_no)
        created += 1

    if created:
        db.commit()
    return created


def _update_final_odds(db: Session, now: datetime.datetime):
    """每次采集后更新未确认回测记录的 final 赔率为最新竞彩赔率"""
    matches = db.query(Match).all()
    odds_map = {}
    for m in matches:
        odds_map[(m.match_date, m.match_no)] = {
            "home": m.jingcai_home,
            "draw": m.jingcai_draw,
            "away": m.jingcai_away,
        }

    opt_map = {"主胜": "home", "平局": "draw", "客胜": "away"}

    # Backtest2x1
    for bt in db.query(Backtest2x1).filter(Backtest2x1.result == None).all():
        m1 = odds_map.get((bt.match1_match_date, bt.match1_no), {})
        m2 = odds_map.get((bt.match2_match_date, bt.match2_no), {})
        k1 = opt_map.get(bt.match1_option)
        k2 = opt_map.get(bt.match2_option)
        if k1 and m1.get(k1):
            bt.match1_final = m1[k1]
        if k2 and m2.get(k2):
            bt.match2_final = m2[k2]

    # SingleBet
    for sb in db.query(SingleBet).filter(SingleBet.result == None).all():
        m = odds_map.get((sb.match_date, sb.match_no), {})
        k = opt_map.get(sb.option_type)
        if k and m.get(k):
            sb.final = m[k]

    # FallBet
    for fb in db.query(FallBet).filter(FallBet.result == None).all():
        m = odds_map.get((fb.match_date, fb.match_no), {})
        k = opt_map.get(fb.option_type)
        if k and m.get(k):
            fb.final = m[k]

    # DrawBet
    for dbet in db.query(DrawBet).filter(DrawBet.result == None).all():
        m = odds_map.get((dbet.match_date, dbet.match_no), {})
        if m.get("draw"):
            dbet.final = m["draw"]

    db.commit()
