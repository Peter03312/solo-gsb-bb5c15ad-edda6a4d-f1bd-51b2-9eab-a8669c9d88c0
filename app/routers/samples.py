"""样本与发布路由：登记样本、录入/更正读数、发布与查询合规报告。"""
from __future__ import annotations

from fastapi import APIRouter, Response

from .. import crud
from ..schemas import PublishIn, ReadingUpsert, SampleCreate

router = APIRouter(tags=["samples"])


@router.post("/samples", status_code=201)
def register_sample(body: SampleCreate):
    return crud.register_sample(body)


@router.get("/samples/{sample_id}")
def get_sample(sample_id: str):
    return crud.get_sample(sample_id)


@router.put("/samples/{sample_id}/readings/{indicator_code}")
def put_reading(sample_id: str, indicator_code: str, body: ReadingUpsert, response: Response):
    # 路径参数与请求体一致；录入返回 200，更正语义由 corrected 字段表达
    if body.indicator_code != indicator_code:
        from ..errors import UnprocessableError

        raise UnprocessableError(
            "indicator_code in path and body must match", code="indicator_mismatch"
        )
    status, payload, _replayed = crud.put_reading(sample_id, body)
    response.status_code = status
    return payload


@router.post("/samples/{sample_id}/publish", status_code=201)
def publish(sample_id: str, body: PublishIn):
    return crud.publish_report(sample_id, body.expected_version)


@router.get("/samples/{sample_id}/reports")
def list_reports(sample_id: str):
    return {"reports": crud.list_reports(sample_id)}


@router.get("/samples/{sample_id}/reports/{report_id}")
def get_report(sample_id: str, report_id: int):
    return crud.get_report(sample_id, report_id)
