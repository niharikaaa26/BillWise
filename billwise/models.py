"""Pydantic models shared by the parser and the API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

MONEY_FIELDS = ("billed_amount", "allowed_amount", "insurer_paid", "patient_responsibility")
LINE_FIELDS = ("service_date", "code") + ("description",) + MONEY_FIELDS


class LineItem(BaseModel):
    service_date: date | None = Field(None, description="Date the service was provided")
    code: str | None = Field(None, description="CPT (5 digits) or HCPCS (letter + 4 digits) code")
    description: str = Field("", description="Service description as printed (whitespace-normalized)")
    billed_amount: Decimal | None = Field(None, description="Provider's billed charge")
    allowed_amount: Decimal | None = Field(None, description="Amount allowed by the plan")
    insurer_paid: Decimal | None = Field(None, description="Amount the insurer paid")
    patient_responsibility: Decimal | None = Field(None, description="Amount the patient owes")


class ParseResult(BaseModel):
    format: str = Field(..., description="Detected layout: pipe_table, csv, fixed_width, key_value or unknown")
    line_items: list[LineItem]
    possible_duplicates: list[tuple[int, int]] = Field(
        default_factory=list, description="Index pairs of line items with identical date, code and charge (description if no code)"
    )
    warnings: list[str] = Field(default_factory=list)
