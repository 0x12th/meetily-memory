from meetily_memory.domain import (
    ContextBundle,
    GraphEdge,
    GraphNode,
    Meeting,
    MeetingChunk,
    MeetingSearchResult,
    MemoryEntity,
    MemoryStats,
    Person,
    PersonMemory,
    ProjectMemory,
    RankedExcerpt,
    SearchHit,
    SearchResults,
    SourceExcerpt,
    StructuredEntities,
    StructuredSignal,
    SummaryMemory,
    TaskStatusResult,
    TimelineMemory,
    Topic,
    TopicAliasResult,
    TopicGraph,
    TopicMemory,
)

JsonObject = dict[str, object]


def envelope(kind: str, data: JsonObject) -> JsonObject:
    return {"kind": kind, "data": data}


def meeting_payload(meeting: Meeting) -> JsonObject:
    return {
        "id": meeting.id,
        "external_id": meeting.external_id,
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
        "meeting_external_id": excerpt.meeting_external_id,
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
        "meeting_id": result.meeting_id,
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


def memory_entity_payload(entity: MemoryEntity) -> JsonObject:
    return {
        "kind": entity.kind,
        "content": entity.content,
        "source": source_excerpt_payload(entity.source),
        "evidence_id": entity.evidence_id,
        "extraction_method": entity.extraction_method,
        "authoritative": entity.authoritative,
    }


def context_bundle_payload(bundle: ContextBundle) -> JsonObject:
    return {
        "question": bundle.question,
        "evidence": [search_hit_payload(hit) for hit in bundle.evidence],
        "entities": [memory_entity_payload(entity) for entity in bundle.entities],
    }


def meeting_chunk_payload(chunk: MeetingChunk) -> JsonObject:
    return {
        "id": chunk.id,
        "external_id": chunk.external_id,
        "kind": chunk.kind,
        "ordinal": chunk.ordinal,
        "text": chunk.text,
        "speaker": chunk.speaker,
        "starts_at_seconds": chunk.starts_at_seconds,
        "ends_at_seconds": chunk.ends_at_seconds,
        "timestamp_label": chunk.timestamp_label,
    }


def memory_stats_payload(stats: MemoryStats) -> JsonObject:
    return {
        "meetings": stats.meetings,
        "chunks": stats.chunks,
        "sources": stats.sources,
        "decisions": stats.decisions,
        "action_items": stats.action_items,
        "risks": stats.risks,
        "open_questions": stats.open_questions,
        "knowledge_nodes": stats.knowledge_nodes,
        "knowledge_edges": stats.knowledge_edges,
    }


def summary_memory_payload(memory: SummaryMemory) -> JsonObject:
    return {
        "stats": memory_stats_payload(memory.stats),
        "latest_meeting": (
            meeting_payload(memory.latest_meeting) if memory.latest_meeting is not None else None
        ),
    }


def ranked_excerpt_payload(value: RankedExcerpt) -> JsonObject:
    meeting = value.meeting
    excerpt = value.excerpt
    return {
        "meeting_id": value.meeting_id,
        "meeting_external_id": meeting.external_id,
        "title": meeting.title,
        "created_at": meeting.created_at,
        "updated_at": meeting.updated_at,
        "language": meeting.language,
        "chunk_external_id": excerpt.chunk_external_id,
        "kind": excerpt.kind,
        "ordinal": excerpt.ordinal,
        "text": excerpt.text,
        "speaker": excerpt.speaker,
        "starts_at_seconds": excerpt.starts_at_seconds,
        "ends_at_seconds": excerpt.ends_at_seconds,
        "timestamp_label": excerpt.timestamp_label,
        "rank": value.rank,
    }


def structured_signal_payload(signal: StructuredSignal) -> JsonObject:
    return {
        "kind": signal.kind,
        "id": signal.id,
        "ordinal": signal.ordinal,
        "text": signal.text,
        "source": signal.extraction_method,
        "created_at": signal.created_at,
        "updated_at": signal.updated_at,
        "status": signal.status,
        "status_note": signal.status_note,
        "status_source": signal.status_source,
        "status_updated_at": signal.status_updated_at,
        "meeting_id": signal.meeting_id,
        "meeting_external_id": signal.meeting_external_id,
        "meeting_title": signal.meeting_title,
        "meeting_language": signal.meeting_language,
        "meeting_date": signal.meeting_date,
        "chunk_external_id": signal.chunk_external_id,
        "chunk_kind": signal.chunk_kind,
        "chunk_speaker": signal.chunk_speaker,
        "chunk_timestamp_label": signal.chunk_timestamp_label,
    }


def project_memory_payload(memory: ProjectMemory) -> JsonObject:
    return {
        "query": memory.query,
        "meetings": [ranked_excerpt_payload(value) for value in memory.meetings],
        "structured_signals": [
            structured_signal_payload(signal) for signal in memory.structured_signals
        ],
    }


def person_memory_payload(memory: PersonMemory) -> JsonObject:
    return {
        "person": memory.name,
        "meetings": [meeting_payload(meeting) for meeting in memory.meetings],
        "structured_signals": [
            structured_signal_payload(signal) for signal in memory.structured_signals
        ],
    }


def timeline_memory_payload(memory: TimelineMemory) -> JsonObject:
    return {
        "query": memory.query,
        "signals": [structured_signal_payload(signal) for signal in memory.signals],
    }


def topic_payload(topic: Topic) -> JsonObject:
    return {"id": topic.id, "title": topic.title, "aliases": list(topic.aliases)}


def person_payload(person: Person) -> JsonObject:
    return {"id": person.id, "display_name": person.display_name}


def topic_memory_payload(memory: TopicMemory) -> JsonObject:
    return {
        "topic": topic_payload(memory.topic),
        "language": memory.language,
        "query_terms": list(memory.query_terms),
        "meetings": [ranked_excerpt_payload(value) for value in memory.meetings],
        "evidence": [ranked_excerpt_payload(value) for value in memory.evidence],
        "structured_signals": [
            structured_signal_payload(signal) for signal in memory.structured_signals
        ],
        "related_people": [person_payload(person) for person in memory.related_people],
    }


def topic_alias_payload(result: TopicAliasResult) -> JsonObject:
    return {**topic_payload(result.topic), "added_aliases": list(result.added_aliases)}


def graph_node_payload(node: GraphNode) -> JsonObject:
    return {"id": node.id, "type": node.type, "title": node.title}


def graph_edge_payload(edge: GraphEdge) -> JsonObject:
    return {
        "id": edge.id,
        "from_node_id": edge.from_node_id,
        "relation": edge.relation,
        "to_node_id": edge.to_node_id,
        "confidence": edge.confidence,
        "source_meeting_id": edge.source_meeting_id,
        "source_chunk_id": edge.source_chunk_id,
        "extraction_method": edge.extraction_method,
        "created_at": edge.created_at,
    }


def topic_graph_payload(graph: TopicGraph) -> JsonObject:
    return {
        "topic": topic_payload(graph.topic),
        "nodes": [graph_node_payload(node) for node in graph.nodes],
        "edges": [graph_edge_payload(edge) for edge in graph.edges],
    }


def structured_entities_payload(result: StructuredEntities) -> JsonObject:
    return {
        "entity_kind": result.entity_kind,
        "status": result.status,
        "entities": [structured_signal_payload(entity) for entity in result.entities],
    }


def task_status_payload(result: TaskStatusResult) -> JsonObject:
    return {
        "id": result.id,
        "text": result.text,
        "status": result.status,
        "status_note": result.status_note,
        "status_source": result.status_source,
        "status_updated_at": result.status_updated_at,
    }
