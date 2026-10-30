from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

INTENTS = ['competitor_complaint', 'problem_description', 'tool_request', 'discussion_only', 'self_promotion', 'unrelated']
STATUSES = ['new', 'replied', 'responded', 'converted', 'ignored']


class Score(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    score: int = Field(ge=1, le=5)
    intent_type: Literal['competitor_complaint', 'problem_description', 'tool_request', 'discussion_only', 'self_promotion', 'unrelated']
    competitor_mentioned: str | None
    pain_point: str
    urgency: Literal['low', 'medium', 'high']
    reason: str
    reply_angle: str
