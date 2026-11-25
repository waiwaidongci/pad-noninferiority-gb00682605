from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str = Field(min_length=1, max_length=1_000_000)


class PairTables(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    baseline_csv: str = Field(min_length=1, max_length=1_000_000)
    trial_csv: str = Field(min_length=1, max_length=1_000_000)


class PairPreview(BaseModel):
    baseline_csv: str = Field(min_length=1, max_length=1_000_000)
    trial_csv: str = Field(min_length=1, max_length=1_000_000)
    options: dict = Field(default_factory=dict)


class IntakeTable(BaseModel):
    csv: str = Field(min_length=1, max_length=1_000_000)
    batch_code: str | None = Field(default=None, max_length=80)
    name: str | None = Field(default=None, max_length=180)


class Run(BaseModel):
    dataset_id: int = Field(gt=0)
    options: dict = Field(default_factory=dict)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None
