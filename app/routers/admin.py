"""主数据管理路由：指标、分区、必测指标、限值生效区间。"""
from __future__ import annotations

from fastapi import APIRouter

from .. import crud
from ..schemas import IndicatorCreate, LimitCreate, MandatoryIndicatorIn, PartitionCreate

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/indicators", status_code=201)
def create_indicator(body: IndicatorCreate):
    return crud.create_indicator(body.code, body.name, body.unit)


@router.get("/indicators")
def list_indicators():
    return {"indicators": crud.list_indicators()}


@router.post("/partitions", status_code=201)
def create_partition(body: PartitionCreate):
    return crud.create_partition(body.code, body.name)


@router.get("/partitions")
def list_partitions():
    return {"partitions": crud.list_partitions()}


@router.get("/partitions/{partition_code}")
def get_partition(partition_code: str):
    return crud.get_partition_detail(partition_code)


@router.post("/partitions/{partition_code}/mandatory-indicators", status_code=201)
def add_mandatory(partition_code: str, body: MandatoryIndicatorIn):
    return crud.add_mandatory_indicator(partition_code, body.indicator_code)


@router.post("/partitions/{partition_code}/limits", status_code=201)
def create_limit(partition_code: str, body: LimitCreate):
    return crud.create_limit(partition_code, body)
