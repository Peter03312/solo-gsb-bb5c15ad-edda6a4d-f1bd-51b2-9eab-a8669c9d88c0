"""HTTP API: 分区/必测指标/限值区间维护, 样本登记, 读数录入与更正, 合规发布."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .db import connect, init_db, norm_ts, utcnow

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "./data/water.db")


# ---------------------------------------------------------------- 请求模型

class ZoneCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class LimitRuleCreate(BaseModel):
    effective_from: datetime
    effective_to: datetime
    max_value: float = Field(ge=0)


class SampleCreate(BaseModel):
    zone: str = Field(min_length=1, max_length=128)
    sampled_at: datetime


class ReadingEntry(BaseModel):
    indicator: str = Field(min_length=1, max_length=128)
    value: float = Field(ge=0)  # 非负读数
    expected_version: int = Field(ge=0)


class ReadingCorrection(BaseModel):
    value: float = Field(ge=0)  # 非负读数
    expected_version: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------- 工具

def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE 写事务: 立即取写锁, 异常回滚, 不留部分写入."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def err(status: int, code: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, **extra})


def get_sample_or_404(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if row is None:
        raise err(404, "sample_not_found", sample_id=sample_id)
    return row


def check_version(sample: sqlite3.Row, expected_version: int) -> None:
    if sample["version"] != expected_version:
        raise err(409, "version_conflict",
                  expected_version=expected_version,
                  current_version=sample["version"])


def report_payload(conn: sqlite3.Connection, report: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM report_items WHERE report_id = ? ORDER BY indicator",
        (report["id"],),
    ).fetchall()
    return {
        "report_id": report["id"],
        "sample_id": report["sample_id"],
        "sample_version": report["sample_version"],
        "report_version": report["report_version"],
        "zone": report["zone"],
        "sampled_at": report["sampled_at"],
        "conclusion": report["conclusion"],
        "created_at": report["created_at"],
        "items": [
            {
                "indicator": it["indicator"],
                "value": it["value"],
                "exceeded": bool(it["exceeded"]),
                "limit": {
                    "rule_id": it["limit_rule_id"],
                    "max_value": it["max_value"],
                    "effective_from": it["effective_from"],
                    "effective_to": it["effective_to"],
                },
            }
            for it in items
        ],
    }


# ---------------------------------------------------------------- 应用工厂

def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or DEFAULT_DB_PATH
    init_db(db_path)
    app = FastAPI(title="自来水厂取样合规复核服务", version="1.0.0")
    app.state.db_path = db_path

    # ------------------------------------------------------------ 健康检查
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------ 分区
    @app.post("/zones", status_code=201)
    def create_zone(body: ZoneCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
        try:
            conn.execute("INSERT INTO zones(name, created_at) VALUES (?, ?)",
                         (body.name, utcnow()))
        except sqlite3.IntegrityError:
            raise err(409, "zone_exists", zone=body.name)
        return {"name": body.name, "created_at": utcnow()}

    @app.get("/zones")
    def list_zones(conn: sqlite3.Connection = Depends(get_db)) -> dict:
        rows = conn.execute("SELECT name FROM zones ORDER BY name").fetchall()
        return {"zones": [r["name"] for r in rows]}

    # ------------------------------------------------------------ 必测指标
    @app.put("/zones/{zone}/indicators/{indicator}")
    def add_required_indicator(zone: str, indicator: str,
                               conn: sqlite3.Connection = Depends(get_db)) -> dict:
        if conn.execute("SELECT 1 FROM zones WHERE name = ?", (zone,)).fetchone() is None:
            raise err(404, "zone_not_found", zone=zone)
        conn.execute(
            "INSERT OR IGNORE INTO required_indicators(zone, indicator) VALUES (?, ?)",
            (zone, indicator))
        return {"zone": zone, "indicator": indicator, "required": True}

    @app.delete("/zones/{zone}/indicators/{indicator}")
    def remove_required_indicator(zone: str, indicator: str,
                                  conn: sqlite3.Connection = Depends(get_db)) -> dict:
        cur = conn.execute(
            "DELETE FROM required_indicators WHERE zone = ? AND indicator = ?",
            (zone, indicator))
        if cur.rowcount == 0:
            raise err(404, "required_indicator_not_found", zone=zone, indicator=indicator)
        return {"zone": zone, "indicator": indicator, "required": False}

    @app.get("/zones/{zone}/indicators")
    def list_required_indicators(zone: str,
                                 conn: sqlite3.Connection = Depends(get_db)) -> dict:
        if conn.execute("SELECT 1 FROM zones WHERE name = ?", (zone,)).fetchone() is None:
            raise err(404, "zone_not_found", zone=zone)
        rows = conn.execute(
            "SELECT indicator FROM required_indicators WHERE zone = ? ORDER BY indicator",
            (zone,)).fetchall()
        return {"zone": zone, "required_indicators": [r["indicator"] for r in rows]}

    # ------------------------------------------------------------ 限值区间
    @app.post("/zones/{zone}/indicators/{indicator}/limits", status_code=201)
    def create_limit_rule(zone: str, indicator: str, body: LimitRuleCreate,
                          conn: sqlite3.Connection = Depends(get_db)) -> dict:
        f, t = norm_ts(body.effective_from), norm_ts(body.effective_to)
        if not f < t:
            raise err(422, "invalid_interval",
                      detail="生效区间无效: 需满足 effective_from < effective_to (左闭右开)",
                      effective_from=f, effective_to=t)
        if conn.execute("SELECT 1 FROM zones WHERE name = ?", (zone,)).fetchone() is None:
            raise err(404, "zone_not_found", zone=zone)
        with write_tx(conn):
            # 左闭右开区间重叠判定: existing.from < new.to AND existing.to > new.from
            clash = conn.execute(
                """SELECT id, effective_from, effective_to FROM limit_rules
                   WHERE zone = ? AND indicator = ?
                     AND effective_from < ? AND effective_to > ?
                   LIMIT 1""",
                (zone, indicator, t, f)).fetchone()
            if clash is not None:
                raise err(409, "interval_overlap",
                          detail="与已有生效区间重叠, 拒绝创建",
                          conflict_rule_id=clash["id"],
                          conflict_interval=[clash["effective_from"], clash["effective_to"]])
            cur = conn.execute(
                """INSERT INTO limit_rules(zone, indicator, effective_from, effective_to,
                                           max_value, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (zone, indicator, f, t, body.max_value, utcnow()))
            rule_id = cur.lastrowid
        return {"rule_id": rule_id, "zone": zone, "indicator": indicator,
                "effective_from": f, "effective_to": t, "max_value": body.max_value}

    @app.get("/zones/{zone}/indicators/{indicator}/limits")
    def list_limit_rules(zone: str, indicator: str,
                         conn: sqlite3.Connection = Depends(get_db)) -> dict:
        rows = conn.execute(
            """SELECT id, effective_from, effective_to, max_value FROM limit_rules
               WHERE zone = ? AND indicator = ?
               ORDER BY effective_from""",
            (zone, indicator)).fetchall()
        return {"zone": zone, "indicator": indicator, "limits": [
            {"rule_id": r["id"], "effective_from": r["effective_from"],
             "effective_to": r["effective_to"], "max_value": r["max_value"]}
            for r in rows]}

    # ------------------------------------------------------------ 样本
    @app.post("/samples", status_code=201)
    def create_sample(body: SampleCreate,
                      conn: sqlite3.Connection = Depends(get_db)) -> dict:
        if conn.execute("SELECT 1 FROM zones WHERE name = ?", (body.zone,)).fetchone() is None:
            raise err(404, "zone_not_found", zone=body.zone)
        sampled_at = norm_ts(body.sampled_at)
        cur = conn.execute(
            "INSERT INTO samples(zone, sampled_at, version, created_at) VALUES (?, ?, 0, ?)",
            (body.zone, sampled_at, utcnow()))
        return {"sample_id": cur.lastrowid, "zone": body.zone,
                "sampled_at": sampled_at, "version": 0}

    @app.get("/samples/{sample_id}")
    def get_sample(sample_id: int,
                   conn: sqlite3.Connection = Depends(get_db)) -> dict:
        sample = get_sample_or_404(conn, sample_id)
        rows = conn.execute(
            "SELECT indicator, value, updated_at FROM readings "
            "WHERE sample_id = ? ORDER BY indicator", (sample_id,)).fetchall()
        return {"sample_id": sample["id"], "zone": sample["zone"],
                "sampled_at": sample["sampled_at"], "version": sample["version"],
                "readings": [{"indicator": r["indicator"], "value": r["value"],
                              "updated_at": r["updated_at"]} for r in rows]}

    # ------------------------------------------------------------ 读数录入
    @app.post("/samples/{sample_id}/readings", status_code=201)
    def enter_reading(sample_id: int, body: ReadingEntry,
                      conn: sqlite3.Connection = Depends(get_db)) -> dict:
        with write_tx(conn):
            sample = get_sample_or_404(conn, sample_id)
            check_version(sample, body.expected_version)
            exists = conn.execute(
                "SELECT 1 FROM readings WHERE sample_id = ? AND indicator = ?",
                (sample_id, body.indicator)).fetchone()
            if exists:
                raise err(409, "reading_exists",
                          detail="该指标已有读数, 请使用更正接口 PUT /samples/{id}/readings/{indicator}",
                          indicator=body.indicator)
            new_version = sample["version"] + 1
            conn.execute(
                "INSERT INTO readings(sample_id, indicator, value, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sample_id, body.indicator, body.value, utcnow()))
            conn.execute("UPDATE samples SET version = ? WHERE id = ?",
                         (new_version, sample_id))
        return {"sample_id": sample_id, "indicator": body.indicator,
                "value": body.value, "version": new_version}

    # ------------------------------------------------------------ 读数更正(幂等)
    @app.put("/samples/{sample_id}/readings/{indicator}")
    def correct_reading(sample_id: int, indicator: str, body: ReadingCorrection,
                        request: Request, conn: sqlite3.Connection = Depends(get_db)
                        ) -> Response:
        payload = {"sample_id": sample_id, "indicator": indicator,
                   "value": body.value, "expected_version": body.expected_version}
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with write_tx(conn):
            prior = conn.execute(
                "SELECT payload_hash, response_json FROM correction_requests "
                "WHERE request_id = ?", (body.request_id,)).fetchone()
            if prior is not None:
                if prior["payload_hash"] != payload_hash:
                    raise err(409, "request_id_conflict",
                              detail="请求号已被不同内容的更正请求占用",
                              request_id=body.request_id)
                # 重放: 返回原结果, 不产生任何写操作
                return Response(content=prior["response_json"],
                                media_type="application/json",
                                headers={"X-Idempotent-Replay": "true"})
            sample = get_sample_or_404(conn, sample_id)
            check_version(sample, body.expected_version)
            exists = conn.execute(
                "SELECT 1 FROM readings WHERE sample_id = ? AND indicator = ?",
                (sample_id, indicator)).fetchone()
            if not exists:
                raise err(404, "reading_not_found",
                          detail="该指标尚无读数, 请使用录入接口 POST /samples/{id}/readings",
                          indicator=indicator)
            new_version = sample["version"] + 1
            conn.execute(
                "UPDATE readings SET value = ?, updated_at = ? "
                "WHERE sample_id = ? AND indicator = ?",
                (body.value, utcnow(), sample_id, indicator))
            conn.execute("UPDATE samples SET version = ? WHERE id = ?",
                         (new_version, sample_id))
            result = {"sample_id": sample_id, "indicator": indicator,
                      "value": body.value, "version": new_version,
                      "request_id": body.request_id}
            conn.execute(
                """INSERT INTO correction_requests(request_id, sample_id, indicator,
                                                   payload_hash, response_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.request_id, sample_id, indicator, payload_hash,
                 json.dumps(result), utcnow()))
        return Response(content=json.dumps(result), media_type="application/json")

    # ------------------------------------------------------------ 发布
    @app.post("/samples/{sample_id}/publish", status_code=201)
    def publish(sample_id: int,
                conn: sqlite3.Connection = Depends(get_db)) -> dict:
        with write_tx(conn):
            sample = get_sample_or_404(conn, sample_id)
            zone, sampled_at = sample["zone"], sample["sampled_at"]

            required = [r["indicator"] for r in conn.execute(
                "SELECT indicator FROM required_indicators WHERE zone = ? ORDER BY indicator",
                (zone,)).fetchall()]
            readings = {r["indicator"]: r["value"] for r in conn.execute(
                "SELECT indicator, value FROM readings WHERE sample_id = ?",
                (sample_id,)).fetchall()}

            missing = [i for i in required if i not in readings]
            if missing:
                raise err(422, "missing_readings",
                          detail="必测指标读数不齐全, 拒绝发布", missing=missing)

            # 采样时刻每项必测指标必须恰有一条生效限值
            items: list[dict[str, Any]] = []
            limit_problems = []
            for indicator in required:
                rules = conn.execute(
                    """SELECT * FROM limit_rules
                       WHERE zone = ? AND indicator = ?
                         AND effective_from <= ? AND effective_to > ?""",
                    (zone, indicator, sampled_at, sampled_at)).fetchall()
                if len(rules) != 1:
                    limit_problems.append({"indicator": indicator,
                                           "effective_limit_count": len(rules)})
                    continue
                rule = rules[0]
                items.append({"indicator": indicator, "value": readings[indicator],
                              "rule": rule,
                              "exceeded": readings[indicator] > rule["max_value"]})
            if limit_problems:
                raise err(422, "limit_not_exactly_one",
                          detail="采样时刻须对每项必测指标恰有一条生效限值",
                          problems=limit_problems)

            # 同一读数版本不得重复发布(并发下由写锁 + 唯一约束双保险)
            dup = conn.execute(
                "SELECT id FROM reports WHERE sample_id = ? AND sample_version = ?",
                (sample_id, sample["version"])).fetchone()
            if dup is not None:
                raise err(409, "already_published",
                          detail="当前读数版本已发布, 更正读数后可再次发布",
                          sample_version=sample["version"], report_id=dup["id"])

            conclusion = "non_compliant" if any(i["exceeded"] for i in items) else "compliant"
            row = conn.execute(
                "SELECT COALESCE(MAX(report_version), 0) + 1 AS v FROM reports "
                "WHERE sample_id = ?", (sample_id,)).fetchone()
            report_version = row["v"]
            try:
                cur = conn.execute(
                    """INSERT INTO reports(sample_id, sample_version, report_version, zone,
                                           sampled_at, conclusion, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (sample_id, sample["version"], report_version, zone,
                     sampled_at, conclusion, utcnow()))
            except sqlite3.IntegrityError:
                raise err(409, "already_published",
                          detail="当前读数版本已发布", sample_version=sample["version"])
            report_id = cur.lastrowid
            for it in items:
                conn.execute(
                    """INSERT INTO report_items(report_id, indicator, value, limit_rule_id,
                                                max_value, effective_from, effective_to,
                                                exceeded)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (report_id, it["indicator"], it["value"], it["rule"]["id"],
                     it["rule"]["max_value"], it["rule"]["effective_from"],
                     it["rule"]["effective_to"], 1 if it["exceeded"] else 0))
            report = conn.execute("SELECT * FROM reports WHERE id = ?",
                                  (report_id,)).fetchone()
            return report_payload(conn, report)

    # ------------------------------------------------------------ 报告查询
    @app.get("/samples/{sample_id}/reports")
    def list_reports(sample_id: int,
                     conn: sqlite3.Connection = Depends(get_db)) -> dict:
        get_sample_or_404(conn, sample_id)
        rows = conn.execute(
            """SELECT id, report_version, sample_version, conclusion, created_at
               FROM reports WHERE sample_id = ? ORDER BY report_version""",
            (sample_id,)).fetchall()
        return {"sample_id": sample_id, "reports": [
            {"report_id": r["id"], "report_version": r["report_version"],
             "sample_version": r["sample_version"], "conclusion": r["conclusion"],
             "created_at": r["created_at"]} for r in rows]}

    @app.get("/samples/{sample_id}/reports/{report_version}")
    def get_report(sample_id: int, report_version: int,
                   conn: sqlite3.Connection = Depends(get_db)) -> dict:
        get_sample_or_404(conn, sample_id)
        report = conn.execute(
            "SELECT * FROM reports WHERE sample_id = ? AND report_version = ?",
            (sample_id, report_version)).fetchone()
        if report is None:
            raise err(404, "report_not_found", sample_id=sample_id,
                      report_version=report_version)
        return report_payload(conn, report)

    return app


app = create_app()
