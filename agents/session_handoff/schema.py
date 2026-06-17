"""Session_handoff handoff schema — Pydantic v2."""
from __future__ import annotations
from pydantic import BaseModel

class StepResult(BaseModel):
    step: str
    description: str
    pol: str  # "pass" | "fail" | "skip"
    evidence: str = ""

class SystemState(BaseModel):
    services_up: list[str] = []
    services_down: list[str] = []
    notes: str = ""

class Blocker(BaseModel):
    item: str
    requires: str
    priority: str = "medium"

class HandoffRecord(BaseModel):
    session_date: str
    milestone: str
    steps_completed: list[StepResult]
    current_state: SystemState
    blockers: list[Blocker]
    next_step: str
    files_modified: list[str]
    commit_hashes: dict[str, str]
    security_flags: list[str] = []
    known_debt: list[str] = []
