"""Stub-server bodies for the CLI tests, built from the gateway's own models.

An invented response body is how a CLI bug once shipped green: the stub
answered in a shape the real server never sends. So every body the new CLI
tests serve comes from here, and here builds them from the gateway's pydantic
response models (and, for `GET /v1/models`, which has no response model, by
calling the real route function).

The SDK test job installs no gateway dependencies, so the tests read a
checked-in snapshot (`cli_gateway_bodies.json`) and
`test_cli_gateway_parity.py` asserts the snapshot still equals what the
gateway's models produce wherever the gateway is importable. Regenerate with:

    (regenerated from the API's own response models, upstream of this repo)
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("cli_gateway_bodies.json")


def load() -> dict:
    return json.loads(SNAPSHOT.read_text())


def build() -> dict:
    """Every body, from the live gateway models. Raises ImportError without them."""
    # The gateway's settings refuse to load without these; the values are
    # never used (nothing here touches a database or signs anything).
    os.environ.setdefault("JWT_SECRET", secrets.token_hex(32))
    os.environ.setdefault("INTERNAL_SECRET", secrets.token_hex(16))
    os.environ.setdefault("ENCRYPTION_KEY", secrets.token_hex(32))
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused/unused")
    os.environ.setdefault("ENGINE_URL", "http://engine.test")

    import asyncio

    from gateway.models.chat_settings import SpaceChatSettings, SpaceChatSettingsBody
    from gateway.models.documents import DocumentItem, DocumentListResponse, FileUploadResponse
    from gateway.models.extraction_settings import ExtractionSettings, ExtractionSettingsBody
    from gateway.models.memories import (
        AddMemoryResponse,
        BatchRecordResponse,
        JobStatusResponse,
        MemoryItem,
        MemoryListResponse,
        SearchResponse,
        SearchResult,
    )
    from gateway.models.reason_settings import SpaceReasonSettings, SpaceReasonSettingsBody
    from gateway.models.spaces import SpaceResponse
    from gateway.routes.models import list_models

    def dump(model) -> dict:
        return model.model_dump(mode="json", by_alias=True)

    result = SearchResult(
        memory_id="mem-0001-aaaa",
        content="The team ships on Tuesdays.",
        relevance_score=1.1,
        memory_type="world",
        tags=["project:alpha"],
    )
    item = MemoryItem(
        id="mem-0001-aaaa",
        content="The team ships on Tuesdays.",
        text="The team ships on Tuesdays.",
        type="observation",
        state="active",
        tags=["project:alpha"],
    )
    models_doc = asyncio.run(list_models(org_id="org-test"))
    return {
        "record_async": dump(AddMemoryResponse(job_id="job-rec-1", status="processing")),
        "record_batch": dump(
            BatchRecordResponse(job_id="job-bat-1", job_ids=["job-bat-1", "job-bat-2"], status="processing", accepted=2)
        ),
        "job_completed": dump(
            JobStatusResponse(
                job_id="job-rec-1",
                status="completed",
                memory_count=2,
                memory_ids=["mem-a", "mem-b"],
                items_submitted=2,
            )
        ),
        "job_completed_empty": dump(
            JobStatusResponse(job_id="job-rec-1", status="completed", memory_count=0, memory_ids=[], items_submitted=1)
        ),
        "job_processing": dump(JobStatusResponse(job_id="job-rec-1", status="processing")),
        "job_failed": dump(JobStatusResponse(job_id="job-rec-1", status="failed", error="Extraction failed.")),
        "search_results": dump(SearchResponse(results=[result])),
        "search_block": dump(
            SearchResponse(results=[result], context="- The team ships on Tuesdays.\n- It deploys at noon.", token_estimate=12)
        ),
        "memory_list": dump(MemoryListResponse(items=[item], total=1, limit=50, offset=0)),
        "upload": dump(FileUploadResponse(job_ids=["job-up-1"])),
        "document_list": dump(
            DocumentListResponse(
                documents=[DocumentItem(document_id="doc-1", source="file", memory_count=3, tags=["q3"])],
                total=1,
                limit=100,
                offset=0,
            )
        ),
        "reason_settings": dump(
            SpaceReasonSettings(space_id="s", model="openai.gpt-oss-120b-1:0", depth="thorough")
        ),
        "chat_settings": dump(
            SpaceChatSettings(
                space_id="s", memory_limit=5, memory_token_budget=2000, auto_record=True, memory=True,
                chat_model="openai.gpt-oss-120b-1:0",
            )
        ),
        "extraction_settings": dump(
            ExtractionSettings(
                space_id="s",
                mode="verbose",
                guidance="Name the customer.",
                custom_prompt=None,
                labels=[{"key": "topic", "type": "text", "description": "", "optional": True, "tag": True, "values": []}],
                free_form_entities=True,
            )
        ),
        "space": dump(SpaceResponse(space_id="notes", name="notes", description="Team notes")),
        "llm_models": {"object": models_doc["object"], "data": models_doc["data"][:2]},
        # The request models' field names: what a PUT may carry, exactly.
        "reason_settings_fields": list(SpaceReasonSettingsBody.model_fields),
        "chat_settings_fields": list(SpaceChatSettingsBody.model_fields),
        "extraction_settings_fields": list(ExtractionSettingsBody.model_fields),
    }
