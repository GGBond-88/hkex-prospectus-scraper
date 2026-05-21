# Recorded HTTP cassettes

VCR / pytest-recording stores real HKEX responses here as YAML so the
discovery integration test runs deterministically without network access.

## Refresh procedure (quarterly per spec section 8)

1. Delete the stale cassette:

       rm tests/cassettes/test_l0_discovery_replays_recorded_hkex_window.yaml

2. Re-record from the live HKEX endpoint:

       python -m pytest tests/test_l0_discovery_integration.py \
         --record-mode=once -v

3. Inspect the new cassette for sensitive headers and commit the file:

       git add tests/cassettes/*.yaml
       git commit -m "tests: refresh L0 discovery cassettes"

## Why a small window

The default cassette is one month (2024-01-01..2024-01-31). Keeping the
window small keeps the cassette under ~50 KB and the refresh predictable.
