"""Build Codex model catalog = bundled entries + Muse Spark 1.3 entries.

Usage: codex debug models > /tmp/bundled_models.json
       python3 build_muse_catalog.py [/tmp/bundled_models.json]
"""
import copy
import json
import os
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bundled_models.json"
DST = os.path.join(os.path.expanduser("~"), ".codex",
                   "model-catalogs", "muse-models.json")
os.makedirs(os.path.dirname(DST), exist_ok=True)

LEVELS = [
    ("minimal", "Minimal reasoning for the fastest simple answers"),
    ("low", "Fast responses with lighter reasoning"),
    ("medium", "Balances speed and reasoning depth for everyday tasks"),
    ("high", "Greater reasoning depth for complex problems"),
    ("xhigh", "Extra high reasoning depth for complex problems"),
    ("max", "Maximum reasoning depth for the hardest problems"),
]

SPECS = [
    {
        "slug": "muse-spark-1.3",
        "display_name": "Muse Spark 1.3",
        "description": "Muse Spark 1.3 via Meta API. Frontier coding model with 1M context.",
        "priority": 30,
        "nux": "Muse Spark 1.3 is now available via your Meta API key.",
    },
    {
        "slug": "muse-spark-1.3-contributor",
        "display_name": "Muse Spark 1.3 Contributor",
        "description": "Muse Spark 1.3 Contributor via Meta API. Content may be used for product improvement.",
        "priority": 31,
        "nux": "Muse Spark 1.3 Contributor is now available via your Meta API key.",
    },
]

with open(SRC) as f:
    catalog = json.load(f)

# Clone gpt-5.5: it uses standard function tools (no tool_mode key,
# use_responses_lite=false). The 6.x code_mode_only transport sends an
# `additional_tools` input item that Meta's Responses API rejects.
template = [m for m in catalog["models"] if m["slug"] == "gpt-5.5"][0]

for spec in SPECS:
    entry = copy.deepcopy(template)
    entry["slug"] = spec["slug"]
    entry["display_name"] = spec["display_name"]
    entry["description"] = spec["description"]
    entry["priority"] = spec["priority"]
    entry["availability_nux"] = {"message": spec["nux"]}
    entry["context_window"] = 1007997
    entry["max_context_window"] = 1007997
    entry["default_reasoning_level"] = "high"
    entry["supported_reasoning_levels"] = [
        {"effort": effort, "description": desc} for effort, desc in LEVELS
    ]
    entry["service_tiers"] = []
    entry["additional_speed_tiers"] = []
    entry["supports_search_tool"] = False
    entry["support_verbosity"] = False
    entry["supports_reasoning_effort_updates"] = True
    entry["visibility"] = "list"
    entry["upgrade"] = None
    entry["base_instructions"] = template["base_instructions"].replace(
        "based on GPT-5", "based on Muse Spark", 1
    )
    assert "Muse Spark" in entry["base_instructions"][:80]
    tpl = entry.get("model_messages", {}).get("instructions_template")
    if isinstance(tpl, str) and "based on GPT-5" in tpl:
        entry["model_messages"]["instructions_template"] = tpl.replace(
            "based on GPT-5", "based on Muse Spark", 1
        )
    catalog["models"].append(entry)

with open(DST, "w") as f:
    json.dump(catalog, f)

print("wrote", DST)
print("slugs:", [m["slug"] for m in catalog["models"]])
