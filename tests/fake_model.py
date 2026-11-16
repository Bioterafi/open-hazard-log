"""A stand-in for anthropic.Anthropic() so tests never call the API."""

import json
from types import SimpleNamespace


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, output, *, model="claude-opus-5-5", stop_reason="end_turn", raw_text=None):
        text = raw_text if raw_text is not None else json.dumps(output)
        self.msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], model=model,
                                   stop_reason=stop_reason, stop_details=None,
                                   usage=SimpleNamespace(input_tokens=12000, output_tokens=3000))
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self.msg)


def candidate(title, citations, *, modifies="", severity="major", likelihood="medium", confidence="medium",
              controls=None, causes=None):
    return {
        "lens": "workflow", "modifies": modifies, "title": title,
        "description": f"How {title.lower()} arises.",
        "causes": causes or [{"text": f"Cause of {title}", "category": "software", "paths": ["src/ranges.ts"]}],
        "effects": "Abnormal result not highlighted", "harm": "Delayed treatment",
        "controls": controls or [{"type": "design", "description": "Age-banded ranges"},
                                 {"type": "test", "description": "Paediatric range tests"}],
        "suggested_severity": severity, "suggested_likelihood": likelihood,
        "rating_rationale": "AGENT-RATIONALE-MARKER children are seen daily",
        "reasoning": "The range table holds adult values only.", "assumptions": "Used on paediatric wards.",
        "citations": [{"path": p, "quote": q} for p, q in citations], "confidence": confidence,
    }


def output(*cands, summary=""):
    return {"lenses_run": ["workflow", "failure_modes", "human_factors"], "candidates": list(cands),
            "not_proposed_summary": summary}
