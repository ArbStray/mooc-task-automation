# -*- coding: utf-8 -*-
"""离线验证回复解析的鲁棒性（不联网）。"""
import sys
sys.path.insert(0, '.')
from ai_provider import _parse_reply, parse_reply_best_effort

CASES = [
    # (说明, 模型原始输出, 期望 letters 或 None)
    ("标准 JSON", '{"answer": "B", "confidence": 0.85, "reason": "只有B对"}', "B"),
    ("```json 代码块", '```json\n{"answer": "C", "confidence": 0.9, "reason": "C正确"}\n```', "C"),
    ("``` 无语言标记", '```\n{"answer": "A", "confidence": 0.7, "reason": "A"}\n```', "A"),
    ("代码块未闭合(截断)", '```json\n{"answer": "D", "confidence": 0.6, "reason": "D对"', "D"),
    ("前后有多余文字", '好的，我分析如下：\n{"answer": "B", "confidence": 0.8, "reason": "B"}\n以上就是答案。', "B"),
    ("前后文字+代码块", '分析：\n```json\n{"answer":"AC","confidence":0.95,"reason":"多选"}\n```\n完毕', "AC"),
    ("截断的 JSON(无闭合括号)", '{"answer": "B", "confidence": 0.8, "reason": "B是对的', "B"),
    ("尾随逗号", '{"answer": "A", "confidence": 0.5, "reason": "A",}', "A"),
    ("单引号", "{'answer': 'C', 'confidence': 0.6, 'reason': 'C'}", "C"),
    ("裸字母值", '{answer: "B", confidence: 0.9}', "B"),
    ("未加引号的键和值", '{answer: B, confidence: 0.9, reason: "B"}', "B"),
    ("置信度整数百分比", '{"answer": "B", "confidence": 85, "reason": "r"}', "B"),
    ("置信度字符串百分比", '{"answer": "B", "confidence": "85%", "reason": "r"}', "B"),
    ("置信度字符串小数", '{"answer": "B", "confidence": "0.85"}', "B"),
    ("答案带括号", '{"answer": "(B)", "confidence": 0.9}', "B"),
    ("答案带文字", '{"answer": "答案是B", "confidence": 0.9}', "B"),
    ("答案小写", '{"answer": "b", "confidence": 0.9}', "B"),
    ("多选带分隔符", '{"answer": "A,B,C", "confidence": 0.9}', "ABC"),
    ("键名变体 question/explanation", '{"answer": "B", "explanation": "因为B", "question": "题干原文"}', "B"),
    ("中文键名", '{"答案": "C", "置信度": 0.9, "理由": "C对"}', "C"),
    ("大写键名", '{"Answer": "D", "Confidence": 0.9, "Reason": "D"}', "D"),
    ("嵌套在别的对象里", '{"result": {"answer": "A", "confidence": 0.8}, "status": "ok"}', "A"),
    ("数组包裹", '[{"answer": "B", "confidence": 0.9}]', "B"),
    ("纯文本 答案：B", "经过分析，答案：B，因为B符合规范。", "B"),
    ("纯文本 **B**", "我认为正确答案是 **C**。", "C"),
    ("纯文本 answer: D", "answer: D", "D"),
    ("孤立单字母", "B", "B"),
    ("完全无法解析", "抱歉，我无法判断这道题。", None),
    ("空字符串", "", None),
    ("非JSON噪声", "<<<NOT_JSON>>>", None),
    ("字母不在A-H范围外的干扰", '{"answer": "Z", "confidence": 0.9}', "Z"),
    ("题干含花括号", '{"answer": "B", "confidence": 0.9, "reason": "函数 {x} 形式", "text": "题干{a}"}', "B"),

    # ---- 真实线上失败样本（claude-sonnet-5 / opus-5，中文引号未转义 + 截断）----
    ("真实样本:未转义中文引号+截断",
     '```json\n{\n  "text": "根据创业技术发展曲线可以看出，在()的时候就意味着企业已经走出了"死亡谷"。",\n'
     '  "options": { "A": "开发", "B": "产品发布", "C": "商业化", "D": "盈亏平衡之后" },\n'
     '  "answer": "D",\n  "confidence": 0.95,', "D"),
    ("真实样本:未转义引号(无尾逗号)",
     '```json\n{ "text": "在()的时候，就意味 着企业已经走出了"死亡谷"。", '
     '  "options": { "A": "开发", "B": "产品发布" }, "answer": "D", "confidence": 0.95', "D"),
    ("未转义引号(完整JSON,无截断)",
     '{"text": "走出"死亡谷"之后", "answer": "B", "confidence": 0.9}', "B"),
    ("未转义引号+reason含引号",
     '{"answer": "C", "reason": "因为他说"不行"", "confidence": 0.8}', "C"),
    ("键名无引号+截断+未转义引号",
     '```json\n{text: "走出"死亡谷"", answer: D, confidence: 0.9', "D"),
    ("字段顺序颠倒(答案在后且被截)",
     '{"text": "题干"引号"内容", "options": {"A":"1"}, "answer": "A", "confidence": 0.7,', "A"),
]

passed = failed = 0
for title, raw, expected in CASES:
    result = _parse_reply(raw)
    got = result[0] if result else None
    ok = got == expected
    passed += ok
    failed += not ok
    mark = "OK  " if ok else "FAIL"
    detail = f"letters={got!r}"
    if result:
        detail += f" conf={result[1]:.2f} reason={result[2][:20]!r} stem={result[3][:20]!r}"
    print(f"[{mark}] {title}: 期望 {expected!r} -> {detail}")

print(f"\n{passed} passed, {failed} failed")

# 额外：确认截断的 reason/stem 会被裁剪
long = '{"answer": "B", "confidence": 0.9, "reason": "' + "长" * 500 + '", "text": "' + "题" * 800 + '"}'
r = _parse_reply(long)
print("长文本裁剪:", len(r[2]), len(r[3]))
sys.exit(1 if failed else 0)
