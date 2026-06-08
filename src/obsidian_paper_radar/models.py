from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class Paper:
    paper_id: str
    title: str
    authors: list[str]
    abstract: str
    published: str
    url: str
    pdf_url: str
    source: str
    categories: list[str] = field(default_factory=list)
    citation_count: int = 0
    influential_citation_count: int = 0
    rough_score: float = 0.0
    matched_interests: list[str] = field(default_factory=list)
    extra_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class RerankResult:
    paper_id: str
    recommend_score: float
    decision: str
    action: str
    matched_interests: list[str] = field(default_factory=list)
    project_relevance: str = ""
    summary_zh: str = ""
    note_title_zh: str = ""
    paper_type: str = ""
    reading_decision: str = ""
    reading_priority_reason: str = ""
    estimated_reading_time: str = ""
    core_problem: str = ""
    method_overview: str = ""
    key_innovation: str = ""
    implementation_details: str = ""
    evidence_strength: str = ""
    evidence_notes: str = ""
    implementation_feasibility: str = ""
    code_availability: str = ""
    dataset_availability: str = ""
    reproducibility_signals: list[str] = field(default_factory=list)
    core_modules: list[dict[str, str]] = field(default_factory=list)
    transferable_modules: list[dict[str, str]] = field(default_factory=list)
    technical_terms: list[dict[str, str]] = field(default_factory=list)
    results: str = ""
    future_improvements: list[str] = field(default_factory=list)
    project_inspiration: str = ""
    reading_questions: list[str] = field(default_factory=list)
    skip_risks: list[str] = field(default_factory=list)
    image_guidance: dict[str, object] = field(default_factory=dict)
    daily_one_sentence_zh: str = ""
    daily_core_contribution_zh: str = ""
    daily_core_points: list[str] = field(default_factory=list)
    daily_key_results: str = ""
    why_read: str = ""
    risks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    fallback_used: bool = False


@dataclass
class RunSummary:
    candidate_count: int
    llm_count: int
    recommended_count: int
    detailed_note_count: int
    image_success: int
    image_failed: int
    daily_note_path: str
    log_path: str
    run_date: date
    report: dict[str, Any] = field(default_factory=dict)


def paper_to_prompt_dict(paper: Paper) -> dict[str, Any]:
    return {
        "id": paper.paper_id,
        "title": paper.title,
        "authors": paper.authors[:8],
        "abstract": paper.abstract,
        "published": paper.published,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "source": paper.source,
        "categories": paper.categories,
        "rough_score": paper.rough_score,
        "matched_interests": paper.matched_interests[:8],
        "citation_count": paper.citation_count,
        "influential_citation_count": paper.influential_citation_count,
        "extra_context": paper.extra_context,
    }
