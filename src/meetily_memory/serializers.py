from meetily_memory.domain import (
    Meeting,
    MeetingRef,
    MeetingSearchResult,
    SearchHit,
    SearchResults,
    SourceExcerpt,
)

JsonObject = dict[str, object]


def meeting_ref_payload(ref: MeetingRef) -> JsonObject:
    return {"source_uuid": ref.source_uuid, "external_id": ref.external_id}


def meeting_payload(meeting: Meeting) -> JsonObject:
    return {
        "local_id": meeting.id,
        "ref": meeting_ref_payload(meeting.ref),
        "title": meeting.title,
        "started_at": meeting.started_at,
        "ended_at": meeting.ended_at,
        "created_at": meeting.created_at,
        "updated_at": meeting.updated_at,
        "language": meeting.language,
        "summary_text": meeting.summary_text,
        "chunk_count": meeting.chunk_count,
    }


def source_excerpt_payload(excerpt: SourceExcerpt) -> JsonObject:
    return {
        "meeting_ref": meeting_ref_payload(excerpt.meeting_ref),
        "chunk_external_id": excerpt.chunk_external_id,
        "kind": excerpt.kind,
        "ordinal": excerpt.ordinal,
        "text": excerpt.text,
        "speaker": excerpt.speaker,
        "starts_at_seconds": excerpt.starts_at_seconds,
        "ends_at_seconds": excerpt.ends_at_seconds,
        "timestamp_label": excerpt.timestamp_label,
    }


def search_hit_payload(hit: SearchHit) -> JsonObject:
    return {
        "id": hit.id,
        "meeting": meeting_payload(hit.meeting),
        "excerpt": source_excerpt_payload(hit.excerpt),
        "is_context": hit.is_context,
    }


def meeting_search_result_payload(result: MeetingSearchResult) -> JsonObject:
    return {
        "meeting_local_id": result.meeting_id,
        "meeting": meeting_payload(result.meeting),
        "rank": result.rank,
        "match_sources": [source.value for source in result.match_sources],
        "evidence": [search_hit_payload(hit) for hit in result.evidence],
        "matched_tags": list(result.matched_tags),
    }


def search_results_payload(results: SearchResults) -> JsonObject:
    return {
        "query": results.query,
        "context": results.context,
        "results": [meeting_search_result_payload(result) for result in results.results],
    }
