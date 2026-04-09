"""
Configuration - 邮件翻译器配置
"""

import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

# 数据存储目录
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

EMAILS_DIR = DATA_DIR / "emails"
EMAILS_DIR.mkdir(exist_ok=True)

OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# 默认配置
DEFAULT_CONFIG = {
    "imap": {
        "server": "imap.gmail.com",
        "port": 993,
        "use_ssl": True
    },
    "email_filters": {
        "max_emails": 50,
        "date_range_days": 30
    },
    "translation": {
        "openclaw_model": "kimi",
        "batch_size": 5
    },
    "output": {
        "format": "markdown",
        "include_original": True,
        "include_translated": True
    }
}

# OpenClaw CLI 配置
OPENCLAW_CONFIG = {
    "executable": "openclaw",
    "timeout": 300,
    "max_retries": 3
}

# 支持的邮箱服务商 IMAP 配置
EMAIL_PROVIDERS = {
    "gmail": {
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
    },
    "outlook": {
        "imap_server": "outlook.office365.com",
        "imap_port": 993,
    },
    "qq": {
        "imap_server": "imap.qq.com",
        "imap_port": 993,
    },
    "163": {
        "imap_server": "imap.163.com",
        "imap_port": 993,
    },
    "aliyun": {
        "imap_server": "imap.aliyun.com",
        "imap_port": 993,
    },
    "custom": {
        "imap_server": "",
        "imap_port": 993,
    }
}