"""ID 生成：UUIDv7 + 业务前缀。

`candidate_id` / `audit_id` 等业务 ID 全局唯一且不复用；修正通过新版本 + supersedes_id 表达。
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """RFC 9562 UUIDv7（48 位毫秒时间戳 + 随机位），可按时间排序。"""
    ts_ms = int(time.time() * 1000)
    rand = os.urandom(10)
    # 48b timestamp | 4b version(7) | 12b rand_a | 2b variant(10) | 62b rand_b
    msb = (ts_ms & 0xFFFFFFFFFFFF) << 16
    msb |= 0x7000
    msb |= int.from_bytes(rand[0:2], "big") & 0x0FFF
    lsb = 0b10 << 62
    lsb |= int.from_bytes(rand[2:10], "big") & 0x3FFFFFFFFFFFFFFF
    return uuid.UUID(int=(msb << 64) | lsb)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid7().hex}"


# 常用前缀
def new_company_id() -> str:
    return new_id("co")


def new_report_id() -> str:
    return new_id("rp")


def new_evidence_id() -> str:
    return new_id("ev")


def new_candidate_id() -> str:
    return new_id("ca")


def new_task_id() -> str:
    return new_id("tk")


def new_model_call_id() -> str:
    return new_id("mc")


def new_audit_id() -> str:
    return new_id("au")


def new_batch_id() -> str:
    return new_id("bt")
