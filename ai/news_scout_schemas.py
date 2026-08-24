"""Structured Output des OpenAI-Web-News-Scouts."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

class NewsScoutCandidate(BaseModel):
    model_config=ConfigDict(extra="forbid")
    company_name:str=Field(min_length=1,max_length=255)
    symbol:str=Field(min_length=1,max_length=32)
    event_type:str=Field(min_length=1,max_length=40)
    headline:str=Field(min_length=1,max_length=500)
    summary:str=Field(min_length=1,max_length=1200)
    published_at:datetime
    source_name:str=Field(min_length=1,max_length=255)
    source_url:str=Field(min_length=1,max_length=2000)
    expected_direction:Literal["POSITIVE","NEGATIVE","NEUTRAL"]
    relevance_score:float=Field(ge=0,le=100)
    reason:str=Field(min_length=1,max_length=500)

class NewsScoutOutput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    candidates:list[NewsScoutCandidate]=Field(max_length=10)
