#!/usr/bin/env bash
# Flip the pipeline back to OpenAI once the account is funded again.
# Reverses the temporary OpenRouter/kimi fallback used during the quota outage.
set -euo pipefail
cd "$(dirname "$0")/.."

# Verify the account actually works before switching (a switch onto a still-dead
# account would just move the failures around).
if ! .venv/bin/python - <<'PY'
import sys
from dotenv import load_dotenv; load_dotenv()
from litellm import completion
try:
    completion(model="openai/gpt-5-mini", messages=[{"role":"user","content":"ok"}], max_tokens=5)
    sys.exit(0)
except Exception as e:
    print("OpenAI still not working:", str(e)[:100]); sys.exit(1)
PY
then
  echo "Aborting: OpenAI is still failing. Load credit first, then re-run."; exit 1
fi

M="openai/gpt-5-mini"
python3 - "$M" <<'PY'
import re, sys
m = sys.argv[1]
p = ".env"
s = open(p).read()
for knob in ("ORCHESTRATOR", "TAILOR", "CRITIC", "WRITER"):
    s = re.sub(rf"(APPLIEDIN_{knob}_MODEL=).*", rf"\g<1>{m}", s)
open(p, "w").write(s)
print("switched orchestrator/tailor/critic/writer ->", m)
PY

# Clear the top-level LLM-error banner (best-effort; server may be down).
curl -s -X POST http://127.0.0.1:8787/actions/clear-llm-error >/dev/null 2>&1 || true

./scripts/restart-server.sh
echo "Pipeline is back on OpenAI. Discover/Process/Apply (agent+vision) all work again."
