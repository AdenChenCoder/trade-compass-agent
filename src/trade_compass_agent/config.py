from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
PACKAGED_CONFIG_PATH = PACKAGE_ROOT / "default.yaml"
SOURCE_ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
PACKAGED_ENV_EXAMPLE_PATH = PACKAGE_ROOT / "env.example"


def is_source_checkout() -> bool:
    """Return whether the package is running from a source checkout."""
    return (PROJECT_ROOT / "pyproject.toml").is_file() and SOURCE_CONFIG_PATH.is_file()


def user_home_path() -> Path:
    """Stable per-user root for installed-app state."""
    configured = os.getenv("TRADE_COMPASS_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".trade-compass"


def user_config_path() -> Path:
    return user_home_path() / "config.yaml"


def user_env_path() -> Path:
    return user_home_path() / ".env"


def active_env_path() -> Path:
    explicit = os.getenv("TRADE_COMPASS_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return PROJECT_ROOT / ".env" if is_source_checkout() else user_env_path()


def runtime_root() -> Path:
    """Writable runtime root while preserving source-checkout compatibility."""
    return PROJECT_ROOT if is_source_checkout() else user_home_path()


DEFAULT_CONFIG_PATH = SOURCE_CONFIG_PATH if is_source_checkout() else PACKAGED_CONFIG_PATH


def resolve_schema_path(relative_path: str) -> Path:
    """Resolve a schema path in source checkout or installed wheel layout."""
    relative = relative_path.removeprefix("schemas/")
    packaged = PACKAGE_ROOT / "schemas" / relative
    if packaged.is_file():
        return packaged
    return PROJECT_ROOT / "schemas" / relative


def load_project_dotenv() -> None:
    """Load the active app `.env` without overriding exported variables."""
    load_dotenv(active_env_path(), override=False)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    memory_dir: Path = Path("memory_vault")
    profile: str = "local"
    allow_external_llm_memory: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("TRADE_COMPASS_DATA_DIR", "data")),
            memory_dir=Path(os.getenv("TRADE_COMPASS_MEMORY_DIR", "memory_vault")),
            profile=os.getenv("TRADE_COMPASS_PROFILE", "local"),
            allow_external_llm_memory=os.getenv("ALLOW_EXTERNAL_LLM_MEMORY", "false").lower()
            == "true",
        )



@dataclass(frozen=True)
class TradingCostConfig:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_bps: float = 5.0
    min_lot_size: int = 100
    price_limit_pct: float = 0.10
    st_price_limit_pct: float = 0.05


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    premarket_time: str = "08:50"
    morning_plan_time: str = "09:15"
    close_time: str = "15:10"
    eod_review_time: str = "15:30"
    postmarket_time: str = "16:30"
    weekly_day: str = "sat"
    weekly_time: str = "10:00"


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool = True
    channels: list[str] = field(default_factory=lambda: ["web_log"])
    macos_enabled: bool = False
    max_records: int = 500


@dataclass(frozen=True)
class AgentConfig:
    require_llm: bool = True
    max_tool_rounds: int = 30
    learning_enabled: bool = False
    multimodal: bool = True
    llm_session_titles: bool = False


@dataclass(frozen=True)
class DebateConfig:
    max_debate_rounds: int = 2
    enable_research_phase: bool = True
    enable_risk_crossexam: bool = True
    analyst_max_tool_rounds: int = 6
    debater_max_tool_rounds: int = 4


@dataclass(frozen=True)
class LLMConfig:
    """LLM provider settings for the agent runtime."""

    provider: str = "disabled"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout: float = 90.0
    max_retries: int = 2
    vision_model: str = ""  # fallback model for vision tasks (e.g. gpt-4o-mini)
    vision_provider: str = ""  # provider for vision_model (defaults to main provider)
    vision_api_key_env: str = ""  # API key env for vision provider (defaults to main)


@dataclass(frozen=True)
class DataConfig:
    """Optional market data provider settings."""

    tushare_enabled: bool = False
    tushare_token_env: str = "TUSHARE_TOKEN"
    cninfo_enabled: bool = True


@dataclass(frozen=True)
class ChannelsConfig:
    """Bidirectional messaging channel configuration."""

    gateway_enabled: bool = False
    feishu_enabled: bool = False
    wecom_enabled: bool = False
    weixin_enabled: bool = False


@dataclass(frozen=True)
class MemoryGovernanceConfig:
    agent_add_confidence: float = 0.4
    min_inject_confidence: float = 0.5
    legacy_promotion_fallback: bool = False
    outcome_feedback_enabled: bool = True
    outcome_confidence_delta: float = -0.15
    outcome_min_confidence_delta: float = -0.05
    outcome_max_confidence_delta: float = -0.30
    outcome_advisor_enabled: bool = False
    outcome_advisor_max_candidates: int = 5
    disproof_pnl_delta: float = 5.0
    min_predicted_magnitude: float = 3.0
    alert_drop_threshold: float = 8.0
    outcome_return_delta: float = 5.0
    alert_signal_enabled: bool = False
    archive_after_disproofs: int = 2


@dataclass(frozen=True)
class MemoryPromotionConfig:
    default_confidence: float = 0.85
    grounding_in_judge: bool = True
    auto_supersede: bool = True


@dataclass(frozen=True)
class MemoryRecallConfig:
    index_knowledge_in_fts: bool = False
    sanitize_reflections: bool = True


@dataclass(frozen=True)
class MemoryCuratorConfig:
    enabled: bool = True
    stale_days: int = 90
    scan_conflicts: bool = True


@dataclass(frozen=True)
class MemoryConfig:
    governance: MemoryGovernanceConfig = field(default_factory=MemoryGovernanceConfig)
    promotion: MemoryPromotionConfig = field(default_factory=MemoryPromotionConfig)
    recall: MemoryRecallConfig = field(default_factory=MemoryRecallConfig)
    curator: MemoryCuratorConfig = field(default_factory=MemoryCuratorConfig)


@dataclass(frozen=True)
class CompressionConfig:
    enabled: bool = True
    chars_per_token: float = 1.5
    trim_threshold_pct: float = 0.60
    summary_threshold_pct: float = 0.80
    emergency_threshold_pct: float = 0.95
    protect_recent_count: int = 20
    protect_recent_tokens: int = 16000
    context_budget: int = 0  # 0 = auto-detect from model


@dataclass(frozen=True)
class RulesConfig:
    enabled: bool = True
    char_limit: int = 4000


@dataclass(frozen=True)
class Watchlists:
    stocks: list[str] = field(default_factory=lambda: ["600519", "300750", "000001"])
    etfs: list[str] = field(default_factory=lambda: ["510300", "512690", "159915"])
    mid_term: list[str] = field(default_factory=lambda: ["600519", "510300"])

    def premarket_symbols(self) -> list[str]:
        return _dedupe(self.stocks + self.etfs)


@dataclass(frozen=True)
class AppConfig:
    profile: str = "local"
    data_dir: Path = Path("data")
    memory_dir: Path = Path("memory_vault")
    data_provider: str = "auto"
    data: DataConfig = field(default_factory=DataConfig)
    trading_costs: TradingCostConfig = field(default_factory=TradingCostConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    watchlists: Watchlists = field(default_factory=Watchlists)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    debate: DebateConfig = field(default_factory=DebateConfig)
    channels: ChannelsConfig = field(default_factory=ChannelsConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context_compression: CompressionConfig = field(default_factory=CompressionConfig)
    allow_external_llm_memory: bool = False


def ensure_runtime_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.memory_dir.mkdir(parents=True, exist_ok=True)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


_config_cache: AppConfig | None = None
_config_cache_key: tuple | None = None


def load_app_config(path: Path | None = None) -> AppConfig:
    """Load and cache AppConfig. Cache is invalidated on file mtime change."""
    global _config_cache, _config_cache_key
    config_path = resolve_config_path(path)

    mtime = config_path.stat().st_mtime if config_path.exists() else 0
    env_key = (
        os.getenv("TRADE_COMPASS_DATA_DIR", ""),
        os.getenv("TRADE_COMPASS_MEMORY_DIR", ""),
        os.getenv("TRADE_COMPASS_DATA_PROVIDER", ""),
        str(user_home_path()),
    )
    cache_key = (str(config_path), mtime, env_key)
    if _config_cache is not None and _config_cache_key == cache_key:
        return _config_cache

    project_root = _config_root(config_path)
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    costs_raw = raw.get("trading_costs", {}) or {}
    scheduler_raw = raw.get("scheduler", {}) or {}
    notifications_raw = raw.get("notifications", {}) or {}
    watchlists_raw = raw.get("watchlists", {}) or {}
    agent_raw = raw.get("agent", {}) or {}
    llm_raw = raw.get("llm", {}) or {}
    debate_raw = raw.get("debate", {}) or {}
    data_raw = raw.get("data", {}) or {}
    privacy_raw = raw.get("privacy", {}) or {}
    channels_raw = raw.get("channels", {}) or {}
    rules_raw = raw.get("rules", {}) or {}
    compression_raw = raw.get("context_compression", {}) or {}
    memory_raw = raw.get("memory", {}) or {}
    gov_raw = memory_raw.get("governance", {}) or {}
    promo_raw = memory_raw.get("promotion", {}) or {}
    recall_raw = memory_raw.get("recall", {}) or {}
    curator_raw = memory_raw.get("curator", {}) or {}

    provider = os.getenv("TRADE_COMPASS_DATA_PROVIDER", raw.get("data_provider", "auto"))
    data_dir = _resolve_config_path(project_root, os.getenv("TRADE_COMPASS_DATA_DIR", raw.get("data_dir", "data")))
    memory_dir = _resolve_config_path(
        project_root, os.getenv("TRADE_COMPASS_MEMORY_DIR", raw.get("memory_dir", "memory_vault"))
    )

    result = AppConfig(
        profile=os.getenv("TRADE_COMPASS_PROFILE", raw.get("profile", "local")),
        data_dir=data_dir,
        memory_dir=memory_dir,
        data_provider=provider,
        data=DataConfig(
            tushare_enabled=bool(data_raw.get("tushare_enabled", False)),
            tushare_token_env=str(data_raw.get("tushare_token_env", "TUSHARE_TOKEN")),
            cninfo_enabled=bool(data_raw.get("cninfo_enabled", True)),
        ),
        trading_costs=TradingCostConfig(
            commission_rate=float(costs_raw.get("commission_rate", 0.0003)),
            min_commission=float(costs_raw.get("min_commission", 5.0)),
            stamp_duty_rate=float(costs_raw.get("stamp_duty_rate", 0.0005)),
            transfer_fee_rate=float(costs_raw.get("transfer_fee_rate", 0.00001)),
            slippage_bps=float(costs_raw.get("slippage_bps", 5.0)),
            min_lot_size=int(costs_raw.get("min_lot_size", 100)),
            price_limit_pct=float(costs_raw.get("price_limit_pct", 0.10)),
            st_price_limit_pct=float(costs_raw.get("st_price_limit_pct", 0.05)),
        ),
        scheduler=SchedulerConfig(
            enabled=bool(scheduler_raw.get("enabled", True)),
            timezone=str(scheduler_raw.get("timezone", "Asia/Shanghai")),
            premarket_time=str(scheduler_raw.get("premarket_time", "08:50")),
            morning_plan_time=str(scheduler_raw.get("morning_plan_time", "09:15")),
            close_time=str(scheduler_raw.get("close_time", "15:10")),
            eod_review_time=str(scheduler_raw.get("eod_review_time", "15:30")),
            postmarket_time=str(scheduler_raw.get("postmarket_time", "16:30")),
            weekly_day=str(scheduler_raw.get("weekly_day", "sat")),
            weekly_time=str(scheduler_raw.get("weekly_time", "10:00")),
        ),
        notifications=NotificationConfig(
            enabled=bool(notifications_raw.get("enabled", True)),
            channels=list(notifications_raw.get("channels", ["web_log"])),
            macos_enabled=bool(notifications_raw.get("macos_enabled", False)),
            max_records=int(notifications_raw.get("max_records", 500)),
        ),
        watchlists=Watchlists(
            stocks=_dedupe(list(watchlists_raw.get("stocks", ["600519", "300750", "000001"]))),
            etfs=_dedupe(list(watchlists_raw.get("etfs", ["510300", "512690", "159915"]))),
            mid_term=_dedupe(list(watchlists_raw.get("mid_term", ["600519", "510300"]))),
        ),
        agent=AgentConfig(
            require_llm=bool(agent_raw.get("require_llm", True)),
            max_tool_rounds=int(agent_raw.get("max_tool_rounds", 30)),
            learning_enabled=bool(agent_raw.get("learning_enabled", False)),
            multimodal=bool(agent_raw.get("multimodal", True)),
            llm_session_titles=bool(agent_raw.get("llm_session_titles", False)),
        ),
        llm=LLMConfig(
            provider=str(llm_raw.get("provider", "disabled")),
            model=str(llm_raw.get("model", "deepseek-chat")),
            api_key_env=str(llm_raw.get("api_key_env", "DEEPSEEK_API_KEY")),
            timeout=float(llm_raw.get("timeout", 90.0)),
            max_retries=int(llm_raw.get("max_retries", 2)),
            vision_model=str(llm_raw.get("vision_model", "")),
            vision_provider=str(llm_raw.get("vision_provider", "")),
            vision_api_key_env=str(llm_raw.get("vision_api_key_env", "")),
        ),
        debate=DebateConfig(
            max_debate_rounds=int(debate_raw.get("max_debate_rounds", 2)),
            enable_research_phase=bool(debate_raw.get("enable_research_phase", True)),
            enable_risk_crossexam=bool(debate_raw.get("enable_risk_crossexam", True)),
            analyst_max_tool_rounds=int(debate_raw.get("analyst_max_tool_rounds", 6)),
            debater_max_tool_rounds=int(debate_raw.get("debater_max_tool_rounds", 4)),
        ),
        channels=ChannelsConfig(
            gateway_enabled=bool(channels_raw.get("gateway_enabled", False)),
            feishu_enabled=bool(channels_raw.get("feishu_enabled", False)),
            wecom_enabled=bool(channels_raw.get("wecom_enabled", False)),
            weixin_enabled=bool(channels_raw.get("weixin_enabled", False)),
        ),
        rules=RulesConfig(
            enabled=bool(rules_raw.get("enabled", True)),
            char_limit=int(rules_raw.get("char_limit", 4000)),
        ),
        memory=MemoryConfig(
            governance=MemoryGovernanceConfig(
                agent_add_confidence=float(gov_raw.get("agent_add_confidence", 0.4)),
                min_inject_confidence=float(gov_raw.get("min_inject_confidence", 0.5)),
                legacy_promotion_fallback=bool(gov_raw.get("legacy_promotion_fallback", False)),
                outcome_feedback_enabled=bool(gov_raw.get("outcome_feedback_enabled", True)),
                outcome_confidence_delta=float(gov_raw.get("outcome_confidence_delta", -0.15)),
                outcome_min_confidence_delta=float(gov_raw.get("outcome_min_confidence_delta", -0.05)),
                outcome_max_confidence_delta=float(gov_raw.get("outcome_max_confidence_delta", -0.30)),
                outcome_advisor_enabled=bool(gov_raw.get("outcome_advisor_enabled", False)),
                outcome_advisor_max_candidates=int(gov_raw.get("outcome_advisor_max_candidates", 5)),
                disproof_pnl_delta=float(gov_raw.get("disproof_pnl_delta", 5.0)),
                min_predicted_magnitude=float(gov_raw.get("min_predicted_magnitude", 3.0)),
                alert_drop_threshold=float(gov_raw.get("alert_drop_threshold", 8.0)),
                outcome_return_delta=float(gov_raw.get("outcome_return_delta", 5.0)),
                alert_signal_enabled=bool(gov_raw.get("alert_signal_enabled", False)),
                archive_after_disproofs=int(gov_raw.get("archive_after_disproofs", 2)),
            ),
            promotion=MemoryPromotionConfig(
                default_confidence=float(promo_raw.get("default_confidence", 0.85)),
                grounding_in_judge=bool(promo_raw.get("grounding_in_judge", True)),
                auto_supersede=bool(promo_raw.get("auto_supersede", True)),
            ),
            recall=MemoryRecallConfig(
                index_knowledge_in_fts=bool(recall_raw.get("index_knowledge_in_fts", False)),
                sanitize_reflections=bool(recall_raw.get("sanitize_reflections", True)),
            ),
            curator=MemoryCuratorConfig(
                enabled=bool(curator_raw.get("enabled", True)),
                stale_days=int(curator_raw.get("stale_days", 90)),
                scan_conflicts=bool(curator_raw.get("scan_conflicts", True)),
            ),
        ),
        allow_external_llm_memory=(
            os.getenv(
                "ALLOW_EXTERNAL_LLM_MEMORY",
                str(privacy_raw.get("allow_external_llm_memory", "false")),
            ).lower()
            == "true"
        ),
        context_compression=CompressionConfig(
            enabled=bool(compression_raw.get("enabled", True)),
            chars_per_token=float(compression_raw.get("chars_per_token", 1.5)),
            trim_threshold_pct=float(compression_raw.get("trim_threshold_pct", 0.60)),
            summary_threshold_pct=float(compression_raw.get("summary_threshold_pct", 0.80)),
            emergency_threshold_pct=float(compression_raw.get("emergency_threshold_pct", 0.95)),
            protect_recent_count=int(compression_raw.get("protect_recent_count", 20)),
            protect_recent_tokens=int(compression_raw.get("protect_recent_tokens", 16000)),
            context_budget=int(compression_raw.get("context_budget", 0)),
        ),
    )
    _config_cache = result
    _config_cache_key = cache_key
    return result


def _resolve_config_path(project_root: Path, value: str | os.PathLike) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def settings_from_config(config: AppConfig) -> Settings:
    return Settings(
        data_dir=config.data_dir,
        memory_dir=config.memory_dir,
        profile=config.profile,
        allow_external_llm_memory=config.allow_external_llm_memory,
    )


def resolve_config_path(path: Path | None = None) -> Path:
    """Return the YAML config file path used by :func:`load_app_config`."""
    explicit = path
    if explicit is None:
        configured = os.getenv("TRADE_COMPASS_CONFIG", "").strip()
        explicit = Path(configured).expanduser() if configured else None
    if explicit is not None:
        return explicit if explicit.is_absolute() else (Path.cwd() / explicit).resolve()
    if is_source_checkout():
        return SOURCE_CONFIG_PATH
    user_config = user_config_path()
    if user_config.is_file():
        return user_config
    return PACKAGED_CONFIG_PATH


def _config_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    if resolved == SOURCE_CONFIG_PATH.resolve():
        return PROJECT_ROOT
    if resolved in {PACKAGED_CONFIG_PATH.resolve(), user_config_path().resolve()}:
        return user_home_path()
    if config_path.parent.name == "config":
        return config_path.parent.parent
    return config_path.parent


def initialize_user_files(*, force: bool = False) -> tuple[Path, Path]:
    """Create the installed-app config and env template without touching user data."""
    home = user_home_path()
    home.mkdir(parents=True, exist_ok=True)
    config_target = user_config_path()
    env_target = user_env_path()

    config_source = SOURCE_CONFIG_PATH if is_source_checkout() else PACKAGED_CONFIG_PATH
    if not config_source.is_file():
        raise FileNotFoundError(f"Default config is missing: {config_source}")
    if force or not config_target.exists():
        shutil.copyfile(config_source, config_target)

    env_source = SOURCE_ENV_EXAMPLE_PATH if is_source_checkout() else PACKAGED_ENV_EXAMPLE_PATH
    if env_source.is_file() and not env_target.exists():
        shutil.copyfile(env_source, env_target)
    if env_target.exists():
        env_target.chmod(0o600)
    config_target.chmod(0o600)

    return config_target, env_target


def initialize_runtime_files(*, force: bool = False) -> tuple[Path, Path]:
    """Initialize the active checkout or installed-app runtime files."""
    if not is_source_checkout():
        return initialize_user_files(force=force)

    env_target = PROJECT_ROOT / ".env"
    if SOURCE_ENV_EXAMPLE_PATH.is_file() and not env_target.exists():
        shutil.copyfile(SOURCE_ENV_EXAMPLE_PATH, env_target)
    if env_target.exists():
        env_target.chmod(0o600)
    return SOURCE_CONFIG_PATH, env_target


def invalidate_config_cache() -> None:
    global _config_cache, _config_cache_key
    _config_cache = None
    _config_cache_key = None


def update_scheduler_config(updates: dict[str, object]) -> AppConfig:
    """Merge scheduler fields into the YAML config and reload."""
    config_path = resolve_config_path()
    if config_path.resolve() == PACKAGED_CONFIG_PATH.resolve():
        config_path, _ = initialize_user_files()
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    scheduler_raw = dict(raw.get("scheduler", {}) or {})
    scheduler_raw.update(updates)
    raw["scheduler"] = scheduler_raw
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    invalidate_config_cache()
    return load_app_config(config_path)
