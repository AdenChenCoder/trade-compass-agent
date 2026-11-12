"""Lossless, request-local encoding of an owned evidence catalogue.

Short references never become persistent identities. All validation, rendering
and artifacts continue to use the original, run-scoped fact IDs.
"""
from __future__ import annotations

from copy import deepcopy
import json

from trade_compass_agent.runtime.report_contract import EvidenceCatalog, ReportDocument


def review_receipts(catalog, facts):
    """Scope batch-bar evidence by owned JSON pointers, never by prose.

    Preserve each selected instrument's entire original subtree and all batch
    metadata. A root-level reference requires the full receipt. Original bytes
    and their hash remain in the publication artifact; a view is explicitly
    labelled and cannot represent the omitted instruments.
    """
    by_receipt = {}
    for fact in facts:
        by_receipt.setdefault(fact.receipt_id, []).append(fact)
    result = []
    for receipt in catalog.receipts:
        selected = by_receipt.get(receipt["id"])
        if not selected:
            continue
        if receipt["tool"] != "batch_get_bars":
            result.append(receipt)
            continue
        data = json.loads(receipt["result"])
        branches = data.get("results")
        keys = set()
        for fact in selected:
            path = fact.pointer.split("/")
            if len(path) < 4 or path[1] != "results":
                break
            keys.add(path[2].replace("~1", "/").replace("~0", "~"))
        else:
            if isinstance(branches, dict) and keys and keys < branches.keys():
                view = {**data, "results": {key: row for key, row in branches.items() if key in keys}}
                result.append({"id": receipt["id"], "tool": receipt["tool"],
                    "original_sha256": receipt["sha256"],
                    "selection": {"kind": "complete_instrument_subtrees",
                                  "included": [key for key in branches if key in keys],
                                  "omitted": [key for key in branches if key not in keys]},
                    "result": json.dumps(view, ensure_ascii=False, separators=(",", ":"))})
                continue
        result.append(receipt)
    return result


class ReportTransport:
    def __init__(self, catalog: EvidenceCatalog):
        self.references = {f"e{i}": key for i, key in enumerate(catalog.facts)}
        self.aliases = {value: key for key, value in self.references.items()}
        table = catalog.model_table()
        original_columns = table["fact_columns"]
        original_rows = [dict(zip(original_columns, row, strict=True)) for row in table["facts"]]
        self._fact_details = {self.aliases[row["id"]]: {**row, "id": self.aliases[row["id"]]}
                              for row in original_rows}
        columns = ["id", "value", "observed_at"]
        shared = [column for column in original_columns if column not in columns]
        groups = []
        by_context = {}
        for original in original_rows:
            context = tuple(original[column] for column in shared)
            if context not in by_context:
                group = {column: original[column] for column in shared}
                group["rows"] = []
                by_context[context] = group
                groups.append(group)
            by_context[context]["rows"].append([self.aliases[original["id"]], original["value"], original["observed_at"]])
        # Sharing literal context removes numeric dictionary joins altogether.
        # Keep exact null dates, values, units and provenance; an explicit order
        # table restores the input sequence independently of grouped presentation.
        self.table = {"fact_columns": columns, "facts": groups, "fact_order": list(self.references)}
        # Show the actual consumer rendering contract once, not a second copy
        # of the catalogue. Observation dates/units are already in fact output.
        sample = next((fact for fact in catalog.facts.values() if fact.observed_at),
                      next(iter(catalog.facts.values()), None))
        if sample is not None:
            self.table["fact_rendering_example"] = {
                "ref": self.aliases[sample.id], "rendered": sample.display(),
            }
            # Demonstrate the actual sentence boundary: a value already owns
            # its unit. This is an example, not a prescribed sentence template.
            ref = self.aliases[sample.id]
            self.table["paragraph_rendering_example"] = {
                "parts": [{"type": "value", "ref": ref, "field": "subject"},
                          {"type": "text", "text": "的"},
                          {"type": "value", "ref": ref, "field": "label"},
                          {"type": "text", "text": "为"},
                          {"type": "value", "ref": ref, "field": "value"},
                          {"type": "text", "text": "。"}],
                "rendered": f"{sample.subject}的{sample.label}为{sample.value}{sample.unit}。",
            }

    def encode_document(self, document):
        """Repair prompts use exactly the same aliases as initial composition."""
        return self._map(document, self.aliases)

    def repair_evidence(self, document, findings):
        """Give unresolved items readable source context without guessing refs.

        The complete catalogue remains available. This view expands only the
        referenced rows, so a repair need not decode every dictionary again.
        """
        if document is None:
            return []
        blocked = {finding.item_id for finding in findings}
        result = []
        for item in document.items:
            if item.id not in blocked:
                continue
            refs = list(getattr(item, "refs", [getattr(item, "ref", "")]))
            refs.extend(part.ref for part in getattr(item, "parts", []) if hasattr(part, "ref"))
            facts, unknown = [], []
            for ref in refs:
                alias = self.aliases.get(ref, ref)
                if alias not in self._fact_details:
                    if ref:
                        unknown.append(ref)
                    continue
                facts.append(dict(self._fact_details[alias]))
            result.append({"item_id": item.id, "facts": facts, "refs_not_in_fact_catalog": unknown})
        return result

    def decode_document(self, document):
        # A report item may not shadow a fact alias: otherwise an inference
        # could silently bind to a fact instead of the named calculation.
        if isinstance(document, dict) and isinstance(document.get("items"), list):
            if any(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in self.references
                   for item in document["items"]):
                raise ValueError("报告条目id不能与事实目录id相同")
        return self._map(document, self.references)

    @staticmethod
    def _map(document, mapping):
        value = deepcopy(document)
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            return value
        for item in value["items"]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("ref"), str):
                item["ref"] = mapping.get(item["ref"], item["ref"])
            if isinstance(item.get("refs"), list):
                item["refs"] = [mapping.get(ref, ref) if isinstance(ref, str) else ref
                                for ref in item["refs"]]
            if isinstance(item.get("parts"), list):
                for part in item["parts"]:
                    if isinstance(part, dict) and part.get("type") == "value" and isinstance(part.get("ref"), str):
                        part["ref"] = mapping.get(part["ref"], part["ref"])
        return value


def parse_report_items(value):
    """Preserve schema-valid siblings without accepting invalid wire framing.

    Top-level schema, item count and duplicate IDs remain document boundaries.
    Invalid items are retained in the private attempt and sent back for repair.
    """
    from pydantic import ValidationError
    from trade_compass_agent.runtime.report_contract import Finding

    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return ReportDocument.model_validate(value), [], []
    # Validate the envelope independently of individual items.
    ReportDocument.model_validate({**value, "items": []})
    if len(value["items"]) > ReportDocument.model_json_schema()["properties"]["items"]["maxItems"]:
        return ReportDocument.model_validate(value), [], []
    ids = [item.get("id") for item in value["items"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if len(ids) != len(set(ids)):
        raise ValueError("报告条目id重复")
    items, findings, errors = [], [], []
    for index, raw in enumerate(value["items"]):
        try:
            item = ReportDocument.model_validate({"schema_version": 1, "items": [raw]}).items[0]
            items.append(item)
        except ValidationError as exc:
            identifier = raw.get("id") if isinstance(raw, dict) else None
            identifier = identifier if isinstance(identifier, str) and identifier else ""
            findings.append(Finding("report", "invalid_structure", "该项报告内容尚未完成核验", identifier))
            errors.append({"index": index, "errors": exc.errors(include_url=False, include_input=False)})
    return ReportDocument(schema_version=1, items=items), findings, errors
