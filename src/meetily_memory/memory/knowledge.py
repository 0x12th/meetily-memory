import hashlib
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.rows import rows_to_dicts
from meetily_memory.db.schema import IndexConnectionFactory, sqlite_read_snapshot
from meetily_memory.memory.entities import ENTITY_NODE_TYPES, structured_entity_sort_key
from meetily_memory.memory.keys import (
    entity_stable_key,
    normalize_key,
    row_matches_terms,
    topic_stable_key,
)
from meetily_memory.user_state import StoredTopic, UserStateRepository

SearchMeetings = Callable[[sqlite3.Connection, str, int], list[dict[str, Any]]]
ChunkRows = Callable[[sqlite3.Connection, int], list[sqlite3.Row]]
PeopleRows = Callable[[sqlite3.Connection, int], list[sqlite3.Row]]
StructuredRows = Callable[[sqlite3.Connection, int], list[dict[str, Any]]]
AllStructuredRows = Callable[[sqlite3.Connection, int], list[dict[str, Any]]]
NowProvider = Callable[[], str]

TOPIC_MATCH_CANDIDATE_LIMIT = 500
VIRTUAL_TOPIC_ID_HIGH_BIT = 1 << 62
VIRTUAL_TOPIC_ID_VALUE_MASK = VIRTUAL_TOPIC_ID_HIGH_BIT - 1


@dataclass(frozen=True)
class KnowledgeContext:
    index_path: Path
    connection: IndexConnectionFactory
    search_meetings: SearchMeetings
    chunk_rows: ChunkRows
    meeting_people_rows: PeopleRows
    structured_entity_rows: StructuredRows
    all_structured_entity_details: AllStructuredRows
    user_state: UserStateRepository
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
        return self.add_topic_aliases(title, aliases)

    def add_topic_aliases(
        self,
        title: str,
        aliases: Iterable[str],
    ) -> dict[str, Any]:
        with self.context.connection(self.context.index_path) as conn, sqlite_read_snapshot(conn):
            topic, topic_id, has_definition = self.resolve_topic(conn, title)
            alias_values = list(aliases)
            added_aliases: tuple[str, ...] = ()
            state = self.context.user_state
            if alias_values:
                now = self.context.now()
                if not has_definition:
                    topic = StoredTopic(
                        stable_key=topic.stable_key,
                        title=topic.title,
                        normalized_title=topic.normalized_title,
                        created_at=now,
                        updated_at=now,
                        raw_metadata_json=topic.raw_metadata_json,
                    )
                state = self.writable_user_state()
                added_aliases = state.add_topic_aliases(topic, alias_values, now=now)
            payload = self.topic_definition_payload(topic, topic_id, state=state)
        payload["added_aliases"] = list(added_aliases)
        return payload

    def remove_topic_aliases(self, aliases: Iterable[str]) -> tuple[str, ...]:
        return self.writable_user_state().delete_topic_aliases(list(aliases))

    def writable_user_state(self) -> UserStateRepository:
        return self.context.user_state.open_existing_writer()

    def resolve_topic(
        self,
        conn: sqlite3.Connection,
        title: str,
    ) -> tuple[StoredTopic, int, bool]:
        state_topic = self.context.user_state.topic_for_query(title)
        stable_key = state_topic.stable_key if state_topic is not None else topic_stable_key(title)
        row = conn.execute(
            """
            SELECT *
            FROM knowledge_nodes
            WHERE type = 'Topic' AND stable_key = ?
            """,
            (stable_key,),
        ).fetchone()
        if state_topic is not None:
            topic = state_topic
        elif row is not None:
            topic = _stored_topic_from_row(row)
        else:
            topic = StoredTopic(
                stable_key=stable_key,
                title=title,
                normalized_title=normalize_key(title),
                created_at="",
                updated_at="",
                raw_metadata_json=None,
            )
        topic_id = (
            int(row["id"])
            if row is not None
            else _request_local_topic_ids((topic.stable_key,))[topic.stable_key]
        )
        return topic, topic_id, state_topic is not None or row is not None

    def topic_definition_payload(
        self,
        topic: StoredTopic,
        topic_id: int,
        *,
        state: UserStateRepository | None = None,
    ) -> dict[str, Any]:
        state = state or self.context.user_state
        aliases = [alias.alias for alias in state.list_topic_aliases(topic.stable_key)]
        return {
            "id": topic_id,
            "stable_key": topic.stable_key,
            "title": topic.title,
            "aliases": aliases,
        }

    def project_topic_aliases(self, *, connection: sqlite3.Connection | None = None) -> None:
        aliases = self.context.user_state.list_topic_aliases()
        connection_context = (
            self.context.connection(self.context.index_path)
            if connection is None
            else nullcontext(connection)
        )
        with connection_context as conn:
            if connection is None:
                conn.execute("BEGIN IMMEDIATE")
            for alias in aliases:
                conn.execute(
                    """
                    INSERT INTO knowledge_nodes (
                      type, stable_key, title, normalized_title,
                      created_at, updated_at, raw_metadata_json
                    ) VALUES ('Topic', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(type, stable_key) DO UPDATE SET
                      title = excluded.title,
                      normalized_title = excluded.normalized_title,
                      created_at = excluded.created_at,
                      updated_at = excluded.updated_at,
                      raw_metadata_json = excluded.raw_metadata_json
                    """,
                    (
                        alias.topic_stable_key,
                        alias.topic_title,
                        alias.topic_normalized_title,
                        alias.topic_created_at,
                        alias.topic_updated_at,
                        alias.topic_raw_metadata_json,
                    ),
                )
                topic = conn.execute(
                    """
                    SELECT id
                    FROM knowledge_nodes
                    WHERE type = 'Topic' AND stable_key = ?
                    """,
                    (alias.topic_stable_key,),
                ).fetchone()
                if topic is None:
                    message = "State-owned topic could not be projected into the index."
                    raise RuntimeError(message)
                conn.execute(
                    """
                    INSERT INTO topic_aliases (
                      topic_node_id, alias, normalized_alias, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(normalized_alias) DO UPDATE SET
                      topic_node_id = excluded.topic_node_id,
                      alias = excluded.alias,
                      created_at = excluded.created_at
                    """,
                    (
                        int(topic["id"]),
                        alias.alias,
                        alias.normalized_alias,
                        alias.alias_created_at,
                    ),
                )
            normalized_aliases = [alias.normalized_alias for alias in aliases]
            if normalized_aliases:
                placeholders = ",".join("?" for _ in normalized_aliases)
                conn.execute(
                    f"DELETE FROM topic_aliases WHERE normalized_alias NOT IN ({placeholders})",
                    normalized_aliases,
                )
            else:
                conn.execute("DELETE FROM topic_aliases")
            if connection is None:
                conn.commit()

    def topic_memory(self, title: str, limit: int = 10) -> dict[str, Any]:
        with self.context.connection(self.context.index_path) as conn, sqlite_read_snapshot(conn):
            topic, topic_id, _has_definition = self.resolve_topic(conn, title)
            topic_payload = self.topic_definition_payload(topic, topic_id)
            topic_terms = [
                str(topic_payload["title"]),
                *(str(alias) for alias in topic_payload["aliases"]),
            ]
            evidence = search_topic_evidence(
                self.context.search_meetings,
                conn,
                topic_terms,
                limit,
            )
            rows = self.topic_entity_details_for_terms(conn, topic_terms, limit)
            related_people = self.list_topic_people_for_rows(conn, rows, limit)
            language = dominant_language(
                [
                    *(meeting.get("language") for meeting in evidence),
                    *(row.get("meeting_language") for row in rows),
                ]
            )
        return {
            "topic": topic_payload,
            "language": language,
            "query_terms": topic_terms,
            "meetings": evidence,
            "evidence": evidence,
            "structured_signals": rows,
            "related_people": related_people,
        }

    def list_topics(self, limit: int = 100) -> list[dict[str, Any]]:
        state_topics = {topic.stable_key: topic for topic in self.context.user_state.list_topics()}
        state_aliases: dict[str, list[str]] = {}
        for alias in self.context.user_state.list_topic_aliases():
            state_aliases.setdefault(alias.topic_stable_key, []).append(alias.alias)
        with self.context.connection(self.context.index_path) as conn, sqlite_read_snapshot(conn):
            index_rows = conn.execute(
                """
                SELECT
                  n.*,
                  EXISTS (
                    SELECT 1
                    FROM knowledge_edges e
                    WHERE e.from_node_id = n.id OR e.to_node_id = n.id
                  ) AS has_knowledge_relationship
                FROM knowledge_nodes n
                WHERE n.type = 'Topic'
                """
            ).fetchall()

        eligible_index_rows = [
            row
            for row in index_rows
            if str(row["stable_key"]) in state_topics or bool(row["has_knowledge_relationship"])
        ]
        projected_keys = {str(row["stable_key"]) for row in eligible_index_rows}
        state_only_keys = tuple(
            stable_key for stable_key in state_topics if stable_key not in projected_keys
        )
        virtual_ids = _request_local_topic_ids(
            state_only_keys,
            occupied_ids=(int(row["id"]) for row in eligible_index_rows),
        )

        entries: list[tuple[str, str, dict[str, Any]]] = []
        for row in eligible_index_rows:
            stable_key = str(row["stable_key"])
            definition = state_topics.get(stable_key) or _stored_topic_from_row(row)
            entries.append(
                (
                    definition.updated_at,
                    definition.title,
                    {
                        "id": int(row["id"]),
                        "stable_key": definition.stable_key,
                        "title": definition.title,
                        "aliases": state_aliases.get(stable_key, []),
                    },
                )
            )
        for stable_key in state_only_keys:
            definition = state_topics[stable_key]
            entries.append(
                (
                    definition.updated_at,
                    definition.title,
                    {
                        "id": virtual_ids[stable_key],
                        "stable_key": definition.stable_key,
                        "title": definition.title,
                        "aliases": state_aliases.get(stable_key, []),
                    },
                )
            )
        entries.sort(key=lambda entry: entry[1].casefold())
        entries.sort(key=lambda entry: entry[0], reverse=True)
        return [entry[2] for entry in entries[:limit]]

    def graph_for_topic(self, title: str, limit: int = 50) -> dict[str, Any]:
        with self.context.connection(self.context.index_path) as conn, sqlite_read_snapshot(conn):
            topic, topic_id, _has_definition = self.resolve_topic(conn, title)
            topic_payload = self.topic_definition_payload(topic, topic_id)
            topic_terms = [
                str(topic_payload["title"]),
                *(str(alias) for alias in topic_payload["aliases"]),
            ]
            rows = self.topic_entity_details_for_terms(conn, topic_terms, max(limit, 0))
            linked_entities = self.topic_entity_nodes(conn, rows)
            computed_edges = self.computed_topic_edges(linked_entities, topic_id)
            remaining = max(limit - len(computed_edges), 0)
            persisted_edges = self.related_entity_edge_rows(
                conn,
                [node_id for node_id, _row in linked_entities],
                remaining,
            )
            edges = [*computed_edges, *rows_to_dicts(persisted_edges)]
            node_ids = sorted(
                {
                    node_id
                    for edge in edges
                    for node_id in (int(edge["from_node_id"]), int(edge["to_node_id"]))
                    if node_id != topic_id
                }
            )
            nodes = self.knowledge_nodes_by_id(conn, node_ids)
            nodes.append({"id": topic_id, "type": "Topic", "title": topic.title})
            nodes.sort(key=lambda node: (str(node["type"]), str(node["title"]), int(node["id"])))
            _validate_graph_identities(nodes, edges)
        return {"topic": topic_payload, "nodes": nodes, "edges": edges}

    def topic_entity_details_for_terms(
        self,
        conn: sqlite3.Connection,
        terms: Iterable[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.context.all_structured_entity_details(
                conn,
                TOPIC_MATCH_CANDIDATE_LIMIT,
            )
            if row_matches_terms(row, terms)
        ]
        rows.sort(key=structured_entity_sort_key, reverse=True)
        return rows[:limit]

    def list_topic_people_for_rows(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        meeting_ids = sorted({int(row["meeting_id"]) for row in rows})
        if not meeting_ids or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in meeting_ids)
        people_sql = f"""
            SELECT DISTINCT p.id, p.display_name, p.normalized_name
            FROM meeting_people mp
            JOIN people p ON p.id = mp.person_id
            WHERE mp.meeting_id IN ({placeholders})
            ORDER BY p.display_name
            LIMIT ?
            """
        return rows_to_dicts(conn.execute(people_sql, (*meeting_ids, limit)).fetchall())

    def topic_entity_nodes(
        self,
        conn: sqlite3.Connection,
        rows: Iterable[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any]]]:
        linked_entities: list[tuple[int, dict[str, Any]]] = []
        seen_node_ids: set[int] = set()
        for row in rows:
            node = conn.execute(
                """
                SELECT id
                FROM knowledge_nodes
                WHERE type = ? AND stable_key = ?
                """,
                (ENTITY_NODE_TYPES[str(row["kind"])], entity_stable_key(row)),
            ).fetchone()
            if node is None:
                continue
            node_id = int(node["id"])
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            linked_entities.append((node_id, row))
        return linked_entities

    def computed_topic_edges(
        self,
        linked_entities: Iterable[tuple[int, dict[str, Any]]],
        topic_id: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": -ordinal,
                "from_node_id": node_id,
                "relation": "belongs_to",
                "to_node_id": topic_id,
                "confidence": 0.7,
                "source_meeting_id": int(row["meeting_id"]),
                "source_chunk_id": optional_int(row.get("source_chunk_id")),
                "extraction_method": "topic_query",
                "created_at": None,
            }
            for ordinal, (node_id, row) in enumerate(linked_entities, start=1)
        ]

    def related_entity_edge_rows(
        self,
        conn: sqlite3.Connection,
        linked_entity_ids: list[int],
        limit: int,
    ) -> list[sqlite3.Row]:
        if not linked_entity_ids or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in linked_entity_ids)
        edge_rows_sql = f"""
            SELECT *
            FROM knowledge_edges
            WHERE relation <> 'belongs_to'
              AND (
                from_node_id IN ({placeholders})
                OR to_node_id IN ({placeholders})
              )
            ORDER BY relation, id
            LIMIT ?
            """
        return list(
            conn.execute(
                edge_rows_sql,
                (*linked_entity_ids, *linked_entity_ids, limit),
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


def _request_local_topic_ids(
    stable_keys: Iterable[str],
    *,
    occupied_ids: Iterable[int] = (),
) -> dict[str, int]:
    occupied = set(occupied_ids)
    assigned: dict[str, int] = {}
    for stable_key in sorted(set(stable_keys)):
        collision_index = 0
        while True:
            identity = stable_key if collision_index == 0 else f"{stable_key}\0{collision_index}"
            digest_value = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big")
            topic_id = -(VIRTUAL_TOPIC_ID_HIGH_BIT | (digest_value & VIRTUAL_TOPIC_ID_VALUE_MASK))
            if topic_id not in occupied:
                occupied.add(topic_id)
                assigned[stable_key] = topic_id
                break
            collision_index += 1
    return assigned


def _validate_graph_identities(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    node_ids = [int(node["id"]) for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        message = "Topic graph contains duplicate node IDs."
        raise RuntimeError(message)
    edge_ids = [int(edge["id"]) for edge in edges]
    if len(edge_ids) != len(set(edge_ids)):
        message = "Topic graph contains duplicate edge IDs."
        raise RuntimeError(message)
    endpoints = {
        int(edge[endpoint]) for edge in edges for endpoint in ("from_node_id", "to_node_id")
    }
    if not endpoints.issubset(node_ids):
        message = "Topic graph edge endpoint is missing from the node result."
        raise RuntimeError(message)


def _stored_topic_from_row(row: sqlite3.Row) -> StoredTopic:
    return StoredTopic(
        stable_key=str(row["stable_key"]),
        title=str(row["title"]),
        normalized_title=str(row["normalized_title"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        raw_metadata_json=(
            str(row["raw_metadata_json"]) if row["raw_metadata_json"] is not None else None
        ),
    )


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
    conn: sqlite3.Connection,
    topic_terms: Iterable[str],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rows: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()
    for term in topic_terms:
        for row in search_meetings(conn, term, limit):
            evidence_id = str(row["evidence_id"])
            if evidence_id in seen_evidence_ids:
                continue
            rows.append(row)
            seen_evidence_ids.add(evidence_id)
            if len(rows) >= limit:
                return rows
    return rows
