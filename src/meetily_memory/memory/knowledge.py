import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import index_connection
from meetily_memory.memory.entities import ENTITY_NODE_TYPES, structured_entity_sort_key
from meetily_memory.memory.keys import (
    entity_stable_key,
    normalize_key,
    row_matches_terms,
    topic_stable_key,
    without_added_aliases,
)

SearchMeetings = Callable[[str, int], list[dict[str, Any]]]
ChunkRows = Callable[[sqlite3.Connection, int], list[sqlite3.Row]]
PeopleRows = Callable[[sqlite3.Connection, int], list[sqlite3.Row]]
StructuredRows = Callable[[sqlite3.Connection, int], list[dict[str, Any]]]
AllStructuredRows = Callable[[sqlite3.Connection, int], list[dict[str, Any]]]
NowProvider = Callable[[], str]


@dataclass(frozen=True)
class KnowledgeContext:
    index_path: Path
    search_meetings: SearchMeetings
    chunk_rows: ChunkRows
    meeting_people_rows: PeopleRows
    structured_entity_rows: StructuredRows
    all_structured_entity_details: AllStructuredRows
    now: NowProvider


class KnowledgeRepository:
    def __init__(self, context: KnowledgeContext) -> None:
        self.context = context

    def delete_meeting_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        node_ids = self.meeting_scoped_knowledge_node_ids(conn, meeting_id)
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            delete_edges_sql = f"""
                DELETE FROM knowledge_edges
                WHERE source_meeting_id = ?
                   OR from_node_id IN ({placeholders})
                   OR to_node_id IN ({placeholders})
                """
            conn.execute(delete_edges_sql, (meeting_id, *node_ids, *node_ids))
            conn.execute(
                f"DELETE FROM knowledge_nodes WHERE id IN ({placeholders})",
                tuple(node_ids),
            )
        else:
            conn.execute("DELETE FROM knowledge_edges WHERE source_meeting_id = ?", (meeting_id,))

    def delete_structured_knowledge(self, conn: sqlite3.Connection, meeting_id: int) -> None:
        node_ids = self.structured_knowledge_node_ids(conn, meeting_id)
        if not node_ids:
            return
        placeholders = ",".join("?" for _ in node_ids)
        delete_edges_sql = f"""
            DELETE FROM knowledge_edges
            WHERE from_node_id IN ({placeholders})
               OR to_node_id IN ({placeholders})
            """
        conn.execute(delete_edges_sql, (*node_ids, *node_ids))
        conn.execute(
            f"DELETE FROM knowledge_nodes WHERE id IN ({placeholders})",
            tuple(node_ids),
        )

    def meeting_scoped_knowledge_node_ids(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
    ) -> list[int]:
        keys = [("Meeting", f"meeting:{meeting_id}")]
        keys.extend(
            ("Chunk", f"chunk:{row['id']}") for row in self.context.chunk_rows(conn, meeting_id)
        )
        keys.extend(
            (
                ENTITY_NODE_TYPES[str(row["kind"])],
                entity_stable_key(row),
            )
            for row in self.context.structured_entity_rows(conn, meeting_id)
        )
        return self.knowledge_node_ids_for_keys(conn, keys)

    def structured_knowledge_node_ids(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
    ) -> list[int]:
        keys = [
            (
                ENTITY_NODE_TYPES[str(row["kind"])],
                entity_stable_key(row),
            )
            for row in self.context.structured_entity_rows(conn, meeting_id)
        ]
        return self.knowledge_node_ids_for_keys(conn, keys)

    def knowledge_node_ids_for_keys(
        self,
        conn: sqlite3.Connection,
        keys: list[tuple[str, str]],
    ) -> list[int]:
        node_ids: list[int] = []
        for node_type, stable_key in keys:
            row = conn.execute(
                """
                SELECT id
                FROM knowledge_nodes
                WHERE type = ? AND stable_key = ?
                """,
                (node_type, stable_key),
            ).fetchone()
            if row:
                node_ids.append(int(row["id"]))
        return node_ids

    def sync_meeting_knowledge(
        self,
        conn: sqlite3.Connection,
        meeting_id: int,
        now: str,
    ) -> None:
        meeting = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if meeting is None:
            return

        meeting_node_id = self.upsert_knowledge_node(
            conn,
            "Meeting",
            f"meeting:{meeting_id}",
            str(meeting["title"]),
            now,
        )

        chunk_nodes: dict[int, int] = {}
        for chunk in self.context.chunk_rows(conn, meeting_id):
            chunk_title = f"{meeting['title']} / {chunk['kind']} #{chunk['ordinal']}"
            chunk_node_id = self.upsert_knowledge_node(
                conn,
                "Chunk",
                f"chunk:{chunk['id']}",
                chunk_title,
                now,
            )
            chunk_nodes[int(chunk["id"])] = chunk_node_id
            self.upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "contains",
                chunk_node_id,
                1.0,
                source_meeting_id=meeting_id,
                source_chunk_id=int(chunk["id"]),
                extraction_method="scan",
                now=now,
            )

        person_nodes: list[tuple[int, str]] = []
        for person in self.context.meeting_people_rows(conn, meeting_id):
            person_node_id = self.upsert_knowledge_node(
                conn,
                "Person",
                f"person:{person['id']}",
                str(person["display_name"]),
                now,
            )
            person_nodes.append((person_node_id, str(person["display_name"])))
            self.upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "mentions",
                person_node_id,
                float(person["confidence"] or 0.8),
                source_meeting_id=meeting_id,
                source_chunk_id=None,
                extraction_method=str(person["source"] or "speaker"),
                now=now,
            )

        for entity in self.context.structured_entity_rows(conn, meeting_id):
            entity_kind = str(entity["kind"])
            entity_node_id = self.upsert_knowledge_node(
                conn,
                ENTITY_NODE_TYPES[entity_kind],
                entity_stable_key(entity),
                str(entity["text"]),
                now,
            )
            source_chunk_id = optional_int(entity["source_chunk_id"])
            self.upsert_knowledge_edge(
                conn,
                meeting_node_id,
                "contains",
                entity_node_id,
                float(entity["confidence"]),
                source_meeting_id=meeting_id,
                source_chunk_id=source_chunk_id,
                extraction_method=entity_kind,
                now=now,
            )
            if source_chunk_id is not None and source_chunk_id in chunk_nodes:
                self.upsert_knowledge_edge(
                    conn,
                    entity_node_id,
                    "originated_in",
                    chunk_nodes[source_chunk_id],
                    1.0,
                    source_meeting_id=meeting_id,
                    source_chunk_id=source_chunk_id,
                    extraction_method=entity_kind,
                    now=now,
                )
            if entity_kind == "action_items":
                entity_text = str(entity["text"]).casefold()
                for person_node_id, display_name in person_nodes:
                    if display_name.casefold() in entity_text:
                        self.upsert_knowledge_edge(
                            conn,
                            entity_node_id,
                            "assigned_to",
                            person_node_id,
                            0.7,
                            source_meeting_id=meeting_id,
                            source_chunk_id=source_chunk_id,
                            extraction_method="heuristic_assignee",
                            now=now,
                        )

    def upsert_knowledge_node(
        self,
        conn: sqlite3.Connection,
        node_type: str,
        stable_key: str,
        title: str,
        now: str,
        raw_metadata_json: str | None = None,
    ) -> int:
        normalized_title = normalize_key(title)
        cursor = conn.execute(
            """
            INSERT INTO knowledge_nodes (
              type, stable_key, title, normalized_title,
              created_at, updated_at, raw_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(type, stable_key) DO UPDATE SET
              title = excluded.title,
              normalized_title = excluded.normalized_title,
              updated_at = excluded.updated_at,
              raw_metadata_json = excluded.raw_metadata_json
            RETURNING id
            """,
            (node_type, stable_key, title, normalized_title, now, now, raw_metadata_json),
        )
        return int(cursor.fetchone()["id"])

    def upsert_knowledge_edge(
        self,
        conn: sqlite3.Connection,
        from_node_id: int,
        relation: str,
        to_node_id: int,
        confidence: float,
        *,
        source_meeting_id: int,
        source_chunk_id: int | None,
        extraction_method: str,
        now: str,
        raw_metadata_json: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO knowledge_edges (
              from_node_id, relation, to_node_id, confidence,
              source_meeting_id, source_chunk_id, extraction_method,
              created_at, raw_metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO UPDATE SET
              confidence = excluded.confidence,
              raw_metadata_json = excluded.raw_metadata_json
            """,
            (
                from_node_id,
                relation,
                to_node_id,
                confidence,
                source_meeting_id,
                source_chunk_id,
                extraction_method,
                now,
                raw_metadata_json,
            ),
        )

    def ensure_topic(
        self,
        title: str,
        *,
        aliases: Iterable[str] = (),
    ) -> dict[str, Any]:
        now = self.context.now()
        with index_connection(self.context.index_path) as conn:
            topic_id = self.resolve_topic_id(conn, title)
            if topic_id is None:
                topic_id = self.upsert_knowledge_node(
                    conn,
                    "Topic",
                    topic_stable_key(title),
                    title,
                    now,
                )
            added_aliases: list[str] = []
            for alias in aliases:
                normalized_alias = normalize_key(alias)
                if not normalized_alias:
                    continue
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO topic_aliases (
                      topic_node_id, alias, normalized_alias, created_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (topic_id, alias, normalized_alias, now),
                )
                if cursor.rowcount:
                    added_aliases.append(alias)
            self.link_topic_matches(conn, topic_id, now)
            conn.commit()
            payload = self.topic_payload(conn, topic_id)
            payload["added_aliases"] = added_aliases
            return payload

    def topic_memory(self, title: str, limit: int = 10) -> dict[str, Any]:
        topic = self.ensure_topic(title)
        topic_terms = [str(topic["title"]), *(str(alias) for alias in topic["aliases"])]
        evidence = search_topic_evidence(self.context.search_meetings, topic_terms, limit)
        with index_connection(self.context.index_path) as conn:
            rows = self.list_topic_entity_details(conn, int(topic["id"]), limit)
            related_people = self.list_topic_people(conn, int(topic["id"]), limit)
        language = dominant_language(
            [
                *(meeting.get("language") for meeting in evidence),
                *(row.get("meeting_language") for row in rows),
            ]
        )
        return {
            "topic": without_added_aliases(topic),
            "language": language,
            "query_terms": topic_terms,
            "meetings": evidence,
            "evidence": evidence,
            "structured_signals": rows,
            "related_people": related_people,
        }

    def list_topics(self, limit: int = 100) -> list[dict[str, Any]]:
        with index_connection(self.context.index_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM knowledge_nodes
                WHERE type = 'Topic'
                ORDER BY updated_at DESC, title ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self.topic_payload(conn, int(row["id"])) for row in rows]

    def graph_for_topic(self, title: str, limit: int = 50) -> dict[str, Any]:
        topic = self.ensure_topic(title)
        topic_id = int(topic["id"])
        with index_connection(self.context.index_path) as conn:
            linked_entity_rows = conn.execute(
                """
                SELECT from_node_id
                FROM knowledge_edges
                WHERE relation = 'belongs_to' AND to_node_id = ?
                LIMIT ?
                """,
                (topic_id, limit),
            ).fetchall()
            linked_entity_ids = [int(row["from_node_id"]) for row in linked_entity_rows]
            edge_rows = self.graph_edge_rows(conn, topic_id, linked_entity_ids, limit)
            node_ids = sorted(
                {topic_id}
                | {int(row["from_node_id"]) for row in edge_rows}
                | {int(row["to_node_id"]) for row in edge_rows}
            )
            nodes = self.knowledge_nodes_by_id(conn, node_ids)
            edges = rows_to_dicts(edge_rows)
        return {
            "topic": without_added_aliases(topic),
            "nodes": nodes,
            "edges": edges,
        }

    def resolve_topic_id(self, conn: sqlite3.Connection, title: str) -> int | None:
        normalized = normalize_key(title)
        alias_row = conn.execute(
            """
            SELECT topic_node_id
            FROM topic_aliases
            WHERE normalized_alias = ?
            """,
            (normalized,),
        ).fetchone()
        if alias_row:
            return int(alias_row["topic_node_id"])
        node_row = conn.execute(
            """
            SELECT id
            FROM knowledge_nodes
            WHERE type = 'Topic' AND stable_key = ?
            """,
            (topic_stable_key(title),),
        ).fetchone()
        return int(node_row["id"]) if node_row else None

    def topic_payload(self, conn: sqlite3.Connection, topic_id: int) -> dict[str, Any]:
        topic = conn.execute(
            "SELECT * FROM knowledge_nodes WHERE id = ?",
            (topic_id,),
        ).fetchone()
        aliases = [
            str(row["alias"])
            for row in conn.execute(
                """
                SELECT alias
                FROM topic_aliases
                WHERE topic_node_id = ?
                ORDER BY alias
                """,
                (topic_id,),
            ).fetchall()
        ]
        return {**dict(topic), "aliases": aliases}

    def link_topic_matches(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        now: str,
    ) -> None:
        topic = self.topic_payload(conn, topic_id)
        terms = [str(topic["title"]), *(str(alias) for alias in topic["aliases"])]
        for row in self.context.all_structured_entity_details(conn, 500):
            if not row_matches_terms(row, terms):
                continue
            kind = str(row["kind"])
            entity_node_id = self.upsert_knowledge_node(
                conn,
                ENTITY_NODE_TYPES[kind],
                entity_stable_key(row),
                str(row["text"]),
                now,
            )
            self.upsert_knowledge_edge(
                conn,
                entity_node_id,
                "belongs_to",
                topic_id,
                0.7,
                source_meeting_id=int(row["meeting_id"]),
                source_chunk_id=optional_int(row.get("source_chunk_id")),
                extraction_method="topic_query",
                now=now,
            )

    def list_topic_entity_details(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        linked_keys = {
            (str(row["type"]), str(row["stable_key"]))
            for row in conn.execute(
                """
                SELECT n.type, n.stable_key
                FROM knowledge_edges e
                JOIN knowledge_nodes n ON n.id = e.from_node_id
                WHERE e.relation = 'belongs_to' AND e.to_node_id = ?
                """,
                (topic_id,),
            ).fetchall()
        }
        rows = [
            row
            for row in self.context.all_structured_entity_details(conn, limit * 8)
            if (
                ENTITY_NODE_TYPES[str(row["kind"])],
                entity_stable_key(row),
            )
            in linked_keys
        ]
        rows.sort(key=structured_entity_sort_key, reverse=True)
        return rows[:limit]

    def list_topic_people(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT DISTINCT p.id, p.display_name, p.normalized_name
            FROM knowledge_edges topic_edge
            JOIN knowledge_edges meeting_edge
              ON meeting_edge.to_node_id = topic_edge.from_node_id
             AND meeting_edge.relation = 'contains'
            JOIN meeting_people mp ON mp.meeting_id = meeting_edge.source_meeting_id
            JOIN people p ON p.id = mp.person_id
            WHERE topic_edge.relation = 'belongs_to'
              AND topic_edge.to_node_id = ?
            ORDER BY p.display_name
            LIMIT ?
            """,
            (topic_id, limit),
        ).fetchall()
        return rows_to_dicts(rows)

    def graph_edge_rows(
        self,
        conn: sqlite3.Connection,
        topic_id: int,
        linked_entity_ids: list[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        edge_ids: set[int] = set()
        topic_edges = conn.execute(
            """
            SELECT *
            FROM knowledge_edges
            WHERE from_node_id = ? OR to_node_id = ?
            LIMIT ?
            """,
            (topic_id, topic_id, limit),
        ).fetchall()
        edge_ids.update(int(row["id"]) for row in topic_edges)
        if linked_entity_ids:
            placeholders = ",".join("?" for _ in linked_entity_ids)
            expanded_edges_sql = f"""
                SELECT *
                FROM knowledge_edges
                WHERE from_node_id IN ({placeholders})
                   OR to_node_id IN ({placeholders})
                LIMIT ?
                """
            expanded_edges = conn.execute(
                expanded_edges_sql,
                (*linked_entity_ids, *linked_entity_ids, limit),
            ).fetchall()
            edge_ids.update(int(row["id"]) for row in expanded_edges)
        if not edge_ids:
            return []
        placeholders = ",".join("?" for _ in edge_ids)
        edge_rows_sql = f"""
            SELECT *
            FROM knowledge_edges
            WHERE id IN ({placeholders})
            ORDER BY relation, id
            LIMIT ?
            """
        return list(
            conn.execute(
                edge_rows_sql,
                (*sorted(edge_ids), limit),
            ).fetchall()
        )

    def knowledge_nodes_by_id(
        self,
        conn: sqlite3.Connection,
        node_ids: list[int],
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        nodes_sql = f"""
            SELECT *
            FROM knowledge_nodes
            WHERE id IN ({placeholders})
            ORDER BY type, title
            """
        rows = conn.execute(nodes_sql, tuple(node_ids)).fetchall()
        return rows_to_dicts(rows)


def optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    message = f"Expected integer-compatible value, got {type(value).__name__}."
    raise TypeError(message)


def dominant_language(values: Iterable[object]) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        normalized = value.casefold().split("-", maxsplit=1)[0]
        if normalized not in {"en", "ru"}:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def search_topic_evidence(
    search_meetings: SearchMeetings,
    topic_terms: Iterable[str],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_chunk_ids: set[int] = set()
    for term in topic_terms:
        for row in search_meetings(term, limit):
            chunk_id = int(row["chunk_id"])
            if chunk_id in seen_chunk_ids:
                continue
            rows.append(row)
            seen_chunk_ids.add(chunk_id)
            if len(rows) >= limit:
                return rows
    return rows
