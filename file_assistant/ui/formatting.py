"""Inert text summaries of Python-owned plans, results and recovery evidence."""


def plan_summary(plan):
    """Exact app-owned paths, shared by the review and confirmation displays."""
    folders = [op.request.destination for op in plan.operations if op.request.action == "mkdir"]
    moves = [op.request for op in plan.operations if op.request.action == "move"]
    lines = [f"Folder: {plan.root}", f"Selected moves: {len(moves)}",
             "New folders: " + (", ".join(folders) if folders else "None")]
    lines.extend(f"{move.source}  ->  {move.destination}" for move in moves)
    return "\n".join(lines)


def format_report(report, plan):
    by_id = {op.plan_id: op.request for op in plan.operations}

    def label(identifier):
        op = by_id.get(identifier)
        if op is None:
            return "Unknown operation"
        return (f"Create folder {op.destination}" if op.action == "mkdir"
                else f"{op.source} -> {op.destination}")

    lines = [f"{report.status}: {report.message}"]
    lines.extend(f"Completed: {label(result.plan_id)} ({result.status})" for result in report.completed)
    if report.stopped:
        lines.append(f"Stopped: {label(report.stopped.plan_id)}: {report.stopped.message}")
    lines.extend(f"Not started: {label(identifier)}" for identifier in report.not_started)
    lines.append("Created folders: " + (", ".join(report.created_folders) or "None"))
    if report.recovery_required:
        lines.append("Recovery inspection required. Records are retained; use Inspect / rescan.")
    return "\n".join(lines)


def recovery_summary(review):
    journals, findings = review
    lines = ["Acknowledge current evidence only. This does not retry, resume or undo any operation."]
    for journal in journals:
        lines.append(f"Historical folder: {journal.plan.root}")
        by_id = {op.plan_id: op.request for op in journal.plan.operations}
        for finding in findings:
            if finding.batch_id != journal.plan.plan_id:
                continue
            op = by_id[finding.operation_id]
            lines.append(f"{op.action}: {op.source or ''} -> {op.destination}\n"
                         f"{finding.conclusion}: {finding.detail}\n"
                         f"Observed evidence: {finding.observations!r}")
    return "\n".join(lines)
