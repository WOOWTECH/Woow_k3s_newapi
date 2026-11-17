#!/usr/bin/env python3
"""Check the tool's rc.44 price tables against a New API source checkout (manual, not CI).

  git clone --depth 1 --branch v1.0.0-rc.44 https://github.com/QuantumNous/new-api /path/rc44
  python3 tests/apporo_sync/rc44_price_tables.py /path/rc44

Reads (as text; nothing is built or run) setting/operation_setting/tools.go
(seedHardcodedToolPrices and its constants, GetGeminiInputAudioPricePerMillionTokens)
and setting/model_setting/grok.go (defaultGrokSettings), and compares them with
newapi_content.RC44_TOOL_PRICE_SEED, GEMINI_AUDIO_PREFIXES and RC44_VIOLATION_FEE.
Exit 0 when they match. scripts/tests/test_apporo_sync.py runs the same check
when NEWAPI_RC44_SRC points at such a checkout.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts"))
import newapi_content as nc  # noqa: E402


def _body(text, signature):
    start = text.index(signature)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    raise ValueError(f"unbalanced braces after {signature}")


def tool_seed(tools_go):
    consts = {m.group(1): float(m.group(2))
              for m in re.finditer(r"(?m)^\s*(default\w+)\s*=\s*([0-9.]+)\s*$", tools_go)}
    body = _body(tools_go, "func seedHardcodedToolPrices(")
    seed = {}
    for m in re.finditer(r'(?m)^\s*prices\["([^"]+)"\]\s*=\s*(\S+)\s*$', body):
        value = m.group(2)
        seed[m.group(1)] = consts[value] if value in consts else float(value)
    return seed


def audio_prefixes(tools_go):
    body = _body(tools_go, "func GetGeminiInputAudioPricePerMillionTokens(")
    return re.findall(r'strings\.HasPrefix\(modelName, "([^"]+)"\)', body)


def violation_fee(grok_go):
    body = _body(grok_go, "var defaultGrokSettings = GrokSettings")
    enabled = re.search(r"ViolationDeductionEnabled:\s*(true|false)", body).group(1) == "true"
    amount = float(re.search(r"ViolationDeductionAmount:\s*([0-9.]+)", body).group(1))
    return enabled, amount


def check(src):
    with open(os.path.join(src, "setting", "operation_setting", "tools.go")) as f:
        tools_go = f.read()
    with open(os.path.join(src, "setting", "model_setting", "grok.go")) as f:
        grok_go = f.read()
    problems = []
    seed = tool_seed(tools_go)
    if seed != nc.RC44_TOOL_PRICE_SEED:
        problems.append(f"tool price seed differs: source-only {sorted(set(seed.items()) - set(nc.RC44_TOOL_PRICE_SEED.items()))}"
                        f", tool-only {sorted(set(nc.RC44_TOOL_PRICE_SEED.items()) - set(seed.items()))}")
    prefixes = audio_prefixes(tools_go)
    covered = all(any(p.startswith(q) for q in nc.GEMINI_AUDIO_PREFIXES) for p in prefixes)
    if not prefixes or not covered:
        problems.append(f"Gemini audio prefixes {prefixes} not covered by {nc.GEMINI_AUDIO_PREFIXES}")
    enabled, amount = violation_fee(grok_go)
    if (enabled, amount) != (nc.PRICE_SWITCHES[nc.VIOLATION_SWITCH_KEY], nc.RC44_VIOLATION_FEE):
        problems.append(f"violation fee default ({enabled}, {amount}) differs from the tool's")
    return seed, prefixes, (enabled, amount), problems


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    seed, prefixes, fee, problems = check(argv[1])
    print(f"tools.go seed: {len(seed)} keys, sha256(go_json) {nc.sha256_text(nc.go_json(seed))}")
    print(f"Gemini audio prefixes: {prefixes}")
    print(f"violation fee default: enabled {fee[0]}, US$ {fee[1]}")
    for p in problems:
        print("MISMATCH " + p)
    print("match" if not problems else "MISMATCH")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
