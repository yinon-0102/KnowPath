"""Grounded graph relation extraction and conservative prerequisite projection.

The local extractor recognizes explicit source declarations, never infers an
edge from document order. Semantic/model adapters can produce the same candidate
shape; pending candidates cannot affect a learner's scope before review.
"""
from __future__ import annotations

import copy
import re
from hashlib import sha256

from .errors import DomainConflict


def topic_identifier(material_id, name):
    return "topic_" + sha256(f"{material_id}:{name}".encode()).hexdigest()[:12]


def _ref(material_id, version, chunk):
    return {"material_id": material_id, "material_version_id": version.id,
            "chunk_id": chunk.id, "page": chunk.page,
            "line_start": chunk.line_start, "line_end": chunk.line_end}


def structural_topics(material_id, version):
    topics = {}
    for index, chunk in enumerate(version.chunks):
        names = chunk.section_path or (chunk.text.split(".", 1)[0][:80],)
        parent = None
        for level, name in enumerate(names, 1):
            identifier = topic_identifier(material_id, name)
            node = topics.setdefault(identifier, {
                "id": identifier, "name": name or f"知识点 {index + 1}",
                "kind": "chapter" if level < len(names) else "concept",
                "parent_id": parent, "level": level, "confidence": 1.0,
                "source_refs": [], "prerequisites": [], "status": "active", "graph_version": 1})
            reference = _ref(material_id, version, chunk)
            if reference not in node["source_refs"]:
                node["source_refs"].append(reference)
            # Preserve existing material/name IDs. Ambiguous same-named headings
            # cannot provide a reliable single parent and must not create a loop.
            if node["parent_id"] != parent or parent == identifier:
                node["parent_id"] = None
            parent = identifier
    return list(topics.values())


def extract_relations(material_id, version, nodes):
    by_id = {n["id"]: n for n in nodes}
    by_name = {n["name"].casefold(): n for n in nodes}
    relations = {}

    def add(kind, source, target, refs, evidence, *, structural=False):
        if source == target:
            return
        identifier = "relation_" + sha256(f"{material_id}:{kind}:{source}:{target}".encode()).hexdigest()[:24]
        relation = relations.setdefault(identifier, {
            "id": identifier, "type": kind, "from_id": source, "to_id": target,
            "material_id": material_id, "material_version_id": version.id,
            "confidence": 1.0, "status": "active" if structural else "pending",
            "confirmed": structural, "source_refs": [], "evidence": evidence,
            "extraction_method": "heading_structure" if structural else "explicit_source_declaration",
            "registered_at": version.created_at.isoformat(), "valid_from": None, "valid_to": None})
        for reference in refs:
            if reference not in relation["source_refs"]:
                relation["source_refs"].append(copy.deepcopy(reference))

    for node in nodes:
        if node.get("parent_id") in by_id:
            add("contains", node["parent_id"], node["id"], node["source_refs"],
                f"{by_id[node['parent_id']]['name']} / {node['name']}", structural=True)
    declaration = re.compile(r"^(?:[-*]\s*)?(Prerequisites?|Requires|前置知识|先修知识|前置条件|Related topics?|相关知识|相关主题)\s*[:：]\s*(.+?)\s*$", re.I)
    sentences = [
        (re.compile(r"^(.+?)\s+is\s+(?:a\s+)?prerequisite\s+of\s+(.+?)[.。]?$", re.I), False),
        (re.compile(r"^(.+?)\s+requires\s+(.+?)[.。]?$", re.I), True),
        (re.compile(r"^(.+?)是(.+?)的(?:前置知识|先修知识)[。.]?$"), False),
        (re.compile(r"^学习(.+?)需要先掌握(.+?)[。.]?$"), True),
    ]
    for chunk in version.chunks:
        current = by_name.get(chunk.section_path[-1].casefold()) if chunk.section_path else None
        if current is None:
            continue
        for line in chunk.text.splitlines():
            matched = declaration.fullmatch(line.strip())
            if not matched:
                for pattern, reverse in sentences:
                    sentence = pattern.fullmatch(line.strip())
                    if sentence:
                        first, second = (by_name.get(sentence[i].strip().casefold()) for i in (1, 2))
                        if first and second:
                            source, target = (second, first) if reverse else (first, second)
                            add("prerequisite_of", source["id"], target["id"],
                                [_ref(material_id, version, chunk)], line.strip())
                        break
                continue
            kind = "related_to" if matched[1].casefold().startswith("related") or matched[1].startswith("相关") else "prerequisite_of"
            # Resolve complete known headings only. Unknown or speculative names
            # never become fabricated topic nodes.
            for name in re.split(r"[,，;；、]", matched[2]):
                target = by_name.get(name.strip().strip("`\"'").casefold())
                if target:
                    source_id, target_id = ((target["id"], current["id"]) if kind == "prerequisite_of"
                                            else (current["id"], target["id"]))
                    add(kind, source_id, target_id, [_ref(material_id, version, chunk)], line.strip())
    return sorted(relations.values(), key=lambda row: row["id"])


def prerequisite_projection(snapshot):
    """Return safe and recommended prerequisites without mutating a snapshot.

    Legacy reviewed active edges have no confirmed flag. An explicit false flag
    remains unconfirmed. Only immutable snapshot sources qualify as grounding.
    Every edge participating in a directed cycle is excluded from automation.
    """
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    sources = {source["id"]: source for source in snapshot.get("sources", [])}
    def grounded(ref):
        source = sources.get(ref.get("chunk_id"))
        return source is not None and all(ref.get(field) == source.get(field) for field in ("material_id", "material_version_id"))
    safe = {identifier: set() for identifier in nodes}
    recommended = {identifier: set() for identifier in nodes}
    for edge in snapshot.get("relations", []):
        if edge.get("type") != "prerequisite_of" or edge.get("status") in {"rejected", "superseded", "inactive"}:
            continue
        source, target = edge.get("from_id"), edge.get("to_id")
        if source not in nodes or target not in nodes:
            continue
        refs = edge.get("source_refs", [])
        valid = (source != target and edge.get("status") == "active" and edge.get("confirmed") is not False
                 and bool(refs) and all(grounded(ref) for ref in refs)
                 and all(nodes[i].get("status", "active") == "active" for i in (source, target)))
        (safe if valid else recommended)[target].add(source)
    def reaches(start, target):
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(safe[node])
        return False
    cyclic = [(target, source) for target, parents in safe.items() for source in parents if reaches(source, target)]
    for target, source in cyclic:
        safe[target].remove(source)
        recommended[target].add(source)
    return safe, recommended


def apply_prerequisites(snapshot):
    safe, recommended = prerequisite_projection(snapshot)
    for node in snapshot["nodes"]:
        node["prerequisites"] = sorted(safe[node["id"]])
        node["recommended_prerequisites"] = sorted(recommended[node["id"]])


def confirm_grounded_relations(snapshot):
    """Validate a publication before confirming explicit source declarations.

    Pending edges can describe unresolved claims, but their prerequisite graph
    must still be acyclic. A rejected publication leaves the stored candidate
    unchanged because GraphService validates a detached publication snapshot.
    """
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    sources = {source["id"]: source for source in snapshot.get("sources", [])}
    parents = {identifier: set() for identifier in nodes}
    children = {identifier: set() for identifier in nodes}
    live = []
    for edge in snapshot.get("relations", []):
        refs = edge.get("source_refs", [])
        for reference in refs:
            source = sources.get(reference.get("chunk_id"))
            if source is None or any(reference.get(field) != source.get(field)
                                     for field in ("material_id", "material_version_id")):
                raise DomainConflict("INVALID_RELATION_SOURCE", "关系来源必须属于正式快照并匹配资料版本")
        if edge.get("status") in {"rejected", "superseded", "inactive"}:
            continue
        source, target = edge.get("from_id"), edge.get("to_id")
        if source not in nodes or target not in nodes:
            raise DomainConflict("INVALID_RELATION", "关系端点必须属于同一快照")
        if edge.get("status") == "active" and not refs:
            raise DomainConflict("INVALID_RELATION_SOURCE", "有效关系必须有来源支持")
        if edge.get("type") == "prerequisite_of":
            parents[target].add(source)
            children[source].add(target)
        live.append(edge)
    # Kahn's algorithm avoids recursion limits on large textbooks. Pending
    # prerequisites count too; publishing a cycle as pending is still invalid.
    pending = [identifier for identifier, dependencies in parents.items() if not dependencies]
    visited = 0
    while pending:
        identifier = pending.pop()
        visited += 1
        for child in children[identifier]:
            parents[child].remove(identifier)
            if not parents[child]:
                pending.append(child)
    if visited != len(nodes):
        raise DomainConflict("PREREQUISITE_CYCLE", "前置关系不能形成环，请先修正候选资料或关系")
    for edge in live:
        endpoints_active = all(nodes[edge[field]].get("status", "active") == "active"
                               for field in ("from_id", "to_id"))
        if not endpoints_active or edge.get("review_required"):
            edge.update(status="pending", confirmed=False)
        elif (edge.get("status") == "pending" and edge.get("source_refs")
              and edge.get("extraction_method") == "explicit_source_declaration"):
            edge.update(status="active", confirmed=True)
    apply_prerequisites(snapshot)
