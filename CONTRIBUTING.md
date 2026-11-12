# Contributing to earthdiff

Thanks for helping. The project's promise is **explainable, account-free change alerts**, so contributions that keep the method inspectable are the most welcome.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,planetary-computer]"
pytest                    # offline tests with synthetic rasters
pytest -m network         # also hits the public STAC API
```

## Guidelines

- **No black boxes in the core.** New change classes must be a documented rule on named indices with thresholds in `Thresholds` (`src/earthdiff/change.py`), a test in `tests/test_core.py`, and an entry in the README method table. ML models are welcome as *optional* extras, never as a hard dependency.
- **Open data only.** Sentinel-2 via public STAC. No commercial/closed imagery sources.
- **No surveillance use cases.** No military targeting, no tracking of people or vehicles. Docs and examples should be about land, water, vegetation and infrastructure at area scale.
- **Be honest about accuracy.** If you add a validation case, state the source of the reference figure, what the tool got right and what it got wrong.
- Keep RAM in mind: read windows, free big arrays, no whole-tile reads.
- Tests: add synthetic-raster tests for any math. Network tests must be marked `@pytest.mark.network`.
- Style: type hints, small functions, docstrings that state units and edge cases.

## Good first issues

Threshold presets per biome, NBR/dNBR burn index (needs B12), Turkish/other-language report strings, a Docker image, more validation cases.

## Reporting bugs

Include: command line, `summary.json`, earthdiff version, and the scene ids from the report if the problem looks data-related.
