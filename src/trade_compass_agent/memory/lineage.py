"""Resolve recorded memory successors without guessing links from similar text."""

from collections import defaultdict

from trade_compass_agent.memory.memory_store import EntryMeta


def memory_lineage(entries: list[EntryMeta]) -> dict[tuple[str, int], dict]:
    by_id = defaultdict(list)
    for entry in entries:
        by_id[entry.entry_id].append(entry)
    result = {}
    for entry in entries:
        kind = "deduplicated" if entry.reason.startswith("duplicate_of:") else entry.change_kind
        trace = {"change_kind": kind, "review_method": entry.review_method,
                 "successors": [], "lineage_status": "complete"}
        seen = {(entry.entry_id, entry.version)}
        current = entry
        while True:
            successor = current.successor_id
            if not successor and current.reason.startswith("duplicate_of:"):
                successor = current.reason.removeprefix("duplicate_of:")
            if not successor:
                if current.reason == "legacy_superseded" or current.change_kind in {"merged", "replaced", "deduplicated"}:
                    trace["lineage_status"] = "unavailable"
                break
            matches = by_id.get(successor, [])
            version = current.successor_version
            if version is None and successor == current.entry_id:
                # Older same-ID revisions advanced exactly one version per commit.
                version = current.version + 1
            if version is not None:
                matches = [m for m in matches if m.version == version]
            if len(matches) != 1:
                trace["lineage_status"] = "ambiguous" if matches else "unavailable"
                break
            following = matches[0]
            identity = (following.entry_id, following.version)
            if identity in seen:
                trace["lineage_status"] = "cycle"
                break
            seen.add(identity)
            trace["successors"].append({"entry_id": following.entry_id, "version": following.version,
                                        "text": following.text, "status": following.status})
            current = following
        result[(entry.entry_id, entry.version)] = trace
    return result
