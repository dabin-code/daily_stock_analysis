# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 存储层
===================================

职责：
1. 管理 SQLite 数据库连接（单例模式）
2. 定义 ORM 数据模型
3. 提供数据存取接口
4. 实现智能更新逻辑（断点续传）
"""

import atexit
from contextlib import contextmanager
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, TYPE_CHECKING, Tuple

import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    Integer,
    ForeignKey,
    Index,
    UniqueConstraint,
    Text,
    select,
    and_,
    or_,
    delete,
    update,
    event,
    desc,
    func,
)
from sqlalchemy.orm import (
    declarative_base,
    sessionmaker,
    Session,
)
from sqlalchemy.exc import IntegrityError

from src.config import get_config

logger = logging.getLogger(__name__)

# stock_daily.adj_convention 的合法取值（见 spec 7.1）。清单外的值会被后续
# 复权重建阶段当作 unverifiable 静默丢弃，所以落库前必须拦下来并告警。
VALID_ADJ_CONVENTIONS = frozenset({'raw', 'qfq', 'unknown'})

# SQLAlchemy ORM 基类
Base = declarative_base()

if TYPE_CHECKING:
    from src.search_service import SearchResponse


# === 数据模型定义 ===

class StockDaily(Base):
    """
    股票日线数据模型
    
    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """
    __tablename__ = 'stock_daily'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 股票代码（如 600519, 000001）
    code = Column(String(10), nullable=False, index=True)
    
    # 交易日期
    date = Column(Date, nullable=False, index=True)
    
    # OHLC 数据
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    
    # 成交数据
    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）
    
    # 技术指标
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比
    
    # 数据来源
    data_source = Column(String(50))  # 记录数据来源（如 AkshareFetcher）

    # ── B6: forward-adjustment factor anchoring ──────────────────────
    # qfq (前复权) prices in OHLC are anchored at the *fetch* moment by
    # default, which means a送股/分红 happening AFTER fetch silently
    # rewrites historical bars and breaks backtest reproducibility.
    # ``adj_factor`` records the factor that was applied at fetch time
    # so the backtest layer can re-normalise prices to a fixed anchor
    # (3b will start consuming this; 3a only persists it).
    #
    # Semantics:
    #   raw_price = qfq_price / adj_factor
    #   adj_factor = 1.0 means "no adjustment applied" (or unknown)
    #   adj_anchor_date is the date the qfq base was anchored to
    #   adj_factor_source records how the value was derived:
    #     - "akshare_qfq_div_raw" — computed from a second non-adjusted fetch
    #     - "tushare_native"      — pulled directly from a provider that exposes it
    #     - "legacy_assume_one"   — backfilled by migration for pre-B6 rows
    #     - "fetcher_unset"       — fetcher did not supply a value (defaults to 1.0)
    adj_factor = Column(Float, nullable=True)
    adj_anchor_date = Column(Date, nullable=True)
    adj_factor_source = Column(String(32), nullable=True, index=True)

    # 前收盘价。Tushare daily() 原生提供，是免费重建复权因子的唯一依据
    # （ratio = pre_close(t) / close(prev_observation)）。历史上三条写入路径
    # 全部丢弃了该字段，导致增量段无法自持。
    pre_close = Column(Float, nullable=True)
    # 该行价格的复权口径：raw / qfq / unknown（枚举取值见 spec 7.1）。
    # 阶段 E 只消费 raw 行，写错值等于让整批数据在复权体系里失效。
    # 实测发现不同数据源口径不一致（Tushare 不复权、Efinance 疑似前复权），
    # 混存会让复权重建在数据源边界上出错，因此必须逐行标注。
    adj_convention = Column(String(16), nullable=True, index=True)

    # 更新时间
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint('code', 'date', name='uix_code_date'),
        Index('ix_code_date', 'code', 'date'),
    )
    
    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'open': self.open,
            'high': self.high,
            'low': self.low,
            'close': self.close,
            'volume': self.volume,
            'amount': self.amount,
            'pct_chg': self.pct_chg,
            'ma5': self.ma5,
            'ma10': self.ma10,
            'ma20': self.ma20,
            'volume_ratio': self.volume_ratio,
            'data_source': self.data_source,
            'adj_factor': self.adj_factor,
            'adj_anchor_date': self.adj_anchor_date,
            'adj_factor_source': self.adj_factor_source,
        }


class StockDailyStaging(Base):
    """日线回补暂存区。

    阶段 C 的回补只写这里，生产表 stock_daily 由阶段 C2 经闸门原子提升。
    这样做的原因：回补要覆写 2024-2026 的存量，而这批存量正是判断
    数据源口径差异的证据本身，直接 INSERT OR REPLACE 会把证据抹掉。
    """
    __tablename__ = 'stock_daily_staging'

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)

    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)

    volume = Column(Float)  # 成交量（股）
    amount = Column(Float)  # 成交额（元）
    pct_chg = Column(Float)  # 涨跌幅（%）

    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    volume_ratio = Column(Float)  # 量比

    data_source = Column(String(50))

    # 与 stock_daily 同义，语义见 StockDaily 上的说明
    adj_factor = Column(Float, nullable=True)
    adj_anchor_date = Column(Date, nullable=True)
    adj_factor_source = Column(String(32), nullable=True, index=True)
    pre_close = Column(Float, nullable=True)
    adj_convention = Column(String(16), nullable=True, index=True)

    # 回补批次，用于断点续跑与按批回滚
    batch_id = Column(String(64), nullable=True, index=True)
    # 写入时的口径版本，供 _is_date_complete 判定「该日是否已按当前口径写过」
    convention_version = Column(String(32), nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # 用 Index(unique=True) 而不是 UniqueConstraint：后者在 SQLite 上落成
        # sqlite_autoindex_stock_daily_staging_1，给定的名字只出现在建表 DDL 的
        # CONSTRAINT 子句里，PRAGMA index_list 查不到，排查时会误判成「约束没建」。
        # 命名唯一索引在唯一性约束与 INSERT OR REPLACE 冲突消解上等价，且顺带
        # 省掉一条与其完全重复的 ix_staging_code_date。请勿改回 UniqueConstraint。
        Index('uix_staging_code_date', 'code', 'date', unique=True),
        Index('ix_staging_date_version', 'date', 'convention_version'),
    )


class NewsIntel(Base):
    """
    新闻情报数据模型

    存储搜索到的新闻情报条目，用于后续分析与查询
    """
    __tablename__ = 'news_intel'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联用户查询操作
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))

    # 搜索上下文
    dimension = Column(String(32), index=True)  # latest_news / risk_check / earnings / market_analysis / industry
    query = Column(String(255))
    provider = Column(String(32), index=True)

    # 新闻内容
    title = Column(String(300), nullable=False)
    snippet = Column(Text)
    url = Column(String(1000), nullable=False)
    source = Column(String(100))
    published_date = Column(DateTime, index=True)

    # 入库时间
    fetched_at = Column(DateTime, default=datetime.now, index=True)
    query_source = Column(String(32), index=True)  # bot/web/cli/system
    requester_platform = Column(String(20))
    requester_user_id = Column(String(64))
    requester_user_name = Column(String(64))
    requester_chat_id = Column(String(64))
    requester_message_id = Column(String(64))
    requester_query = Column(String(255))

    __table_args__ = (
        UniqueConstraint('url', name='uix_news_url'),
        Index('ix_news_code_pub', 'code', 'published_date'),
    )

    def __repr__(self) -> str:
        return f"<NewsIntel(code={self.code}, title={self.title[:20]}...)>"


class FundamentalSnapshot(Base):
    """
    基本面上下文快照（P0 write-only）。

    仅用于写入，主链路不依赖读取该表，便于后续回测/画像扩展。
    """
    __tablename__ = 'fundamental_snapshot'

    id = Column(Integer, primary_key=True, autoincrement=True)
    query_id = Column(String(64), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    source_chain = Column(Text)
    coverage = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_fundamental_snapshot_query_code', 'query_id', 'code'),
        Index('ix_fundamental_snapshot_created', 'created_at'),
    )

    def __repr__(self) -> str:
        return f"<FundamentalSnapshot(query_id={self.query_id}, code={self.code})>"


class AnalysisHistory(Base):
    """
    分析结果历史记录模型

    保存每次分析结果，支持按 query_id/股票代码检索
    """
    __tablename__ = 'analysis_history'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 关联查询链路
    query_id = Column(String(64), index=True)

    # 股票信息
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    report_type = Column(String(16), index=True)

    # 核心结论
    sentiment_score = Column(Integer)
    operation_advice = Column(String(20))
    trend_prediction = Column(String(50))
    analysis_summary = Column(Text)

    # 详细数据
    raw_result = Column(Text)
    news_content = Column(Text)
    context_snapshot = Column(Text)

    # 狙击点位（用于回测）
    ideal_buy = Column(Float)
    secondary_buy = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)

    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_analysis_code_time', 'code', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'id': self.id,
            'query_id': self.query_id,
            'code': self.code,
            'name': self.name,
            'report_type': self.report_type,
            'sentiment_score': self.sentiment_score,
            'operation_advice': self.operation_advice,
            'trend_prediction': self.trend_prediction,
            'analysis_summary': self.analysis_summary,
            'raw_result': self.raw_result,
            'news_content': self.news_content,
            'context_snapshot': self.context_snapshot,
            'ideal_buy': self.ideal_buy,
            'secondary_buy': self.secondary_buy,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class BacktestResult(Base):
    """单条分析记录的回测结果。"""

    __tablename__ = 'backtest_results'

    id = Column(Integer, primary_key=True, autoincrement=True)

    analysis_history_id = Column(
        Integer,
        ForeignKey('analysis_history.id'),
        nullable=False,
        index=True,
    )

    # 冗余字段，便于按股票筛选
    code = Column(String(10), nullable=False, index=True)
    analysis_date = Column(Date, index=True)

    # 回测参数
    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default='v1')

    # 状态
    eval_status = Column(String(16), nullable=False, default='pending')
    evaluated_at = Column(DateTime, default=datetime.now, index=True)

    # 建议快照（避免未来分析字段变化导致回测不可解释）
    operation_advice = Column(String(20))
    position_recommendation = Column(String(8))  # long/cash

    # 价格与收益
    start_price = Column(Float)
    end_close = Column(Float)
    max_high = Column(Float)
    min_low = Column(Float)
    stock_return_pct = Column(Float)

    # 方向与结果
    direction_expected = Column(String(16))  # up/down/flat/not_down
    direction_correct = Column(Boolean, nullable=True)
    outcome = Column(String(16))  # win/loss/neutral

    # 目标价命中（仅 long 且配置了止盈/止损时有意义）
    stop_loss = Column(Float)
    take_profit = Column(Float)
    hit_stop_loss = Column(Boolean)
    hit_take_profit = Column(Boolean)
    first_hit = Column(String(16))  # take_profit/stop_loss/ambiguous/neither/not_applicable
    first_hit_date = Column(Date)
    first_hit_trading_days = Column(Integer)

    # 模拟执行（long-only）
    simulated_entry_price = Column(Float)
    simulated_exit_price = Column(Float)
    simulated_exit_reason = Column(String(24))  # stop_loss/take_profit/window_end/cash/ambiguous_stop_loss
    simulated_return_pct = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            'analysis_history_id',
            'eval_window_days',
            'engine_version',
            name='uix_backtest_analysis_window_version',
        ),
        Index('ix_backtest_code_date', 'code', 'analysis_date'),
    )


class BacktestSummary(Base):
    """回测汇总指标（按股票或全局）。"""

    __tablename__ = 'backtest_summaries'

    id = Column(Integer, primary_key=True, autoincrement=True)

    scope = Column(String(16), nullable=False, index=True)  # overall/stock
    code = Column(String(16), index=True)

    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default='v1')
    computed_at = Column(DateTime, default=datetime.now, index=True)

    # 计数
    total_evaluations = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    insufficient_count = Column(Integer, default=0)
    long_count = Column(Integer, default=0)
    cash_count = Column(Integer, default=0)

    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    neutral_count = Column(Integer, default=0)

    # 准确率/胜率
    direction_accuracy_pct = Column(Float)
    win_rate_pct = Column(Float)
    neutral_rate_pct = Column(Float)

    # 收益
    avg_stock_return_pct = Column(Float)
    avg_simulated_return_pct = Column(Float)

    # 目标价触发统计（仅 long 且配置止盈/止损时统计）
    stop_loss_trigger_rate = Column(Float)
    take_profit_trigger_rate = Column(Float)
    ambiguous_rate = Column(Float)
    avg_days_to_first_hit = Column(Float)

    # 诊断字段（JSON 字符串）
    advice_breakdown_json = Column(Text)
    diagnostics_json = Column(Text)

    __table_args__ = (
        UniqueConstraint(
            'scope',
            'code',
            'eval_window_days',
            'engine_version',
            name='uix_backtest_summary_scope_code_window_version',
        ),
    )


class PortfolioAccount(Base):
    """Portfolio account metadata."""

    __tablename__ = 'portfolio_accounts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(String(64), index=True)
    name = Column(String(64), nullable=False)
    broker = Column(String(64))
    market = Column(String(8), nullable=False, default='cn', index=True)  # cn/hk/us
    base_currency = Column(String(8), nullable=False, default='CNY')
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index('ix_portfolio_account_owner_active', 'owner_id', 'is_active'),
    )


class PortfolioTrade(Base):
    """Executed trade events used as the source of truth for replay."""

    __tablename__ = 'portfolio_trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    trade_uid = Column(String(128))
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    trade_date = Column(Date, nullable=False, index=True)
    side = Column(String(8), nullable=False)  # buy/sell
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    note = Column(String(255))
    dedup_hash = Column(String(64), index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint('account_id', 'trade_uid', name='uix_portfolio_trade_uid'),
        UniqueConstraint('account_id', 'dedup_hash', name='uix_portfolio_trade_dedup_hash'),
        Index('ix_portfolio_trade_account_date', 'account_id', 'trade_date'),
    )


class PortfolioCashLedger(Base):
    """Cash in/out events."""

    __tablename__ = 'portfolio_cash_ledger'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    event_date = Column(Date, nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # in/out
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default='CNY')
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_cash_account_date', 'account_id', 'event_date'),
    )


class PortfolioCorporateAction(Base):
    """Corporate actions that impact cash or share quantity."""

    __tablename__ = 'portfolio_corporate_actions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    effective_date = Column(Date, nullable=False, index=True)
    action_type = Column(String(24), nullable=False)  # cash_dividend/split_adjustment
    cash_dividend_per_share = Column(Float)
    split_ratio = Column(Float)
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_ca_account_date', 'account_id', 'effective_date'),
    )


class PortfolioPosition(Base):
    """Latest replayed position snapshot for each symbol in one account."""

    __tablename__ = 'portfolio_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    quantity = Column(Float, nullable=False, default=0.0)
    avg_cost = Column(Float, nullable=False, default=0.0)
    total_cost = Column(Float, nullable=False, default=0.0)
    last_price = Column(Float, nullable=False, default=0.0)
    market_value_base = Column(Float, nullable=False, default=0.0)
    unrealized_pnl_base = Column(Float, nullable=False, default=0.0)
    valuation_currency = Column(String(8), nullable=False, default='CNY')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint(
            'account_id',
            'symbol',
            'market',
            'currency',
            'cost_method',
            name='uix_portfolio_position_account_symbol_market_currency',
        ),
    )


class PortfolioPositionLot(Base):
    """Lot-level remaining quantities used by FIFO replay."""

    __tablename__ = 'portfolio_position_lots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default='cn')
    currency = Column(String(8), nullable=False, default='CNY')
    open_date = Column(Date, nullable=False, index=True)
    remaining_quantity = Column(Float, nullable=False, default=0.0)
    unit_cost = Column(Float, nullable=False, default=0.0)
    source_trade_id = Column(Integer, ForeignKey('portfolio_trades.id'))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        Index('ix_portfolio_lot_account_symbol', 'account_id', 'symbol'),
    )


class PortfolioDailySnapshot(Base):
    """Daily account snapshot generated by read-time replay."""

    __tablename__ = 'portfolio_daily_snapshots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey('portfolio_accounts.id'), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default='fifo')  # fifo/avg
    base_currency = Column(String(8), nullable=False, default='CNY')
    total_cash = Column(Float, nullable=False, default=0.0)
    total_market_value = Column(Float, nullable=False, default=0.0)
    total_equity = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    fee_total = Column(Float, nullable=False, default=0.0)
    tax_total = Column(Float, nullable=False, default=0.0)
    fx_stale = Column(Boolean, nullable=False, default=False)
    payload = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            'account_id',
            'snapshot_date',
            'cost_method',
            name='uix_portfolio_snapshot_account_date_method',
        ),
    )


class PortfolioFxRate(Base):
    """Cached FX rates used for cross-currency portfolio conversion."""

    __tablename__ = 'portfolio_fx_rates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String(8), nullable=False, index=True)
    to_currency = Column(String(8), nullable=False, index=True)
    rate_date = Column(Date, nullable=False, index=True)
    rate = Column(Float, nullable=False)
    source = Column(String(32), nullable=False, default='manual')
    is_stale = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            'from_currency',
            'to_currency',
            'rate_date',
            name='uix_portfolio_fx_pair_date',
        ),
    )


class ConversationMessage(Base):
    """
    Agent 对话历史记录表
    """
    __tablename__ = 'conversation_messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), index=True, nullable=False)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)


class LLMUsage(Base):
    """One row per litellm.completion() call — token-usage audit log."""

    __tablename__ = 'llm_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 'analysis' | 'agent' | 'market_review'
    call_type = Column(String(32), nullable=False, index=True)
    model = Column(String(128), nullable=False)
    stock_code = Column(String(16), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    called_at = Column(DateTime, default=datetime.now, index=True)


class InstrumentMaster(Base):
    """正式股票池主数据。"""

    __tablename__ = "instrument_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(16), nullable=False, unique=True, index=True)
    name = Column(String(64), nullable=False)
    market = Column(String(16), nullable=False, default="cn", index=True)
    exchange = Column(String(16), nullable=True, index=True)
    listing_status = Column(String(16), nullable=False, default="active", index=True)
    is_st = Column(Boolean, nullable=False, default=False, index=True)
    industry = Column(String(64))
    list_date = Column(Date, nullable=True, index=True)
    # 退市日期。缺失该字段时无法区分「停牌」「退市」「抓取失败」三类缺口，
    # 也无法消除幸存者偏差——已退市股票会整体缺席历史回测。
    delist_date = Column(Date, nullable=True, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "market": self.market,
            "exchange": self.exchange,
            "listing_status": self.listing_status,
            "is_st": self.is_st,
            "industry": self.industry,
            "list_date": self.list_date.isoformat() if self.list_date else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TradingCalendar(Base):
    """交易日历权威表。

    既有 src/core/trading_calendar.py 依赖第三方库且 fail-open，
    不能用于回补与缺口归因——把未知日期当作开市会制造假缺口。
    本表落库后，日历查询改为 fail-closed：查不到即报错，不猜。
    """

    __tablename__ = "trading_calendar"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    trade_date = Column(Date, nullable=False, index=True)
    is_open = Column(Boolean, nullable=False, default=True)
    source = Column(String(32), nullable=False, default="akshare")
    # 与 exchange_calendars 对照的结果：match / mismatch / unchecked
    cross_check = Column(String(16), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        # 与 StockDailyStaging 同样的取舍：用 Index(unique=True) 而不是
        # UniqueConstraint。后者在 SQLite 上落成 sqlite_autoindex_trading_calendar_1，
        # 给定的名字只出现在建表 DDL 里，PRAGMA index_list 查不到，排查时会误判成
        # 「约束没建」；命名唯一索引在唯一性与 INSERT OR REPLACE 冲突消解上等价，
        # 且顺带省掉一条与其完全重复的普通索引。请勿改回 UniqueConstraint。
        Index('uix_calendar_market_date', 'market', 'trade_date', unique=True),
    )


class BoardMaster(Base):
    """股票所属板块主数据。"""

    __tablename__ = "board_master"

    id = Column(Integer, primary_key=True, autoincrement=True)
    board_code = Column(String(64), nullable=True, index=True)
    board_name = Column(String(128), nullable=False)
    board_type = Column(String(32), nullable=False, default="unknown", index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    source = Column(String(32), nullable=False, default="unknown", index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("market", "source", "board_name", "board_type", name="uix_board_identity"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "board_code": self.board_code,
            "board_name": self.board_name,
            "board_type": self.board_type,
            "market": self.market,
            "source": self.source,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class InstrumentBoardMembership(Base):
    """股票和板块的多对多关系。"""

    __tablename__ = "instrument_board_membership"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument_code = Column(String(16), nullable=False, index=True)
    board_id = Column(Integer, ForeignKey("board_master.id"), nullable=False, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    source = Column(String(32), nullable=False, default="unknown", index=True)
    is_primary = Column(Boolean, nullable=False, default=False, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("instrument_code", "board_id", "source", name="uix_instrument_board_membership"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instrument_code": self.instrument_code,
            "board_id": self.board_id,
            "market": self.market,
            "source": self.source,
            "is_primary": self.is_primary,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DailyFactorSnapshot(Base):
    """日度因子快照。"""

    __tablename__ = "daily_factor_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    code = Column(String(16), nullable=False, index=True)
    close = Column(Float)
    pct_chg = Column(Float)
    ma5 = Column(Float)
    ma10 = Column(Float)
    ma20 = Column(Float)
    ma60 = Column(Float)
    volume_ratio = Column(Float)
    turnover_rate = Column(Float)
    trend_score = Column(Float)
    liquidity_score = Column(Float)
    risk_flags_json = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uix_factor_snapshot_trade_code"),
        Index("ix_factor_snapshot_trade_code", "trade_date", "code"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "code": self.code,
            "close": self.close,
            "pct_chg": self.pct_chg,
            "ma5": self.ma5,
            "ma10": self.ma10,
            "ma20": self.ma20,
            "ma60": self.ma60,
            "volume_ratio": self.volume_ratio,
            "turnover_rate": self.turnover_rate,
            "trend_score": self.trend_score,
            "liquidity_score": self.liquidity_score,
            "risk_flags": json.loads(self.risk_flags_json) if self.risk_flags_json else [],
        }


class ScreeningRun(Base):
    """全市场筛选任务记录。"""

    __tablename__ = "screening_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    trade_date = Column(Date, nullable=False, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    universe_size = Column(Integer, nullable=False, default=0)
    candidate_count = Column(Integer, nullable=False, default=0)
    ai_top_k = Column(Integer, nullable=False, default=0)
    config_snapshot = Column(Text)
    error_summary = Column(Text)
    started_at = Column(DateTime, default=datetime.now, index=True)
    completed_at = Column(DateTime, nullable=True, index=True)
    last_activity_at = Column(DateTime, nullable=True)
    # -- Notification lifecycle fields --
    trigger_type = Column(String(32), nullable=False, default="manual")
    notification_status = Column(String(32), nullable=True)
    notification_attempts = Column(Integer, nullable=False, default=0)
    notification_sent_at = Column(DateTime, nullable=True)
    notification_error = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_screening_run_trade_market", "trade_date", "market"),
    )

    def to_dict(self) -> Dict[str, Any]:
        snapshot = json.loads(self.config_snapshot) if self.config_snapshot else {}
        return {
            "run_id": self.run_id,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "market": self.market,
            "mode": snapshot.get("mode", "balanced"),
            "status": self.status,
            "universe_size": self.universe_size,
            "candidate_count": self.candidate_count,
            "ai_top_k": self.ai_top_k,
            "config_snapshot": snapshot,
            "error_summary": self.error_summary,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
            "trigger_type": self.trigger_type or "manual",
            "notification_status": self.notification_status,
            "notification_attempts": self.notification_attempts or 0,
            "notification_sent_at": self.notification_sent_at.isoformat() if self.notification_sent_at else None,
            "notification_error": self.notification_error,
        }


class ScreeningCandidate(Base):
    """全市场筛选候选结果。"""

    __tablename__ = "screening_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("screening_runs.run_id"), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    name = Column(String(50))
    rank = Column(Integer, nullable=False, index=True)
    # ``rule_score`` 是五层优先级排序原地覆盖后的复合优先级分
    # （stage + pool + theme + 原始分 * 0.01），不是 screener 的质量分。
    rule_score = Column(Float, nullable=False, default=0.0)
    # 覆盖前 screener 产出的原始质量分，供 Rank IC 等连续量分析使用。
    raw_rule_score = Column(Float, nullable=True)
    selected_for_ai = Column(Boolean, nullable=False, default=False)
    candidate_decision_json = Column(Text)
    matched_strategies_json = Column(Text)
    rule_hits_json = Column(Text)
    factor_snapshot_json = Column(Text)
    ai_query_id = Column(String(64), index=True)
    ai_summary = Column(Text)
    ai_operation_advice = Column(String(20))
    # -- AI 二筛协议字段 (Phase 3B-1) --
    ai_trade_stage = Column(String(32), nullable=True)
    ai_reasoning = Column(Text, nullable=True)
    ai_confidence = Column(Float, nullable=True)
    # -- 五层系统新增字段 (Phase 1) --
    trade_stage = Column(String(32), nullable=True)
    setup_type = Column(String(64), nullable=True)
    entry_maturity = Column(String(16), nullable=True)
    risk_level = Column(String(16), nullable=True)
    market_regime = Column(String(32), nullable=True)
    theme_position = Column(String(32), nullable=True)
    candidate_pool_level = Column(String(32), nullable=True)
    trade_plan_json = Column(Text, nullable=True)
    # ``created_at`` is the FIRST time this (run_id, code) pair was persisted.
    # ``updated_at`` is the LATEST persistence time. They diverge when a
    # candidate is re-saved (e.g. AI re-screening, manual rerun) — backtest
    # consumers can compare ``updated_at`` against backtest_run.started_at to
    # detect whether the row was rewritten after the backtest snapshot was
    # supposed to be frozen (A4 lite: visibility instead of immutable table).
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("run_id", "code", name="uix_screening_candidate_run_code"),
        Index("ix_screening_candidate_run_rank", "run_id", "rank"),
    )

    def to_dict(self) -> Dict[str, Any]:
        fallback_payload = {
            "id": self.id,
            "run_id": self.run_id,
            "code": self.code,
            "name": self.name,
            "rank": self.rank,
            # 复合优先级分（stage + pool + theme + 原始质量分 * 0.01）
            "rule_score": self.rule_score,
            "raw_rule_score": self.raw_rule_score,
            "selected_for_ai": self.selected_for_ai,
            "matched_strategies": json.loads(self.matched_strategies_json) if self.matched_strategies_json else [],
            "rule_hits": json.loads(self.rule_hits_json) if self.rule_hits_json else [],
            "factor_snapshot": json.loads(self.factor_snapshot_json) if self.factor_snapshot_json else {},
            "ai_query_id": self.ai_query_id,
            "ai_summary": self.ai_summary,
            "ai_operation_advice": self.ai_operation_advice,
            "ai_trade_stage": self.ai_trade_stage,
            "ai_reasoning": self.ai_reasoning,
            "ai_confidence": self.ai_confidence,
            "trade_stage": self.trade_stage,
            "setup_type": self.setup_type,
            "entry_maturity": self.entry_maturity,
            "risk_level": self.risk_level,
            "market_regime": self.market_regime,
            "theme_position": self.theme_position,
            "candidate_pool_level": self.candidate_pool_level,
            "trade_plan": json.loads(self.trade_plan_json) if self.trade_plan_json else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if self.candidate_decision_json:
            try:
                payload = json.loads(self.candidate_decision_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning(
                    "Failed to decode candidate_decision_json for screening candidate %s/%s; falling back to row fields",
                    self.run_id,
                    self.code,
                )
            else:
                if isinstance(payload, dict):
                    return {**fallback_payload, **payload}
        return fallback_payload


class KlineAuditRun(Base):
    """K 线审计执行记录。"""

    __tablename__ = "kline_audit_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    trade_date = Column(Date, nullable=False, index=True)
    run_type = Column(String(32), nullable=False, default="daily")
    trigger_type = Column(String(32), nullable=False, default="manual")
    run_result = Column(String(32), nullable=False, default="pending", index=True)
    pass_status = Column(String(32), nullable=False, default="pending", index=True)
    rule_version = Column(String(64), nullable=False)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.now, index=True)
    completed_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        Index("ix_kline_audit_runs_market_trade_date", "market", "trade_date"),
    )


class KlineAuditTradeDate(Base):
    """交易日级审计真相源。"""

    __tablename__ = "kline_audit_trade_dates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    trade_date = Column(Date, nullable=False, index=True)
    pass_status = Column(String(32), nullable=False, default="pending", index=True)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    rule_version = Column(String(64), nullable=False)
    source_run_id = Column(String(64), ForeignKey("kline_audit_runs.run_id"), nullable=False, index=True)
    checked_at = Column(DateTime, nullable=False, default=datetime.now)
    passed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("market", "trade_date", name="uix_kline_audit_trade_date_market_date"),
        Index("ix_kline_audit_trade_dates_market_status", "market", "pass_status"),
    )


class KlineAuditGap(Base):
    """K 线缺口真相源。"""

    __tablename__ = "kline_audit_gaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gap_key = Column(String(255), nullable=False, unique=True, index=True)
    source_run_id = Column(String(64), ForeignKey("kline_audit_runs.run_id"), nullable=False, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    gap_scope = Column(String(32), nullable=False, index=True)
    code = Column(String(16), nullable=True, index=True)
    trade_date = Column(Date, nullable=True, index=True)
    missing_date_from = Column(Date, nullable=True)
    missing_date_to = Column(Date, nullable=True)
    status = Column(String(32), nullable=False, default="open", index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_kline_audit_gaps_market_scope_status", "market", "gap_scope", "status"),
    )

    @staticmethod
    def build_gap_key(
        *,
        market: str,
        gap_scope: str,
        trade_date: Optional[date] = None,
        code: Optional[str] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
    ) -> str:
        if gap_scope == "market_day_gap":
            if trade_date is None:
                raise ValueError("market_day_gap requires trade_date")
            return f"{market}|{trade_date.isoformat()}|{gap_scope}"
        if gap_scope == "symbol_range_gap":
            if not code or missing_date_from is None or missing_date_to is None:
                raise ValueError("symbol_range_gap requires code, missing_date_from, and missing_date_to")
            return (
                f"{market}|{code}|{missing_date_from.isoformat()}|"
                f"{missing_date_to.isoformat()}|{gap_scope}"
            )
        raise ValueError(f"Unsupported gap_scope: {gap_scope}")


class KlineAuditEvent(Base):
    """K 线审计事件流水。"""

    __tablename__ = "kline_audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_run_id = Column(String(64), ForeignKey("kline_audit_runs.run_id"), nullable=True, index=True)
    gap_key = Column(String(255), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    event_status = Column(String(32), nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)


class KlineSkipRegistry(Base):
    """K 线豁免登记真相源。"""

    __tablename__ = "kline_skip_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skip_key = Column(String(255), nullable=False, unique=True, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    gap_scope = Column(String(32), nullable=False, index=True)
    code = Column(String(16), nullable=True, index=True)
    trade_date = Column(Date, nullable=True, index=True)
    missing_date_from = Column(Date, nullable=True)
    missing_date_to = Column(Date, nullable=True)
    status = Column(String(32), nullable=False, default="candidate_skip", index=True)
    reason_type = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    approved_by = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    success_streak = Column(Integer, nullable=False, default=0)
    last_success_at = Column(DateTime, nullable=True)
    last_recovered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_kline_skip_registry_market_scope_status", "market", "gap_scope", "status"),
    )

    @staticmethod
    def build_skip_key(
        *,
        market: str,
        gap_scope: str,
        trade_date: Optional[date] = None,
        code: Optional[str] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
    ) -> str:
        if gap_scope == "market_day_gap":
            if trade_date is None:
                raise ValueError("market_day_gap requires trade_date")
            return f"{market}|{trade_date.isoformat()}|{gap_scope}"
        if gap_scope == "symbol_range_gap":
            if not code or missing_date_from is None or missing_date_to is None:
                raise ValueError("symbol_range_gap requires code, missing_date_from, and missing_date_to")
            return (
                f"{market}|{code}|{missing_date_from.isoformat()}|"
                f"{missing_date_to.isoformat()}|{gap_scope}"
            )
        raise ValueError(f"Unsupported gap_scope: {gap_scope}")


class DailySectorHeat(Base):
    """板块每日热度快照——SectorHeatEngine 的持久化输出。"""

    __tablename__ = "daily_sector_heat"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    board_name = Column(String(128), nullable=False, index=True)
    board_type = Column(String(32), nullable=False, default="concept")
    # 四维分数
    breadth_score = Column(Float, default=0.0)
    strength_score = Column(Float, default=0.0)
    persistence_score = Column(Float, default=0.0)
    leadership_score = Column(Float, default=0.0)
    sector_hot_score = Column(Float, default=0.0, index=True)
    # 状态判定
    sector_status = Column(String(16))   # hot/warm/neutral/cold
    sector_stage = Column(String(16))    # launch/ferment/expand/climax/fade
    # 统计元数据
    stock_count = Column(Integer, default=0)
    up_count = Column(Integer, default=0)
    limit_up_count = Column(Integer, default=0)
    avg_pct_chg = Column(Float, default=0.0)
    leader_codes_json = Column(Text)     # JSON: ["600519", ...]
    front_codes_json = Column(Text)      # JSON: ["600519", "000858", ...]
    board_strength_score = Column(Float, default=0.0, index=True)
    board_strength_rank = Column(Integer, default=0, index=True)
    board_strength_percentile = Column(Float, default=0.0)
    leader_candidate_count = Column(Integer, default=0)
    quality_flags_json = Column(Text)
    # 审计
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("trade_date", "board_name", name="uq_sector_heat_date_board"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "trade_date": self.trade_date,
            "board_name": self.board_name,
            "board_type": self.board_type,
            "breadth_score": self.breadth_score,
            "strength_score": self.strength_score,
            "persistence_score": self.persistence_score,
            "leadership_score": self.leadership_score,
            "sector_hot_score": self.sector_hot_score,
            "sector_status": self.sector_status,
            "sector_stage": self.sector_stage,
            "stock_count": self.stock_count,
            "up_count": self.up_count,
            "limit_up_count": self.limit_up_count,
            "avg_pct_chg": self.avg_pct_chg,
            "leader_codes_json": self.leader_codes_json,
            "front_codes_json": self.front_codes_json,
            "board_strength_score": self.board_strength_score,
            "board_strength_rank": self.board_strength_rank,
            "board_strength_percentile": self.board_strength_percentile,
            "leader_candidate_count": self.leader_candidate_count,
            "quality_flags_json": self.quality_flags_json,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class DatabaseManager:
    """
    数据库管理器 - 单例模式
    
    职责：
    1. 管理数据库连接池
    2. 提供 Session 上下文管理
    3. 封装数据存取操作
    """
    
    _instance: Optional['DatabaseManager'] = None
    _initialized: bool = False

    _screening_status_transitions = {
        "pending": {
            "pending",
            "resolving_universe",
            "syncing_universe",
            "ingesting",
            "factorizing",
            "screening",
            "ai_enriching",
            "failed",
        },
        "resolving_universe": {"resolving_universe", "syncing_universe", "ingesting", "failed"},
        "syncing_universe": {"syncing_universe", "ingesting", "failed"},
        "ingesting": {"ingesting", "factorizing", "failed"},
        "factorizing": {"factorizing", "screening", "failed"},
        "screening": {"screening", "ai_enriching", "completed", "completed_with_ai_degraded", "failed"},
        "ai_enriching": {"ai_enriching", "completed", "completed_with_ai_degraded", "failed"},
        "completed": {"completed"},
        "completed_with_ai_degraded": {"completed_with_ai_degraded"},
        "failed": {"failed"},
    }

    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_url: Optional[str] = None):
        """
        初始化数据库管理器
        
        Args:
            db_url: 数据库连接 URL（可选，默认从配置读取）
        """
        if getattr(self, '_initialized', False):
            return
        
        if db_url is None:
            config = get_config()
            db_url = config.get_db_url()
        
        # 创建数据库引擎
        _is_sqlite = db_url and db_url.startswith("sqlite")
        self._engine = create_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
            # SQLite: 锁等待 30s，防止并发写入时立即报 database is locked
            connect_args={"timeout": 30} if _is_sqlite else {},
        )

        # SQLite: 启用外键约束 + 设置忙等超时 + 启用 WAL
        if _is_sqlite:
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                # WAL 让读写不互相阻塞。历史数据回补是数小时、数千次事务的长作业，
                # 默认的 rollback journal 会让写锁阻塞全部读，与每日任务相撞即
                # database is locked。
                # 切换 WAL 需要写库头，会在多种真实场景下失败：升级时另一个连接正持有
                # 读事务（等满 busy_timeout 后 database is locked）、库文件只读、
                # 库放在不支持共享内存索引的网络盘。这些情况下 DELETE 模式只是慢，
                # 应用本身仍然正确，绝不能因此让整个进程拿不到连接。
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError as exc:
                    logger.warning("启用 WAL 失败，沿用当前 journal 模式: %s", exc)
                cursor.close()

        # 创建 Session 工厂
        self._SessionLocal = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
        )

        # Register backtest ORM models so their tables are included in create_all
        try:
            import src.backtest.models.backtest_models  # noqa: F401
        except ImportError:
            pass

        # Create tables first, then apply lightweight inline schema migrations
        # for backward-compatible upgrades on existing SQLite databases.
        Base.metadata.create_all(self._engine)
        self._apply_inline_migrations()

        self._initialized = True
        logger.info(f"数据库初始化完成: {db_url}")

        # 注册退出钩子，确保程序退出时关闭数据库连接
        atexit.register(DatabaseManager._cleanup_engine, self._engine)
    
    @classmethod
    def get_instance(cls) -> 'DatabaseManager':
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        elif not getattr(cls._instance, "_initialized", False) or not hasattr(cls._instance, "_SessionLocal"):
            cls._cleanup_engine(getattr(cls._instance, "_engine", None))
            cls._instance = None
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（用于测试）"""
        if cls._instance is not None:
            if hasattr(cls._instance, '_engine') and cls._instance._engine is not None:
                cls._instance._engine.dispose()
            cls._instance._initialized = False
            cls._instance = None

    @classmethod
    def _cleanup_engine(cls, engine) -> None:
        """
        清理数据库引擎（atexit 钩子）

        确保程序退出时关闭所有数据库连接，避免 ResourceWarning

        Args:
            engine: SQLAlchemy 引擎对象
        """
        try:
            if engine is not None:
                engine.dispose()
                logger.debug("数据库引擎已清理")
        except Exception as e:
            logger.warning(f"清理数据库引擎时出错: {e}")

    def _apply_inline_migrations(self) -> None:
        """Apply lightweight backward-compatible schema migrations.

        This keeps existing SQLite data files usable after adding new columns,
        especially for local/Docker deployments that copy old databases to new
        machines. The migration is intentionally additive-only.
        """
        try:
            dialect = self._engine.dialect.name
            if dialect == "sqlite":
                self._migrate_sqlite_screening_runs_notification_fields()
                self._migrate_sqlite_screening_runs_heartbeat_field()
                self._migrate_sqlite_screening_candidates_decision_fields()
                self._migrate_sqlite_screening_candidates_strategy_fields()
                self._migrate_sqlite_screening_candidates_ai_review_fields()
                self._migrate_sqlite_screening_candidates_updated_at_field()
                self._migrate_sqlite_screening_candidate_raw_rule_score()
                self._migrate_sqlite_daily_sector_heat_rank_fields()
                self._migrate_sqlite_five_layer_backtest_group_summary_fields()
                self._migrate_sqlite_five_layer_backtest_evaluation_fields()
                self._migrate_sqlite_stock_daily_adj_factor_fields()
                self._migrate_sqlite_stock_daily_pre_close_fields()
                self._migrate_sqlite_instrument_delist_date_field()
        except Exception as exc:
            logger.exception("Inline database migration failed: %s", exc)
            raise

    def _migrate_sqlite_screening_candidates_decision_fields(self) -> None:
        """Ensure screening_candidates has the unified candidate_decision_json column."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(screening_candidates)").fetchall()
            }
            if "candidate_decision_json" in existing:
                return

            logger.info(
                "Applying inline SQLite migration: adding candidate_decision_json to screening_candidates"
            )
            conn.exec_driver_sql(
                "ALTER TABLE screening_candidates ADD COLUMN candidate_decision_json TEXT"
            )
            logger.info("Inline SQLite migration for candidate_decision_json completed")

    def _migrate_sqlite_screening_runs_notification_fields(self) -> None:
        """Ensure screening_runs has notification-related columns on SQLite."""
        expected_columns = {
            "trigger_type": "ALTER TABLE screening_runs ADD COLUMN trigger_type VARCHAR(32) NOT NULL DEFAULT 'manual'",
            "notification_status": "ALTER TABLE screening_runs ADD COLUMN notification_status VARCHAR(32)",
            "notification_attempts": "ALTER TABLE screening_runs ADD COLUMN notification_attempts INTEGER NOT NULL DEFAULT 0",
            "notification_sent_at": "ALTER TABLE screening_runs ADD COLUMN notification_sent_at DATETIME",
            "notification_error": "ALTER TABLE screening_runs ADD COLUMN notification_error TEXT",
        }

        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(screening_runs)").fetchall()
            }
            missing = [name for name in expected_columns if name not in existing]
            if missing:
                logger.info(
                    "Applying inline SQLite migration for screening_runs, missing columns: %s",
                    ", ".join(missing),
                )
                for column_name in missing:
                    conn.exec_driver_sql(expected_columns[column_name])

            conn.exec_driver_sql(
                "UPDATE screening_runs "
                "SET trigger_type='manual' "
                "WHERE trigger_type IS NULL OR TRIM(trigger_type)=''"
            )
            conn.exec_driver_sql(
                "UPDATE screening_runs "
                "SET notification_status='pending' "
                "WHERE notification_status IS NULL"
            )
            conn.exec_driver_sql(
                "UPDATE screening_runs "
                "SET notification_attempts=0 "
                "WHERE notification_attempts IS NULL"
            )
            if missing:
                logger.info("Inline SQLite migration for screening_runs completed")

    def _migrate_sqlite_screening_runs_heartbeat_field(self) -> None:
        """Ensure screening_runs has last_activity_at column on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(screening_runs)").fetchall()
            }
            if "last_activity_at" in existing:
                return

            logger.info("Applying inline SQLite migration: adding last_activity_at to screening_runs")
            conn.exec_driver_sql(
                "ALTER TABLE screening_runs ADD COLUMN last_activity_at DATETIME"
            )
            # 回填已有记录：用 started_at 作为初始值
            conn.exec_driver_sql(
                "UPDATE screening_runs SET last_activity_at = started_at WHERE last_activity_at IS NULL"
            )
            logger.info("Inline SQLite migration for last_activity_at completed")

    def _migrate_sqlite_screening_candidates_updated_at_field(self) -> None:
        """Ensure screening_candidates has the updated_at column on SQLite.

        Back-fills ``updated_at = created_at`` so legacy rows present a stable
        timestamp pair (created_at == updated_at means "never rewritten").
        """
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(screening_candidates)"
                ).fetchall()
            }
            if "updated_at" in existing:
                return

            logger.info(
                "Applying inline SQLite migration: adding updated_at to screening_candidates"
            )
            conn.exec_driver_sql(
                "ALTER TABLE screening_candidates ADD COLUMN updated_at DATETIME"
            )
            conn.exec_driver_sql(
                "UPDATE screening_candidates SET updated_at = created_at WHERE updated_at IS NULL"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_screening_candidates_updated_at "
                "ON screening_candidates(updated_at)"
            )
            logger.info("Inline SQLite migration for updated_at completed")

    def _migrate_sqlite_screening_candidate_raw_rule_score(self) -> None:
        """Ensure screening_candidates has the raw_rule_score column on SQLite.

        No back-fill: legacy rows only ever stored the composite priority score,
        so the pre-overwrite quality score cannot be recovered after the fact.
        They stay NULL and consumers must treat NULL as "unknown".
        """
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(screening_candidates)"
                ).fetchall()
            }
            if "raw_rule_score" in existing:
                return

            logger.info(
                "Applying inline SQLite migration: adding raw_rule_score to screening_candidates"
            )
            conn.exec_driver_sql(
                "ALTER TABLE screening_candidates ADD COLUMN raw_rule_score FLOAT"
            )
            logger.info("Inline SQLite migration for raw_rule_score completed")

    @staticmethod
    def _ensure_sqlite_index(conn, index_name: str, create_sql: str) -> None:
        """确保 SQLite 索引存在，缺失时创建并记日志。

        调用方必须在「本次是否新增了列」之外独立调用本方法：列已存在而索引
        缺失是可达状态，而 create_all 对已存在的表整表跳过（索引一起跳过），
        缺的索引没有别的路径会补。
        """
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        if exists:
            return

        logger.info(
            "Applying inline SQLite migration: creating index %s", index_name
        )
        conn.exec_driver_sql(create_sql)

    def _migrate_sqlite_stock_daily_adj_factor_fields(self) -> None:
        """B6: ensure stock_daily carries adj_factor / adj_anchor_date /
        adj_factor_source on SQLite.

        Pre-B6 rows are backfilled with ``adj_factor=1.0`` and
        ``adj_factor_source='legacy_assume_one'`` so the backtest layer
        can detect "this row's qfq base is unknown and may drift" without
        crashing on NULL. New rows from updated fetchers will carry the
        true factor; old rows stay at 1.0 (lossy but stable — see
        scripts/migrate_stock_daily_adj_factor.py for a verbose offline
        migration path).
        """
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(stock_daily)"
                ).fetchall()
            }
            added: List[str] = []
            if "adj_factor" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN adj_factor FLOAT"
                )
                added.append("adj_factor")
            if "adj_anchor_date" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN adj_anchor_date DATE"
                )
                added.append("adj_anchor_date")
            if "adj_factor_source" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN adj_factor_source VARCHAR(32)"
                )
                added.append("adj_factor_source")

            if added:
                logger.info(
                    "Applying inline SQLite migration: added %s to stock_daily",
                    ", ".join(added),
                )
                # Backfill pre-B6 rows so callers can rely on adj_factor being
                # non-NULL. ``legacy_assume_one`` is intentionally distinct
                # from ``fetcher_unset`` so analysts can audit how much of the
                # corpus pre-dates B6.
                if "adj_factor" in added:
                    conn.exec_driver_sql(
                        "UPDATE stock_daily SET adj_factor = 1.0 WHERE adj_factor IS NULL"
                    )
                if "adj_factor_source" in added:
                    conn.exec_driver_sql(
                        "UPDATE stock_daily SET adj_factor_source = 'legacy_assume_one' "
                        "WHERE adj_factor_source IS NULL"
                    )

            # 索引独立于「本次是否新增列」判断，理由见 _ensure_sqlite_index。
            # 放在回填之后，避免在全表 UPDATE 期间还要维护索引。
            if "adj_factor_source" in existing or "adj_factor_source" in added:
                self._ensure_sqlite_index(
                    conn,
                    "ix_stock_daily_adj_factor_source",
                    "CREATE INDEX IF NOT EXISTS ix_stock_daily_adj_factor_source "
                    "ON stock_daily(adj_factor_source)",
                )

    def _migrate_sqlite_stock_daily_pre_close_fields(self) -> None:
        """向后兼容：给已有 stock_daily 补 pre_close / adj_convention 两列。

        两列都留 NULL，不做任何回填：``pre_close`` 没有可辩护的默认值，写任何
        值都是在编造数据；``adj_convention`` 同理——存量行的口径无法事后判定，
        标成 ``raw`` 会让后续只消费 raw 的阶段吃进一整批错口径数据。存量行由
        重新抓取的阶段填充。
        """
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(stock_daily)"
                ).fetchall()
            }
            added: List[str] = []
            if "pre_close" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN pre_close FLOAT"
                )
                added.append("pre_close")
            if "adj_convention" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE stock_daily ADD COLUMN adj_convention VARCHAR(16)"
                )
                added.append("adj_convention")

            # 索引必须独立判断，不能挂在「本次新增了列」里：列已存在而索引缺失
            # 是可达状态——SQLite 的 ALTER TABLE ADD COLUMN 即使没有显式 commit
            # 也会落盘，离线脚本在两条 ALTER 之后、CREATE INDEX 之前被打断就会
            # 留下这种库。此时 create_all 会整表跳过（索引一起跳过），只有这里
            # 还能补上。
            if "adj_convention" in existing or "adj_convention" in added:
                self._ensure_sqlite_index(
                    conn,
                    "ix_stock_daily_adj_convention",
                    "CREATE INDEX IF NOT EXISTS ix_stock_daily_adj_convention "
                    "ON stock_daily(adj_convention)",
                )

            if not added:
                return

            logger.info(
                "Applying inline SQLite migration: added %s to stock_daily",
                ", ".join(added),
            )

    def _migrate_sqlite_instrument_delist_date_field(self) -> None:
        """向后兼容：给已有 instrument_master 补 delist_date 列与索引。

        不做任何回填：存量行的退市日期无法事后判定，写任何值都是编造数据。
        列留 NULL 表示「未知/在市」，由 ListingLifecycleService.sync_from_baostock()
        全量回填。
        """
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(instrument_master)"
                ).fetchall()
            }
            added = False
            if "delist_date" not in existing:
                conn.exec_driver_sql(
                    "ALTER TABLE instrument_master ADD COLUMN delist_date DATE"
                )
                added = True

            # 索引必须独立判断，不能挂在「本次新增了列」里：列已存在而索引缺失
            # 是可达状态——SQLite 的 ALTER TABLE ADD COLUMN 即使没有显式 commit
            # 也会落盘，离线脚本在 ALTER 之后、CREATE INDEX 之前被打断就会留下
            # 这种库。此时 create_all 会整表跳过（索引一起跳过），只有这里还能补上。
            if added or "delist_date" in existing:
                self._ensure_sqlite_index(
                    conn,
                    "ix_instrument_master_delist_date",
                    "CREATE INDEX IF NOT EXISTS ix_instrument_master_delist_date "
                    "ON instrument_master(delist_date)",
                )

            if added:
                logger.info(
                    "Applying inline SQLite migration: added delist_date to instrument_master"
                )

    def _migrate_sqlite_screening_candidates_strategy_fields(self) -> None:
        """Ensure screening_candidates keeps matched strategy names on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(screening_candidates)").fetchall()
            }
            if "matched_strategies_json" in existing:
                return

            logger.info(
                "Applying inline SQLite migration: adding matched_strategies_json to screening_candidates"
            )
            conn.exec_driver_sql(
                "ALTER TABLE screening_candidates ADD COLUMN matched_strategies_json TEXT"
            )
            logger.info("Inline SQLite migration for matched_strategies_json completed")

    def _migrate_sqlite_screening_candidates_ai_review_fields(self) -> None:
        """Ensure screening_candidates has AI review protocol columns on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(screening_candidates)").fetchall()
            }
            new_columns = {
                "ai_trade_stage": "ALTER TABLE screening_candidates ADD COLUMN ai_trade_stage VARCHAR(32)",
                "ai_reasoning": "ALTER TABLE screening_candidates ADD COLUMN ai_reasoning TEXT",
                "ai_confidence": "ALTER TABLE screening_candidates ADD COLUMN ai_confidence FLOAT",
            }
            for col_name, ddl in new_columns.items():
                if col_name not in existing:
                    logger.info("Applying inline SQLite migration: adding %s to screening_candidates", col_name)
                    conn.exec_driver_sql(ddl)

    def _migrate_sqlite_daily_sector_heat_rank_fields(self) -> None:
        """Ensure daily_sector_heat keeps rank-driven fields on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(daily_sector_heat)").fetchall()
            }
            new_columns = {
                "board_strength_score": "ALTER TABLE daily_sector_heat ADD COLUMN board_strength_score FLOAT NOT NULL DEFAULT 0.0",
                "board_strength_rank": "ALTER TABLE daily_sector_heat ADD COLUMN board_strength_rank INTEGER NOT NULL DEFAULT 0",
                "board_strength_percentile": "ALTER TABLE daily_sector_heat ADD COLUMN board_strength_percentile FLOAT NOT NULL DEFAULT 0.0",
                "leader_candidate_count": "ALTER TABLE daily_sector_heat ADD COLUMN leader_candidate_count INTEGER NOT NULL DEFAULT 0",
                "quality_flags_json": "ALTER TABLE daily_sector_heat ADD COLUMN quality_flags_json TEXT",
            }
            for col_name, ddl in new_columns.items():
                if col_name not in existing:
                    logger.info("Applying inline SQLite migration: adding %s to daily_sector_heat", col_name)
                    conn.exec_driver_sql(ddl)

    def _migrate_sqlite_five_layer_backtest_group_summary_fields(self) -> None:
        """Ensure five-layer group summaries expose the latest aggregate fields on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(five_layer_backtest_group_summaries)"
                ).fetchall()
            }
            new_columns = {
                "profit_factor": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN profit_factor FLOAT",
                "avg_holding_days": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN avg_holding_days FLOAT",
                "max_consecutive_losses": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN max_consecutive_losses INTEGER",
                "plan_execution_rate": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN plan_execution_rate FLOAT",
                "stage_accuracy_rate": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN stage_accuracy_rate FLOAT",
                "system_grade": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN system_grade VARCHAR(4)",
                # D2: leader_pool_win_share replaces the misnamed top_k_hit_rate.
                # Old column is kept (above) as a deprecated alias; this new
                # column is the canonical one. Backfilled from top_k_hit_rate
                # below when it already had data.
                "leader_pool_win_share": "ALTER TABLE five_layer_backtest_group_summaries ADD COLUMN leader_pool_win_share FLOAT",
            }
            added_columns: List[str] = []
            for col_name, ddl in new_columns.items():
                if col_name not in existing:
                    logger.info(
                        "Applying inline SQLite migration: adding %s to five_layer_backtest_group_summaries",
                        col_name,
                    )
                    conn.exec_driver_sql(ddl)
                    added_columns.append(col_name)

            # If the alias column was just introduced, backfill from the legacy
            # top_k_hit_rate so existing summaries keep displaying the metric
            # under both names without a re-run of the aggregator.
            if "leader_pool_win_share" in added_columns and "top_k_hit_rate" in existing.union({"leader_pool_win_share"}):
                logger.info(
                    "Backfilling leader_pool_win_share from top_k_hit_rate on existing rows",
                )
                conn.exec_driver_sql(
                    "UPDATE five_layer_backtest_group_summaries "
                    "SET leader_pool_win_share = top_k_hit_rate "
                    "WHERE leader_pool_win_share IS NULL "
                    "AND top_k_hit_rate IS NOT NULL"
                )

    def _migrate_sqlite_five_layer_backtest_evaluation_fields(self) -> None:
        """Ensure five-layer evaluations expose the latest entry timing metrics on SQLite."""
        with self._engine.begin() as conn:
            existing = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(five_layer_backtest_evaluations)"
                ).fetchall()
            }
            new_columns = {
                "optimal_entry_deviation": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN optimal_entry_deviation FLOAT"
                ),
                "optimal_entry_timing": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN optimal_entry_timing INTEGER"
                ),
                "eval_status": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN eval_status VARCHAR(16)"
                ),
                "suppression_reason": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN suppression_reason VARCHAR(64)"
                ),
                "planned_entry_price": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN planned_entry_price FLOAT"
                ),
                "planned_stop_loss_price": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN planned_stop_loss_price FLOAT"
                ),
                "planned_take_profit_price": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN planned_take_profit_price FLOAT"
                ),
                "actual_entry_price": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN actual_entry_price FLOAT"
                ),
                "actual_entry_date": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN actual_entry_date DATE"
                ),
                "actual_exit_price": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN actual_exit_price FLOAT"
                ),
                "actual_exit_date": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN actual_exit_date DATE"
                ),
                "exit_reason": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN exit_reason VARCHAR(32)"
                ),
                "trade_return_pct": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN trade_return_pct FLOAT"
                ),
                "trade_replay_status": (
                    "ALTER TABLE five_layer_backtest_evaluations "
                    "ADD COLUMN trade_replay_status VARCHAR(32)"
                ),
            }
            for col_name, ddl in new_columns.items():
                if col_name not in existing:
                    logger.info(
                        "Applying inline SQLite migration: adding %s to five_layer_backtest_evaluations",
                        col_name,
                    )
                    conn.exec_driver_sql(ddl)

            conn.exec_driver_sql(
                "UPDATE five_layer_backtest_evaluations "
                "SET eval_status = 'pending' "
                "WHERE eval_status IS NULL"
            )
            index_names = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA index_list(five_layer_backtest_evaluations)"
                ).fetchall()
            }
            if (
                "ix_five_layer_backtest_evaluations_suppression_reason" not in index_names
                and "ix_flbe_suppression_reason" not in index_names
            ):
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_five_layer_backtest_evaluations_suppression_reason "
                    "ON five_layer_backtest_evaluations(suppression_reason)"
                )
            if "ix_five_layer_backtest_evaluations_trade_replay_status" not in index_names:
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_five_layer_backtest_evaluations_trade_replay_status "
                    "ON five_layer_backtest_evaluations(trade_replay_status)"
                )

    def get_session(self) -> Session:
        """
        获取数据库 Session
        
        使用示例:
            with db.get_session() as session:
                # 执行查询
                session.commit()  # 如果需要
        """
        if not getattr(self, '_initialized', False) or not hasattr(self, '_SessionLocal'):
            raise RuntimeError(
                "DatabaseManager 未正确初始化。"
                "请确保通过 DatabaseManager.get_instance() 获取实例。"
            )
        session = self._SessionLocal()
        try:
            return session
        except Exception:
            session.close()
            raise

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope around a series of operations."""
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_kline_audit_run_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        market: str,
        trade_date: date,
        run_type: str,
        trigger_type: str,
        run_result: str,
        pass_status: str,
        rule_version: str,
        window_start: date,
        window_end: date,
    ) -> KlineAuditRun:
        run = session.execute(
            select(KlineAuditRun).where(KlineAuditRun.run_id == run_id)
        ).scalar_one_or_none()
        if run is None:
            run = KlineAuditRun(run_id=run_id)
            session.add(run)

        run.market = market
        run.trade_date = trade_date
        run.run_type = run_type
        run.trigger_type = trigger_type
        run.run_result = run_result
        run.pass_status = pass_status
        run.rule_version = rule_version
        run.window_start = window_start
        run.window_end = window_end
        if run_result != "pending":
            run.completed_at = datetime.now()
        else:
            run.completed_at = None
        session.flush()
        session.refresh(run)
        session.expunge(run)
        return run

    def create_kline_audit_run(
        self,
        *,
        run_id: str,
        market: str,
        trade_date: date,
        run_type: str,
        trigger_type: str,
        run_result: str,
        pass_status: str,
        rule_version: str,
        window_start: date,
        window_end: date,
    ) -> KlineAuditRun:
        with self.session_scope() as session:
            return self._create_kline_audit_run_in_session(
                session,
                run_id=run_id,
                market=market,
                trade_date=trade_date,
                run_type=run_type,
                trigger_type=trigger_type,
                run_result=run_result,
                pass_status=pass_status,
                rule_version=rule_version,
                window_start=window_start,
                window_end=window_end,
            )

    def list_kline_audit_runs(
        self,
        *,
        market: Optional[str] = None,
        limit: int = 50,
    ) -> List[KlineAuditRun]:
        with self.session_scope() as session:
            stmt = select(KlineAuditRun).order_by(desc(KlineAuditRun.created_at)).limit(limit)
            if market:
                stmt = stmt.where(KlineAuditRun.market == market)
            runs = list(session.execute(stmt).scalars().all())
            for run in runs:
                session.expunge(run)
            return runs

    def _upsert_kline_audit_trade_date_in_session(
        self,
        session: Session,
        *,
        market: str,
        trade_date: date,
        pass_status: str,
        window_start: date,
        window_end: date,
        rule_version: str,
        source_run_id: str,
        passed_at: Optional[datetime],
    ) -> KlineAuditTradeDate:
        record = session.execute(
            select(KlineAuditTradeDate).where(
                and_(
                    KlineAuditTradeDate.market == market,
                    KlineAuditTradeDate.trade_date == trade_date,
                )
            )
        ).scalar_one_or_none()
        if record is None:
            record = KlineAuditTradeDate(
                market=market,
                trade_date=trade_date,
            )
            session.add(record)

        record.pass_status = pass_status
        record.window_start = window_start
        record.window_end = window_end
        record.rule_version = rule_version
        record.source_run_id = source_run_id
        record.checked_at = datetime.now()
        record.passed_at = passed_at
        session.flush()
        session.refresh(record)
        session.expunge(record)
        return record

    def upsert_kline_audit_trade_date(
        self,
        *,
        market: str,
        trade_date: date,
        pass_status: str,
        window_start: date,
        window_end: date,
        rule_version: str,
        source_run_id: str,
        passed_at: Optional[datetime],
    ) -> KlineAuditTradeDate:
        with self.session_scope() as session:
            return self._upsert_kline_audit_trade_date_in_session(
                session,
                market=market,
                trade_date=trade_date,
                pass_status=pass_status,
                window_start=window_start,
                window_end=window_end,
                rule_version=rule_version,
                source_run_id=source_run_id,
                passed_at=passed_at,
            )

    def get_kline_audit_trade_date(
        self,
        *,
        market: str,
        trade_date: date,
    ) -> Optional[KlineAuditTradeDate]:
        with self.session_scope() as session:
            record = session.execute(
                select(KlineAuditTradeDate).where(
                    and_(
                        KlineAuditTradeDate.market == market,
                        KlineAuditTradeDate.trade_date == trade_date,
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            session.expunge(record)
            return record

    def get_latest_passed_kline_audit_trade_date(
        self,
        *,
        market: str,
    ) -> Optional[KlineAuditTradeDate]:
        with self.session_scope() as session:
            record = session.execute(
                select(KlineAuditTradeDate)
                .where(
                    and_(
                        KlineAuditTradeDate.market == market,
                        KlineAuditTradeDate.pass_status == "passed",
                    )
                )
                .order_by(desc(KlineAuditTradeDate.trade_date))
                .limit(1)
            ).scalar_one_or_none()
            if record is None:
                return None
            session.expunge(record)
            return record

    def _upsert_kline_audit_gap_in_session(
        self,
        session: Session,
        *,
        market: str,
        gap_scope: str,
        source_run_id: str,
        status: str,
        trade_date: Optional[date] = None,
        code: Optional[str] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
    ) -> KlineAuditGap:
        gap_key = KlineAuditGap.build_gap_key(
            market=market,
            gap_scope=gap_scope,
            trade_date=trade_date,
            code=code,
            missing_date_from=missing_date_from,
            missing_date_to=missing_date_to,
        )
        record = session.execute(
            select(KlineAuditGap).where(KlineAuditGap.gap_key == gap_key)
        ).scalar_one_or_none()
        if record is None:
            record = KlineAuditGap(gap_key=gap_key)
            session.add(record)

        record.source_run_id = source_run_id
        record.market = market
        record.gap_scope = gap_scope
        record.code = code
        record.trade_date = trade_date
        record.missing_date_from = missing_date_from
        record.missing_date_to = missing_date_to
        record.status = status
        session.flush()
        session.refresh(record)
        session.expunge(record)
        return record

    def upsert_kline_audit_gap(
        self,
        *,
        market: str,
        gap_scope: str,
        source_run_id: str,
        status: str,
        trade_date: Optional[date] = None,
        code: Optional[str] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
    ) -> KlineAuditGap:
        with self.session_scope() as session:
            return self._upsert_kline_audit_gap_in_session(
                session,
                market=market,
                gap_scope=gap_scope,
                source_run_id=source_run_id,
                status=status,
                trade_date=trade_date,
                code=code,
                missing_date_from=missing_date_from,
                missing_date_to=missing_date_to,
            )

    def list_kline_audit_gaps(
        self,
        *,
        market: Optional[str] = None,
        gap_scope: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[KlineAuditGap]:
        with self.session_scope() as session:
            stmt = select(KlineAuditGap).order_by(KlineAuditGap.created_at.asc())
            if market:
                stmt = stmt.where(KlineAuditGap.market == market)
            if gap_scope:
                stmt = stmt.where(KlineAuditGap.gap_scope == gap_scope)
            if status:
                stmt = stmt.where(KlineAuditGap.status == status)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows

    @staticmethod
    def _validate_kline_skip_scope(
        *,
        gap_scope: str,
        trade_date: Optional[date],
        code: Optional[str],
        missing_date_from: Optional[date],
        missing_date_to: Optional[date],
    ) -> None:
        if gap_scope == "market_day_gap":
            if trade_date is None:
                raise ValueError("market_day_gap requires trade_date")
            return
        if gap_scope == "symbol_range_gap":
            if not code or missing_date_from is None or missing_date_to is None:
                raise ValueError("symbol_range_gap requires code, missing_date_from, and missing_date_to")
            return
        raise ValueError(f"Unsupported gap_scope: {gap_scope}")

    def _append_kline_audit_event_in_session(
        self,
        session: Session,
        *,
        event_type: str,
        source_run_id: Optional[str] = None,
        gap_key: Optional[str] = None,
        event_status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> KlineAuditEvent:
        event_row = KlineAuditEvent(
            source_run_id=source_run_id,
            gap_key=gap_key,
            event_type=event_type,
            event_status=event_status,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        )
        session.add(event_row)
        session.flush()
        session.refresh(event_row)
        session.expunge(event_row)
        return event_row

    def append_kline_audit_event(
        self,
        *,
        event_type: str,
        source_run_id: Optional[str] = None,
        gap_key: Optional[str] = None,
        event_status: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> KlineAuditEvent:
        with self.session_scope() as session:
            return self._append_kline_audit_event_in_session(
                session,
                event_type=event_type,
                source_run_id=source_run_id,
                gap_key=gap_key,
                event_status=event_status,
                payload=payload,
            )

    def list_kline_audit_events(
        self,
        *,
        gap_key: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[KlineAuditEvent]:
        with self.session_scope() as session:
            stmt = select(KlineAuditEvent).order_by(KlineAuditEvent.created_at.asc(), KlineAuditEvent.id.asc())
            if gap_key:
                stmt = stmt.where(KlineAuditEvent.gap_key == gap_key)
            if event_type:
                stmt = stmt.where(KlineAuditEvent.event_type == event_type)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows

    def persist_kline_audit_result(
        self,
        *,
        run_payload: Dict[str, Any],
        trade_date_payload: Dict[str, Any],
        gap_payloads: List[Dict[str, Any]],
        event_payloads: List[Dict[str, Any]],
    ) -> None:
        with self.session_scope() as session:
            self._create_kline_audit_run_in_session(session, **run_payload)
            self._upsert_kline_audit_trade_date_in_session(session, **trade_date_payload)
            for gap_payload in gap_payloads:
                self._upsert_kline_audit_gap_in_session(session, **gap_payload)
            for event_payload in event_payloads:
                self._append_kline_audit_event_in_session(session, **event_payload)

    def upsert_kline_skip_registry(
        self,
        *,
        market: str,
        gap_scope: str,
        status: str,
        code: Optional[str] = None,
        trade_date: Optional[date] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
        reason_type: Optional[str] = None,
        notes: Optional[str] = None,
        approved_by: Optional[str] = None,
        approved_at: Optional[datetime] = None,
        success_streak: Optional[int] = None,
        last_success_at: Optional[datetime] = None,
        last_recovered_at: Optional[datetime] = None,
    ) -> KlineSkipRegistry:
        self._validate_kline_skip_scope(
            gap_scope=gap_scope,
            trade_date=trade_date,
            code=code,
            missing_date_from=missing_date_from,
            missing_date_to=missing_date_to,
        )
        skip_key = KlineSkipRegistry.build_skip_key(
            market=market,
            gap_scope=gap_scope,
            trade_date=trade_date,
            code=code,
            missing_date_from=missing_date_from,
            missing_date_to=missing_date_to,
        )
        with self.session_scope() as session:
            record = session.execute(
                select(KlineSkipRegistry).where(KlineSkipRegistry.skip_key == skip_key)
            ).scalar_one_or_none()
            if record is None:
                record = KlineSkipRegistry(skip_key=skip_key)
                session.add(record)

            record.skip_key = skip_key
            record.market = market
            record.gap_scope = gap_scope
            record.code = code
            record.trade_date = trade_date
            record.missing_date_from = missing_date_from
            record.missing_date_to = missing_date_to
            record.status = status
            if reason_type is not None:
                record.reason_type = reason_type
            if notes is not None:
                record.notes = notes
            if approved_by is not None:
                record.approved_by = approved_by
            if approved_at is not None:
                record.approved_at = approved_at
            record.success_streak = success_streak if success_streak is not None else (record.success_streak or 0)
            if last_success_at is not None:
                record.last_success_at = last_success_at
            if last_recovered_at is not None:
                record.last_recovered_at = last_recovered_at
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                record = session.execute(
                    select(KlineSkipRegistry).where(KlineSkipRegistry.skip_key == skip_key)
                ).scalar_one()
                record.market = market
                record.gap_scope = gap_scope
                record.code = code
                record.trade_date = trade_date
                record.missing_date_from = missing_date_from
                record.missing_date_to = missing_date_to
                record.status = status
                if reason_type is not None:
                    record.reason_type = reason_type
                if notes is not None:
                    record.notes = notes
                if approved_by is not None:
                    record.approved_by = approved_by
                if approved_at is not None:
                    record.approved_at = approved_at
                record.success_streak = (
                    success_streak if success_streak is not None else (record.success_streak or 0)
                )
                if last_success_at is not None:
                    record.last_success_at = last_success_at
                if last_recovered_at is not None:
                    record.last_recovered_at = last_recovered_at
                session.flush()
            session.refresh(record)
            session.expunge(record)
            return record

    def get_kline_skip_registry(
        self,
        *,
        market: str,
        gap_scope: str,
        code: Optional[str] = None,
        trade_date: Optional[date] = None,
        missing_date_from: Optional[date] = None,
        missing_date_to: Optional[date] = None,
    ) -> Optional[KlineSkipRegistry]:
        self._validate_kline_skip_scope(
            gap_scope=gap_scope,
            trade_date=trade_date,
            code=code,
            missing_date_from=missing_date_from,
            missing_date_to=missing_date_to,
        )
        skip_key = KlineSkipRegistry.build_skip_key(
            market=market,
            gap_scope=gap_scope,
            trade_date=trade_date,
            code=code,
            missing_date_from=missing_date_from,
            missing_date_to=missing_date_to,
        )
        with self.session_scope() as session:
            record = session.execute(
                select(KlineSkipRegistry).where(KlineSkipRegistry.skip_key == skip_key)
            ).scalar_one_or_none()
            if record is None:
                return None
            session.expunge(record)
            return record

    def list_kline_skip_registry(
        self,
        *,
        market: Optional[str] = None,
        gap_scope: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[KlineSkipRegistry]:
        with self.session_scope() as session:
            stmt = select(KlineSkipRegistry).order_by(KlineSkipRegistry.created_at.asc())
            if market:
                stmt = stmt.where(KlineSkipRegistry.market == market)
            if gap_scope:
                stmt = stmt.where(KlineSkipRegistry.gap_scope == gap_scope)
            if status:
                stmt = stmt.where(KlineSkipRegistry.status == status)
            rows = list(session.execute(stmt).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows
    
    def has_today_data(self, code: str, target_date: Optional[date] = None) -> bool:
        """
        检查是否已有指定日期的数据
        
        用于断点续传逻辑：如果已有数据则跳过网络请求
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            是否存在数据
        """
        if target_date is None:
            target_date = date.today()
        # 注意：这里的 target_date 语义是“自然日”，而不是“最新交易日”。
        # 在周末/节假日/非交易日运行时，即使数据库已有最新交易日数据，这里也会返回 False。
        # 该行为目前保留（按需求不改逻辑）。
        
        with self.get_session() as session:
            result = session.execute(
                select(StockDaily).where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date == target_date
                    )
                )
            ).scalar_one_or_none()
            
            return result is not None

    def batch_has_today_data(self, codes: List[str], target_date: date) -> set:
        """
        批量检查哪些股票已有指定日期的数据。

        比逐一调用 has_today_data 快 100x+（单次 SQL 替代 N 次）。

        Args:
            codes: 待检查的股票代码列表
            target_date: 目标日期

        Returns:
            已有数据的股票代码集合
        """
        if not codes:
            return set()
        with self.get_session() as session:
            rows = session.execute(
                select(StockDaily.code)
                .where(StockDaily.date == target_date, StockDaily.code.in_(codes))
            ).scalars().all()
            return set(rows)

    def get_latest_date(self, code: str) -> Optional[date]:
        """获取指定股票在 stock_daily 中的最新日期。"""
        with self.get_session() as session:
            result = session.execute(
                select(func.max(StockDaily.date)).where(StockDaily.code == code)
            ).scalar()
            return result

    def get_stock_row_count(self, code: str) -> int:
        """获取指定股票在 stock_daily 中的总行数。"""
        with self.get_session() as session:
            result = session.execute(
                select(func.count(StockDaily.id)).where(StockDaily.code == code)
            ).scalar()
            return result or 0

    def is_data_fresh(self, code: str, max_stale_days: int = 3) -> bool:
        """
        判断数据是否足够新鲜（兼容周末/节假日）。

        逻辑：DB 中该股票的最新日期距今不超过 max_stale_days 个自然日。
        - 工作日：max_stale_days=3 能跨越一个周末
        - 长假期间需要适当增大
        """
        latest = self.get_latest_date(code)
        if latest is None:
            return False
        delta = (date.today() - latest).days
        return delta <= max_stale_days

    def get_latest_data(
        self, 
        code: str, 
        days: int = 2
    ) -> List[StockDaily]:
        """
        获取最近 N 天的数据
        
        用于计算"相比昨日"的变化
        
        Args:
            code: 股票代码
            days: 获取天数
            
        Returns:
            StockDaily 对象列表（按日期降序）
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(StockDaily.code == code)
                .order_by(desc(StockDaily.date))
                .limit(days)
            ).scalars().all()
            
            return list(results)

    def save_news_intel(
        self,
        code: str,
        name: str,
        dimension: str,
        query: str,
        response: 'SearchResponse',
        query_context: Optional[Dict[str, str]] = None
    ) -> int:
        """
        保存新闻情报到数据库

        去重策略：
        - 优先按 URL 去重（唯一约束）
        - URL 缺失时按 title + source + published_date 进行软去重

        关联策略：
        - query_context 记录用户查询信息（平台、用户、会话、原始指令等）
        """
        if not response or not response.results:
            return 0

        saved_count = 0
        query_ctx = query_context or {}
        current_query_id = (query_ctx.get("query_id") or "").strip()

        with self.get_session() as session:
            try:
                for item in response.results:
                    title = (item.title or '').strip()
                    url = (item.url or '').strip()
                    source = (item.source or '').strip()
                    snippet = (item.snippet or '').strip()
                    published_date = self._parse_published_date(item.published_date)

                    if not title and not url:
                        continue

                    url_key = url or self._build_fallback_url_key(
                        code=code,
                        title=title,
                        source=source,
                        published_date=published_date
                    )

                    # 优先按 URL 或兜底键去重
                    existing = session.execute(
                        select(NewsIntel).where(NewsIntel.url == url_key)
                    ).scalar_one_or_none()

                    if existing:
                        existing.name = name or existing.name
                        existing.dimension = dimension or existing.dimension
                        existing.query = query or existing.query
                        existing.provider = response.provider or existing.provider
                        existing.snippet = snippet or existing.snippet
                        existing.source = source or existing.source
                        existing.published_date = published_date or existing.published_date
                        existing.fetched_at = datetime.now()

                        if query_context:
                            # Keep the first query_id to avoid overwriting historical links.
                            if not existing.query_id and current_query_id:
                                existing.query_id = current_query_id
                            existing.query_source = (
                                query_context.get("query_source") or existing.query_source
                            )
                            existing.requester_platform = (
                                query_context.get("requester_platform") or existing.requester_platform
                            )
                            existing.requester_user_id = (
                                query_context.get("requester_user_id") or existing.requester_user_id
                            )
                            existing.requester_user_name = (
                                query_context.get("requester_user_name") or existing.requester_user_name
                            )
                            existing.requester_chat_id = (
                                query_context.get("requester_chat_id") or existing.requester_chat_id
                            )
                            existing.requester_message_id = (
                                query_context.get("requester_message_id") or existing.requester_message_id
                            )
                            existing.requester_query = (
                                query_context.get("requester_query") or existing.requester_query
                            )
                    else:
                        try:
                            with session.begin_nested():
                                record = NewsIntel(
                                    code=code,
                                    name=name,
                                    dimension=dimension,
                                    query=query,
                                    provider=response.provider,
                                    title=title,
                                    snippet=snippet,
                                    url=url_key,
                                    source=source,
                                    published_date=published_date,
                                    fetched_at=datetime.now(),
                                    query_id=current_query_id or None,
                                    query_source=query_ctx.get("query_source"),
                                    requester_platform=query_ctx.get("requester_platform"),
                                    requester_user_id=query_ctx.get("requester_user_id"),
                                    requester_user_name=query_ctx.get("requester_user_name"),
                                    requester_chat_id=query_ctx.get("requester_chat_id"),
                                    requester_message_id=query_ctx.get("requester_message_id"),
                                    requester_query=query_ctx.get("requester_query"),
                                )
                                session.add(record)
                                session.flush()
                            saved_count += 1
                        except IntegrityError:
                            # 单条 URL 唯一约束冲突（如并发插入），仅跳过本条，保留本批其余成功项
                            logger.debug("新闻情报重复（已跳过）: %s %s", code, url_key)

                session.commit()
                logger.info(f"保存新闻情报成功: {code}, 新增 {saved_count} 条")

            except Exception as e:
                session.rollback()
                logger.error(f"保存新闻情报失败: {e}")
                raise

        return saved_count

    def save_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
        payload: Optional[Dict[str, Any]],
        source_chain: Optional[Any] = None,
        coverage: Optional[Any] = None,
    ) -> int:
        """
        保存基本面快照（P0 write-only）。失败不抛异常，返回写入条数 0/1。
        """
        if not query_id or not code or payload is None:
            return 0

        with self.get_session() as session:
            try:
                session.add(
                    FundamentalSnapshot(
                        query_id=query_id,
                        code=code,
                        payload=self._safe_json_dumps(payload),
                        source_chain=self._safe_json_dumps(source_chain or []),
                        coverage=self._safe_json_dumps(coverage or {}),
                    )
                )
                session.commit()
                return 1
            except Exception as e:
                session.rollback()
                logger.debug(
                    "基本面快照写入失败（fail-open）: query_id=%s code=%s err=%s",
                    query_id,
                    code,
                    e,
                )
                return 0

    def get_latest_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定 query_id + code 的最新基本面快照 payload。

        读取失败或不存在时返回 None（fail-open）。
        """
        if not query_id or not code:
            return None

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(FundamentalSnapshot)
                    .where(
                        and_(
                            FundamentalSnapshot.query_id == query_id,
                            FundamentalSnapshot.code == code,
                        )
                    )
                    .order_by(desc(FundamentalSnapshot.created_at))
                    .limit(1)
                ).scalar_one_or_none()
            except Exception as e:
                logger.debug(
                    "基本面快照读取失败（fail-open）: query_id=%s code=%s err=%s",
                    query_id,
                    code,
                    e,
                )
                return None

            if row is None:
                return None
            try:
                payload = json.loads(row.payload or "{}")
                return payload if isinstance(payload, dict) else None
            except Exception:
                return None

    def get_recent_news(self, code: str, days: int = 7, limit: int = 20) -> List[NewsIntel]:
        """
        获取指定股票最近 N 天的新闻情报
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            results = session.execute(
                select(NewsIntel)
                .where(
                    and_(
                        NewsIntel.code == code,
                        NewsIntel.fetched_at >= cutoff_date
                    )
                )
                .order_by(desc(NewsIntel.fetched_at))
                .limit(limit)
            ).scalars().all()

            return list(results)

    def get_news_intel_by_query_id(
        self,
        query_id: str,
        limit: int = 20,
        as_of_date: Optional[date] = None,
    ) -> List[NewsIntel]:
        """
        根据 query_id 获取新闻情报列表

        Args:
            query_id: 分析记录唯一标识
            limit: 返回数量限制
            as_of_date: 可选截止日期过滤

        Returns:
            NewsIntel 列表（按发布时间或抓取时间倒序）
        """
        from sqlalchemy import func

        with self.get_session() as session:
            stmt = select(NewsIntel).where(NewsIntel.query_id == query_id)
            if as_of_date is not None:
                cutoff = datetime.combine(as_of_date, datetime.max.time())
                stmt = stmt.where(func.coalesce(NewsIntel.published_date, NewsIntel.fetched_at) <= cutoff)
            results = session.execute(
                stmt.order_by(
                    desc(func.coalesce(NewsIntel.published_date, NewsIntel.fetched_at)),
                    desc(NewsIntel.fetched_at)
                )
                .limit(limit)
            ).scalars().all()

            return list(results)

    def save_analysis_history(
        self,
        result: Any,
        query_id: str,
        report_type: str,
        news_content: Optional[str],
        context_snapshot: Optional[Dict[str, Any]] = None,
        save_snapshot: bool = True
    ) -> int:
        """
        保存分析结果历史记录
        """
        if result is None:
            return 0

        sniper_points = self._extract_sniper_points(result)
        raw_result = self._build_raw_result(result)
        context_text = None
        if save_snapshot and context_snapshot is not None:
            context_text = self._safe_json_dumps(context_snapshot)

        record = AnalysisHistory(
            query_id=query_id,
            code=result.code,
            name=result.name,
            report_type=report_type,
            sentiment_score=result.sentiment_score,
            operation_advice=result.operation_advice,
            trend_prediction=result.trend_prediction,
            analysis_summary=result.analysis_summary,
            raw_result=self._safe_json_dumps(raw_result),
            news_content=news_content,
            context_snapshot=context_text,
            ideal_buy=sniper_points.get("ideal_buy"),
            secondary_buy=sniper_points.get("secondary_buy"),
            stop_loss=sniper_points.get("stop_loss"),
            take_profit=sniper_points.get("take_profit"),
            created_at=datetime.now(),
        )

        with self.get_session() as session:
            try:
                session.add(record)
                session.commit()
                return 1
            except Exception as e:
                session.rollback()
                logger.error(f"保存分析历史失败: {e}")
                return 0

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50,
        exclude_query_id: Optional[str] = None,
    ) -> List[AnalysisHistory]:
        """
        Query analysis history records.

        Notes:
        - If query_id is provided, perform exact lookup and ignore days window.
        - If query_id is not provided, apply days-based time filtering.
        - exclude_query_id: exclude records with this query_id (for history comparison).
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            conditions = []

            if query_id:
                conditions.append(AnalysisHistory.query_id == query_id)
            else:
                conditions.append(AnalysisHistory.created_at >= cutoff_date)

            if code:
                conditions.append(AnalysisHistory.code == code)

            # exclude_query_id only applies when not doing exact lookup (query_id is None)
            if exclude_query_id and not query_id:
                conditions.append(AnalysisHistory.query_id != exclude_query_id)

            results = session.execute(
                select(AnalysisHistory)
                .where(and_(*conditions))
                .order_by(desc(AnalysisHistory.created_at))
                .limit(limit)
            ).scalars().all()

            return list(results)
    
    def get_analysis_history_paginated(
        self,
        code: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        offset: int = 0,
        limit: int = 20
    ) -> Tuple[List[AnalysisHistory], int]:
        """
        分页查询分析历史记录（带总数）
        
        Args:
            code: 股票代码筛选
            start_date: 开始日期（含）
            end_date: 结束日期（含）
            offset: 偏移量（跳过前 N 条）
            limit: 每页数量
            
        Returns:
            Tuple[List[AnalysisHistory], int]: (记录列表, 总数)
        """
        from sqlalchemy import func
        
        with self.get_session() as session:
            conditions = []
            
            if code:
                conditions.append(AnalysisHistory.code == code)
            if start_date:
                # created_at >= start_date 00:00:00
                conditions.append(AnalysisHistory.created_at >= datetime.combine(start_date, datetime.min.time()))
            if end_date:
                # created_at < end_date+1 00:00:00 (即 <= end_date 23:59:59)
                conditions.append(AnalysisHistory.created_at < datetime.combine(end_date + timedelta(days=1), datetime.min.time()))
            
            # 构建 where 子句
            where_clause = and_(*conditions) if conditions else True
            
            # 查询总数
            total_query = select(func.count(AnalysisHistory.id)).where(where_clause)
            total = session.execute(total_query).scalar() or 0
            
            # 查询分页数据
            data_query = (
                select(AnalysisHistory)
                .where(where_clause)
                .order_by(desc(AnalysisHistory.created_at))
                .offset(offset)
                .limit(limit)
            )
            results = session.execute(data_query).scalars().all()
            
            return list(results), total
    
    def get_analysis_history_by_id(self, record_id: int) -> Optional[AnalysisHistory]:
        """
        根据数据库主键 ID 查询单条分析历史记录
        
        由于 query_id 可能重复（批量分析时多条记录共享同一 query_id），
        使用主键 ID 确保精确查询唯一记录。
        
        Args:
            record_id: 分析历史记录的主键 ID
            
        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            result = session.execute(
                select(AnalysisHistory).where(AnalysisHistory.id == record_id)
            ).scalars().first()
            return result

    def delete_analysis_history_records(self, record_ids: List[int]) -> int:
        """
        删除指定的分析历史记录。

        同时清理依赖这些历史记录的回测结果，避免外键约束失败。

        Args:
            record_ids: 要删除的历史记录主键 ID 列表

        Returns:
            实际删除的历史记录数量
        """
        ids = sorted({int(record_id) for record_id in record_ids if record_id is not None})
        if not ids:
            return 0

        with self.session_scope() as session:
            session.execute(
                delete(BacktestResult).where(BacktestResult.analysis_history_id.in_(ids))
            )
            result = session.execute(
                delete(AnalysisHistory).where(AnalysisHistory.id.in_(ids))
            )
            return result.rowcount or 0

    def get_latest_analysis_by_query_id(self, query_id: str) -> Optional[AnalysisHistory]:
        """
        根据 query_id 查询最新一条分析历史记录

        query_id 在批量分析时可能重复，故返回最近创建的一条。

        Args:
            query_id: 分析记录关联的 query_id

        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            result = session.execute(
                select(AnalysisHistory)
                .where(AnalysisHistory.query_id == query_id)
                .order_by(desc(AnalysisHistory.created_at))
                .limit(1)
            ).scalars().first()
            return result

    def get_latest_analysis_by_query_id_and_code(
        self,
        query_id: str,
        code: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[AnalysisHistory]:
        """根据 query_id 和股票代码查询最新分析历史记录。"""
        with self.get_session() as session:
            stmt = select(AnalysisHistory).where(
                AnalysisHistory.query_id == query_id,
                AnalysisHistory.code == code,
            )
            if as_of_date is not None:
                stmt = stmt.where(AnalysisHistory.created_at <= datetime.combine(as_of_date, datetime.max.time()))
            result = session.execute(
                stmt.order_by(desc(AnalysisHistory.created_at)).limit(1)
            ).scalars().first()
            return result

    # ------------------------------------------------------------------
    # Screening Run / Candidate CRUD
    # ------------------------------------------------------------------

    def create_screening_run(
        self,
        trade_date: date,
        market: str = "cn",
        config_snapshot: Optional[Dict[str, Any]] = None,
        status: str = "pending",
        ai_top_k: int = 0,
        run_id: Optional[str] = None,
        return_created: bool = False,
        trigger_type: str = "manual",
    ) -> Any:
        """创建筛选任务记录并返回 run_id。"""
        if run_id is None:
            run_id = f"run-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        # Every newly completed screening run is eligible for auto notification.
        notification_status = "pending"

        record = ScreeningRun(
            run_id=run_id,
            trade_date=trade_date,
            market=market,
            status=status,
            ai_top_k=ai_top_k,
            config_snapshot=self._safe_json_dumps(config_snapshot or {}),
            started_at=datetime.now(),
            trigger_type=trigger_type,
            notification_status=notification_status,
        )

        created = True
        with self.session_scope() as session:
            try:
                session.add(record)
                session.flush()
            except IntegrityError:
                session.rollback()
                created = False

        if return_created:
            return run_id, created
        return run_id

    def update_screening_run_status(
        self,
        run_id: str,
        status: str,
        trade_date: Optional[date] = None,
        universe_size: Optional[int] = None,
        candidate_count: Optional[int] = None,
        error_summary: Optional[str] = None,
    ) -> bool:
        """更新筛选任务状态。"""
        with self.session_scope() as session:
            record = session.execute(
                select(ScreeningRun).where(ScreeningRun.run_id == run_id)
            ).scalar_one_or_none()
            if record is None:
                return False

            allowed_statuses = self._screening_status_transitions.get(record.status, {record.status})
            if status not in allowed_statuses:
                return False

            record.status = status
            record.last_activity_at = datetime.now()
            if trade_date is not None:
                record.trade_date = trade_date
            if universe_size is not None:
                record.universe_size = universe_size
            if candidate_count is not None:
                record.candidate_count = candidate_count
            if error_summary is not None:
                record.error_summary = error_summary
            if status in {"completed", "completed_with_ai_degraded", "failed"}:
                if record.completed_at is None:
                    record.completed_at = datetime.now()
            elif record.completed_at is not None:
                record.completed_at = None
            return True

    def touch_screening_run_heartbeat(self, run_id: str) -> bool:
        """更新筛选任务心跳时间，用于区分'正在运行但慢'和'已崩溃'。"""
        with self.session_scope() as session:
            result = session.execute(
                update(ScreeningRun)
                .where(ScreeningRun.run_id == run_id)
                .values(last_activity_at=datetime.now())
            )
            return result.rowcount > 0

    def update_notification_status(
        self,
        run_id: str,
        notification_status: str,
        notification_error: Optional[str] = None,
    ) -> bool:
        """Update notification lifecycle fields on a screening run."""
        with self.session_scope() as session:
            record = session.execute(
                select(ScreeningRun).where(ScreeningRun.run_id == run_id)
            ).scalar_one_or_none()
            if record is None:
                return False
            record.notification_status = notification_status
            record.notification_attempts = (record.notification_attempts or 0) + 1
            if notification_status == "sent":
                record.notification_sent_at = datetime.now()
            if notification_error is not None:
                record.notification_error = notification_error
            return True

    def get_screening_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """获取单次筛选任务。"""
        with self.get_session() as session:
            record = session.execute(
                select(ScreeningRun).where(ScreeningRun.run_id == run_id)
            ).scalar_one_or_none()
            return record.to_dict() if record else None

    def find_latest_screening_run(
        self,
        market: str = "cn",
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find the latest screening run for the same effective request snapshot."""
        expected_identity = self._screening_run_identity(config_snapshot or {})
        with self.get_session() as session:
            rows = session.execute(
                select(ScreeningRun)
                .where(ScreeningRun.market == market)
                .order_by(desc(ScreeningRun.started_at))
            ).scalars().all()

            for row in rows:
                payload = row.to_dict()
                if self._screening_run_identity(payload.get("config_snapshot", {})) == expected_identity:
                    return payload
        return None

    def reset_screening_run_for_rerun(
        self,
        run_id: str,
        config_snapshot: Optional[Dict[str, Any]] = None,
        ai_top_k: Optional[int] = None,
    ) -> bool:
        """Reset a failed screening run so it can be re-executed in place."""
        with self.session_scope() as session:
            values = {
                "status": "pending",
                "universe_size": 0,
                "candidate_count": 0,
                "error_summary": None,
                "completed_at": None,
                "started_at": datetime.now(),
                "last_activity_at": datetime.now(),
            }
            if config_snapshot is not None:
                values["config_snapshot"] = self._safe_json_dumps(config_snapshot)
            if ai_top_k is not None:
                values["ai_top_k"] = ai_top_k

            result = session.execute(
                update(ScreeningRun)
                .where(
                    ScreeningRun.run_id == run_id,
                    ScreeningRun.status == "failed",
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return False

            session.execute(
                delete(ScreeningCandidate).where(ScreeningCandidate.run_id == run_id)
            )
            return True

    def update_screening_run_context(
        self,
        run_id: str,
        config_snapshot_updates: Optional[Dict[str, Any]] = None,
        ai_top_k: Optional[int] = None,
    ) -> bool:
        """Update the persisted config snapshot or ai_top_k for an existing run."""
        with self.session_scope() as session:
            record = session.execute(
                select(ScreeningRun).where(ScreeningRun.run_id == run_id)
            ).scalar_one_or_none()
            if record is None:
                return False

            if config_snapshot_updates:
                snapshot = json.loads(record.config_snapshot) if record.config_snapshot else {}
                snapshot.update(config_snapshot_updates)
                record.config_snapshot = self._safe_json_dumps(snapshot)
            if ai_top_k is not None:
                record.ai_top_k = ai_top_k
            return True

    @staticmethod
    def _screening_run_identity(config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Build a stable identity used for screening run idempotency."""
        return {
            "requested_trade_date": str(config_snapshot.get("requested_trade_date") or ""),
            "mode": str(config_snapshot.get("mode") or "balanced"),
            "stock_codes": sorted(
                {
                    str(code).strip().upper()
                    for code in (config_snapshot.get("stock_codes") or [])
                    if str(code).strip()
                }
            ),
            "candidate_limit": config_snapshot.get("candidate_limit"),
            "ai_top_k": config_snapshot.get("ai_top_k"),
            "screening_min_list_days": config_snapshot.get("screening_min_list_days"),
            "screening_min_volume_ratio": config_snapshot.get("screening_min_volume_ratio"),
            "screening_breakout_lookback_days": config_snapshot.get("screening_breakout_lookback_days"),
            "screening_factor_lookback_days": config_snapshot.get("screening_factor_lookback_days"),
            "screening_ingest_failure_threshold": config_snapshot.get("screening_ingest_failure_threshold", 0.02),
        }

    def list_screening_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """按开始时间倒序列出筛选任务。"""
        with self.get_session() as session:
            rows = session.execute(
                select(ScreeningRun)
                .order_by(desc(ScreeningRun.started_at))
                .limit(limit)
            ).scalars().all()
            return [row.to_dict() for row in rows]

    def clear_screening_runs(self) -> int:
        """删除所有筛选任务及其候选结果，返回删除的任务数。"""
        with self.session_scope() as session:
            count = session.execute(
                select(func.count()).select_from(ScreeningRun)
            ).scalar() or 0
            session.execute(delete(ScreeningCandidate))
            session.execute(delete(ScreeningRun))
        return int(count)

    def delete_screening_run(self, run_id: str) -> bool:
        """删除单条筛选任务及其候选结果。"""
        with self.session_scope() as session:
            session.execute(
                delete(ScreeningCandidate).where(ScreeningCandidate.run_id == run_id)
            )
            result = session.execute(
                delete(ScreeningRun).where(ScreeningRun.run_id == run_id)
            )
            return result.rowcount > 0

    def save_screening_candidates(self, run_id: str, candidates: List[Dict[str, Any]]) -> int:
        """批量保存筛选候选结果。

        DELETE-then-INSERT 仍是默认语义（用于人工重跑同一 run_id 时清空旧
        数据），但会优先保留 ``(run_id, code)`` 的**首次** ``created_at``
        作为决策时刻锚点；本次写入的时间记录到 ``updated_at``。
        backtest 侧可以据此检测候选是否在回测触发后被改写过（A4 lite）。
        """
        now = datetime.now()
        with self.session_scope() as session:
            existing_first_seen: Dict[str, datetime] = {
                row.code: row.created_at
                for row in session.query(ScreeningCandidate)
                .filter(ScreeningCandidate.run_id == run_id)
                .all()
                if row.created_at is not None
            }
            session.execute(
                delete(ScreeningCandidate).where(ScreeningCandidate.run_id == run_id)
            )

            for item in candidates:
                ai_review = item.get("ai_review") or {}
                if not ai_review:
                    ai_review = {
                        "ai_query_id": item.get("ai_query_id"),
                        "ai_summary": item.get("ai_summary"),
                        "ai_operation_advice": item.get("ai_operation_advice"),
                        "ai_trade_stage": item.get("ai_trade_stage"),
                        "ai_reasoning": item.get("ai_reasoning"),
                        "ai_confidence": item.get("ai_confidence"),
                        "ai_environment_ok": item.get("ai_environment_ok"),
                        "ai_theme_alignment": item.get("ai_theme_alignment"),
                        "ai_entry_quality": item.get("ai_entry_quality"),
                        "stage_conflict": item.get("stage_conflict"),
                        "result_source": item.get("result_source"),
                        "is_fallback": item.get("is_fallback"),
                        "fallback_reason": item.get("fallback_reason"),
                        "downgrade_reasons": item.get("downgrade_reasons"),
                        "initial_position": item.get("initial_position"),
                        "stop_loss_rule": item.get("stop_loss_rule"),
                        "take_profit_plan": item.get("take_profit_plan"),
                        "invalidation_rule": item.get("invalidation_rule"),
                        "prompt_version": item.get("prompt_version"),
                        "model_name": item.get("model_name"),
                        "parse_status": item.get("parse_status"),
                        "retry_count": item.get("retry_count"),
                    }
                decision_payload = dict(item)
                if "ai_review" not in decision_payload and any(value is not None for value in ai_review.values()):
                    decision_payload["ai_review"] = ai_review
                if decision_payload.get("trade_plan") is None and item.get("trade_plan_json"):
                    try:
                        parsed_trade_plan = json.loads(item["trade_plan_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_trade_plan = None
                    if isinstance(parsed_trade_plan, dict):
                        decision_payload["trade_plan"] = parsed_trade_plan
                session.add(
                    ScreeningCandidate(
                        run_id=run_id,
                        code=item["code"],
                        name=item.get("name"),
                        rank=int(item.get("rank", 0)),
                        rule_score=float(item.get("rule_score", 0.0)),
                        raw_rule_score=item.get("raw_rule_score"),
                        selected_for_ai=bool(item.get("selected_for_ai", False)),
                        candidate_decision_json=self._safe_json_dumps(decision_payload),
                        matched_strategies_json=self._safe_json_dumps(item.get("matched_strategies", [])),
                        rule_hits_json=self._safe_json_dumps(item.get("rule_hits", [])),
                        factor_snapshot_json=self._safe_json_dumps(item.get("factor_snapshot", {})),
                        ai_query_id=ai_review.get("ai_query_id"),
                        ai_summary=ai_review.get("ai_summary"),
                        ai_operation_advice=ai_review.get("ai_operation_advice"),
                        # ── AI 二筛协议字段 (Phase 3B-1) ──
                        ai_trade_stage=ai_review.get("ai_trade_stage"),
                        ai_reasoning=ai_review.get("ai_reasoning"),
                        ai_confidence=ai_review.get("ai_confidence"),
                        # ── 五层决策字段 (Phase 2A) ──
                        setup_type=item.get("setup_type"),
                        trade_stage=item.get("trade_stage"),
                        entry_maturity=item.get("entry_maturity"),
                        risk_level=item.get("risk_level"),
                        market_regime=item.get("market_regime"),
                        theme_position=item.get("theme_position"),
                        candidate_pool_level=item.get("candidate_pool_level"),
                        trade_plan_json=self._safe_json_dumps(item.get("trade_plan")) if item.get("trade_plan") is not None else None,
                        # Keep the first-ever insertion time as the
                        # decision-time anchor; refresh updated_at on every
                        # rewrite. See class docstring for ``ScreeningCandidate``.
                        created_at=existing_first_seen.get(item["code"], now),
                        updated_at=now,
                    )
                )

        return len(candidates)

    def list_screening_candidates(
        self,
        run_id: str,
        limit: int = 100,
        with_ai_only: bool = False,
        as_of_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """查询某次筛选任务的候选结果。"""
        with self.get_session() as session:
            stmt = (
                select(ScreeningCandidate)
                .where(ScreeningCandidate.run_id == run_id)
                .order_by(ScreeningCandidate.rank)
            )
            if with_ai_only:
                stmt = stmt.where(ScreeningCandidate.selected_for_ai.is_(True))
            rows = session.execute(stmt).scalars().all()
            return self._enrich_screening_candidates([row.to_dict() for row in rows], as_of_date=as_of_date)[:limit]

    def get_screening_candidate_detail(self, run_id: str, code: str) -> Optional[Dict[str, Any]]:
        """查询某次筛选任务下单只候选的完整详情。"""
        with self.get_session() as session:
            rows = session.execute(
                select(ScreeningCandidate)
                .where(ScreeningCandidate.run_id == run_id)
                .order_by(ScreeningCandidate.rank)
            ).scalars().all()
            if not rows:
                return None

        enriched = self._enrich_screening_candidates([row.to_dict() for row in rows])
        item = next((candidate for candidate in enriched if candidate.get("code") == code), None)
        if item is None:
            return None

        item["analysis_history"] = self._build_screening_analysis_history_ref(
            query_id=(item.get("ai_review") or {}).get("ai_query_id"),
            code=item.get("code"),
        )
        return item

    def _enrich_screening_candidates(
        self,
        items: List[Dict[str, Any]],
        as_of_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        for item in items:
            news_records = []
            ai_review = item.get("ai_review") or {}
            if not ai_review:
                ai_review = {
                    "ai_query_id": item.get("ai_query_id"),
                    "ai_summary": item.get("ai_summary"),
                    "ai_operation_advice": item.get("ai_operation_advice"),
                    "ai_trade_stage": item.get("ai_trade_stage"),
                    "ai_reasoning": item.get("ai_reasoning"),
                    "ai_confidence": item.get("ai_confidence"),
                    "ai_environment_ok": item.get("ai_environment_ok"),
                    "ai_theme_alignment": item.get("ai_theme_alignment"),
                    "ai_entry_quality": item.get("ai_entry_quality"),
                    "stage_conflict": item.get("stage_conflict"),
                    "result_source": item.get("result_source"),
                    "is_fallback": item.get("is_fallback"),
                    "fallback_reason": item.get("fallback_reason"),
                    "downgrade_reasons": item.get("downgrade_reasons"),
                    "initial_position": item.get("initial_position"),
                    "stop_loss_rule": item.get("stop_loss_rule"),
                    "take_profit_plan": item.get("take_profit_plan"),
                    "invalidation_rule": item.get("invalidation_rule"),
                    "prompt_version": item.get("prompt_version"),
                    "model_name": item.get("model_name"),
                    "parse_status": item.get("parse_status"),
                    "retry_count": item.get("retry_count"),
                }
            ai_query_id = ai_review.get("ai_query_id")
            if ai_query_id:
                news_records = self.get_news_intel_by_query_id(ai_query_id, limit=3, as_of_date=as_of_date)

            news_titles = [record.title for record in news_records if getattr(record, "title", None)]
            news_count = len(news_records)
            structured_ai_present = bool(
                ai_review.get("ai_trade_stage")
                or ai_review.get("ai_environment_ok") is not None
                or ai_review.get("fallback_reason")
                or ai_review.get("result_source")
            )
            recommendation_source = str(
                ai_review.get("result_source")
                or ("rules_plus_ai" if structured_ai_present and ai_review.get("ai_trade_stage") else "rules_only")
            )
            has_ai_analysis = recommendation_source == "rules_plus_ai"
            final_score = round(float(item.get("rule_score", 0.0)), 2)

            reason_parts = [f"规则得分 {float(item.get('rule_score', 0.0)):.1f}"]
            if has_ai_analysis:
                reason_parts.append(f"AI 建议 {ai_review.get('ai_operation_advice') or '已分析'}")
            else:
                reason_parts.append("按规则结果输出")
            if news_count:
                reason_parts.append(f"新闻补充 {news_count} 条")

            enriched_item = {
                **item,
                "has_ai_analysis": has_ai_analysis,
                "news_count": news_count,
                "news_summary": "；".join(news_titles[:2]) if news_titles else None,
                "recommendation_source": recommendation_source,
                "recommendation_reason": "；".join(reason_parts),
                "final_score": final_score,
                "ai_query_id": ai_query_id,
                "ai_summary": ai_review.get("ai_summary"),
                "ai_operation_advice": ai_review.get("ai_operation_advice"),
                "ai_trade_stage": ai_review.get("ai_trade_stage"),
                "ai_reasoning": ai_review.get("ai_reasoning"),
                "ai_confidence": ai_review.get("ai_confidence"),
                "ai_environment_ok": ai_review.get("ai_environment_ok"),
                "ai_theme_alignment": ai_review.get("ai_theme_alignment"),
                "ai_entry_quality": ai_review.get("ai_entry_quality"),
                "stage_conflict": ai_review.get("stage_conflict"),
                "result_source": recommendation_source,
                "is_fallback": bool(ai_review.get("is_fallback", recommendation_source == "rules_fallback")),
                "fallback_reason": ai_review.get("fallback_reason"),
                "downgrade_reasons": list(ai_review.get("downgrade_reasons", []) or []),
                "initial_position": ai_review.get("initial_position"),
                "stop_loss_rule": ai_review.get("stop_loss_rule"),
                "take_profit_plan": ai_review.get("take_profit_plan"),
                "invalidation_rule": ai_review.get("invalidation_rule"),
                "prompt_version": ai_review.get("prompt_version"),
                "model_name": ai_review.get("model_name"),
                "parse_status": ai_review.get("parse_status"),
                "retry_count": ai_review.get("retry_count"),
            }
            if ai_review:
                enriched_item["ai_review"] = {
                    **ai_review,
                    "ai_query_id": ai_query_id,
                    "result_source": recommendation_source,
                }
            enriched.append(enriched_item)

        ordered = sorted(
            enriched,
            key=lambda item: (int(item["rank"]), -float(item["rule_score"])),
        )
        for index, item in enumerate(ordered, start=1):
            item["final_rank"] = index
        return ordered

    def _build_screening_analysis_history_ref(self, query_id: Optional[str], code: Optional[str]) -> Optional[Dict[str, Any]]:
        if not query_id or not code:
            return None
        record = self.get_latest_analysis_by_query_id_and_code(query_id=query_id, code=code)
        if record is None:
            return None
        return {
            "id": record.id,
            "query_id": record.query_id,
            "stock_code": record.code,
            "stock_name": record.name,
            "report_type": record.report_type,
            "operation_advice": record.operation_advice,
            "trend_prediction": record.trend_prediction,
            "sentiment_score": record.sentiment_score,
            "analysis_summary": record.analysis_summary,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    @staticmethod
    def _screening_ai_bonus(ai_trade_stage: Optional[str], result_source: Optional[str]) -> float:
        if result_source != "rules_plus_ai":
            return 0.0
        mapping = {
            "add_on_strength": 6.0,
            "probe_entry": 4.0,
            "focus": 2.0,
            "watch": 0.0,
            "stand_aside": -2.0,
            "reject": -4.0,
            "买入": 8.0,
            "加仓": 6.0,
            "关注": 4.0,
            "持有": 2.0,
            "观望": 0.0,
            "减仓": -4.0,
            "卖出": -8.0,
        }
        return mapping.get(str(ai_trade_stage or "").strip(), 0.0)

    # ------------------------------------------------------------------
    # Instrument Master / Factor Snapshot CRUD
    # ------------------------------------------------------------------

    def upsert_instruments(self, instruments: List[Dict[str, Any]]) -> int:
        """批量写入或更新股票池主数据。"""
        if not instruments:
            return 0

        with self.session_scope() as session:
            normalized_items = []
            for item in instruments:
                code = str(item["code"]).strip().upper()
                normalized_items.append((code, item))

            existing_codes = [code for code, _ in normalized_items]
            existing_records = session.execute(
                select(InstrumentMaster).where(InstrumentMaster.code.in_(existing_codes))
            ).scalars().all()
            record_map = {record.code: record for record in existing_records}

            for code, item in normalized_items:
                record = record_map.get(code)
                if record is None:
                    record = InstrumentMaster(code=code)
                    session.add(record)
                    record_map[code] = record

                record.name = item.get("name") or code
                record.market = item.get("market") or "cn"
                record.exchange = item.get("exchange")
                record.listing_status = item.get("listing_status") or "active"
                record.is_st = bool(item.get("is_st", False))
                record.industry = item.get("industry")
                record.list_date = item.get("list_date")
                record.updated_at = datetime.now()

        return len(instruments)

    def upsert_boards(self, boards: List[Dict[str, Any]]) -> int:
        """批量写入或更新板块主数据。"""
        if not boards:
            return 0

        normalized_items: List[Dict[str, Any]] = []
        for item in boards:
            board_name = str(item.get("board_name") or "").strip()
            if not board_name:
                continue
            normalized_items.append(
                {
                    "board_code": str(item.get("board_code")).strip() if item.get("board_code") is not None else None,
                    "board_name": board_name,
                    "board_type": str(item.get("board_type") or "unknown").strip() or "unknown",
                    "market": str(item.get("market") or "cn").strip() or "cn",
                    "source": str(item.get("source") or "unknown").strip() or "unknown",
                    "is_active": bool(item.get("is_active", True)),
                }
            )

        if not normalized_items:
            return 0

        with self.session_scope() as session:
            identities = {
                (item["market"], item["source"], item["board_name"], item["board_type"])
                for item in normalized_items
            }
            existing_records = session.execute(select(BoardMaster)).scalars().all()
            record_map = {
                (record.market, record.source, record.board_name, record.board_type): record
                for record in existing_records
                if (record.market, record.source, record.board_name, record.board_type) in identities
            }

            for item in normalized_items:
                identity = (item["market"], item["source"], item["board_name"], item["board_type"])
                record = record_map.get(identity)
                if record is None:
                    record = BoardMaster(
                        board_name=item["board_name"],
                        board_type=item["board_type"],
                        market=item["market"],
                        source=item["source"],
                    )
                    session.add(record)
                    record_map[identity] = record

                record.board_code = item["board_code"]
                record.is_active = item["is_active"]
                record.updated_at = datetime.now()

        return len(normalized_items)

    def replace_instrument_board_memberships(
        self,
        instrument_code: str,
        memberships: List[Dict[str, Any]],
        market: str = "cn",
        source: Optional[str] = None,
    ) -> int:
        """按股票替换板块关系。"""
        normalized_code = str(instrument_code).strip().upper()
        normalized_market = str(market or "cn").strip() or "cn"
        if not normalized_code:
            return 0

        normalized_memberships: List[Dict[str, Any]] = []
        for item in memberships:
            board_name = str(item.get("board_name") or "").strip()
            if not board_name:
                continue
            normalized_memberships.append(
                {
                    "board_code": str(item.get("board_code")).strip() if item.get("board_code") is not None else None,
                    "board_name": board_name,
                    "board_type": str(item.get("board_type") or "unknown").strip() or "unknown",
                    "market": str(item.get("market") or normalized_market).strip() or normalized_market,
                    "source": str(item.get("source") or source or "unknown").strip() or "unknown",
                    "is_active": bool(item.get("is_active", True)),
                    "is_primary": bool(item.get("is_primary", False)),
                }
            )

        if normalized_memberships:
            self.upsert_boards(normalized_memberships)

        with self.session_scope() as session:
            delete_stmt = delete(InstrumentBoardMembership).where(
                InstrumentBoardMembership.instrument_code == normalized_code,
                InstrumentBoardMembership.market == normalized_market,
            )
            if source:
                delete_stmt = delete_stmt.where(InstrumentBoardMembership.source == source)
            session.execute(delete_stmt)

            if not normalized_memberships:
                return 0

            identities = {
                (item["market"], item["source"], item["board_name"], item["board_type"])
                for item in normalized_memberships
            }
            board_records = session.execute(select(BoardMaster)).scalars().all()
            board_map = {
                (record.market, record.source, record.board_name, record.board_type): record
                for record in board_records
                if (record.market, record.source, record.board_name, record.board_type) in identities
            }

            deduped_memberships = {}
            for item in normalized_memberships:
                deduped_memberships[
                    (item["board_name"], item["board_type"], item["market"], item["source"])
                ] = item

            for item in deduped_memberships.values():
                identity = (item["market"], item["source"], item["board_name"], item["board_type"])
                board_record = board_map.get(identity)
                if board_record is None:
                    continue
                session.add(
                    InstrumentBoardMembership(
                        instrument_code=normalized_code,
                        board_id=board_record.id,
                        market=item["market"],
                        source=item["source"],
                        is_primary=item["is_primary"],
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                )

        return len(
            {
                (item["board_name"], item["board_type"], item["market"], item["source"])
                for item in normalized_memberships
            }
        )

    def batch_get_instrument_board_names(
        self,
        codes: List[str],
        market: str = "cn",
    ) -> Dict[str, List[str]]:
        """批量读取股票所属板块名称。"""
        normalized_codes = [str(code).strip().upper() for code in codes if str(code).strip()]
        result = {code: [] for code in normalized_codes}
        if not normalized_codes:
            return result

        with self.get_session() as session:
            rows = session.execute(
                select(InstrumentBoardMembership.instrument_code, BoardMaster.board_name)
                .join(BoardMaster, InstrumentBoardMembership.board_id == BoardMaster.id)
                .where(
                    InstrumentBoardMembership.instrument_code.in_(normalized_codes),
                    InstrumentBoardMembership.market == market,
                    BoardMaster.is_active.is_(True),
                )
                .order_by(InstrumentBoardMembership.instrument_code, BoardMaster.board_name)
            ).all()

        for instrument_code, board_name in rows:
            if board_name and board_name not in result[instrument_code]:
                result[instrument_code].append(board_name)

        return result

    # ── SectorHeat 相关方法 ─────────────────────────────────────────────────

    def list_active_boards_with_member_count(
        self,
        market: str = "cn",
        board_type: Optional[str] = None,
        min_member_count: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出所有活跃板块及其成员股票数量。

        Returns:
            [{"board_id": 1, "board_name": "白酒", "board_type": "industry",
              "member_count": 45}, ...]
        """
        with self.get_session() as session:
            query = (
                select(
                    BoardMaster.id.label("board_id"),
                    BoardMaster.board_name,
                    BoardMaster.board_type,
                    func.count(InstrumentBoardMembership.id).label("member_count"),
                )
                .select_from(BoardMaster)
                .outerjoin(
                    InstrumentBoardMembership,
                    InstrumentBoardMembership.board_id == BoardMaster.id,
                )
                .where(
                    BoardMaster.is_active.is_(True),
                    BoardMaster.market == market,
                )
                .group_by(BoardMaster.id, BoardMaster.board_name, BoardMaster.board_type)
            )
            if board_type:
                query = query.where(BoardMaster.board_type == board_type)
            if min_member_count > 0:
                query = query.having(func.count(InstrumentBoardMembership.id) >= min_member_count)
            query = query.order_by(desc(func.count(InstrumentBoardMembership.id)))

            rows = session.execute(query).all()

        return [
            {
                "board_id": row.board_id,
                "board_name": row.board_name,
                "board_type": row.board_type,
                "member_count": row.member_count,
            }
            for row in rows
        ]

    def batch_get_board_member_codes(
        self,
        board_names: List[str],
        market: str = "cn",
    ) -> Dict[str, List[str]]:
        """批量获取板块成员股票代码（板块→股票反向查询）。

        Returns:
            {"白酒": ["600519", "000858", ...], "锂电池": ["300750", ...]}
        """
        result = {name: [] for name in board_names}
        if not board_names:
            return result

        with self.get_session() as session:
            rows = session.execute(
                select(BoardMaster.board_name, InstrumentBoardMembership.instrument_code)
                .join(BoardMaster, InstrumentBoardMembership.board_id == BoardMaster.id)
                .where(
                    BoardMaster.board_name.in_(board_names),
                    BoardMaster.is_active.is_(True),
                    InstrumentBoardMembership.market == market,
                )
                .order_by(BoardMaster.board_name, InstrumentBoardMembership.instrument_code)
            ).all()

        for board_name, instrument_code in rows:
            if instrument_code and instrument_code not in result.get(board_name, []):
                result.setdefault(board_name, []).append(instrument_code)

        return result

    def save_sector_heat_batch(
        self,
        trade_date: date,
        heat_records: List[Dict[str, Any]],
    ) -> int:
        """批量写入板块热度数据。同一 (trade_date, board_name) 先删后插实现覆盖。"""
        if not heat_records:
            return 0

        board_names = [r["board_name"] for r in heat_records if r.get("board_name")]

        with self.session_scope() as session:
            if board_names:
                session.execute(
                    delete(DailySectorHeat).where(
                        DailySectorHeat.trade_date == trade_date,
                        DailySectorHeat.board_name.in_(board_names),
                    )
                )

            for item in heat_records:
                session.add(
                    DailySectorHeat(
                        trade_date=item.get("trade_date", trade_date),
                        board_name=item["board_name"],
                        board_type=item.get("board_type", "concept"),
                        breadth_score=float(item.get("breadth_score", 0.0)),
                        strength_score=float(item.get("strength_score", 0.0)),
                        persistence_score=float(item.get("persistence_score", 0.0)),
                        leadership_score=float(item.get("leadership_score", 0.0)),
                        sector_hot_score=float(item.get("sector_hot_score", 0.0)),
                        sector_status=item.get("sector_status"),
                        sector_stage=item.get("sector_stage"),
                        stock_count=int(item.get("stock_count", 0)),
                        up_count=int(item.get("up_count", 0)),
                        limit_up_count=int(item.get("limit_up_count", 0)),
                        avg_pct_chg=float(item.get("avg_pct_chg", 0.0)),
                        leader_codes_json=item.get("leader_codes_json"),
                        front_codes_json=item.get("front_codes_json"),
                        board_strength_score=float(item.get("board_strength_score", 0.0) or 0.0),
                        board_strength_rank=int(item.get("board_strength_rank", 0) or 0),
                        board_strength_percentile=float(item.get("board_strength_percentile", 0.0) or 0.0),
                        leader_candidate_count=int(item.get("leader_candidate_count", 0) or 0),
                        quality_flags_json=item.get("quality_flags_json"),
                        reason=item.get("reason"),
                        created_at=datetime.now(),
                    )
                )

        return len(heat_records)

    def list_sector_heat_history(
        self,
        board_name: str,
        end_date: date,
        lookback_days: int = 5,
    ) -> List[Dict[str, Any]]:
        """查询板块热度历史，按日期升序返回最近 lookback_days 条。

        用于 persistence_score 计算和冷启动判断。
        """
        start_date = end_date - timedelta(days=lookback_days + 10)

        with self.get_session() as session:
            # 子查询：按日期降序取最近 N 条，外层反转为升序
            subq = (
                select(DailySectorHeat)
                .where(
                    DailySectorHeat.board_name == board_name,
                    DailySectorHeat.trade_date >= start_date,
                    DailySectorHeat.trade_date <= end_date,
                )
                .order_by(desc(DailySectorHeat.trade_date))
                .limit(lookback_days)
            ).subquery()
            rows = session.execute(
                select(DailySectorHeat)
                .join(subq, DailySectorHeat.id == subq.c.id)
                .order_by(DailySectorHeat.trade_date)
            ).scalars().all()

        return [row.to_dict() for row in rows]

    def get_instrument(self, code: str) -> Optional[Dict[str, Any]]:
        """根据代码查询单个股票池主数据。"""
        with self.get_session() as session:
            record = session.execute(
                select(InstrumentMaster).where(InstrumentMaster.code == str(code).strip().upper())
            ).scalar_one_or_none()
            return record.to_dict() if record else None

    def list_instruments(
        self,
        market: Optional[str] = None,
        listing_status: Optional[str] = None,
        exclude_st: bool = False,
        codes: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """查询股票池主数据列表。"""
        with self.get_session() as session:
            stmt = select(InstrumentMaster).order_by(InstrumentMaster.code)
            if market:
                stmt = stmt.where(InstrumentMaster.market == market)
            if listing_status:
                stmt = stmt.where(InstrumentMaster.listing_status == listing_status)
            if exclude_st:
                stmt = stmt.where(InstrumentMaster.is_st.is_(False))
            if codes:
                normalized_codes = [str(code).strip().upper() for code in codes if str(code).strip()]
                stmt = stmt.where(InstrumentMaster.code.in_(normalized_codes))
            if limit is not None:
                stmt = stmt.limit(limit)

            rows = session.execute(stmt).scalars().all()
            return [row.to_dict() for row in rows]

    def replace_factor_snapshots(self, trade_date: date, snapshots: List[Dict[str, Any]]) -> int:
        """按交易日全量替换因子快照。"""
        with self.session_scope() as session:
            session.execute(
                delete(DailyFactorSnapshot).where(DailyFactorSnapshot.trade_date == trade_date)
            )
            for item in snapshots:
                session.add(
                    DailyFactorSnapshot(
                        trade_date=trade_date,
                        code=str(item["code"]).strip().upper(),
                        close=item.get("close"),
                        pct_chg=item.get("pct_chg"),
                        ma5=item.get("ma5"),
                        ma10=item.get("ma10"),
                        ma20=item.get("ma20"),
                        ma60=item.get("ma60"),
                        volume_ratio=item.get("volume_ratio"),
                        turnover_rate=item.get("turnover_rate"),
                        trend_score=item.get("trend_score"),
                        liquidity_score=item.get("liquidity_score"),
                        risk_flags_json=self._safe_json_dumps(item.get("risk_flags", [])),
                        created_at=datetime.now(),
                    )
                )
        return len(snapshots)

    def list_factor_snapshots(self, trade_date: date, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """查询指定交易日的因子快照。"""
        with self.get_session() as session:
            stmt = (
                select(DailyFactorSnapshot)
                .where(DailyFactorSnapshot.trade_date == trade_date)
                .order_by(DailyFactorSnapshot.code)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.execute(stmt).scalars().all()
            return [row.to_dict() for row in rows]

    def get_data_range(
        self,
        code: str, 
        start_date: date, 
        end_date: date
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据
        
        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            StockDaily 对象列表
        """
        with self.get_session() as session:
            results = session.execute(
                select(StockDaily)
                .where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date >= start_date,
                        StockDaily.date <= end_date
                    )
                )
                .order_by(StockDaily.date)
            ).scalars().all()
            
            return list(results)
    
    def save_daily_data(
        self, 
        df: pd.DataFrame, 
        code: str,
        data_source: str = "Unknown"
    ) -> int:
        """
        保存日线数据到数据库
        
        策略：
        - 使用 UPSERT 逻辑（存在则更新，不存在则插入）
        - 跳过已存在的数据，避免重复
        
        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源名称
            
        Returns:
            新增/更新的记录数
        """
        if df is None or df.empty:
            logger.warning(f"保存数据为空，跳过 {code}")
            return 0
        
        saved_count = 0
        
        with self.get_session() as session:
            try:
                for _, row in df.iterrows():
                    # 解析日期
                    row_date = row.get('date')
                    if isinstance(row_date, str):
                        row_date = datetime.strptime(row_date, '%Y-%m-%d').date()
                    elif isinstance(row_date, datetime):
                        row_date = row_date.date()
                    elif isinstance(row_date, pd.Timestamp):
                        row_date = row_date.date()
                    
                    # 检查是否已存在
                    existing = session.execute(
                        select(StockDaily).where(
                            and_(
                                StockDaily.code == code,
                                StockDaily.date == row_date
                            )
                        )
                    ).scalar_one_or_none()
                    
                    # B6: read forward-adjustment metadata from the row
                    # if the fetcher provided it; otherwise default to
                    # adj_factor=1.0 with source='fetcher_unset' so the
                    # backtest layer can distinguish "not yet supplied" from
                    # "legacy data backfilled by migration".
                    adj_factor = row.get('adj_factor')
                    if adj_factor is None or pd.isna(adj_factor):
                        adj_factor = 1.0
                        adj_factor_source = row.get('adj_factor_source') or 'fetcher_unset'
                    else:
                        adj_factor_source = row.get('adj_factor_source') or 'fetcher_provided'
                    adj_anchor_date = row.get('adj_anchor_date')
                    if isinstance(adj_anchor_date, str):
                        try:
                            adj_anchor_date = datetime.strptime(
                                adj_anchor_date, '%Y-%m-%d'
                            ).date()
                        except ValueError:
                            adj_anchor_date = None
                    elif isinstance(adj_anchor_date, (datetime, pd.Timestamp)):
                        adj_anchor_date = adj_anchor_date.date() if hasattr(
                            adj_anchor_date, 'date'
                        ) else adj_anchor_date

                    # pre_close 是免费重建复权因子的唯一依据；缺失必须落 NULL，
                    # 落 0.0 会让 ratio = pre_close / close 静默得到 0。
                    # NaN 必须在这里归一成 None：NaN 不是 None，会通过下面更新分支的
                    # `if pre_close is not None` 判断，把已落库的值覆盖成 NULL。
                    pre_close = row.get('pre_close')
                    if pre_close is not None and pd.isna(pre_close):
                        pre_close = None
                    # 口径必须由取数路径显式声明。这条路的数据源可能是 efinance
                    # （疑似前复权）或 Tushare（开关打开时为 qfq），按数据源名字
                    # 猜测等于谎报，因此未声明一律落 unknown。
                    adj_convention = row.get('adj_convention')
                    if not adj_convention or pd.isna(adj_convention):
                        adj_convention = 'unknown'
                    elif adj_convention not in VALID_ADJ_CONVENTIONS:
                        # 枚举外的值（例如 unadjusted 这类近义词）原样落库，会在
                        # 后续复权重建阶段被当作 unverifiable 静默排除，整批数据
                        # 失效却没有任何信号。降级成 unknown 并告警。
                        logger.warning(
                            f"{code} adj_convention 取值 {adj_convention!r} 不在 "
                            f"{sorted(VALID_ADJ_CONVENTIONS)} 内，降级为 unknown"
                        )
                        adj_convention = 'unknown'

                    if existing:
                        # 更新现有记录
                        existing.open = row.get('open')
                        existing.high = row.get('high')
                        existing.low = row.get('low')
                        existing.close = row.get('close')
                        existing.volume = row.get('volume')
                        existing.amount = row.get('amount')
                        existing.pct_chg = row.get('pct_chg')
                        existing.ma5 = row.get('ma5')
                        existing.ma10 = row.get('ma10')
                        existing.ma20 = row.get('ma20')
                        existing.volume_ratio = row.get('volume_ratio')
                        existing.data_source = data_source
                        existing.adj_factor = adj_factor
                        existing.adj_anchor_date = adj_anchor_date
                        existing.adj_factor_source = adj_factor_source
                        # 口径标签必须跟着价格走：上面的 OHLC 已被本次取数覆盖，
                        # 沿用旧标签会把新数据源的价格贴上别人的口径。未声明就
                        # 降级为 unknown——可用性损失可接受，错误标注不可接受。
                        existing.adj_convention = adj_convention
                        # pre_close 相反：本次没带就保留已有值，不覆盖成 NULL。
                        # 兜底路径的 DataFrame 经 _normalize_data 后通常不含该字段，
                        # 无条件覆盖会让任何 force 重同步或 K 线修复擦掉 bulk 写入的
                        # pre_close，也就是复权重建唯一的免费依据。
                        if pre_close is not None:
                            existing.pre_close = pre_close
                        existing.updated_at = datetime.now()
                    else:
                        # 创建新记录
                        record = StockDaily(
                            code=code,
                            date=row_date,
                            open=row.get('open'),
                            high=row.get('high'),
                            low=row.get('low'),
                            close=row.get('close'),
                            volume=row.get('volume'),
                            amount=row.get('amount'),
                            pct_chg=row.get('pct_chg'),
                            ma5=row.get('ma5'),
                            ma10=row.get('ma10'),
                            ma20=row.get('ma20'),
                            volume_ratio=row.get('volume_ratio'),
                            data_source=data_source,
                            adj_factor=adj_factor,
                            adj_anchor_date=adj_anchor_date,
                            adj_factor_source=adj_factor_source,
                            pre_close=pre_close,
                            adj_convention=adj_convention,
                        )
                        session.add(record)
                        saved_count += 1
                
                session.commit()
                logger.info(f"保存 {code} 数据成功，新增 {saved_count} 条")
                
            except Exception as e:
                session.rollback()
                logger.error(f"保存 {code} 数据失败: {e}")
                raise
        
        return saved_count
    
    def get_analysis_context(
        self, 
        code: str,
        target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析所需的上下文数据
        
        返回今日数据 + 昨日数据的对比信息
        
        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）
            
        Returns:
            包含今日数据、昨日对比等信息的字典
        """
        if target_date is None:
            target_date = date.today()
        # 注意：尽管入参提供了 target_date，但当前实现实际使用的是“最新两天数据”（get_latest_data），
        # 并不会按 target_date 精确取当日/前一交易日的上下文。
        # 因此若未来需要支持“按历史某天复盘/重算”的可解释性，这里需要调整。
        # 该行为目前保留（按需求不改逻辑）。
        
        # 获取最近2天数据
        recent_data = self.get_latest_data(code, days=2)
        
        if not recent_data:
            logger.warning(f"未找到 {code} 的数据")
            return None
        
        today_data = recent_data[0]
        yesterday_data = recent_data[1] if len(recent_data) > 1 else None
        
        context = {
            'code': code,
            'date': today_data.date.isoformat(),
            'today': today_data.to_dict(),
        }
        
        if yesterday_data:
            context['yesterday'] = yesterday_data.to_dict()
            
            # 计算相比昨日的变化
            if yesterday_data.volume and yesterday_data.volume > 0:
                context['volume_change_ratio'] = round(
                    today_data.volume / yesterday_data.volume, 2
                )
            
            if yesterday_data.close and yesterday_data.close > 0:
                context['price_change_ratio'] = round(
                    (today_data.close - yesterday_data.close) / yesterday_data.close * 100, 2
                )
            
            # 均线形态判断
            context['ma_status'] = self._analyze_ma_status(today_data)
        
        return context
    
    def _analyze_ma_status(self, data: StockDaily) -> str:
        """
        分析均线形态
        
        判断条件：
        - 多头排列：close > ma5 > ma10 > ma20
        - 空头排列：close < ma5 < ma10 < ma20
        - 震荡整理：其他情况
        """
        # 注意：这里的均线形态判断基于“close/ma5/ma10/ma20”静态比较，
        # 未考虑均线拐点、斜率、或不同数据源复权口径差异。
        # 该行为目前保留（按需求不改逻辑）。
        close = data.close or 0
        ma5 = data.ma5 or 0
        ma10 = data.ma10 or 0
        ma20 = data.ma20 or 0
        
        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

    @staticmethod
    def _parse_published_date(value: Optional[str]) -> Optional[datetime]:
        """
        解析发布时间字符串（失败返回 None）
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        text = str(value).strip()
        if not text:
            return None

        # 优先尝试 ISO 格式
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

        return None

    @staticmethod
    def _safe_json_dumps(data: Any) -> str:
        """
        安全序列化为 JSON 字符串
        """
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(str(data), ensure_ascii=False)

    @staticmethod
    def _build_raw_result(result: Any) -> Dict[str, Any]:
        """
        生成完整分析结果字典
        """
        data = result.to_dict() if hasattr(result, "to_dict") else {}
        data.update({
            'data_sources': getattr(result, 'data_sources', ''),
            'raw_response': getattr(result, 'raw_response', None),
        })
        return data

    @staticmethod
    def _parse_sniper_value(value: Any) -> Optional[float]:
        """
        Parse a sniper point value from various formats to float.

        Handles: numeric types, plain number strings, Chinese price formats
        like "18.50元", range formats like "18.50-19.00", and text with
        embedded numbers while filtering out MA indicators.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else None

        text = str(value).replace(',', '').replace('，', '').strip()
        if not text or text == '-' or text == '—' or text == 'N/A':
            return None

        # 尝试直接解析纯数字字符串
        try:
            return float(text)
        except ValueError:
            pass

        # 优先截取 "：" 到 "元" 之间的价格，避免误提取 MA5/MA10 等技术指标数字
        colon_pos = max(text.rfind("："), text.rfind(":"))
        yuan_pos = text.find("元", colon_pos + 1 if colon_pos != -1 else 0)
        if yuan_pos != -1:
            segment_start = colon_pos + 1 if colon_pos != -1 else 0
            segment = text[segment_start:yuan_pos]
            
            # 使用 finditer 并过滤掉 MA 开头的数字
            matches = list(re.finditer(r"-?\d+(?:\.\d+)?", segment))
            valid_numbers = []
            for m in matches:
                # 检查前面是否是 "MA" (忽略大小写)
                start_idx = m.start()
                if start_idx >= 2:
                    prefix = segment[start_idx-2:start_idx].upper()
                    if prefix == "MA":
                        continue
                valid_numbers.append(m.group())
            
            if valid_numbers:
                try:
                    return abs(float(valid_numbers[-1]))
                except ValueError:
                    pass

        # 兜底：无"元"字时，先截去第一个括号后的内容，避免误提取括号内技术指标数字
        # 例如 "1.52-1.53 (回踩MA5/10附近)" → 仅在 "1.52-1.53 " 中搜索
        paren_pos = len(text)
        for paren_char in ('(', '（'):
            pos = text.find(paren_char)
            if pos != -1:
                paren_pos = min(paren_pos, pos)
        search_text = text[:paren_pos].strip() or text  # 括号前为空时降级用全文

        valid_numbers = []
        for m in re.finditer(r"\d+(?:\.\d+)?", search_text):
            start_idx = m.start()
            if start_idx >= 2 and search_text[start_idx-2:start_idx].upper() == "MA":
                continue
            valid_numbers.append(m.group())
        if valid_numbers:
            try:
                return float(valid_numbers[-1])
            except ValueError:
                pass
        return None

    def _extract_sniper_points(self, result: Any) -> Dict[str, Optional[float]]:
        """
        Extract sniper point values from an AnalysisResult.

        Tries multiple extraction paths to handle different dashboard structures:
        1. result.get_sniper_points() (standard path)
        2. Direct dashboard dict traversal with various nesting levels
        3. Fallback from raw_result dict if available
        """
        raw_points = {}

        # Path 1: standard method
        if hasattr(result, "get_sniper_points"):
            raw_points = result.get_sniper_points() or {}

        # Path 2: direct dashboard traversal when standard path yields empty values
        if not any(raw_points.get(k) for k in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")):
            dashboard = getattr(result, "dashboard", None)
            if isinstance(dashboard, dict):
                raw_points = self._find_sniper_in_dashboard(dashboard) or raw_points

        # Path 3: try raw_result for agent mode results
        if not any(raw_points.get(k) for k in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")):
            raw_response = getattr(result, "raw_response", None)
            if isinstance(raw_response, dict):
                raw_points = self._find_sniper_in_dashboard(raw_response) or raw_points

        return {
            "ideal_buy": self._parse_sniper_value(raw_points.get("ideal_buy")),
            "secondary_buy": self._parse_sniper_value(raw_points.get("secondary_buy")),
            "stop_loss": self._parse_sniper_value(raw_points.get("stop_loss")),
            "take_profit": self._parse_sniper_value(raw_points.get("take_profit")),
        }

    @staticmethod
    def _find_sniper_in_dashboard(d: dict) -> Optional[Dict[str, Any]]:
        """
        Recursively search for sniper_points in a dashboard dict.
        Handles various nesting: dashboard.battle_plan.sniper_points,
        dashboard.dashboard.battle_plan.sniper_points, etc.
        """
        if not isinstance(d, dict):
            return None

        # Direct: d has sniper_points keys at top level
        if "ideal_buy" in d:
            return d

        # d.sniper_points
        sp = d.get("sniper_points")
        if isinstance(sp, dict) and sp:
            return sp

        # d.battle_plan.sniper_points
        bp = d.get("battle_plan")
        if isinstance(bp, dict):
            sp = bp.get("sniper_points")
            if isinstance(sp, dict) and sp:
                return sp

        # d.dashboard.battle_plan.sniper_points (double-nested)
        inner = d.get("dashboard")
        if isinstance(inner, dict):
            bp = inner.get("battle_plan")
            if isinstance(bp, dict):
                sp = bp.get("sniper_points")
                if isinstance(sp, dict) and sp:
                    return sp

        return None

    @staticmethod
    def _build_fallback_url_key(
        code: str,
        title: str,
        source: str,
        published_date: Optional[datetime]
    ) -> str:
        """
        生成无 URL 时的去重键（确保稳定且较短）
        """
        date_str = published_date.isoformat() if published_date else ""
        raw_key = f"{code}|{title}|{source}|{date_str}"
        digest = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
        return f"no-url:{code}:{digest}"

    def save_conversation_message(self, session_id: str, role: str, content: str) -> None:
        """
        保存 Agent 对话消息
        """
        with self.session_scope() as session:
            msg = ConversationMessage(
                session_id=session_id,
                role=role,
                content=content
            )
            session.add(msg)

    def get_conversation_history(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        获取 Agent 对话历史
        """
        with self.session_scope() as session:
            stmt = select(ConversationMessage).filter(
                ConversationMessage.session_id == session_id
            ).order_by(ConversationMessage.created_at.desc()).limit(limit)
            messages = session.execute(stmt).scalars().all()

            # 倒序返回，保证时间顺序
            return [{"role": msg.role, "content": msg.content} for msg in reversed(messages)]

    def conversation_session_exists(self, session_id: str) -> bool:
        """Return True when at least one message exists for the given session."""
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage.id)
                .where(ConversationMessage.session_id == session_id)
                .limit(1)
            )
            return session.execute(stmt).scalar() is not None

    def get_chat_sessions(
        self,
        limit: int = 50,
        session_prefix: Optional[str] = None,
        extra_session_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取聊天会话列表（从 conversation_messages 聚合）

        Args:
            limit: Maximum number of sessions to return.
            session_prefix: If provided, only return sessions whose session_id
                starts with this prefix.  Used for per-user isolation (e.g.
                ``"telegram_12345"``).
            extra_session_ids: Optional exact session ids to include in
                addition to the scoped prefix.

        Returns:
            按最近活跃时间倒序的会话列表，每条包含 session_id, title, message_count, last_active
        """
        from sqlalchemy import func

        with self.session_scope() as session:
            normalized_prefix = None
            if session_prefix:
                normalized_prefix = session_prefix if session_prefix.endswith(":") else f"{session_prefix}:"
            exact_ids = [sid for sid in (extra_session_ids or []) if sid]

            # 聚合每个 session 的消息数和最后活跃时间
            base = (
                select(
                    ConversationMessage.session_id,
                    func.count(ConversationMessage.id).label("message_count"),
                    func.min(ConversationMessage.created_at).label("created_at"),
                    func.max(ConversationMessage.created_at).label("last_active"),
                )
            )
            conditions = []
            if normalized_prefix:
                conditions.append(ConversationMessage.session_id.startswith(normalized_prefix))
            if exact_ids:
                conditions.append(ConversationMessage.session_id.in_(exact_ids))
            if conditions:
                base = base.where(or_(*conditions))
            stmt = (
                base
                .group_by(ConversationMessage.session_id)
                .order_by(desc(func.max(ConversationMessage.created_at)))
                .limit(limit)
            )
            rows = session.execute(stmt).all()

            results = []
            for row in rows:
                sid = row.session_id
                # 取该会话第一条 user 消息作为标题
                first_user_msg = session.execute(
                    select(ConversationMessage.content)
                    .where(
                        and_(
                            ConversationMessage.session_id == sid,
                            ConversationMessage.role == "user",
                        )
                    )
                    .order_by(ConversationMessage.created_at)
                    .limit(1)
                ).scalar()
                title = (first_user_msg or "新对话")[:60]

                results.append({
                    "session_id": sid,
                    "title": title,
                    "message_count": row.message_count,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "last_active": row.last_active.isoformat() if row.last_active else None,
                })
            return results

    def get_conversation_messages(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取单个会话的完整消息列表（用于前端恢复历史）
        """
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at)
                .limit(limit)
            )
            messages = session.execute(stmt).scalars().all()
            return [
                {
                    "id": str(msg.id),
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
                for msg in messages
            ]

    def delete_conversation_session(self, session_id: str) -> int:
        """
        删除指定会话的所有消息

        Returns:
            删除的消息数
        """
        with self.session_scope() as session:
            result = session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.session_id == session_id
                )
            )
            return result.rowcount

    # ------------------------------------------------------------------
    # LLM usage tracking
    # ------------------------------------------------------------------

    def record_llm_usage(
        self,
        call_type: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        stock_code: Optional[str] = None,
    ) -> None:
        """Append one LLM call record to llm_usage."""
        row = LLMUsage(
            call_type=call_type,
            model=model or "unknown",
            stock_code=stock_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        with self.session_scope() as session:
            session.add(row)

    def get_llm_usage_summary(
        self,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Dict[str, Any]:
        """Return aggregated token usage between from_dt and to_dt.

        Returns a dict with keys:
          total_calls, total_tokens,
          by_call_type: list of {call_type, calls, total_tokens},
          by_model:     list of {model, calls, total_tokens}
        """
        with self.session_scope() as session:
            base_filter = and_(
                LLMUsage.called_at >= from_dt,
                LLMUsage.called_at <= to_dt,
            )

            # Overall totals
            totals = session.execute(
                select(
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                ).where(base_filter)
            ).one()

            # Breakdown by call_type
            by_type_rows = session.execute(
                select(
                    LLMUsage.call_type,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                )
                .where(base_filter)
                .group_by(LLMUsage.call_type)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

            # Breakdown by model
            by_model_rows = session.execute(
                select(
                    LLMUsage.model,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                )
                .where(base_filter)
                .group_by(LLMUsage.model)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

        return {
            "total_calls": totals.calls,
            "total_tokens": totals.tokens,
            "by_call_type": [
                {"call_type": r.call_type, "calls": r.calls, "total_tokens": r.tokens}
                for r in by_type_rows
            ],
            "by_model": [
                {"model": r.model, "calls": r.calls, "total_tokens": r.tokens}
                for r in by_model_rows
            ],
        }


# 便捷函数
def get_db() -> DatabaseManager:
    """获取数据库管理器实例的快捷方式"""
    return DatabaseManager.get_instance()


def persist_llm_usage(
    usage: Dict[str, Any],
    model: str,
    call_type: str,
    stock_code: Optional[str] = None,
) -> None:
    """Fire-and-forget: write one LLM call record to llm_usage. Never raises."""
    try:
        db = DatabaseManager.get_instance()
        db.record_llm_usage(
            call_type=call_type,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0,
            completion_tokens=usage.get("completion_tokens", 0) or 0,
            total_tokens=usage.get("total_tokens", 0) or 0,
            stock_code=stock_code,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("[LLM usage] failed to persist usage record: %s", exc)


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    db = get_db()
    
    print("=== 数据库测试 ===")
    print(f"数据库初始化成功")
    
    # 测试检查今日数据
    has_data = db.has_today_data('600519')
    print(f"茅台今日是否有数据: {has_data}")
    
    # 测试保存数据
    test_df = pd.DataFrame({
        'date': [date.today()],
        'open': [1800.0],
        'high': [1850.0],
        'low': [1780.0],
        'close': [1820.0],
        'volume': [10000000],
        'amount': [18200000000],
        'pct_chg': [1.5],
        'ma5': [1810.0],
        'ma10': [1800.0],
        'ma20': [1790.0],
        'volume_ratio': [1.2],
    })
    
    saved = db.save_daily_data(test_df, '600519', 'TestSource')
    print(f"保存测试数据: {saved} 条")
    
    # 测试获取上下文
    context = db.get_analysis_context('600519')
    print(f"分析上下文: {context}")
