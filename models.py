from sqlalchemy import create_engine, Column, Integer, String, Float, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_no = Column(String(10))
    league = Column(String(50))
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(String(20))
    start_time = Column(String(10))
    status = Column(String(10))
    home_odds = Column(Float)
    draw_odds = Column(Float)
    away_odds = Column(Float)
    jingcai_home = Column(Float)
    jingcai_draw = Column(Float)
    jingcai_away = Column(Float)
    odds_time = Column(String(30))


class OddsChange(Base):
    __tablename__ = "odds_changes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    time = Column(String(30))
    league = Column(String(50))
    home = Column(String(100))
    away = Column(String(100))
    change_type = Column(String(20))
    from_odds = Column(Float)
    to_odds = Column(Float)
    odds = Column(String(30))


class ValueMatch(Base):
    __tablename__ = "value_matches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer)
    capture_time = Column(String(20))
    created_date = Column(String(20))
    match_no = Column(String(10))
    league = Column(String(50))
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(String(20))
    start_time = Column(String(10))
    huangg_odds = Column(String(30))
    jingcai_odds = Column(String(30))
    selected_option = Column(String(10))
    backtest_odds = Column(Float)
    huangg_option = Column(Float)
    huangg_home = Column(Float)
    huangg_draw = Column(Float)
    huangg_away = Column(Float)
    current_odds = Column(Float)
    change_pct = Column(String(10))
    backtest_eligible = Column(Integer, default=0)


class Backtest2x1(Base):
    __tablename__ = "backtest_2x1"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(String(30))
    match1_no = Column(String(10))
    match1_league = Column(String(50))
    match1_home = Column(String(100))
    match1_away = Column(String(100))
    match1_option = Column(String(10))
    match1_odds = Column(Float)
    match1_huangg = Column(Float)
    match1_final = Column(Float)
    match1_match_date = Column(String(20))
    match1_start_time = Column(String(10))
    match2_no = Column(String(10))
    match2_league = Column(String(50))
    match2_home = Column(String(100))
    match2_away = Column(String(100))
    match2_option = Column(String(10))
    match2_odds = Column(Float)
    match2_huangg = Column(Float)
    match2_final = Column(Float)
    match2_match_date = Column(String(20))
    match2_start_time = Column(String(10))
    total_odds = Column(Float)
    bet_amount = Column(Float, default=100)
    potential_win = Column(Float)
    result = Column(Integer, default=None, nullable=True)
    result_at = Column(String(30))


class SingleBet(Base):
    __tablename__ = "single_bet"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(String(30))
    match_no = Column(String(10))
    league = Column(String(50))
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(String(20))
    start_time = Column(String(10))
    option_type = Column(String(10))
    odds = Column(Float)
    huangg = Column(Float)
    bet_amount = Column(Float, default=100)
    potential_win = Column(Float)
    change_pct = Column(Float)
    result = Column(Integer, default=None, nullable=True)
    result_at = Column(String(30))
    final = Column(Float)


class FallBet(Base):
    __tablename__ = "fall_bet"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(String(30))
    match_no = Column(String(10))
    league = Column(String(50))
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(String(20))
    start_time = Column(String(10))
    option_type = Column(String(10))
    odds = Column(Float)
    huangg = Column(Float)
    bet_amount = Column(Float, default=100)
    potential_win = Column(Float)
    change_pct = Column(Float)
    result = Column(Integer, default=None, nullable=True)
    result_at = Column(String(30))
    final = Column(Float)


class DrawBet(Base):
    __tablename__ = "draw_bet"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(String(30))
    match_no = Column(String(10))
    league = Column(String(50))
    home_team = Column(String(100))
    away_team = Column(String(100))
    match_date = Column(String(20))
    start_time = Column(String(10))
    option_type = Column(String(10), default="平局")
    odds = Column(Float)
    huangg = Column(Float)
    bet_amount = Column(Float, default=100)
    potential_win = Column(Float)
    change_pct = Column(Float)
    result = Column(Integer, default=None, nullable=True)
    result_at = Column(String(30))
    final = Column(Float)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
