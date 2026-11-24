"""Final input shape with synthetic routing and redacted contact identifiers."""


def final_input():
    return {
        "userId": "final-input-test-user", "sessionId": "final-input-test-session",
        "name": "测试小陈", "age": "26", "sex": "女",
        "fileList": [
            {"url": "https://test.slmc.top/api/v1/file/download?objectName=f6087da0947a5521bfffccb20171e642.pdf", "fileType": "pdf"},
            {"url": "https://test.slmc.top/api/v1/file/preview?objectName=148633488734d8c9985d02ca7fac1b63.pdf", "fileType": "pdf"},
        ],
        "healthSurvey": {
            "seqNo": "4", "submitTime": "2026/9/24 10:25:06", "usedTime": "114秒",
            "source": "微信", "sourceDetail": "N/A", "fromIp": "redacted-test-ip",
            "name": "测试小陈", "gender": "女", "idCard": "redacted-test-id-card", "age": "26",
            "heightCm": "160", "weightKg": "90", "diet": "喜辣", "digestion": "消化不良",
            "lifestyle": "熬夜┋运动┋良好", "skin": "长痘痤疮┋暗沉发黄",
            "sleep": "晚上22:00后入睡┋每日睡眠在8小时以上", "fourHigh": "无",
            "pastDisease": "其他", "familyDisease": "其他", "allergy": "其他",
            "bowelMovement": "时常腹泻", "currentMedication": "无", "specialMedication": "其他",
            "wantImprove": "便秘、腹泻、腹胀等肠道不适┋改善皮肤状态、祛痘提亮┋提升免疫力、减少生病┋改善睡眠、缓解疲劳┋调理代谢、预防慢病",
        },
        "gutHealthIndicator": {
            "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
            "diamineOxidase": {"value": "3.8", "unit": "U/L"},
            "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
            "hsCRP": {"value": ">3.0", "unit": "mg/L"},
        },
        "testData": [], "hospitalData": [], "microbiomeData": None,
    }


def current_health_input():
    """October input shape; retain supplied clinical data, redact routing/IP."""
    payload = final_input()
    payload.update(userId="current-health-test-user", sessionId="current-health-test-session",
                   age="42", sex="男", fileList=[])
    payload["healthSurvey"].pop("idCard")
    payload["gutHealthIndicator"] = {
        "bacterialEndotoxin": {"value": "11.9", "unit": "U/L"},
        "diamineOxidase": {"value": "24.9", "unit": "U/L"},
        "hsCRP": {"value": "1.02", "unit": "mg/L"},
        "complete": True,
        "dLacticAcid": {"value": "12.77", "unit": "mg/L"},
    }
    return payload
