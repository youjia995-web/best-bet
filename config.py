import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'odds_monitor.db')}",
)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
REFRESH_INTERVAL = 60  # seconds（对标原站）
REST_HOURS = []  # e.g. [0, 1, 2, 3, 4, 5, 6] to rest during midnight
DATA_SOURCE_STATE_FILE = os.path.join(DATA_DIR, "data_source_state.json")
DEFAULT_DATA_SOURCE = "20002028"

# 回测筛选阈值
BACKTEST_2X1_MAX_HUANGG = 3.5    # 2串1：皇G欧 < 3.5
SINGLE_BET_MAX_HUANGG = 4.0      # 单关：皇G欧 < 4
FALL_BET_MAX_HUANGG = 3.5        # 跌水：皇G欧 < 3.5
FALL_BET_MIN_CHANGE = -3.0       # 跌水：比率升跌 < -3

# 2串1配对策略: "same_date" | "odds_desc"
PAIRING_STRATEGY = "same_date"

# API 采集配置
API_TIMEOUT = 6        # 单次请求超时秒数
MAX_RETRIES = 3        # 最大重试次数
RETRY_BACKOFF = [1, 2, 4]  # 重试间隔秒数（指数退避）

# 备用接口：体彩竞彩 + Odds-API.io Bet365
# 免费注册后把 key 放到环境变量 ODDS_API_IO_KEY，或放到本项目根目录 odds_api_io.key。
SPORTTERY_API_BASE = "https://webapi.sporttery.cn"
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"
ODDS_API_IO_KEY_FILE = os.path.join(DATA_DIR, "odds_api_io.key")
ODDS_API_IO_KEY = (
    os.environ.get("ODDS_API_IO_KEY", "").strip()
    or (
        open(ODDS_API_IO_KEY_FILE, encoding="utf-8").read().strip()
        if os.path.exists(ODDS_API_IO_KEY_FILE)
        else ""
    )
)
ODDS_API_IO_BOOKMAKERS = os.environ.get("ODDS_API_IO_BOOKMAKERS", "Bet365,Unibet").strip()
ODDS_API_IO_EVENT_LIMIT = int(os.environ.get("ODDS_API_IO_EVENT_LIMIT", "120"))
ODDS_API_IO_CACHE_TTL = int(os.environ.get("ODDS_API_IO_CACHE_TTL", "60"))
ODDS_API_IO_STATE_FILE = os.path.join(DATA_DIR, "odds_api_io_state.json")
ODDS_API_IO_FREE_REQUESTS_PER_HOUR = int(os.environ.get("ODDS_API_IO_FREE_REQUESTS_PER_HOUR", "100"))
ODDS_API_IO_REQUEST_RESERVE = int(os.environ.get("ODDS_API_IO_REQUEST_RESERVE", "10"))
ODDS_API_IO_MIN_INTERVAL = int(os.environ.get("ODDS_API_IO_MIN_INTERVAL", "180"))
ODDS_API_IO_BATCH_SIZE = min(10, int(os.environ.get("ODDS_API_IO_BATCH_SIZE", "10")))
ODDS_API_IO_MAX_REQUESTS_PER_CYCLE = int(os.environ.get("ODDS_API_IO_MAX_REQUESTS_PER_CYCLE", "4"))
ODDS_API_IO_ODDS_STALE_AFTER = int(os.environ.get("ODDS_API_IO_ODDS_STALE_AFTER", "1200"))
