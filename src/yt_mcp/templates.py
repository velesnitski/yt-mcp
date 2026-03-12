ISSUE_TEMPLATES = {
    "bug": {
        "name": "Bug Report",
        "sections": [
            ("Summary", "Brief description of the bug"),
            ("Steps to Reproduce", "1. \n2. \n3. "),
            ("Expected Result", "What should happen"),
            ("Actual Result", "What actually happens"),
            ("Environment", "OS, browser, app version, device"),
            ("Severity", "Critical / Major / Minor / Trivial"),
            ("Screenshots / Logs", "Attach if available"),
        ],
    },
    "feature": {
        "name": "Feature Request",
        "sections": [
            ("Problem", "What problem does this solve?"),
            ("Proposed Solution", "How should it work?"),
            ("Alternatives Considered", "Other approaches evaluated"),
            ("Acceptance Criteria", "- [ ] Criterion 1\n- [ ] Criterion 2"),
            ("Priority", "Must have / Should have / Nice to have"),
        ],
    },
    "task": {
        "name": "Task",
        "sections": [
            ("Objective", "What needs to be done"),
            ("Requirements", "- Requirement 1\n- Requirement 2"),
            ("Technical Notes", "Implementation details, links, references"),
            ("Definition of Done", "- [ ] Done criterion 1\n- [ ] Done criterion 2"),
        ],
    },
    "daily": {
        "name": "Daily Standup",
        "sections": [
            ("Done Yesterday", "- "),
            ("Planned Today", "- "),
            ("Blockers", "None"),
        ],
    },
    "spike": {
        "name": "Research / Spike",
        "sections": [
            ("Goal", "What are we trying to learn?"),
            ("Context", "Why is this research needed?"),
            ("Scope", "What's in/out of scope"),
            ("Timebox", "Maximum time to spend"),
            ("Findings", "To be filled after research"),
            ("Recommendation", "To be filled after research"),
        ],
    },
    "release": {
        "name": "Release Checklist",
        "sections": [
            ("Version", "x.y.z"),
            ("Changes Included", "- "),
            ("Pre-release Checklist", "- [ ] Tests pass\n- [ ] Code review done\n- [ ] Staging tested"),
            ("Post-release Checklist", "- [ ] Production verified\n- [ ] Monitoring checked\n- [ ] Docs updated"),
            ("Rollback Plan", "Steps to rollback if needed"),
        ],
    },
    "devops": {
        "name": "DevOps / Infrastructure Task",
        "sections": [
            ("Description", "Brief overview of the infrastructure task and its scope"),
            ("Requirement", "- What needs to be deployed, configured, or changed\n- Capacity and performance considerations\n- Monitoring and alerting requirements"),
            ("Expected Result", "- Servers/services are deployed and operational\n- Monitoring confirms healthy state\n- Documentation and scripts are updated"),
            ("Affected Services", "List of services, servers, or environments involved"),
            ("Rollback Plan", "Steps to revert changes if something goes wrong"),
        ],
    },
    "incident": {
        "name": "Production Incident",
        "sections": [
            ("Impact", "Who is affected? How many users? Which regions?"),
            ("Symptoms", "What errors/behaviors are observed?"),
            ("Environment", "prod / staging / dev"),
            ("Steps to Reproduce", "1. \n2. "),
            ("Root Cause", "To be filled after investigation"),
            ("Fix Applied", "To be filled after resolution"),
            ("Prevention", "How to prevent recurrence"),
        ],
    },
    "epic": {
        "name": "Epic",
        "sections": [
            ("Goal", "What are we trying to achieve?"),
            ("Background", "Why is this needed?"),
            ("Scope", "What's included and excluded"),
            ("Success Criteria", "How do we know we're done?"),
            ("Dependencies", "External teams, services, approvals needed"),
            ("Risks", "What could go wrong?"),
            ("Subtasks", "- [ ] Task 1\n- [ ] Task 2"),
        ],
    },
}


def build_description(template_key: str, fields: str = "") -> tuple[str, str] | None:
    """Build a structured description from a template.

    Returns (template_name, description_text) or None if template not found.
    """
    tpl = ISSUE_TEMPLATES.get(template_key.lower())
    if not tpl:
        return None

    provided = {}
    if fields:
        for pair in fields.split("|||"):
            pair = pair.strip()
            if ":" in pair:
                key, value = pair.split(":", 1)
                provided[key.strip().lower()] = value.strip()

    desc_parts = []
    for section_name, placeholder in tpl["sections"]:
        value = provided.get(section_name.lower(), placeholder)
        desc_parts.append(f"## {section_name}\n{value}")

    return tpl["name"], "\n\n".join(desc_parts)
