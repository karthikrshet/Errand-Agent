# Real bugs hit while building

Each entry: what broke, why, the fix, and the regression test that now guards it. Only real bugs.

## 1. A field named `date` silently broke its own type annotation

- **What broke:** importing the agent failed with `TypeError: unsupported operand type(s) for |: 'NoneType' and 'NoneType'` while pydantic built `RescheduleTo`.
- **Why:** the model declared `date: date | None = None`. With `from __future__ import annotations` the annotation is a string evaluated later *in the class namespace*, where `date` now refers to the field's default (`None`), so it became `None | None`. mypy did not flag it because it resolves the name statically.
- **Fix:** import the module (`import datetime as dt`) and annotate `date: dt.date | None`; keep the field name `date` because it is what the LLM-facing schema should say.
- **Regression test:** `tests/test_nlu.py::test_reschedule_to_parses_date_field`.
