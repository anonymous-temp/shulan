"""Read-only projections from submitted questionnaires, never generated advice."""

from datetime import datetime
from datetime import timezone


def answer_rows(snapshot: dict) -> list[dict]:
    if snapshot.get('status') != 'submitted':
        return []
    rows = []
    submitted_at = snapshot.get('submittedAt')
    if isinstance(submitted_at, (int, float)):
        submitted_at = datetime.fromtimestamp(submitted_at, timezone.utc).isoformat()
    for name, item in snapshot.get('answers', {}).items():
        if not isinstance(item, dict) or item.get('status') != 'answered' or not item.get('provided'):
            continue
        question = item['question']
        value = item['value']
        if question['type'] == 'text':
            text = value
        else:
            choices = {choice['value']: choice.get('text', choice['value']) for choice in question['choices']}
            if question.get('showNoneItem'):
                choices['none'] = question.get('noneText', '没有')
            selected = value if isinstance(value, list) else [value]
            text = '、'.join(choices[token] for token in selected)
        rows.append(
            {
                'answerIndex': len(rows),
                'sourceKind': 'questionnaire_answer',
                'sourcePointer': '/answers/' + name.replace('~', '~0').replace('/', '~1') + '/value',
                'questionId': name,
                'questionType': question['type'],
                'intakeDimension': 'goal' if name == 'health_goals' else name,
                'question': question['title'],
                'answer': text,
                'answeredAt': submitted_at,
            }
        )
    return rows


def report_observations(snapshot: dict) -> list[dict]:
    """Project accepted source observations into the existing hospital-data schema."""
    result, seen = [], set()
    for report in snapshot.get('reports', []):
        if not isinstance(report, dict) or not isinstance(report.get('interpretation'), dict):
            continue
        root = report['interpretation']
        pending = [root]
        while pending:
            section = pending.pop(0)
            if not isinstance(section, dict):
                continue
            pending.extend(section.get('sections') or [])
            report_type = section.get('reportType') or root.get('reportType')
            observations = list(section.get('observations') or [])
            expansion = section.get('expansion')
            if report_type == 'imaging' and isinstance(expansion, dict):
                for field, label in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                    if isinstance(expansion.get(field), str) and expansion[field].strip():
                        observations.append({'itemName': label, 'resultValue': expansion[field], 'kind': 'assessment'})
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                name, value = observation.get('itemName'), observation.get('resultValue')
                if not isinstance(name, str) or not name.strip() or value in (None, ''):
                    continue
                narrative = observation.get('kind') in {'assessment', 'narrative'}
                if narrative and report_type not in {'checkup', 'lab', 'imaging'}:
                    continue
                text = str(value)
                unit = observation.get('unit')
                if isinstance(unit, str) and unit and not text.endswith(unit):
                    text += ' ' + unit
                observed_at = observation.get('observedAt') or section.get('reportTime') or root.get('reportTime')
                key = (report.get('artifactId'), name, text, observed_at, observation.get('referenceRange'))
                if key in seen:
                    continue
                seen.add(key)
                item = {
                    'dataType': 'EXAM' if narrative else 'INSPECTION',
                    'itemName': name,
                    'examResult' if narrative else 'testResultValue': text,
                    'reportTime': observed_at,
                    'referenceRange': observation.get('referenceRange'),
                    'resultStatusName': observation.get('flag'),
                    'sourceReportType': report_type,
                    'sourceFile': 'questionnaire:' + snapshot['questionnaireId'],
                    'sourceQuestionnaireId': snapshot['questionnaireId'],
                    'sourceArtifactId': report.get('artifactId'),
                }
                result.append(item)
    return result


def submitted_context(snapshot: dict) -> dict:
    if snapshot.get('status') != 'submitted':
        return {}
    rows = answer_rows(snapshot)
    # Defaults remain recognisable as missing answers; they never become source rows.
    unknown = [name for name, item in snapshot.get('answers', {}).items() if item.get('status') == 'unknown']
    return {
        'questionnaireId': snapshot['questionnaireId'],
        'sessionId': snapshot['sessionId'],
        'submittedAt': snapshot.get('submittedAt'),
        'answers': [
            {
                'question': row['question'],
                'answer': row['answer'],
                'sourcePointer': row['sourcePointer'],
                'answeredAt': row['answeredAt'],
            }
            for row in rows
        ],
        'unansweredQuestions': unknown,
        'healthData': {'hospitalData': report_observations(snapshot)},
    }
