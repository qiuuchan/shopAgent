# -*- coding: utf-8 -*-
"""
common.core.config —— 统一配置加载模块
======================================
本文件用途：为「拼多多自动回复」系统的各服务（backend / websocket /
scheduler）提供统一的配置加载入口。所有配置一律优先从环境变量（或 .env 文件）
读取，未提供时回退到「合理默认值」，保证在缺省环境下也能启动。

关键约束（开发规范 21 + 需求 25.4）：
- 禁止在业务逻辑中写死 `localhost` 等具体地址；所有地址、连接信息均经环境
  变量管理。代码逻辑是「先读环境变量，缺省才回退默认」，而非硬编码业务地址。
- 默认值采用「Docker 服务名 / 占位符」（如 mysql、redis、websocket），既能在
  docker-compose 多容器编排下直接连通，又避免把单机 `localhost` 固化进代码。
- 时间统一使用北京时间 Asia/Shanghai（开发规范 17、需求 24.8）。

实现说明：
- 基于 pydantic-settings 的 BaseSettings，字段名与环境变量名大小写不敏感地
  对应（例如字段 mysql_host 对应环境变量 MYSQL_HOST）。
- 通过 get_settings() 以 lru_cache 提供单例访问；测试可调用 reload_settings()
  清空缓存以使新的环境变量生效。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录下的 .env 绝对路径。
# 本文件位于 <project_root>/common/core/config.py，故根目录为 parents[2]。
# 使用绝对路径而非相对 ".env"，可避免「从子目录（如 backend/）启动时按当前
# 工作目录解析而漏加载根目录 .env」的问题（规范 21：配置经环境变量统一管理）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ROOT_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """系统统一配置类。

    所有字段均「环境变量优先、缺省回退默认值」。默认值刻意采用 Docker 服务名或
    占位符，避免在代码中写死 localhost（开发规范 21、需求 25.4）。
    """

    # pydantic-settings 配置：
    # - 支持从 .env 文件加载（UTF-8 编码）；
    # - 环境变量名大小写不敏感；
    # - extra="ignore" 忽略未声明的多余变量，避免无关环境变量导致启动失败。
    model_config = SettingsConfigDict(
        env_file=str(_ROOT_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 运行环境与日志
    # ------------------------------------------------------------------
    # 运行环境标识（development / production 等），仅作区分用途
    environment: str = Field(default="development")
    # 日志级别（开发规范 38：禁止使用 debug，默认 INFO）
    log_level: str = Field(default="INFO")
    # 静态资源目录（backend 提供静态文件时使用）
    static_dir: str = Field(default="static")
    # 全链路时区，统一北京时间（开发规范 17、需求 24.8）
    timezone: str = Field(default="Asia/Shanghai")

    # ------------------------------------------------------------------
    # MySQL 配置（开发规范 8：使用 MySQL）
    # 默认 host 采用 Docker 服务名 "mysql"，而非写死 localhost
    # ------------------------------------------------------------------
    mysql_host: str = Field(default="mysql")
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(default="root")
    mysql_password: str = Field(default="root", repr=False)
    mysql_database: str = Field(default="pdd_auto_reply")
    # SQLAlchemy 同步驱动（参数化查询，禁止字符串拼接 SQL —— 开发规范 16）
    sync_driver: str = Field(default="mysql+pymysql")

    # ------------------------------------------------------------------
    # Redis 配置（缓存 / 可选）
    # 默认 host 采用 Docker 服务名 "redis"，而非写死 localhost
    # ------------------------------------------------------------------
    redis_host: str = Field(default="redis")
    redis_port: int = Field(default=6379)
    redis_password: str = Field(default="", repr=False)
    redis_db: int = Field(default=0)

    # ------------------------------------------------------------------
    # JWT 配置（需求 1：认证鉴权）
    # 注意：jwt_secret_key 默认仅为占位，生产应经环境变量或数据库统一托管，
    # 切勿直接使用默认值。
    # ------------------------------------------------------------------
    jwt_secret_key: str = Field(default="change-me-in-env", repr=False)
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=30)

    # ------------------------------------------------------------------
    # 敏感字段加密密钥（需求 3.6 / 8.6）
    # 用于对 Cookie 凭据、账号密码等敏感字段做可逆加密存储（区别于密码的不可逆
    # 哈希）。默认仅为占位，生产环境必须经环境变量 DATA_ENCRYPT_KEY 注入，
    # 切勿直接使用默认值。密钥经派生后用于对称加密（见 common.utils.crypto）。
    # ------------------------------------------------------------------
    data_encrypt_key: str = Field(default="change-me-data-encrypt-key", repr=False)

    # ------------------------------------------------------------------
    # 各服务监听端口（设计：backend 8089 / websocket 8090 / scheduler 8091）
    # ------------------------------------------------------------------
    backend_web_port: int = Field(default=8089)
    websocket_port: int = Field(default=8090)
    scheduler_port: int = Field(default=8091)

    # ------------------------------------------------------------------
    # 服务间 HTTP 调用地址（开发规范 21：禁止写死 localhost，经环境变量配置）
    # 默认使用 Docker 服务名拼接，docker-compose 下可直接连通
    # ------------------------------------------------------------------
    backend_web_service_url: str = Field(default="http://backend:8089")
    websocket_service_url: str = Field(default="http://websocket:8090")
    scheduler_service_url: str = Field(default="http://scheduler:8091")

    # ------------------------------------------------------------------
    # 服务间内部接口调用共享密钥（在线聊天实时推送等内部回调用，需求 14）
    # websocket 服务回调 backend 内部接口时携带本密钥校验来源，避免内部接口
    # 被外部直接调用。默认仅为占位，生产应经环境变量 INTERNAL_SERVICE_TOKEN 注入。
    # ------------------------------------------------------------------
    internal_service_token: str = Field(
        default="change-me-internal-service-token", repr=False
    )

    # ------------------------------------------------------------------
    # 代理配置（需求 21.14/21.15）
    # 开启代理时，代理 API 地址不得为空（由业务层校验并给出中文提示）
    # ------------------------------------------------------------------
    proxy_enabled: bool = Field(default=False)
    proxy_api_url: str = Field(default="")

    # ------------------------------------------------------------------
    # Playwright 浏览器与验证码并发配置（需求 4，运行于 websocket 服务）
    # browser_headless：是否无头模式；账号密码登录通常需非无头以便人工验证
    # max_captcha_concurrent：人工验证码处理的最大并发数
    # ------------------------------------------------------------------
    browser_headless: bool = Field(default=False)
    max_captcha_concurrent: int = Field(default=1)

    # ------------------------------------------------------------------
    # TikTok 店铺通道配置（需求 24.x，TIK-017）
    # 灰度总开关 tiktok_shop_enabled：默认关闭，false 时 websocket 侧拒绝 TikTok
    # 店铺的连接请求（创建通道即抛错 → 路由返回失败响应），防止误接入；
    # 其余参数为 TikTokChannel 监控 / 发送 / 登录链路的默认值（均可在 .env 覆盖）。
    # ------------------------------------------------------------------
    # 灰度总开关：是否允许建立 TikTok 店铺连接（默认 false——灰度期先关后开）
    tiktok_shop_enabled: bool = Field(default=False)
    # 监控循环轮询间隔（秒），默认 5.0（会话列表快照抓取周期）
    tiktok_poll_interval_seconds: float = Field(default=5.0)
    # 会话回复去抖静默阈值（秒），默认 45.0（卖家回复后窗口期内不回）
    tiktok_debounce_seconds: float = Field(default=45.0)
    # DOM 发送超时（秒），默认 15.0（消息流出现己方气泡视为成功）
    tiktok_send_timeout_seconds: float = Field(default=15.0)
    # 登录等待超时（毫秒），默认 120000（预留人工完成验证码时间，对齐 PDD 口径）
    tiktok_login_wait_timeout_ms: int = Field(default=120_000)
    # 最大并发浏览器实例数，默认 4（超限拒绝新连接并告警，防资源耗尽）
    tiktok_max_browser_instances: int = Field(default=4)

    # ------------------------------------------------------------------
    # 初始管理员账号（需求 1 / 2：首次启动自检时幂等创建超级管理员）
    # 仅当数据库中尚无任何用户时创建，避免影响已有数据（规范 14）。
    # 账号名与密码均可经环境变量配置，禁止在代码中写死生产凭据（规范 21）；
    # 生产环境务必经 .env 注入强密码，首次登录后应及时修改。
    # ------------------------------------------------------------------
    default_admin_username: str = Field(default="admin")
    default_admin_password: str = Field(default="admin123")

    # ==================================================================
    # 派生连接信息（只读属性）
    # ==================================================================
    @property
    def database_url(self) -> str:
        """组装 MySQL 同步连接 URL（密码做 URL 转义，防止特殊字符破坏 URL）。"""
        password = quote_plus(self.mysql_password)
        return (
            f"{self.sync_driver}://{self.mysql_user}:{password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )

    @property
    def redis_url(self) -> str:
        """组装 Redis 连接 URL；无密码时省略密码段。"""
        if self.redis_password:
            password = quote_plus(self.redis_password)
            return f"redis://:{password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """以单例方式返回配置实例（lru_cache 缓存，进程内仅构造一次）。

    各服务统一通过本函数获取配置，避免重复读取环境变量与多份配置实例。
    """
    return Settings()


def reload_settings() -> Settings:
    """清空配置缓存并重新加载（主要供测试在修改环境变量后重新读取使用）。"""
    get_settings.cache_clear()
    return get_settings()
