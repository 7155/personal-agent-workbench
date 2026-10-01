"""Publish a compact delivery record from canonical tasks, without another turn."""
from __future__ import annotations


def completion_report(content, tasks, reviews, *, success, artifact_revisions=None):
    # The answer itself is the deliverable for a short, single text-only request.
    # Do not turn a greeting into an empty engineering report.
    if len(tasks) == 1 and not tasks[0].artifacts and len(content) <= 160:
        return content
    deliveries = [task for task in tasks if task.parent_id] or list(tasks)
    refs = []
    for task in deliveries:
        # Only the existing scoped file reader's available result and the
        # document owner's applied relocation can change a displayed path.
        # Task artifacts and historical receipts retain their original refs.
        relocated = {item['sourceRef']: item['resolvedRef']
            for item in (artifact_revisions or {}).get(task.id, [])
            if item.get('status') == 'available' and item.get('resolvedRef')
            and item.get('relocationReceiptId')}
        refs.extend(relocated.get(ref, ref) for ref in task.artifacts)
    refs = list(dict.fromkeys(refs))
    lines = ['### 成果报告', '**任务状态：**' + ('已完成' if success else '尚未全部完成'), '', '**交付物**']
    if refs:
        for ref in refs[:24]:
            # These are references, not executable URLs or promises that a file exists.
            lines.append('- ' + ref.replace('\n', ' ')[:400])
        if len(refs) > 24:
            lines.append(f'- 另有 {len(refs) - 24} 项，见对应任务的完整成果记录。')
    else:
        lines.append('- 文字成果见上方答复；本轮未登记文件交付物。')
    lines.extend(['', '**验证结果**'])
    unresolved = []
    for index, task in enumerate(deliveries):
        review = reviews.get(task.id, {})
        title = ' '.join(task.objective.split())[:120]
        passed = review.get('operabilityVerdict') == 'passed' and review.get('requirementVerdict') == 'satisfied'
        verdict = '已验收' if task.state == 'done' and passed else '未完成或验证不完整'
        reason = ' '.join(str(review.get('reason') or '').split())[:350]
        if index < 6:
            lines.append(f'- {title}：{verdict}' + (f'。{reason}' if reason else '。未记录具体核验范围。'))
        if task.state != 'done' or not passed:
            unresolved.append(title)
    if len(deliveries) > 6:
        lines.append(f'- 另有 {len(deliveries) - 6} 项任务，完整结论见“任务与分派”。')
    lines.extend(['', '**未完成与未验证项**'])
    lines.extend(['- ' + title for title in unresolved[:6]] or ['- 当前任务记录没有未关闭项；检查范围以上述验证结果为准。'])
    if len(unresolved) > 6:
        lines.append(f'- 另有 {len(unresolved) - 6} 项未关闭，完整记录见“任务与分派”。')
    report = '\n'.join(lines)
    # The original synthesis stays in its bound execution output. Keep the
    # public post inside its existing contract, marking any shortened narrative.
    budget = 16000 - len(report) - 8
    narrative = content if len(content) <= budget else content[:max(0, budget - 32)] + '\n（完整答复保留在执行记录中。）'
    return narrative + '\n\n---\n\n' + report
