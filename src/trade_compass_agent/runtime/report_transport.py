"""Lossless, request-local encoding of an owned evidence catalogue.

Short references never become persistent identities. Validation and rendering
use original run-scoped IDs; saved wire views retain their explicit alias map.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from trade_compass_agent.runtime.report_contract import (
    EvidenceCatalog, ReportDocument, owned_read_result,
)


def _owned_read_result(receipt):
    """A real first-party read can return no numeric rows, including empty lists."""
    return owned_read_result(receipt['tool'], json.loads(receipt['result']))


def review_receipts(catalog, facts):
    """Scope batch-bar evidence by owned JSON pointers, never by prose.

    All owned non-batch sources remain available, even when the writer selected
    no numeric binding from them. A successful first-party read may have no
    numeric rows. Model drafts and excluded sources do not become evidence.
    Preserve each selected instrument's entire original subtree and all batch
    metadata. A root-level reference requires the full receipt. Original bytes
    and their hash remain in the publication artifact; a view is explicitly
    labelled and cannot represent the omitted instruments.
    """
    by_receipt = {}
    for fact in facts:
        by_receipt.setdefault(fact.receipt_id, []).append(fact)
    owned_sources = {fact.receipt_id for fact in catalog.facts.values()}
    result = []
    for receipt in catalog.receipts:
        selected = by_receipt.get(receipt["id"])
        if receipt["tool"] != "batch_get_bars":
            if receipt['id'] in owned_sources or _owned_read_result(receipt):
                result.append(receipt)
            continue
        if not selected:
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


def source_record_context(catalog, contexts, receipts, display_timezone):
    """Keep owned source facts addressable independently of paragraph selection.

    Raw receipt visibility alone cannot supply a canonical temporal witness.
    Preserve original numeric facts as well as quotations, without duplicating
    existing bindings or widening the separately selected batch-bar view.
    """
    known = {fact['id'] for context in contexts.values() for fact in context['evidence']}
    sources = {receipt['id'] for receipt in receipts if receipt['tool'] != 'batch_get_bars'}
    records = [{**asdict(fact), 'display_value_with_unit': fact.display_value_with_unit,
                'observed_date': fact.observation_date(display_timezone)}
               for fact in catalog.facts.values()
               if fact.receipt_id in sources and fact.id not in known]
    if not records:
        return contexts
    name = 'source_records'
    while name in contexts:
        name = '_'+name
    return {**contexts, name: {'evidence': records, 'calculations': []}}


def composition_source_records(catalog):
    """Keep source qualifications without repeating catalogue numbers.

    This labelled writing view omits only exact numeric leaves already in the
    owned catalogue. It retains strings, units, limitations, unindexed numbers,
    list positions and genuine empty states. Review and storage retain originals.
    """
    known = {}
    for fact in catalog.facts.values():
        known.setdefault(fact.receipt_id, {})[fact.pointer] = fact.value

    def metadata(value, path, facts):
        if isinstance(value, dict):
            result, removed = {}, 0
            for key, item in value.items():
                pointer = path+'/'+key.replace('~', '~0').replace('/', '~1')
                if type(item) in (int, float) and facts.get(pointer) == str(item):
                    removed += 1
                    continue
                result[key], count = metadata(item, pointer, facts)
                removed += count
            # Never manufacture an empty record from an originally nonempty one.
            return (result, removed) if result or not value else (value, 0)
        if isinstance(value, list):
            rows = [metadata(item, path+'/'+str(index), facts) for index, item in enumerate(value)]
            return [item for item, _ in rows], sum(count for _, count in rows)
        return value, 0

    records = []
    for receipt in catalog.receipts:
        data, count = metadata(json.loads(receipt['result']), '', known.get(receipt['id'], {}))
        row = {'receipt_id': receipt['id'], 'tool': receipt['tool'], 'data': data}
        if count:
            row['selection'] = {'kind': 'metadata_with_catalogue_values_omitted', 'omitted_numeric_values': count}
        records.append(row)
    return records


def source_reference_view(contexts, selected_ids):
    """Address unselected numbers through original receipts without copying them.

    Runtime checks retain the complete canonical contexts. The assessor gets
    every original receipt and the exact ID/pointer/date linkage; selected
    facts, quotations and derived categorical facts retain their full shape.
    """
    sources, result = {}, {}
    for name, context in contexts.items():
        evidence = []
        for fact in context['evidence']:
            if fact['id'] in selected_ids or fact['kind'] in {'quotation', 'classification'}:
                evidence.append(fact)
            else:
                dates = (fact['observed_at'], fact['observed_date'])
                sources.setdefault(fact['receipt_id'], {}).setdefault(dates, []).append([fact['id'], fact['pointer']])
        result[name] = {**context, 'evidence': evidence}
    references = {'columns': ['id', 'pointer'], 'sources': [{'receipt_id': key,
        'observations': [{'observed_at': stamp, 'observed_date': day, 'rows': rows}
                         for (stamp, day), rows in dates.items()]} for key, dates in sources.items()]}
    return result, references


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
            if original['kind'] == 'classification':
                by_context[context].setdefault('display_values', {})[self.aliases[original['id']]] = (
                    catalog.resolve(original['id']).display_value_with_unit)
        # Sharing literal context removes numeric dictionary joins altogether.
        # Keep exact null dates, values, units and provenance; an explicit order
        # table restores the input sequence independently of grouped presentation.
        self.table = {"fact_columns": columns, "facts": groups, "fact_order": list(self.references)}
        # Show the actual consumer rendering contract once, not a second copy
        # of the catalogue. Observation dates/units are already in fact output.
        displayable = [fact for fact in catalog.facts.values() if fact.metric != 'read_result']
        sample = next((fact for fact in displayable if fact.observed_at),
                      next(iter(displayable), None))
        if sample is not None:
            self.table["fact_rendering_example"] = {
                "ref": self.aliases[sample.id], "rendered": sample.display(),
            }
            # Name the atomic display field explicitly so neither the writer nor
            # renderer has to infer whether a numeric value includes its unit.
            ref = self.aliases[sample.id]
            self.table["paragraph_rendering_example"] = {
                "parts": [[ref, "subject"], "的", [ref, "label"], "为", [ref, "value_with_unit"], "。"],
                "rendered": f"{sample.subject}的{sample.label}为{sample.display_value_with_unit}。",
            }
        categorical = next((fact for fact in catalog.facts.values() if fact.kind == 'classification'), None)
        if categorical is not None:
            ref = self.aliases[categorical.id]
            self.table['categorical_rendering_example'] = {
                'parts': [[ref, 'subject'], '的', [ref, 'label'], '为', [ref, 'value_with_unit'], '。'],
                'rendered': f'{categorical.subject}的{categorical.label}为{categorical.display_value_with_unit}。',
            }
        dated = [fact for fact in catalog.facts.values() if fact.observed_at]
        if dated:
            # Show the same calendar that the actual date binding renders. UTC
            # storage can fall on a different day; it is never a reader date.
            from trade_compass_agent.runtime.report_reading import _date
            sample = next((fact for fact in dated if _date(fact)[:10] != fact.observed_at[:10]), dated[0])
            self.table["date_rendering_example"] = {
                "part": [self.aliases[sample.id], "date"],
                "observed_at": sample.observed_at, "rendered": _date(sample),
            }

    def encode_document(self, document):
        """Repair prompts use exactly the same aliases as initial composition."""
        value = self._map(document, self.aliases)
        return self._reader_parts(value, compact=True)

    def encode_factual_review(self, contexts, statements, references):
        """Encode only reference slots; original values and receipts stay literal."""
        contexts, statements, references = deepcopy((contexts, statements, references))
        for context in contexts.values():
            for fact in context['evidence']:
                fact['id'] = self.aliases.get(fact['id'], fact['id'])
            for calculation in context['calculations']:
                calculation['refs'] = [self.aliases.get(ref, ref) for ref in calculation.get('refs', [])]
        for statement in statements:
            for key in ('bound_fact_refs', 'time_fact_refs'):
                statement[key] = [self.aliases.get(ref, ref) for ref in statement.get(key, [])]
        for source in references['sources']:
            for observation in source['observations']:
                for row in observation['rows']:
                    row[0] = self.aliases.get(row[0], row[0])
        return contexts, statements, references

    def decode_factual_review(self, review):
        review = deepcopy(review)
        if isinstance(review, dict) and isinstance(review.get('assessments'), list):
            for row in review['assessments']:
                if isinstance(row, dict) and isinstance(row.get('time_evidence'), list):
                    row['time_evidence'] = [self.references.get(ref, ref) if isinstance(ref, str) else ref
                                            for ref in row['time_evidence']]
        return review

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
        return self._map(self._reader_parts(document, compact=False), self.references)

    @staticmethod
    def _reader_parts(document, *, compact):
        """Wire-only representation; never edit a string or infer a binding.

        Canonical object parts remain readable for old artifacts and explicit
        callers. Unknown shapes stay unchanged for strict item validation.
        """
        value = deepcopy(document)
        if not isinstance(value, dict) or not isinstance(value.get('items'), list):
            return value
        for item in value['items']:
            if not isinstance(item, dict) or item.get('type') != 'paragraph' or not isinstance(item.get('parts'), list):
                continue
            parts = []
            for part in item['parts']:
                if compact:
                    if isinstance(part, dict) and set(part) == {'type', 'text'} and part['type'] == 'text':
                        part = part['text']
                    elif isinstance(part, dict) and set(part) == {'type', 'ref', 'field'} and part['type'] == 'value':
                        part = [part['ref'], part['field']]
                elif isinstance(part, str):
                    part = {'type': 'text', 'text': part}
                elif isinstance(part, list) and len(part) == 2 and all(isinstance(value, str) for value in part):
                    part = {'type': 'value', 'ref': part[0], 'field': part[1]}
                parts.append(part)
            item['parts'] = parts
        return value

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
            candidate = raw
            if isinstance(raw, dict) and raw.get('type') == 'paragraph' and isinstance(raw.get('parts'), list):
                # An exact empty text token contributes no reader characters.
                # Keep whitespace, extra keys and every other invalid shape for
                # strict validation. An all-empty paragraph still fails minItems.
                candidate = {**raw, 'parts': [part for part in raw['parts']
                    if part != {'type': 'text', 'text': ''}]}
            item = ReportDocument.model_validate({"schema_version": 1, "items": [candidate]}).items[0]
            items.append(item)
        except ValidationError as exc:
            identifier = raw.get("id") if isinstance(raw, dict) else None
            identifier = identifier if isinstance(identifier, str) and identifier else ""
            findings.append(Finding("report", "invalid_structure", "该项报告内容尚未完成核验", identifier))
            errors.append({"index": index, "errors": exc.errors(include_url=False, include_input=False)})
    return ReportDocument(schema_version=1, items=items), findings, errors
