"""Lossless, request-local encoding of an owned evidence catalogue.

Short references never become persistent identities. All validation, rendering
and artifacts continue to use the original, run-scoped fact IDs.
"""
from __future__ import annotations

from copy import deepcopy

from trade_compass_agent.runtime.report_contract import EvidenceCatalog, ReportDocument


class ReportTransport:
    def __init__(self, catalog: EvidenceCatalog):
        self.references = {f"e{i}": key for i, key in enumerate(catalog.facts)}
        self.aliases = {value: key for key, value in self.references.items()}
        table = catalog.model_table()
        columns = table["fact_columns"]
        # Literal values and IDs stay literal. Other columns are dictionary
        # encoded independently; null dates and unknown units remain distinct.
        dictionaries = {}
        for index, column in enumerate(columns):
            if column in {"id", "value"}:
                continue
            values = list(dict.fromkeys(row[index] for row in table["facts"]))
            dictionaries[column] = values
            indexes = {value: i for i, value in enumerate(values)}
            for row in table["facts"]:
                row[index] = indexes[row[index]]
        for row in table["facts"]:
            row[0] = self.aliases[row[0]]
        self.table = {**table, "fact_dictionaries": dictionaries}
        # Show the actual consumer rendering contract once, not a second copy
        # of the catalogue. Observation dates/units are already in fact output.
        sample = next((fact for fact in catalog.facts.values() if fact.observed_at),
                      next(iter(catalog.facts.values()), None))
        if sample is not None:
            self.table["fact_rendering_example"] = {
                "ref": self.aliases[sample.id], "rendered": sample.display(),
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
        columns = self.table["fact_columns"]
        rows = {row[0]: row for row in self.table["facts"]}
        result = []
        for item in document.items:
            if item.id not in blocked:
                continue
            refs = getattr(item, "refs", [getattr(item, "ref", "")])
            facts, unknown = [], []
            for ref in refs:
                alias = self.aliases.get(ref, ref)
                if alias not in rows:
                    if ref:
                        unknown.append(ref)
                    continue
                facts.append({column: self.table["fact_dictionaries"][column][value]
                              if column in self.table["fact_dictionaries"] else value
                              for column, value in zip(columns, rows[alias], strict=True)})
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
