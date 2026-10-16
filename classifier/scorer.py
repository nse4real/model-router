"""Pure heuristic diagnostic; thresholds are frozen in configuration."""
import re


def score(request, thresholds=(2, 5)):
    text = " ".join(m["content"] for m in request.messages)
    words = re.findall(r"\w+", text.lower())
    terms = {"reason", "reasoning", "prove", "analyse", "analyze", "algorithm", "code",
             "math", "maths", "calculate", "derive", "step", "steps", "compare"}
    keywords = sum(w in terms for w in words)
    density = keywords / max(1, len(words))
    code_block = "```" in text
    task_type = ("code" if code_block or "code" in words else
                 "maths" if any(w in words for w in ("math", "maths", "calculate", "prove")) else
                 "reasoning" if keywords else "general")
    length = min(4, len(text) // 500)
    value = length + min(3, keywords) + int(density >= .1) + 2 * code_block
    features = dict(length=len(text), length_score=length, keyword_density=density,
                    keywords=keywords, task_type=task_type, code_block=code_block)
    tier = "simple" if value < thresholds[0] else "medium" if value < thresholds[1] else "complex"
    return tier, value, features
