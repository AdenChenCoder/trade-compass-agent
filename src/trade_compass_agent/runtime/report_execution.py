"""Publication of actual operation receipts, independent of report generation.

Only mutation tools can establish a current execution. Reading an old trade,
an analyst's recommendation or a submitted order is not an execution receipt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from trade_compass_agent.runtime.report_contract import Finding


@dataclass
class ExecutionEvidence:
    records: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    trade_table: str = ""
    trade_details: str = ""

    def render(self, *, heading=True, include_table=True):
        messages = [row["message"] for row in self.records]
        if any(row["status"] == "executed" for row in self.records):
            messages.insert(0, "本轮已成交，逐笔结果见下方成交核对。")
        prefix = "\n\n## 执行记录\n\n" if heading else ""
        return (prefix + "\n\n".join(messages) if messages else "") + (self.trade_table if include_table else "")


def execution_evidence(tool_results):
    from trade_compass_agent.runtime.verifier import trade_receipt_section
    result = ExecutionEvidence()
    table_receipts = []
    for index, (name, raw) in enumerate(tool_results):
        if name == "analyze_portfolio":
            table_receipts.append((name, raw))
        if name == "batch_paper_trades":
            try:
                data = json.loads(raw)
                rows = data.get("results", [])
                if not rows:
                    raise ValueError("missing batch outcomes")
                child = execution_evidence([("place_paper_trade", json.dumps(row)) for row in rows])
                result.records.extend({**row, "receipt_index": index} for row in child.records)
                result.findings.extend(child.findings)
                table_receipts.extend(("place_paper_trade", json.dumps(row)) for row in rows
                                      if row.get("status") == "executed" and row.get("timestamp"))
            except (ValueError, TypeError, AttributeError):
                result.findings.append(Finding("execution", "outcome_unknown", "批量操作结果尚未确认，请核对账本"))
            continue
        if name not in {"place_paper_trade", "write_memory"}:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        status, message = "unknown", "操作结果尚未确认，请核对记录后再决定是否重试。"
        if name == "place_paper_trade":
            if (data.get("status") == "executed" and data.get("trade_id")
                    and all(key in data for key in ("symbol", "account", "side", "quantity", "price", "timestamp"))):
                status = "executed"
                side = {"buy": "买入", "sell": "卖出"}.get(data["side"], data["side"])
                message = (f"已成交：{data['symbol']}，{side}{data['quantity']}股，成交价{data['price']}元/股，"
                           f"账户{data['account']}，{data['timestamp']}；成交编号 {data['trade_id']}。")
                if data.get("realized_pnl_basis") == "fifo_after_entry_and_exit_fees":
                    message += f" 本次卖出FIFO净结算盈亏{data['realized_pnl']}元（已分摊买卖费用）。"
                table_receipts.append((name, raw))
            elif data.get("trade_rejected") is True and data.get("error"):
                status, message = "rejected", "本次未成交：" + str(data["error"])
        else:
            if (data.get("path") and data.get("content_hash")
                    and not any(data.get(key) for key in ("error", "fact_check_failed", "execution_unconfirmed"))):
                status, message = "saved", "记忆已保存。"
            elif data.get("fact_check_failed") or data.get("error_type") == "invalid_scope":
                status, message = "rejected", "记忆未保存：内容或保存位置未通过校验。"
        if status == "unknown":
            result.findings.append(Finding("execution", "outcome_unknown", message))
        result.records.append({"receipt_index": index, "tool": name, "status": status, "message": message})
    result.trade_table = trade_receipt_section(table_receipts)
    result.trade_details = trade_receipt_section(table_receipts, compact=True)
    return result
