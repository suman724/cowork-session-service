"""Fixtures for DynamoDB service tests (LocalStack or DynamoDB Local)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aioboto3
import pytest

from session_service.repositories.dynamo import DynamoSessionRepository

DYNAMODB_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")


@pytest.fixture
async def dynamo_table() -> AsyncIterator[Any]:
    """Create a temporary DynamoDB table for service tests."""
    table_name = f"test-sessions-{uuid.uuid4().hex[:8]}"
    session = aioboto3.Session()
    async with session.resource(
        "dynamodb",
        endpoint_url=DYNAMODB_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ) as dynamodb:
        table = await dynamodb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "sessionId", "AttributeType": "S"},
                {"AttributeName": "tenantId", "AttributeType": "S"},
                {"AttributeName": "createdAt", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "tenantId-userId-index",
                    "KeySchema": [
                        {"AttributeName": "tenantId", "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        await table.meta.client.get_waiter("table_exists").wait(TableName=table_name)
        yield table
        await table.delete()


@pytest.fixture
def session_repo(dynamo_table: Any) -> DynamoSessionRepository:
    return DynamoSessionRepository(dynamo_table)
