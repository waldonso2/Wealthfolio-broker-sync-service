# Contract tests

One folder per adapter (`dummy/`, later `dkb/`, `trade-republic/`, `scalable/`), one JSON file per case:

```json
{
  "description": "what the case covers",
  "recording": { "...": "the broker's answers, as the adapter's replay() expects them" },
  "since": null,
  "expected": [ { "id": "...", "kind": "BUY", "...": "model.to_dict() of each transaction" } ]
}
```

- `Adapter.replay(recording)` returns an adapter that answers from the recording instead of the broker. HTTP adapters build it on an `httpx.MockTransport` that serves the recorded responses in order; the WebSocket adapter (Trade Republic) replays the recorded messages.
- When a broker changes its API, record the new answers into a **new** case. The failing diff shows what changed, and the old case documents the old format.
- **Never commit real data.** Recordings are fabricated, or anonymised so that no name, IBAN, account number, address, amount or ISIN combination points to a real person.
